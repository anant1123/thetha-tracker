# import os
# import time
# import logging
# import random
# from datetime import datetime, date, time as dtime, timezone

# import requests
# import pytz
# import pyotp
# from pymongo import MongoClient
# from SmartApi import SmartConnect

# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s [%(levelname)s] %(message)s",
# )
# log = logging.getLogger("greeks_tracker")

# # ---------- Config ----------
# ANGEL_API_KEY = os.environ["ANGEL_API_KEY"]
# ANGEL_CLIENT_CODE = os.environ["ANGEL_CLIENT_CODE"]
# ANGEL_PIN = os.environ["ANGEL_PIN"]
# ANGEL_TOTP_SECRET = os.environ["ANGEL_TOTP_SECRET"]

# MONGO_URI = os.environ["MONGO_URI"]
# MONGO_DB = os.environ.get("MONGO_DB", "theta_tracker")
# MONGO_COLLECTION = os.environ.get("MONGO_COLLECTION", "option_greeks")

# SYMBOLS = [s.strip().upper() for s in os.environ.get("SYMBOLS", "NIFTY,BANKNIFTY").split(",") if s.strip()]
# _strike_range_raw = os.environ.get("STRIKE_RANGE", "2").strip().upper()
# STRIKE_RANGE = None if _strike_range_raw == "ALL" else int(_strike_range_raw)  # None = keep the full chain
# POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "30"))

# IST = pytz.timezone("Asia/Kolkata")
# MARKET_OPEN = dtime(9, 15)
# MARKET_CLOSE = dtime(15, 30)

# # NSE is used ONLY for the free, non-sensitive expiry-date lookup and
# # holiday calendar — all financial data (theta, IV, etc.) comes from
# # the authenticated Angel One API.
# NSE_BASE = "https://www.nseindia.com"
# NSE_HEADERS = {
#     "User-Agent": (
#         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
#         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
#     ),
#     "Accept-Language": "en-US,en;q=0.9",
#     "Accept": "*/*",
#     "Referer": f"{NSE_BASE}/option-chain",
# }


# # ---------- Angel One session ----------
# def angel_login() -> SmartConnect:
#     smart_api = SmartConnect(api_key=ANGEL_API_KEY)
#     totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
#     session_data = smart_api.generateSession(ANGEL_CLIENT_CODE, ANGEL_PIN, totp)
#     if not session_data.get("status"):
#         raise RuntimeError(f"Angel One login failed: {session_data}")
#     log.info("Logged in to Angel One SmartAPI as %s", ANGEL_CLIENT_CODE)
#     return smart_api


# # ---------- NSE helpers (expiry dates + holiday calendar only) ----------
# def new_nse_session() -> requests.Session:
#     s = requests.Session()
#     s.headers.update(NSE_HEADERS)
#     s.get(NSE_BASE, timeout=10)
#     s.get(f"{NSE_BASE}/option-chain", timeout=10)
#     return s


# def fetch_nearest_expiry_angel_format(nse_session: requests.Session, symbol: str) -> str:
#     """Returns nearest expiry in Angel One's required 'DDMMMYYYY' format, e.g. '28AUG2026'."""
#     resp = nse_session.get(f"{NSE_BASE}/api/option-chain-contract-info?symbol={symbol}", timeout=10)
#     resp.raise_for_status()
#     nearest = resp.json()["expiryDates"][0]  # e.g. '28-Aug-2026'
#     dt = datetime.strptime(nearest, "%d-%b-%Y")
#     return dt.strftime("%d%b%Y").upper()


# def fetch_fo_holidays() -> set:
#     """Uses nselib's trading_holiday_calendar() — runs once a day, so its per-call
#     overhead doesn't matter here, unlike in the fast 30s data loop."""
#     try:
#         from nselib import libutil
#         df = libutil.trading_holiday_calendar()
#         fo_df = df[df["Product"] == "Equity Derivatives"]
#         holidays = set()
#         for d in fo_df["tradingDate"]:
#             try:
#                 holidays.add(datetime.strptime(d, "%d-%b-%Y").date())
#             except ValueError:
#                 continue
#         log.info("Loaded %d F&O holiday dates for this year", len(holidays))
#         return holidays
#     except Exception as e:
#         log.warning("Could not fetch holiday calendar (%s) — using weekday-only check", e)
#         return set()


# def is_market_open(holidays: set) -> bool:
#     now_ist = datetime.now(IST)
#     if now_ist.weekday() >= 5:
#         return False
#     if now_ist.date() in holidays:
#         return False
#     return MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE


# def seconds_until_market_open(holidays: set):
#     """Returns seconds until today's market open, or None if the market
#     won't open today at all (weekend/holiday) or open has already passed."""
#     now_ist = datetime.now(IST)
#     if now_ist.weekday() >= 5 or now_ist.date() in holidays:
#         return None
#     if now_ist.time() >= MARKET_OPEN:
#         return None
#     open_dt = IST.localize(datetime.combine(now_ist.date(), MARKET_OPEN))
#     return (open_dt - now_ist).total_seconds()


# # ---------- Parsing & ATM selection ----------
# def safe_float(v):
#     try:
#         return float(v)
#     except (TypeError, ValueError):
#         return None


# def select_atm_strikes(greek_rows: list, strike_range) -> list:
#     """
#     Groups the raw optionGreek rows by strike, finds ATM as the strike
#     whose CE delta is closest to 0.5 (the standard definition — no
#     separate spot-price lookup or instrument-token needed), then
#     returns ATM +/- strike_range strikes with both CE and PE greeks.
#     strike_range=None means keep the ENTIRE chain (no filtering).
#     """
#     by_strike = {}
#     for row in greek_rows:
#         strike = safe_float(row.get("strikePrice"))
#         opt_type = row.get("optionType")
#         if strike is None or opt_type not in ("CE", "PE"):
#             continue
#         by_strike.setdefault(strike, {})[opt_type] = row

#     strikes_sorted = sorted(by_strike.keys())
#     if not strikes_sorted:
#         return []

#     def ce_delta_distance(k):
#         ce = by_strike[k].get("CE")
#         delta = safe_float(ce.get("delta")) if ce else None
#         return abs(delta - 0.5) if delta is not None else float("inf")

#     atm_strike = min(strikes_sorted, key=ce_delta_distance)

#     if strike_range is None:
#         lo, hi = 0, len(strikes_sorted)  # keep everything
#     else:
#         atm_idx = strikes_sorted.index(atm_strike)
#         lo = max(0, atm_idx - strike_range)
#         hi = min(len(strikes_sorted), atm_idx + strike_range + 1)

#     result = []
#     for k in strikes_sorted[lo:hi]:
#         entry = {"strike": k, "is_atm": (k == atm_strike)}
#         for opt_type in ("CE", "PE"):
#             row = by_strike[k].get(opt_type)
#             if row:
#                 entry[opt_type] = {
#                     "delta": safe_float(row.get("delta")),
#                     "gamma": safe_float(row.get("gamma")),
#                     "theta": safe_float(row.get("theta")),
#                     "vega": safe_float(row.get("vega")),
#                     "iv": safe_float(row.get("impliedVolatility")),
#                 }
#         result.append(entry)
#     return result

# JOB_SAFETY_MAX_SECONDS = float(os.environ.get("JOB_SAFETY_MAX_SECONDS", str(5 * 3600 + 30 * 60)))  # 5h30m
# PRE_MARKET_GRACE_MINUTES = float(os.environ.get("PRE_MARKET_GRACE_MINUTES", "35"))


# def main():
#     job_start = time.time()

#     client = MongoClient(MONGO_URI)
#     db = client[MONGO_DB]
#     collections = {symbol: db[f"{MONGO_COLLECTION}_{symbol.lower()}"] for symbol in SYMBOLS}
#     log.info("Connected to MongoDB db=%s collections=%s", MONGO_DB, list(collections.values()))
#     log.info("Tracking symbols=%s strike_range=%s poll_seconds=%s", SYMBOLS, STRIKE_RANGE, POLL_SECONDS)

#     holidays = fetch_fo_holidays()

#     wait_secs = seconds_until_market_open(holidays)
#     if wait_secs is not None and wait_secs <= PRE_MARKET_GRACE_MINUTES * 60:
#         log.info("Market opens in %.0fs — waiting for it instead of exiting", wait_secs)
#         time.sleep(wait_secs + 2)
#     elif not is_market_open(holidays):
#         log.info("Market is closed and not opening soon — exiting so the next scheduled run can pick up.")
#         return

#     smart_api = angel_login()
#     nse_session = new_nse_session()
#     expiry_cache = {}  # symbol -> expiry_str, fetched once per job

#     consecutive_failures = 0

#     while True:
#         if time.time() - job_start > JOB_SAFETY_MAX_SECONDS:
#             log.info("Approaching GitHub Actions' time limit — exiting cleanly so the next scheduled run continues.")
#             return

#         if not is_market_open(holidays):
#             log.info("Market closed — exiting cleanly.")
#             return

#         loop_start = time.time()

#         for symbol in SYMBOLS:
#             try:
#                 if symbol not in expiry_cache:
#                     try:
#                         expiry = fetch_nearest_expiry_angel_format(nse_session, symbol)
#                     except requests.exceptions.RequestException:
#                         nse_session = new_nse_session()
#                         expiry = fetch_nearest_expiry_angel_format(nse_session, symbol)
#                     expiry_cache[symbol] = expiry
#                 else:
#                     expiry = expiry_cache[symbol]

#                 resp = smart_api.optionGreek({"name": symbol, "expirydate": expiry})

#                 if not resp.get("status"):
#                     log.warning("optionGreek failed for %s: %s", symbol, resp)
#                     consecutive_failures += 1
#                     if consecutive_failures >= 3:
#                         log.info("Repeated failures — forcing re-login")
#                         smart_api = angel_login()
#                         consecutive_failures = 0
#                     continue

#                 strikes = select_atm_strikes(resp.get("data", []), STRIKE_RANGE)
#                 if not strikes:
#                     log.warning("No strikes parsed for %s, skipping", symbol)
#                     continue

#                 doc = {
#                     "timestamp": datetime.now(timezone.utc),
#                     "symbol": symbol,
#                     "expiry": expiry,
#                     "strikes": strikes,
#                 }
#                 collections[symbol].insert_one(doc)

#                 atm = next((s["strike"] for s in strikes if s["is_atm"]), None)
#                 atm_theta_ce = next(
#                     (s["CE"]["theta"] for s in strikes if s["is_atm"] and "CE" in s), None
#                 )
#                 log.info(
#                     "Saved %s | expiry=%s | strikes=%d | ATM=%s | ATM theta_CE=%s",
#                     symbol, expiry, len(strikes), atm, atm_theta_ce,
#                 )
#                 consecutive_failures = 0

#             except Exception as e:
#                 log.exception("Error processing %s: %s", symbol, e)
#                 consecutive_failures += 1

#             time.sleep(1)

#         elapsed = time.time() - loop_start
#         sleep_for = max(POLL_SECONDS - elapsed, 0.5)
#         sleep_for += random.uniform(0, 0.5)
#         time.sleep(sleep_for)


# if __name__ == "__main__":
#     main()


import os
import time
import logging
import random
from datetime import datetime, date, time as dtime, timezone
from zoneinfo import ZoneInfo

import requests
import pyotp
from pymongo import MongoClient
from pymongo.errors import PyMongoError
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
_strike_range_raw = os.environ.get("STRIKE_RANGE", "2").strip().upper()
STRIKE_RANGE = None if _strike_range_raw == "ALL" else int(_strike_range_raw)  # None = keep the full chain
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "30"))

# Data-quality thresholds. Values outside these are FLAGGED, never silently
# dropped -- a downstream consumer can filter on `flags`, but the raw print
# from the API is still there for audit. This is what makes things like
# "positive theta on a deep-ITM strike near expiry" or ">80% IV" visible
# instead of blending in with good ticks, which is what happened before.
MAX_PLAUSIBLE_IV = float(os.environ.get("MAX_PLAUSIBLE_IV", "30"))   # % -- Nifty/BankNifty rarely trade above this
MIN_PLAUSIBLE_IV = float(os.environ.get("MIN_PLAUSIBLE_IV", "3"))    # %
DELTA_PARITY_TOLERANCE = float(os.environ.get("DELTA_PARITY_TOLERANCE", "0.1"))  # |CE.delta - PE.delta - 1|
ATM_DELTA_DISTANCE_WARN = float(os.environ.get("ATM_DELTA_DISTANCE_WARN", "0.15"))

# zoneinfo (stdlib, Python 3.9+) instead of pytz -- one less dependency,
# and datetime.now(IST) / datetime.combine(..., tzinfo=IST) both just work.
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

# Only used if the live nselib holiday fetch fails or comes back empty.
# A silently-empty holiday set doesn't just risk running on a holiday
# (harmless) -- nselib returning a *wrong* calendar could also make the
# script treat a real trading day as a holiday and skip it entirely, which
# looks identical to "the workflow didn't fire" in the stored data. This
# fallback is a safety net, not the primary source -- update it yearly.
FALLBACK_HOLIDAYS_2026 = {
    date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26), date(2026, 3, 31),
    date(2026, 4, 3), date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2), date(2026, 10, 20),
    date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25),
}

# NSE is used ONLY for the free, non-sensitive expiry-date lookup and
# holiday calendar -- all financial data (theta, IV, etc.) comes from
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
HTTP_TIMEOUT = 10
HTTP_RETRIES = 3

JOB_SAFETY_MAX_SECONDS = float(os.environ.get("JOB_SAFETY_MAX_SECONDS", str(5 * 3600)))  # 5h
PRE_MARKET_GRACE_MINUTES = float(os.environ.get("PRE_MARKET_GRACE_MINUTES", "35"))


# ---------- small retry helper ----------
def with_retries(fn, *args, retries=HTTP_RETRIES, backoff=1.5, what="operation", **kwargs):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            log.warning("%s failed (attempt %d/%d): %s", what, attempt, retries, e)
            time.sleep(backoff ** attempt)
    raise last_exc


# ---------- Angel One session ----------
def angel_login() -> SmartConnect:
    def _login():
        smart_api = SmartConnect(api_key=ANGEL_API_KEY)
        totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
        session_data = smart_api.generateSession(ANGEL_CLIENT_CODE, ANGEL_PIN, totp)
        if not session_data.get("status"):
            raise RuntimeError(f"Angel One login failed: {session_data}")
        return smart_api

    smart_api = with_retries(_login, what="Angel One login")
    log.info("Logged in to Angel One SmartAPI as %s", ANGEL_CLIENT_CODE)
    return smart_api


# ---------- NSE helpers (expiry dates + holiday calendar only) ----------
def new_nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    s.get(NSE_BASE, timeout=HTTP_TIMEOUT)
    s.get(f"{NSE_BASE}/option-chain", timeout=HTTP_TIMEOUT)
    return s


def fetch_nearest_expiry_angel_format(nse_session: requests.Session, symbol: str) -> str:
    """Returns nearest expiry in Angel One's required 'DDMMMYYYY' format, e.g. '28AUG2026'."""
    resp = nse_session.get(f"{NSE_BASE}/api/option-chain-contract-info?symbol={symbol}", timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    nearest = resp.json()["expiryDates"][0]  # e.g. '28-Aug-2026'
    dt = datetime.strptime(nearest, "%d-%b-%Y")
    return dt.strftime("%d%b%Y").upper()


def fetch_fo_holidays() -> set:
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
        if not holidays:
            raise RuntimeError("nselib returned an empty holiday list")
        log.info("Loaded %d F&O holiday dates from nselib", len(holidays))
        return holidays
    except Exception as e:
        log.warning(
            "Could not fetch live holiday calendar (%s) -- using hardcoded fallback "
            "list (%d dates, update yearly)", e, len(FALLBACK_HOLIDAYS_2026),
        )
        return set(FALLBACK_HOLIDAYS_2026)


def is_market_open(now_ist_dt: datetime, holidays: set) -> bool:
    if now_ist_dt.weekday() >= 5:
        return False
    if now_ist_dt.date() in holidays:
        return False
    return MARKET_OPEN <= now_ist_dt.time() <= MARKET_CLOSE


def seconds_until_market_open(now_ist_dt: datetime, holidays: set):
    """Returns seconds until today's market open, or None if the market
    won't open today at all (weekend/holiday) or open has already passed."""
    if now_ist_dt.weekday() >= 5 or now_ist_dt.date() in holidays:
        return None
    if now_ist_dt.time() >= MARKET_OPEN:
        return None
    open_dt = datetime.combine(now_ist_dt.date(), MARKET_OPEN, tzinfo=IST)
    return (open_dt - now_ist_dt).total_seconds()


def now_ist() -> datetime:
    return datetime.now(IST)


def fmt_ist(dt_utc: datetime) -> str:
    """Human-readable IST string for a tz-aware UTC datetime.

    Kept separate from the canonical `timestamp` field: MongoDB always
    stores BSON dates as UTC milliseconds on disk no matter what tzinfo
    you pass in, so there is no way to make the stored value itself "be"
    IST -- and storing a naive IST clock time as the canonical timestamp
    would silently break range queries/indexes/sorting. This field exists
    purely so IST doesn't need to be reconstructed by hand on every read.
    """
    return dt_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


# ---------- Parsing, ATM selection & data-quality flags ----------
def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def flag_option_side(side: dict) -> list:
    """Flags for one CE/PE leg. Nothing is dropped here -- this just makes
    bad prints (positive theta, implausible IV) visible/filterable instead
    of indistinguishable from good data, which is what let those slip
    through silently before."""
    flags = []
    theta = side.get("theta")
    iv = side.get("iv")
    if theta is not None and theta > 0:
        flags.append("positive_theta")
    if iv is not None and (iv < MIN_PLAUSIBLE_IV or iv > MAX_PLAUSIBLE_IV):
        flags.append("iv_out_of_range")
    return flags


def select_atm_strikes(greek_rows: list, strike_range):
    """
    Groups the raw optionGreek rows by strike, finds ATM as the strike
    whose CE delta is closest to 0.5, then returns ATM +/- strike_range
    strikes with both CE and PE greeks, each annotated with data-quality
    flags. strike_range=None keeps the entire chain.

    Returns (strikes, doc_flags) -- doc_flags covers snapshot-level issues
    (e.g. an unreliable ATM pick, an empty chain).
    """
    doc_flags = []
    by_strike = {}
    for row in greek_rows:
        strike = safe_float(row.get("strikePrice"))
        opt_type = row.get("optionType")
        if strike is None or opt_type not in ("CE", "PE"):
            continue
        by_strike.setdefault(strike, {})[opt_type] = row

    strikes_sorted = sorted(by_strike.keys())
    if not strikes_sorted:
        return [], ["empty_chain"]

    def ce_delta_distance(k):
        ce = by_strike[k].get("CE")
        delta = safe_float(ce.get("delta")) if ce else None
        return abs(delta - 0.5) if delta is not None else float("inf")

    atm_strike = min(strikes_sorted, key=ce_delta_distance)
    if ce_delta_distance(atm_strike) > ATM_DELTA_DISTANCE_WARN:
        # Happens when even the closest CE delta is far from 0.5 -- usually
        # illiquid/stale quotes across the whole chain (e.g. near expiry).
        doc_flags.append("atm_pick_uncertain")

    if strike_range is None:
        lo, hi = 0, len(strikes_sorted)
    else:
        atm_idx = strikes_sorted.index(atm_strike)
        lo = max(0, atm_idx - strike_range)
        hi = min(len(strikes_sorted), atm_idx + strike_range + 1)

    result = []
    for k in strikes_sorted[lo:hi]:
        entry = {"strike": k, "is_atm": (k == atm_strike)}

        for opt_type in ("CE", "PE"):
            row = by_strike[k].get(opt_type)
            if not row:
                continue
            side = {
                "delta": safe_float(row.get("delta")),
                "gamma": safe_float(row.get("gamma")),
                "theta": safe_float(row.get("theta")),
                "vega": safe_float(row.get("vega")),
                "iv": safe_float(row.get("impliedVolatility")),
            }
            side_flags = flag_option_side(side)
            if side_flags:
                side["flags"] = side_flags
            entry[opt_type] = side

        entry_flags = []
        if "CE" not in entry or "PE" not in entry:
            entry_flags.append("missing_leg")
        elif entry["CE"].get("delta") is not None and entry["PE"].get("delta") is not None:
            diff = entry["CE"]["delta"] - entry["PE"]["delta"]
            if abs(diff - 1) > DELTA_PARITY_TOLERANCE:
                entry_flags.append("delta_parity_violation")
        if entry_flags:
            entry["flags"] = entry_flags

        result.append(entry)

    return result, doc_flags


def main():
    job_start_utc = datetime.now(timezone.utc)

    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    collections = {symbol: db[f"{MONGO_COLLECTION}_{symbol.lower()}"] for symbol in SYMBOLS}
    # Separate "runs" collection: one doc per meaningful lifecycle event
    # (start / market-closed exit / login failure / end). This is what
    # turns "no rows for 5 hours" from an unexplained gap into a specific,
    # loggable reason -- Aug 27-28 being fully missing, or most of
    # September starting hours late, would both have shown up here
    # immediately instead of only being discoverable by analyzing gaps
    # in the data after the fact.
    runs = db[f"{MONGO_COLLECTION}_runs"]

    def log_run(event, **extra):
        ts = datetime.now(timezone.utc)
        try:
            runs.insert_one({
                "event": event,
                "timestamp": ts,
                "timestamp_ist": fmt_ist(ts),
                **extra,
            })
        except PyMongoError as e:
            log.warning("Could not write heartbeat doc (%s): %s", event, e)

    log.info("Connected to MongoDB db=%s collections=%s", MONGO_DB, [c.name for c in collections.values()])
    log.info("Tracking symbols=%s strike_range=%s poll_seconds=%s", SYMBOLS, STRIKE_RANGE, POLL_SECONDS)
    log_run("job_start", symbols=SYMBOLS)

    holidays = fetch_fo_holidays()
    nowi = now_ist()

    wait_secs = seconds_until_market_open(nowi, holidays)
    if wait_secs is not None and wait_secs <= PRE_MARKET_GRACE_MINUTES * 60:
        log.info("Market opens in %.0fs (now=%s) -- waiting for it instead of exiting",
                  wait_secs, nowi.strftime("%H:%M:%S IST"))
        time.sleep(wait_secs + 2)
    elif not is_market_open(nowi, holidays):
        log.info("Market is closed at %s and not opening soon -- exiting so the next scheduled run can pick up.",
                  nowi.strftime("%Y-%m-%d %H:%M:%S IST"))
        log_run("exit_market_closed", now_ist=nowi.isoformat())
        return

    try:
        smart_api = angel_login()
    except Exception as e:
        log.exception("Could not log in to Angel One -- exiting without capturing any data this run.")
        log_run("exit_login_failed", error=str(e))
        return

    nse_session = new_nse_session()
    expiry_cache = {}
    consecutive_failures = 0
    saved_this_run = 0
    flagged_this_run = 0

    while True:
        if (datetime.now(timezone.utc) - job_start_utc).total_seconds() > JOB_SAFETY_MAX_SECONDS:
            log.info("Approaching the time limit -- exiting cleanly so the next scheduled run continues.")
            break

        nowi = now_ist()
        if not is_market_open(nowi, holidays):
            log.info("Market closed at %s -- exiting cleanly.", nowi.strftime("%H:%M:%S IST"))
            break

        loop_start = time.time()

        for symbol in SYMBOLS:
            try:
                if symbol not in expiry_cache:
                    try:
                        expiry = fetch_nearest_expiry_angel_format(nse_session, symbol)
                    except requests.exceptions.RequestException:
                        nse_session = new_nse_session()
                        expiry = fetch_nearest_expiry_angel_format(nse_session, symbol)
                    expiry_cache[symbol] = expiry
                else:
                    expiry = expiry_cache[symbol]

                resp = smart_api.optionGreek({"name": symbol, "expirydate": expiry})

                if not resp.get("status"):
                    log.warning("optionGreek failed for %s: %s", symbol, resp)
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        log.info("Repeated failures -- forcing re-login")
                        try:
                            smart_api = angel_login()
                        except Exception as e:
                            log.exception("Re-login failed: %s", e)
                        consecutive_failures = 0
                    continue

                strikes, doc_flags = select_atm_strikes(resp.get("data", []), STRIKE_RANGE)
                if not strikes:
                    log.warning("No strikes parsed for %s, skipping", symbol)
                    continue

                ts_utc = datetime.now(timezone.utc)
                doc = {
                    "timestamp": ts_utc,                                     # canonical, UTC -- correct for Mongo
                    "timestamp_ist": fmt_ist(ts_utc),                        # human-readable IST
                    "trading_date_ist": ts_utc.astimezone(IST).strftime("%Y-%m-%d"),  # unambiguous session key
                    "symbol": symbol,
                    "expiry": expiry,
                    "strikes": strikes,
                }
                if doc_flags:
                    doc["flags"] = doc_flags

                try:
                    collections[symbol].insert_one(doc)
                except PyMongoError as e:
                    log.error("Mongo insert failed for %s: %s", symbol, e)
                    continue

                saved_this_run += 1
                doc_flag_count = sum(
                    1 for s in strikes
                    if s.get("flags") or (s.get("CE") or {}).get("flags") or (s.get("PE") or {}).get("flags")
                )
                flagged_this_run += doc_flag_count

                atm = next((s["strike"] for s in strikes if s["is_atm"]), None)
                atm_theta_ce = next((s["CE"]["theta"] for s in strikes if s["is_atm"] and "CE" in s), None)
                log.info(
                    "Saved %s | expiry=%s | strikes=%d (flagged=%d) | ATM=%s | ATM theta_CE=%s | %s",
                    symbol, expiry, len(strikes), doc_flag_count, atm, atm_theta_ce, doc["timestamp_ist"],
                )
                consecutive_failures = 0

            except Exception as e:
                log.exception("Error processing %s: %s", symbol, e)
                consecutive_failures += 1

            time.sleep(1)

        elapsed = time.time() - loop_start
        sleep_for = max(POLL_SECONDS - elapsed, 0.5)
        sleep_for += random.uniform(0, 0.5)
        time.sleep(sleep_for)

    log_run("job_end", saved=saved_this_run, flagged=flagged_this_run)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Unhandled exception -- job is exiting")
        raise
