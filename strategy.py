"""DAYDRIVE v1 · residual session rank + squeeze + EMA/VWAP pullback."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config


@dataclass
class Signal:
    symbol: str
    side: str  # LONG | SHORT
    src: str  # PB | SQZ
    entry: float
    sl: float
    tp1: float
    tp2: float
    score: int
    atr: float
    rvol: float
    resid_pct: float  # cross-section residual rank 0-100
    resid_ret: float  # residual return in window
    squeeze: bool
    reason: str
    level_px: float = 0.0
    confirm_tag: str = ""
    ts: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())


def utc_hour() -> int:
    return datetime.now(timezone.utc).hour


def in_session() -> bool:
    h = utc_hour()
    return config.SESSION_START <= h < config.SESSION_END


def _ema(vals: list[float], length: int) -> list[float]:
    if not vals or length < 1:
        return []
    k = 2.0 / (length + 1)
    out = [vals[0]]
    for i in range(1, len(vals)):
        out.append(vals[i] * k + out[-1] * (1 - k))
    return out


def _atr(h: list[float], l: list[float], c: list[float], length: int) -> list[float]:
    trs = []
    for i in range(len(c)):
        if i == 0:
            trs.append(h[i] - l[i])
        else:
            trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    # RMA-like
    out = [trs[0]]
    alpha = 1.0 / length
    for i in range(1, len(trs)):
        out.append(out[-1] * (1 - alpha) + trs[i] * alpha)
    return out


def _sma(vals: list[float], length: int) -> list[float]:
    out = []
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= length:
            s -= vals[i - length]
        if i >= length - 1:
            out.append(s / length)
        else:
            out.append(s / (i + 1))
    return out


def _percentile_rank(value: float, series: list[float]) -> float:
    if not series:
        return 50.0
    below = sum(1 for x in series if x <= value)
    return 100.0 * below / len(series)


def session_active() -> bool:
    return in_session()


def residual_return(
    closes: list[float],
    btc_closes: list[float],
    rows: list[dict],
    end_idx: int,
) -> Optional[float]:
    """Return residual (coin - BTC) over Europe window bars of the current UTC day."""
    if end_idx < 5 or not btc_closes or len(btc_closes) != len(closes):
        return None
    start_h = config.RESID_START_HOUR
    end_h = config.RESID_END_HOUR
    t_ref = int(rows[end_idx].get("time") or 0)
    if t_ref <= 0:
        return None
    day0 = (t_ref // 1000) // 86400
    i0 = i1 = None
    for j in range(max(0, end_idx - 60), end_idx + 1):
        tj = int(rows[j].get("time") or 0)
        if tj <= 0:
            continue
        dayj = (tj // 1000) // 86400
        if dayj != day0:
            continue
        hj = int((tj // 1000) % 86400 // 3600)
        if hj >= start_h and i0 is None:
            i0 = j
        if hj < end_h:
            i1 = j
    # if still in Europe window, use from start to now
    h_now = int((t_ref // 1000) % 86400 // 3600)
    if h_now < end_h:
        i1 = end_idx
    if i0 is None or i1 is None or i1 <= i0:
        # fallback: last N bars
        n = max(config.RESID_MIN_BARS, 6)
        if end_idx < n:
            return None
        i0, i1 = end_idx - n, end_idx
    if closes[i0] <= 0 or btc_closes[i0] <= 0:
        return None
    r_coin = closes[i1] / closes[i0] - 1.0
    r_btc = btc_closes[i1] / btc_closes[i0] - 1.0
    return r_coin - r_btc


def is_squeezed(h: list[float], l: list[float], c: list[float], i: int) -> tuple[bool, float]:
    atr_s = _atr(h, l, c, config.ATR_SHORT)
    atr_l = _atr(h, l, c, config.ATR_LONG)
    if i >= len(atr_s) or i >= len(atr_l) or atr_l[i] <= 0:
        return False, 50.0
    ratio = atr_s[i] / atr_l[i]
    look = min(config.SQUEEZE_LOOKBACK, i)
    hist = [atr_s[j] / atr_l[j] for j in range(i - look, i + 1) if atr_l[j] > 0]
    pct = _percentile_rank(ratio, hist)
    return pct <= config.SQUEEZE_PCT_MAX, pct


def asia_sweep_bar(h: list[float], l: list[float], c: list[float], o: list[float], i: int, atr: float) -> bool:
    """Heuristic: tall wick beyond body after Asia — leave to IPDA."""
    if not config.SKIP_ASIA_GRAB or atr <= 0:
        return False
    # only relevant early day
    if utc_hour() > 14:
        return False
    body = abs(c[i] - o[i])
    upper = h[i] - max(c[i], o[i])
    lower = min(c[i], o[i]) - l[i]
    # long upper wick close mid/low or long lower wick close mid/high
    if upper > 0.8 * atr and upper > 1.5 * max(body, 1e-12) and c[i] < o[i]:
        return True
    if lower > 0.8 * atr and lower > 1.5 * max(body, 1e-12) and c[i] > o[i]:
        return True
    return False


def analyze(
    symbol: str,
    rows: list[dict],
    btc_rows: Optional[list[dict]] = None,
    resid_rank_pct: float = 50.0,
    resid_ret: float = 0.0,
    btc_bull: bool = True,
) -> Optional[Signal]:
    """
    Build DAYDRIVE signal on last closed 15m bar.
    resid_rank_pct: 100 = strongest residual long vs universe; 0 = weakest.
    """
    if not session_active():
        return None
    need = max(config.EMA_TREND, config.ATR_LONG, config.VOL_LEN) + 5
    if not rows or len(rows) < need:
        return None

    o = [float(r["open"]) for r in rows]
    h = [float(r["high"]) for r in rows]
    l = [float(r["low"]) for r in rows]
    c = [float(r["close"]) for r in rows]
    v = [float(r.get("volume") or 0) for r in rows]

    i = len(c) - 2  # last closed
    if i < need - 2:
        return None

    atr_s = _atr(h, l, c, config.ATR_LEN)
    a = atr_s[i]
    if a <= 0 or not math.isfinite(a):
        return None

    vol_ma = _sma(v, config.VOL_LEN)
    rvol = (v[i] / vol_ma[i]) if vol_ma[i] > 0 else 0.0

    ema_f = _ema(c, config.EMA_FAST)
    ema_s = _ema(c, config.EMA_SLOW)
    ema_t = _ema(c, config.EMA_TREND)

    trend_up = ema_f[i] > ema_s[i] and c[i] > ema_t[i]
    trend_dn = ema_f[i] < ema_s[i] and c[i] < ema_t[i]

    sqz, sqz_pct = is_squeezed(h, l, c, i)
    # expansion bar: range vs ATR
    bar_range = h[i] - l[i]
    expanding = bar_range >= 1.0 * a and rvol >= config.EXPAND_RVOL

    if asia_sweep_bar(h, l, c, o, i, a):
        return None

    # Direction from residual rank + local trend
    want_long = resid_rank_pct >= config.MIN_RESID_RANK_PCT and trend_up
    want_short = resid_rank_pct <= (100.0 - config.MIN_RESID_RANK_PCT) and trend_dn

    if config.USE_BTC_ALIGN:
        if want_long and not btc_bull:
            want_long = False
        if want_short and btc_bull:
            # mild: allow short only if residual very weak
            if resid_rank_pct > 20:
                want_short = False

    if not want_long and not want_short:
        return None
    if want_long and not config.ALLOW_LONG:
        return None
    if want_short and not config.ALLOW_SHORT:
        return None

    side = "LONG" if want_long else "SHORT"

    # --- Trigger A: pullback to EMA fast/slow cluster ---
    src = ""
    level = 0.0
    body = abs(c[i] - o[i])
    body_ok = body >= config.MIN_BODY_ATR * a

    if side == "LONG":
        dist_f = abs(l[i] - ema_f[i]) / a
        dist_s = abs(l[i] - ema_s[i]) / a
        touched = l[i] <= ema_f[i] + 0.15 * a or l[i] <= ema_s[i] + 0.15 * a
        reclaim = c[i] > ema_f[i] and c[i] > o[i]
        near = min(dist_f, dist_s) <= config.PB_ATR_MAX
        if touched and reclaim and near and rvol >= config.PB_RVOL_MIN and body_ok:
            src = "PB"
            level = min(ema_f[i], ema_s[i])
    else:
        dist_f = abs(h[i] - ema_f[i]) / a
        dist_s = abs(h[i] - ema_s[i]) / a
        touched = h[i] >= ema_f[i] - 0.15 * a or h[i] >= ema_s[i] - 0.15 * a
        reclaim = c[i] < ema_f[i] and c[i] < o[i]
        near = min(dist_f, dist_s) <= config.PB_ATR_MAX
        if touched and reclaim and near and rvol >= config.PB_RVOL_MIN and body_ok:
            src = "PB"
            level = max(ema_f[i], ema_s[i])

    # --- Trigger B: squeeze release + pullback continuation ---
    if not src and config.USE_SQUEEZE and sqz and expanding:
        # prior bars compressed; this bar expands in trend direction
        if side == "LONG" and c[i] > o[i] and c[i] > ema_f[i] and rvol >= config.EXPAND_RVOL:
            # prefer not chasing: close not too far from mid of bar
            if (h[i] - c[i]) <= 0.55 * bar_range:
                src = "SQZ"
                level = ema_f[i]
        if side == "SHORT" and c[i] < o[i] and c[i] < ema_f[i] and rvol >= config.EXPAND_RVOL:
            if (c[i] - l[i]) <= 0.55 * bar_range:
                src = "SQZ"
                level = ema_f[i]

    if not src:
        return None

    entry = c[i]
    if side == "LONG":
        sl = min(l[i], level) - config.SL_ATR * a * 0.35
        # structural: below swing low of signal bar
        sl = min(sl, l[i] - 0.15 * a)
        risk = entry - sl
        if risk <= 0 or risk > 3.5 * a:
            return None
        tp1 = entry + config.TP1_R * risk
        tp2 = entry + config.TP2_R * risk
    else:
        sl = max(h[i], level) + config.SL_ATR * a * 0.35
        sl = max(sl, h[i] + 0.15 * a)
        risk = sl - entry
        if risk <= 0 or risk > 3.5 * a:
            return None
        tp1 = entry - config.TP1_R * risk
        tp2 = entry - config.TP2_R * risk

    score = 40
    # residual extremity
    if side == "LONG":
        score += int(min(25, (resid_rank_pct - 50) * 0.5))
    else:
        score += int(min(25, (50 - resid_rank_pct) * 0.5))
    if src == "PB":
        score += 12
    if src == "SQZ":
        score += 10
        if sqz_pct <= 25:
            score += 5
    if rvol >= 1.5:
        score += 8
    elif rvol >= 1.2:
        score += 4
    if trend_up or trend_dn:
        score += 8
    if config.USE_BTC_ALIGN and ((side == "LONG" and btc_bull) or (side == "SHORT" and not btc_bull)):
        score += 6
    score = int(max(0, min(100, score)))

    if score < config.MIN_SCORE:
        return None

    reason = (
        f"{src} · resid rank {resid_rank_pct:.0f} ({resid_ret:+.2%}) · "
        f"RVOL {rvol:.2f}x · sqz {sqz_pct:.0f} · EMA {config.EMA_FAST}/{config.EMA_SLOW}"
    )

    return Signal(
        symbol=symbol,
        side=side,
        src=src,
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        score=score,
        atr=a,
        rvol=rvol,
        resid_pct=resid_rank_pct,
        resid_ret=resid_ret,
        squeeze=sqz,
        reason=reason,
        level_px=float(level),
    )


def apply_residual_ranks(
    candidates: list[tuple[str, float, list[dict]]],
    btc_rows: list[dict],
) -> dict[str, tuple[float, float]]:
    """
    candidates: list of (symbol, dummy, rows)
    Returns symbol -> (resid_ret, rank_pct 0-100)
    """
    btc_c = [float(r["close"]) for r in btc_rows] if btc_rows else []
    raw: list[tuple[str, float]] = []
    for sym, _, rows in candidates:
        if not rows or not btc_c:
            continue
        c = [float(r["close"]) for r in rows]
        # align lengths
        n = min(len(c), len(btc_c))
        if n < 20:
            continue
        rr = residual_return(c[-n:], btc_c[-n:], rows[-n:], n - 2)
        if rr is None:
            continue
        raw.append((sym, rr))
    if not raw:
        return {}
    rets = [r for _, r in raw]
    out = {}
    for sym, rr in raw:
        # higher residual → higher rank pct
        pct = _percentile_rank(rr, rets)
        out[sym] = (rr, pct)
    return out


def btc_trend_up(btc_rows: list[dict]) -> bool:
    if not btc_rows or len(btc_rows) < config.BTC_EMA + 2:
        return True
    c = [float(r["close"]) for r in btc_rows]
    e = _ema(c, config.BTC_EMA)
    i = len(c) - 2
    return c[i] > e[i]
