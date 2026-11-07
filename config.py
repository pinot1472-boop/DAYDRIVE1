"""DAYDRIVE v1 · config from environment (Railway)."""
from __future__ import annotations

import os


def _bool(k: str, default: bool = False) -> bool:
    v = os.getenv(k)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _int(k: str, default: int) -> int:
    v = os.getenv(k)
    if v is None or str(v).strip() == "":
        return default
    try:
        return int(float(v))
    except Exception:
        return default


def _float(k: str, default: float) -> float:
    v = os.getenv(k)
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(v)
    except Exception:
        return default


# ── Mode ──────────────────────────────────────────────────────────────
MODE = os.getenv("MODE", "SIGNAL").strip().upper()  # SIGNAL | LIVE
LIVE_CONFIRMED = _bool("LIVE_CONFIRMED", False)

# ── BingX ─────────────────────────────────────────────────────────────
BINGX_API_KEY = os.getenv("BINGX_API_KEY", "").strip()
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET", "").strip()
BINGX_BASE_URL = os.getenv("BINGX_BASE_URL", "https://open-api.bingx.com").rstrip("/")
RECV_WINDOW = _int("RECV_WINDOW", 5000)
POST_ONLY = _bool("POST_ONLY", True)

# ── Telegram ──────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ── Universe / scan ───────────────────────────────────────────────────
TIMEFRAME = os.getenv("TIMEFRAME", "15m").strip()
SCAN_SEC = _int("SCAN_SEC", 90)
MAX_SYMBOLS = _int("MAX_SYMBOLS", 50)
MIN_QUOTE_VOL_24H = _float("MIN_QUOTE_VOL_24H", 3_000_000)
KLINE_CONCURRENCY = _int("KLINE_CONCURRENCY", 10)
SYMBOLS = os.getenv("SYMBOLS", "").strip()  # CSV override
BTC_SYMBOL = os.getenv("BTC_SYMBOL", "BTC-USDT").strip()
EXCLUDE_PREFIXES = [
    x.strip().upper()
    for x in os.getenv("EXCLUDE_PREFIXES", "USDC,EUR,BUSD,TUSD,NCCO,NCSI,NCS").split(",")
    if x.strip()
]

# ── Session window (UTC) ──────────────────────────────────────────────
SESSION_START = _int("SESSION_START", 8)   # inclusive
SESSION_END = _int("SESSION_END", 20)     # exclusive for new entries
RESID_START_HOUR = _int("RESID_START_HOUR", 8)
RESID_END_HOUR = _int("RESID_END_HOUR", 13)  # Europe residual window

# ── Residual rank ─────────────────────────────────────────────────────
USE_RESIDUAL_RANK = _bool("USE_RESIDUAL_RANK", True)
MIN_RESID_RANK_PCT = _float("MIN_RESID_RANK_PCT", 70.0)  # top/bottom 30%
RESID_MIN_BARS = _int("RESID_MIN_BARS", 8)

# ── Trend / pullback ──────────────────────────────────────────────────
EMA_FAST = _int("EMA_FAST", 9)
EMA_SLOW = _int("EMA_SLOW", 21)
EMA_TREND = _int("EMA_TREND", 50)
PB_ATR_MAX = _float("PB_ATR_MAX", 1.2)   # max distance to EMA for pullback
PB_RVOL_MIN = _float("PB_RVOL_MIN", 1.10)
MIN_BODY_ATR = _float("MIN_BODY_ATR", 0.15)

# ── Squeeze ───────────────────────────────────────────────────────────
USE_SQUEEZE = _bool("USE_SQUEEZE", True)
ATR_SHORT = _int("ATR_SHORT", 5)
ATR_LONG = _int("ATR_LONG", 30)
SQUEEZE_PCT_MAX = _float("SQUEEZE_PCT_MAX", 35.0)
SQUEEZE_LOOKBACK = _int("SQUEEZE_LOOKBACK", 80)
EXPAND_RVOL = _float("EXPAND_RVOL", 1.20)

# ── Score / filters ───────────────────────────────────────────────────
MIN_SCORE = _int("MIN_SCORE", 60)
ALLOW_LONG = _bool("ALLOW_LONG", True)
ALLOW_SHORT = _bool("ALLOW_SHORT", True)
SKIP_ASIA_GRAB = _bool("SKIP_ASIA_GRAB", True)  # no trade if bar looks like Asia sweep
ASIA_END_HOUR = _int("ASIA_END_HOUR", 8)
ATR_LEN = _int("ATR_LEN", 14)
VOL_LEN = _int("VOL_LEN", 20)

# ── Risk ──────────────────────────────────────────────────────────────
RISK_PCT = _float("RISK_PCT", 0.75)
MAX_LEVERAGE = _float("MAX_LEVERAGE", 5.0)
SL_ATR = _float("SL_ATR", 1.0)
TP1_R = _float("TP1_R", 1.5)
TP2_R = _float("TP2_R", 2.2)
MAX_HOLD_BARS = _int("MAX_HOLD_BARS", 24)  # 15m bars ~6h
MAX_TRADES_DAY = _int("MAX_TRADES_DAY", 4)
MAX_LOSSES_DAY = _int("MAX_LOSSES_DAY", 2)
COOLDOWN_SEC = _int("COOLDOWN_SEC", 300)
MAX_OPEN = _int("MAX_OPEN", 2)
MAX_SIGNALS_CYCLE = _int("MAX_SIGNALS_CYCLE", 2)

# ── Confirm 5m ────────────────────────────────────────────────────────
USE_CONFIRM_5M = _bool("USE_CONFIRM_5M", True)
CONFIRM_REQUIRED = _bool("CONFIRM_REQUIRED", True)
CONFIRM_INTERVAL = os.getenv("CONFIRM_INTERVAL", "5m").strip()
CONFIRM_RVOL_MIN = _float("CONFIRM_RVOL_MIN", 1.05)
CONFIRM_MIN_WICK_ATR = _float("CONFIRM_MIN_WICK_ATR", 0.12)

# ── BTC filter ────────────────────────────────────────────────────────
USE_BTC_ALIGN = _bool("USE_BTC_ALIGN", True)
BTC_EMA = _int("BTC_EMA", 21)

# ── State / log ───────────────────────────────────────────────────────
STATE_PATH = os.getenv("STATE_PATH", "/data/daydrive_state.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip()
LOG_HTTP = _bool("LOG_HTTP", False)
DAILY_REPORT_UTC = _int("DAILY_REPORT_UTC", 7)

VERSION = "DAYDRIVE v1"
