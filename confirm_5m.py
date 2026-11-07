"""5m confirmation for DAYDRIVE pullback / squeeze (not IPDA sweep)."""
from __future__ import annotations

from typing import Optional

import config


def _atr(h, l, c, length: int):
    trs = []
    for i in range(len(c)):
        if i == 0:
            trs.append(h[i] - l[i])
        else:
            trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    out = [trs[0]]
    alpha = 1.0 / max(length, 1)
    for i in range(1, len(trs)):
        out.append(out[-1] * (1 - alpha) + trs[i] * alpha)
    return out


def _sma(vals, length: int):
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= length:
            s -= vals[i - length]
        out.append(s / min(i + 1, length))
    return out


def confirm_pullback(
    side: str,
    rows_5m: list[dict],
    level_px: float,
) -> tuple[bool, str]:
    """
    Confirm on last closed 5m bar:
    LONG: wick below/near level or EMA zone, close back up, RVOL ok
    SHORT: mirror
    """
    if not rows_5m or len(rows_5m) < 25:
        return False, "pocas velas 5m"

    o = [float(r["open"]) for r in rows_5m]
    h = [float(r["high"]) for r in rows_5m]
    l = [float(r["low"]) for r in rows_5m]
    c = [float(r["close"]) for r in rows_5m]
    v = [float(r.get("volume") or 0) for r in rows_5m]
    i = len(c) - 2
    atr = _atr(h, l, c, 14)
    a = atr[i]
    if a <= 0:
        return False, "atr0"

    vol_ma = _sma(v, 20)
    rvol = v[i] / vol_ma[i] if vol_ma[i] > 0 else 0.0
    if rvol < config.CONFIRM_RVOL_MIN:
        return False, f"rvol {rvol:.2f}"

    body = abs(c[i] - o[i])
    upper = h[i] - max(c[i], o[i])
    lower = min(c[i], o[i]) - l[i]

    if side == "LONG":
        wick_ok = lower >= config.CONFIRM_MIN_WICK_ATR * a
        close_bull = c[i] > o[i]
        near = True
        if level_px > 0:
            near = l[i] <= level_px + 0.5 * a
        if wick_ok and close_bull and near:
            return True, f"pb5m rvol={rvol:.2f}"
        return False, f"no rechazo long (w={lower/a:.2f})"
    else:
        wick_ok = upper >= config.CONFIRM_MIN_WICK_ATR * a
        close_bear = c[i] < o[i]
        near = True
        if level_px > 0:
            near = h[i] >= level_px - 0.5 * a
        if wick_ok and close_bear and near:
            return True, f"pb5m rvol={rvol:.2f}"
        return False, f"no rechazo short (w={upper/a:.2f})"
