"""
NIFTY / BANKNIFTY Option Greeks Tracker (Angel One SmartAPI)
--------------------------------------------------------------
Every 30 seconds (market hours only), fetches live option Greeks
(Delta, Gamma, Theta, Vega, IV) for NIFTY and BANKNIFTY from Angel
One's authenticated SmartAPI, picks the ATM strike + 2 strikes on
either side (5 total), and saves one document per symbol per poll
to MongoDB.

Runs continuously as a cloud background worker (e.g. Render) — no
local machine needed once deployed. Read-only market data calls are
NOT subject to Angel One's static-IP order-execution mandate, so a
normal (non-static) host IP is fine here.

Environment variables required (set in your host's dashboard,
never hardcode secrets in this file):
    ANGEL_API_KEY       -> from your SmartAPI app (required)
    ANGEL_CLIENT_CODE   -> your Angel One client/login ID (required)
    ANGEL_PIN           -> your trading PIN (required)
    ANGEL_TOTP_SECRET   -> the 32-char TOTP secret from enable-totp (required)
    MONGO_URI           -> your MongoDB Atlas connection string (required)
    MONGO_DB            -> database name (default: theta_tracker)
    MONGO_COLLECTION    -> collection name (default: option_greeks)
    SYMBOLS             -> comma-separated (default: NIFTY,BANKNIFTY)
    STRIKE_RANGE         -> strikes on each side of ATM (default: 2)
    POLL_SECONDS         -> polling interval during market hours (default: 30)
"""

import os
import time
import logging
import random
from datetime import datetime, date, time as dtime, timezone

import requests
import pytz
import pyotp
from pymongo import MongoClient
from SmartApi import SmartConnect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("greeks_tracker")

# ---------- Config ----------
ANGEL_API_KEY = os.environ["ANGEL_API_KEY"]
ANGEL_CLIENT_CODE = os.environ["ANGEL_CLIENT_CODE"]
ANGEL_PIN = os.environ["ANGEL_PIN"]
ANGEL_TOTP_SECRET = os.environ["ANGEL_TOTP_SECRET"]

MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.environ.get("MONGO_DB", "theta_tracker")
MONGO_COLLECTION = os.environ.get("MONGO_COLLECTION", "option_greeks")

SYMBOLS = [s.strip().upper() for s in os.environ.get("SYMBOLS", "NIFTY,BANKNIFTY").split(",") if s.strip()]
STRIKE_RANGE = int(os.environ.get("STRIKE_RANGE", "2"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "30"))

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
CLOSED_MARKET_CHECK_SECONDS = 300

# NSE is used ONLY for the free, non-sensitive expiry-date lookup and
# holiday calendar — all financial data (theta, IV, etc.) comes from
# the authenticated Angel One API.
NSE_BASE = "https://www.nseindia.com"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "*/*",
    "Referer": f"{NSE_BASE}/option-chain",
}


# ---------- Angel One session ----------
def angel_login() -> SmartConnect:
    smart_api = SmartConnect(api_key=ANGEL_API_KEY)
    totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
    session_data = smart_api.generateSession(ANGEL_CLIENT_CODE, ANGEL_PIN, totp)
    if not session_data.get("status"):
        raise RuntimeError(f"Angel One login failed: {session_data}")
    log.info("Logged in to Angel One SmartAPI as %s", ANGEL_CLIENT_CODE)
    return smart_api


# ---------- NSE helpers (expiry dates + holiday calendar only) ----------
def new_nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    s.get(NSE_BASE, timeout=10)
    s.get(f"{NSE_BASE}/option-chain", timeout=10)
    return s


def fetch_nearest_expiry_angel_format(nse_session: requests.Session, symbol: str) -> str:
    """Returns nearest expiry in Angel One's required 'DDMMMYYYY' format, e.g. '28AUG2026'."""
    resp = nse_session.get(f"{NSE_BASE}/api/option-chain-contract-info?symbol={symbol}", timeout=10)
    resp.raise_for_status()
    nearest = resp.json()["expiryDates"][0]  # e.g. '28-Aug-2026'
    dt = datetime.strptime(nearest, "%d-%b-%Y")
    return dt.strftime("%d%b%Y").upper()


def fetch_fo_holidays() -> set:
    """Uses nselib's trading_holiday_calendar() — runs once a day, so its per-call
    overhead doesn't matter here, unlike in the fast 30s data loop."""
    try:
        from nselib import libutil
        df = libutil.trading_holiday_calendar()
        fo_df = df[df["Product"] == "Equity Derivatives"]
        holidays = set()
        for d in fo_df["tradingDate"]:
            try:
                holidays.add(datetime.strptime(d, "%d-%b-%Y").date())
            except ValueError:
                continue
        log.info("Loaded %d F&O holiday dates for this year", len(holidays))
        return holidays
    except Exception as e:
        log.warning("Could not fetch holiday calendar (%s) — using weekday-only check", e)
        return set()


def is_market_open(holidays: set) -> bool:
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:
        return False
    if now_ist.date() in holidays:
        return False
    return MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE


# ---------- Parsing & ATM selection ----------
def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def select_atm_strikes(greek_rows: list, strike_range: int) -> list:
    """
    Groups the raw optionGreek rows by strike, finds ATM as the strike
    whose CE delta is closest to 0.5 (the standard definition — no
    separate spot-price lookup or instrument-token needed), then
    returns ATM +/- strike_range strikes with both CE and PE greeks.
    """
    by_strike = {}
    for row in greek_rows:
        strike = safe_float(row.get("strikePrice"))
        opt_type = row.get("optionType")
        if strike is None or opt_type not in ("CE", "PE"):
            continue
        by_strike.setdefault(strike, {})[opt_type] = row

    strikes_sorted = sorted(by_strike.keys())
    if not strikes_sorted:
        return []

    def ce_delta_distance(k):
        ce = by_strike[k].get("CE")
        delta = safe_float(ce.get("delta")) if ce else None
        return abs(delta - 0.5) if delta is not None else float("inf")

    atm_strike = min(strikes_sorted, key=ce_delta_distance)
    atm_idx = strikes_sorted.index(atm_strike)

    lo = max(0, atm_idx - strike_range)
    hi = min(len(strikes_sorted), atm_idx + strike_range + 1)

    result = []
    for k in strikes_sorted[lo:hi]:
        entry = {"strike": k, "is_atm": (k == atm_strike)}
        for opt_type in ("CE", "PE"):
            row = by_strike[k].get(opt_type)
            if row:
                entry[opt_type] = {
                    "delta": safe_float(row.get("delta")),
                    "gamma": safe_float(row.get("gamma")),
                    "theta": safe_float(row.get("theta")),
                    "vega": safe_float(row.get("vega")),
                    "iv": safe_float(row.get("impliedVolatility")),
                }
        result.append(entry)
    return result


# ---------- Main loop ----------
def main():
    client = MongoClient(MONGO_URI)
    collection = client[MONGO_DB][MONGO_COLLECTION]
    log.info("Connected to MongoDB db=%s collection=%s", MONGO_DB, MONGO_COLLECTION)
    log.info("Tracking symbols=%s strike_range=%s poll_seconds=%s", SYMBOLS, STRIKE_RANGE, POLL_SECONDS)

    smart_api = angel_login()
    login_date = date.today()

    nse_session = new_nse_session()
    holidays = fetch_fo_holidays()
    holidays_fetched_date = date.today()
    expiry_cache = {}  # symbol -> (date_fetched, expiry_str)

    consecutive_failures = 0

    while True:
        today = date.today()

        # Angel One session expires at midnight IST — re-login once per day
        if today != login_date:
            try:
                smart_api = angel_login()
                login_date = today
            except Exception as e:
                log.error("Daily re-login failed, will retry next cycle: %s", e)
                time.sleep(30)
                continue

        if today != holidays_fetched_date:
            holidays = fetch_fo_holidays()
            holidays_fetched_date = today

        if not is_market_open(holidays):
            log.info("Market closed — sleeping %ds", CLOSED_MARKET_CHECK_SECONDS)
            time.sleep(CLOSED_MARKET_CHECK_SECONDS)
            continue

        loop_start = time.time()

        for symbol in SYMBOLS:
            try:
                cached = expiry_cache.get(symbol)
                if cached is None or cached[0] != today:
                    try:
                        expiry = fetch_nearest_expiry_angel_format(nse_session, symbol)
                    except requests.exceptions.RequestException:
                        nse_session = new_nse_session()
                        expiry = fetch_nearest_expiry_angel_format(nse_session, symbol)
                    expiry_cache[symbol] = (today, expiry)
                else:
                    expiry = cached[1]

                resp = smart_api.optionGreek({"name": symbol, "expirydate": expiry})

                if not resp.get("status"):
                    log.warning("optionGreek failed for %s: %s", symbol, resp)
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        log.info("Repeated failures — forcing re-login")
                        smart_api = angel_login()
                        login_date = date.today()
                        consecutive_failures = 0
                    continue

                strikes = select_atm_strikes(resp.get("data", []), STRIKE_RANGE)
                if not strikes:
                    log.warning("No strikes parsed for %s, skipping", symbol)
                    continue

                doc = {
                    "timestamp": datetime.now(timezone.utc),
                    "symbol": symbol,
                    "expiry": expiry,
                    "strikes": strikes,
                }
                collection.insert_one(doc)

                atm = next((s["strike"] for s in strikes if s["is_atm"]), None)
                atm_theta_ce = next(
                    (s["CE"]["theta"] for s in strikes if s["is_atm"] and "CE" in s), None
                )
                log.info(
                    "Saved %s | expiry=%s | strikes=%d | ATM=%s | ATM theta_CE=%s",
                    symbol, expiry, len(strikes), atm, atm_theta_ce,
                )
                consecutive_failures = 0

            except Exception as e:
                log.exception("Error processing %s: %s", symbol, e)
                consecutive_failures += 1

            # small gap between symbols within the same cycle — stays well
            # under Angel One's rate limits even with multiple symbols
            time.sleep(1)

        elapsed = time.time() - loop_start
        sleep_for = max(POLL_SECONDS - elapsed, 0.5)
        sleep_for += random.uniform(0, 0.5)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
