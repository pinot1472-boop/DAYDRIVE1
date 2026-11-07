"""DAYDRIVE v1 — residual session drive + EMA pullback / squeeze."""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

import config
from bingx import BingX, BingXError
from confirm_5m import confirm_pullback
from notify import State, Telegram
from strategy import Signal, analyze, apply_residual_ranks, btc_trend_up, session_active, utc_hour

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daydrive")
STOP = asyncio.Event()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _handle_sig(*_):
    log.info("Parada")
    STOP.set()


def fmt_sig(s: Signal, mode: str) -> str:
    arrow = "🟢 LONG" if s.side == "LONG" else "🔴 SHORT"
    live = "LIVE" if mode == "LIVE" else "SOLO SEÑAL"
    return (
        f"<b>DAYDRIVE · {arrow}</b> · {live}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Par: <b>{s.symbol}</b>\n"
        f"Origen: <b>{s.src}</b> · Score <b>{s.score}/100</b>\n"
        f"Resid rank <b>{s.resid_pct:.0f}</b> · ret {s.resid_ret:+.2%}\n"
        f"RVOL {s.rvol:.2f}x · squeeze {'yes' if s.squeeze else 'no'}\n"
        f"\n"
        f"📍 Entrada: <code>{s.entry}</code>\n"
        f"🛑 SL: <code>{s.sl}</code>\n"
        f"🎯 TP1: <code>{s.tp1}</code> · TP2: <code>{s.tp2}</code>\n"
        f"\n"
        f"{s.reason}\n"
        f"{('5m: ' + s.confirm_tag) if s.confirm_tag else ''}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"TF {config.TIMEFRAME} · {config.VERSION} · MODE={mode}"
    )


async def pick_symbols(bx: BingX) -> list[str]:
    if config.SYMBOLS:
        return [s.strip().upper() for s in config.SYMBOLS.split(",") if s.strip()]
    try:
        all_syms = await bx.symbols()
        vols = await bx.tickers_24h()
    except Exception as exc:  # noqa: BLE001
        log.error("universe: %s", exc)
        return []
    scored = []
    for s in all_syms:
        base = s.split("-")[0].upper() if "-" in s else s.upper()
        if any(base.startswith(p) for p in config.EXCLUDE_PREFIXES):
            continue
        qv = float(vols.get(s) or 0)
        if qv < config.MIN_QUOTE_VOL_24H:
            continue
        scored.append((qv, s))
    scored.sort(reverse=True)
    return [s for _, s in scored[: config.MAX_SYMBOLS]]


async def fetch_klines_batch(bx: BingX, symbols: list[str], interval: str, limit: int = 120) -> dict[str, list]:
    sem = asyncio.Semaphore(config.KLINE_CONCURRENCY)
    out: dict[str, list] = {}

    async def one(sym: str):
        async with sem:
            try:
                rows = await bx.klines(sym, interval, limit)
                if rows and len(rows) >= 50:
                    out[sym] = rows
            except Exception as exc:  # noqa: BLE001
                log.debug("klines %s: %s", sym, exc)

    await asyncio.gather(*[one(s) for s in symbols])
    return out


async def try_live(bx: BingX, sig: Signal, equity: float) -> bool:
    if equity <= 0:
        return False
    risk_usd = equity * (config.RISK_PCT / 100.0)
    dist = abs(sig.entry - sig.sl)
    if dist <= 0:
        return False
    qty = risk_usd / dist
    notional = qty * sig.entry
    max_n = equity * config.MAX_LEVERAGE
    if notional > max_n:
        qty = max_n / sig.entry
    qty = bx.round_qty(sig.symbol, qty)
    if qty <= 0:
        return False
    side = "BUY" if sig.side == "LONG" else "SELL"
    lev_side = "LONG" if sig.side == "LONG" else "SHORT"
    try:
        await bx.set_margin_mode(sig.symbol, "ISOLATED")
        await bx.set_leverage(sig.symbol, lev_side, int(config.MAX_LEVERAGE))
        await bx.market_order(sig.symbol, side, qty, sig.sl, sig.tp1)
        return True
    except BingXError as exc:
        log.error("LIVE %s: %s", sig.symbol, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        log.error("LIVE %s: %s", sig.symbol, exc)
        return False


async def scan_once(
    bx: BingX,
    tg: Telegram,
    state: State,
    symbols: list[str],
    mode: str,
) -> int:
    if not session_active():
        log.info("fuera de sesión h=%s", utc_hour())
        return 0

    state.roll_day()
    if state.data["day_trades"] >= config.MAX_TRADES_DAY:
        return 0
    if state.data["day_losses"] >= config.MAX_LOSSES_DAY:
        return 0

    data = await fetch_klines_batch(bx, symbols + [config.BTC_SYMBOL], config.TIMEFRAME, 120)
    btc_rows = data.get(config.BTC_SYMBOL) or []
    btc_bull = btc_trend_up(btc_rows)

    cand = [(s, 0.0, rows) for s, rows in data.items() if s != config.BTC_SYMBOL]
    ranks = apply_residual_ranks(cand, btc_rows)

    equity = 0.0
    if mode == "LIVE" and config.LIVE_CONFIRMED:
        try:
            equity = await bx.balance_usdt()
        except Exception:
            equity = 0.0

    signals: list[Signal] = []
    for sym, rows in data.items():
        if sym == config.BTC_SYMBOL:
            continue
        if not state.can_trade(sym):
            continue
        rr, pct = ranks.get(sym, (0.0, 50.0))
        sig = analyze(
            sym,
            rows,
            btc_rows=btc_rows,
            resid_rank_pct=pct,
            resid_ret=rr,
            btc_bull=btc_bull,
        )
        if sig:
            signals.append(sig)

    signals.sort(key=lambda s: (s.score, abs(s.resid_pct - 50)), reverse=True)
    signals = signals[: config.MAX_SIGNALS_CYCLE]

    n_sent = 0
    for sig in signals:
        if config.USE_CONFIRM_5M:
            try:
                rows5 = await bx.klines(sig.symbol, config.CONFIRM_INTERVAL, 80)
            except Exception:
                rows5 = []
            ok, tag = confirm_pullback(sig.side, rows5, sig.level_px)
            sig.confirm_tag = tag
            if not ok:
                log.info("5m BLOCK %s %s: %s", sig.symbol, sig.side, tag)
                if config.CONFIRM_REQUIRED:
                    continue
                sig.score = max(0, sig.score - 15)
                if sig.score < config.MIN_SCORE:
                    continue

        await tg.send(fmt_sig(sig, mode))
        state.mark_signal(sig.symbol)
        st_log = state.data.setdefault("signals_log", [])
        st_log.append(
            {
                "ts": time.time(),
                "symbol": sig.symbol,
                "side": sig.side,
                "src": sig.src,
                "score": sig.score,
                "resid": sig.resid_pct,
            }
        )
        state.data["signals_log"] = st_log[-200:]
        state.save()
        n_sent += 1

        if mode == "LIVE" and config.LIVE_CONFIRMED:
            ok_o = await try_live(bx, sig, equity)
            await tg.send(
                f"{'✅' if ok_o else '⚠️'} LIVE {sig.symbol} {sig.side} "
                f"{'ok' if ok_o else 'rechazada'}"
            )
    return n_sent


async def main() -> None:
    mode = config.MODE if config.MODE in ("SIGNAL", "LIVE") else "SIGNAL"
    log.info(
        "%s · MODE=%s · sesión %s-%s UTC · Resid≥%s · score≥%s",
        config.VERSION,
        mode,
        config.SESSION_START,
        config.SESSION_END,
        config.MIN_RESID_RANK_PCT,
        config.MIN_SCORE,
    )

    async with httpx.AsyncClient() as client:
        bx = BingX(client)
        tg = Telegram(client)
        state = State(config.STATE_PATH)
        symbols = await pick_symbols(bx)
        await tg.send(
            f"🤖 <b>{config.VERSION}</b> arrancado\n"
            f"MODE=<b>{mode}</b> · TF {config.TIMEFRAME} · {len(symbols)} sym\n"
            f"Sesión {config.SESSION_START}:00–{config.SESSION_END}:00 UTC\n"
            f"Resid rank ≥{config.MIN_RESID_RANK_PCT} · MIN_SCORE={config.MIN_SCORE}\n"
            f"Confirm 5m={'ON' if config.USE_CONFIRM_5M else 'off'} · "
            f"SKIP Asia GRAB={'ON' if config.SKIP_ASIA_GRAB else 'off'}\n"
            f"Edges: ResidRank · EMA-Pullback · Squeeze · BTC"
        )

        last_hb = 0.0
        while not STOP.is_set():
            t0 = time.time()
            try:
                if int(time.time()) % 1800 < config.SCAN_SEC:
                    symbols = await pick_symbols(bx)
                n = await scan_once(bx, tg, state, symbols, mode)
                if time.time() - last_hb > 3600:
                    await tg.send(
                        f"💓 DAYDRIVE heartbeat\n"
                        f"{len(symbols)} sym · señales ciclo {n}\n"
                        f"Trades día {state.data.get('day_trades', 0)} · "
                        f"sess {config.SESSION_START}-{config.SESSION_END} h={utc_hour()}\n"
                        f"{config.VERSION}"
                    )
                    last_hb = time.time()
            except Exception as exc:  # noqa: BLE001
                log.exception("cycle: %s", exc)
                await tg.send(f"⚠️ DAYDRIVE error: {exc}")

            wait = max(5.0, config.SCAN_SEC - (time.time() - t0))
            try:
                await asyncio.wait_for(STOP.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

        await tg.send(f"🛑 {config.VERSION} detenido")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)
    asyncio.run(main())
