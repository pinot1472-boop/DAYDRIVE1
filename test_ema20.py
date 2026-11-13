"""Pruebas offline (sin red) de la alerta "retroceso alcista a la EMA 20".

  Tendencia alcista (EMA 9 >= EMA 20 y EMA 20 subiendo, cierre > EMA 200) + una vela cuyo rango
  toca la EMA 20 y que cierra POR ENCIMA de ella, tras haberse alejado >= 1 ATR de la media.

Ejecutar:  python -m unittest discover -p "test_*.py"
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from config import load_config
from messages import format_ema20_alerts, format_startup
from models import Candle, Ema20Alert, SymbolMeta
from scanner import Scanner
from strategy import build_series, ema20_pullback
from test_pullback import LTF, T0, candle, downtrend, mk_cfg, uptrend
from test_scanner import NOW0, FakeBingX, FakeTG
from tracker import Tracker

TOUCH_TXT = "Retroceso alcista a la EMA 20"


def pullback_rows(n_up: int = 290, leg: int = 5, step: float = 0.9, close_above: bool = True):
    """Tendencia alcista (+0.5/vela), `leg` velas rojas de retroceso y una última vela que toca la
    EMA 20 (low <= EMA20) y cierra por encima de ella (o por debajo si close_above=False)."""
    rows = []
    for k in range(n_up):
        o = 100 + 0.5 * k
        rows.append(candle(k, o, o + 0.8))
    for _ in range(leg):
        rows.append(candle(len(rows), rows[-1].c, rows[-1].c - step))
    e_prev = build_series(rows, mk_cfg()).ema20[-1]
    c = e_prev + 1.0 if close_above else e_prev - 1.0
    alpha = 2 / 21
    e_new = alpha * c + (1 - alpha) * e_prev
    o = rows[-1].c
    rows.append(Candle(T0 + len(rows) * LTF, o, max(o, c) + 0.1, min(e_new - 0.2, c - 0.1), c, 1500.0))
    return rows


def ending_at(rows, boundary_ms):
    """Re-sitúa las velas en el tiempo para que la última cierre exactamente en `boundary_ms`."""
    n = len(rows)
    return [Candle(boundary_ms - (n - k) * LTF, r.o, r.h, r.l, r.c, r.v) for k, r in enumerate(rows)]


def alert_for(rows, cfg, S_patch=None):
    S = build_series(rows, cfg)
    if S_patch:
        S_patch(S)
    return ema20_pullback(S, len(rows) - 1, cfg, "TEST-USDT")


class TestEma20Detector(unittest.TestCase):
    def test_fires_on_bullish_pullback_to_ema20(self):
        cfg = mk_cfg()
        al, why = alert_for(pullback_rows(), cfg)
        self.assertEqual(why, "ok")
        self.assertIsInstance(al, Ema20Alert)
        self.assertEqual(al.symbol, "TEST-USDT")
        self.assertLessEqual(al.low, al.ema20)        # el rango toca la EMA 20...
        self.assertGreater(al.close, al.ema20)        # ...y cierra por encima
        self.assertGreater(al.dist_pct, 0)
        self.assertGreaterEqual(al.ext_atr, cfg.ema20_min_ext_atr)

    def test_no_alert_in_plain_uptrend_without_touch(self):
        cfg = mk_cfg()
        al, why = alert_for(uptrend(), cfg)  # el precio va muy por encima de la EMA 20
        self.assertIsNone(al)
        self.assertEqual(why, "sin_toque")

    def test_close_below_ema20_is_a_break_not_a_bounce(self):
        cfg = mk_cfg()
        al, why = alert_for(pullback_rows(close_above=False), cfg)
        self.assertIsNone(al)
        self.assertIn(why, ("ema20_rota", "no_tendencia"))  # nunca un aviso alcista

    def test_no_alert_in_downtrend(self):
        al, why = alert_for(downtrend(), mk_cfg())
        self.assertIsNone(al)
        self.assertEqual(why, "no_tendencia")

    def test_requires_close_above_ema200_when_enabled(self):
        rows = pullback_rows()
        patch = lambda S: S.ema200.__setitem__(-1, S.c[-1] + 10.0)  # noqa: E731  EMA200 por encima del precio
        al, why = alert_for(rows, mk_cfg(ema20_require_ema200=True), patch)
        self.assertIsNone(al)
        self.assertEqual(why, "bajo_ema200")
        al, why = alert_for(rows, mk_cfg(ema20_require_ema200=False), patch)
        self.assertIsNotNone(al)

    def test_needs_a_real_pullback_min_extension(self):
        rows = pullback_rows()
        al, why = alert_for(rows, mk_cfg(ema20_min_ext_atr=50.0))
        self.assertIsNone(al)
        self.assertEqual(why, "sin_retroceso")
        self.assertIsNotNone(alert_for(rows, mk_cfg(ema20_min_ext_atr=0.0))[0])

    def test_too_few_bars(self):
        cfg = mk_cfg()
        S = build_series(pullback_rows(), cfg)
        self.assertEqual(ema20_pullback(S, 3, cfg, "X-USDT"), (None, "nodata"))

    def test_no_lookahead(self):
        """El resultado en la vela k es idéntico con la serie completa o cortada en k."""
        cfg = mk_cfg()
        rows = pullback_rows()
        full = build_series(rows, cfg)
        fired = 0
        for k in range(len(rows) - 40, len(rows)):
            part = build_series(rows[:k + 1], cfg)
            a = ema20_pullback(part, k, cfg, "T-USDT")
            b = ema20_pullback(full, k, cfg, "T-USDT")
            self.assertEqual(a, b, f"difiere en la vela {k}")
            fired += a[0] is not None
        self.assertGreaterEqual(fired, 1)


class TestEma20Config(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_config()
        self.assertTrue(cfg.ema20_alert)
        self.assertEqual(cfg.ema20_min_ext_atr, 1.0)
        self.assertEqual(cfg.ema20_cooldown_bars, 8)
        self.assertTrue(cfg.ema20_require_ema200)
        self.assertEqual(cfg.ema20_max_alerts, 0)

    def test_env_overrides(self):
        env = {"EMA20_ALERT": "false", "EMA20_COOLDOWN_BARS": "3", "EMA20_MAX_ALERTS": "5",
               "EMA20_MIN_EXT_ATR": "2.5", "EMA20_REQUIRE_EMA200": "no"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertFalse(cfg.ema20_alert)
        self.assertEqual(cfg.ema20_cooldown_bars, 3)
        self.assertEqual(cfg.ema20_max_alerts, 5)
        self.assertEqual(cfg.ema20_min_ext_atr, 2.5)
        self.assertFalse(cfg.ema20_require_ema200)

    def test_startup_mentions_alert_only_when_enabled(self):
        self.assertIn("retroceso alcista a la EMA 20", format_startup(mk_cfg(ema20_alert=True), 10, [], 0))
        self.assertNotIn("EMA 20", format_startup(mk_cfg(ema20_alert=False), 10, [], 0))


class TestTrackerEma20(unittest.TestCase):
    def test_cooldown_window(self):
        tr = Tracker(mk_cfg(ema20_cooldown_bars=8))
        t0 = T0 + 100 * LTF
        self.assertFalse(tr.ema20_recent("SOL-USDT", t0))
        tr.register_ema20("SOL-USDT", t0)
        self.assertTrue(tr.ema20_recent("SOL-USDT", t0))                 # misma vela (p.ej. reinicio)
        self.assertTrue(tr.ema20_recent("SOL-USDT", t0 + 7 * LTF))       # dentro de las 8 velas
        self.assertFalse(tr.ema20_recent("SOL-USDT", t0 + 8 * LTF))      # ya puede avisar otra vez

    def test_same_candle_never_repeats_even_without_cooldown(self):
        tr = Tracker(mk_cfg(ema20_cooldown_bars=0))
        t0 = T0 + 100 * LTF
        tr.register_ema20("SOL-USDT", t0)
        self.assertTrue(tr.ema20_recent("SOL-USDT", t0))
        self.assertFalse(tr.ema20_recent("SOL-USDT", t0 + LTF))

    def test_keyed_by_underlying(self):
        tr = Tracker(mk_cfg())
        tr.register_ema20("BTC-USDT", T0)
        self.assertTrue(tr.ema20_recent("BTC-USDC", T0 + LTF))
        self.assertFalse(tr.ema20_recent("ETH-USDT", T0 + LTF))

    def test_persists_across_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = mk_cfg(state_file=os.path.join(d, "state.json"), dry_run=False)
            tr = Tracker(cfg)
            tr.register_ema20("SOL-USDT", T0)
            tr.save()
            tr2 = Tracker(cfg)
            self.assertTrue(tr2.ema20_recent("SOL-USDT", T0 + LTF))


def mk_alert(sym, t=T0, price=100.0):
    return Ema20Alert(symbol=sym, t=t, close=price, low=price - 1, ema20=price - 0.5, atr=1.0,
                      dist_pct=0.5, ext_atr=2.0, rvol=1.3, cbc_green=True, htf_bull=True, above_vwap=True)


class TestFormatEma20(unittest.TestCase):
    def test_text(self):
        cfg = mk_cfg()
        uni = {"SOL-USDT": SymbolMeta("SOL-USDT", price_prec=2)}
        out = format_ema20_alerts([mk_alert("SOL-USDT", price=182.45)], cfg, uni)
        self.assertEqual(len(out), 1)
        txt, group = out[0]
        self.assertIn(TOUCH_TXT, txt)
        self.assertIn("15m", txt)
        self.assertIn("SOL-USDT", txt)
        self.assertIn("<code>182.45</code>", txt)
        self.assertIn("1 moneda ", txt)
        self.assertEqual([a.symbol for a in group], ["SOL-USDT"])

    def test_splits_in_several_messages_under_telegram_limit(self):
        cfg = mk_cfg()
        syms = [f"COIN{k}-USDT" for k in range(90)]
        uni = {s: SymbolMeta(s, price_prec=4) for s in syms}
        out = format_ema20_alerts([mk_alert(s) for s in syms], cfg, uni)
        self.assertGreater(len(out), 1)
        for txt, _ in out:
            self.assertLess(len(txt), 4096)
        covered = [a.symbol for _, g in out for a in g]
        self.assertEqual(covered, syms)  # cada moneda en un único mensaje y en orden
        self.assertIn("continuación", out[1][0])
        self.assertIn("90 monedas", out[0][0])


def scanner_with(cfg, symbols, boundary, volumes=None, tg=None):
    bingx = FakeBingX()
    tg = tg or FakeTG()
    sc = Scanner(cfg, bingx, tg, Tracker(cfg))
    sc.universe = {}
    for k, sym in enumerate(symbols):
        bingx.candles[sym] = ending_at(pullback_rows(), boundary)
        sc.universe[sym] = SymbolMeta(sym, price_prec=2, quote_volume=(volumes or {}).get(sym, 5e7))
    sc._universe_ts = 1e18
    return sc, tg


class TestScannerEma20(unittest.TestCase):
    boundary = (NOW0 * 1000 // LTF) * LTF

    def scan(self, sc, once=False, dt=5):
        with mock.patch("time.time", return_value=self.boundary / 1000 + dt):
            asyncio.run(sc.scan_once(once=once))

    def ema20_msgs(self, tg):
        return [m for m in tg.sent if TOUCH_TXT in m]

    def test_one_grouped_message_and_not_repeated(self):
        cfg = mk_cfg()
        sc, tg = scanner_with(cfg, ["AAA-USDT", "BBB-USDT", "CCC-USDT"], self.boundary)
        self.scan(sc)
        msgs = self.ema20_msgs(tg)
        self.assertEqual(len(msgs), 1, tg.sent)
        for s in ("AAA-USDT", "BBB-USDT", "CCC-USDT"):
            self.assertIn(s, msgs[0])
        # misma vela otra vez (p.ej. reinicio del bot): no se vuelve a avisar
        n = len(tg.sent)
        self.scan(sc, dt=30)
        self.assertEqual(len(self.ema20_msgs(tg)), 1)
        self.assertEqual(len(tg.sent), n)

    def test_once_mode_ignores_dedupe(self):
        cfg = mk_cfg()
        sc, tg = scanner_with(cfg, ["AAA-USDT"], self.boundary)
        self.scan(sc)
        self.scan(sc, once=True)
        self.assertEqual(len(self.ema20_msgs(tg)), 2)

    def test_disabled_sends_nothing(self):
        sc, tg = scanner_with(mk_cfg(ema20_alert=False), ["AAA-USDT"], self.boundary)
        self.scan(sc)
        self.assertEqual(self.ema20_msgs(tg), [])
        self.assertEqual(sc.tracker.last_ema20, {})

    def test_failed_send_is_not_registered(self):
        class FailTG(FakeTG):
            async def send(self, text):
                self.sent.append(text)
                return False

        cfg = mk_cfg()
        sc, tg = scanner_with(cfg, ["AAA-USDT"], self.boundary, tg=FailTG())
        self.scan(sc)
        self.assertEqual(sc.tracker.last_ema20, {})  # sin registrar -> se reintentará

    def test_one_alert_per_underlying_most_liquid_wins(self):
        cfg = mk_cfg()
        sc, tg = scanner_with(cfg, ["ABC-USDC", "ABC-USDT"], self.boundary,
                              volumes={"ABC-USDC": 1e6, "ABC-USDT": 9e8})
        self.scan(sc)
        msgs = self.ema20_msgs(tg)
        self.assertEqual(len(msgs), 1)
        self.assertIn("ABC-USDT", msgs[0])
        self.assertNotIn("ABC-USDC", msgs[0])
        self.assertIn("ABC", sc.tracker.last_ema20)

    def test_max_alerts_keeps_the_most_liquid(self):
        cfg = mk_cfg(ema20_max_alerts=2)
        sc, tg = scanner_with(cfg, ["A-USDT", "B-USDT", "C-USDT"], self.boundary,
                              volumes={"A-USDT": 1e6, "B-USDT": 9e8, "C-USDT": 5e8})
        self.scan(sc)
        txt = self.ema20_msgs(tg)[0]
        self.assertIn("B-USDT", txt)
        self.assertIn("C-USDT", txt)
        self.assertNotIn("A-USDT", txt)
        self.assertLess(txt.index("B-USDT"), txt.index("C-USDT"))  # orden por liquidez

    def test_no_message_when_nothing_pulls_back(self):
        cfg = mk_cfg()
        bingx, tg = FakeBingX(), FakeTG()
        sc = Scanner(cfg, bingx, tg, Tracker(cfg))
        rows = ending_at(uptrend(), self.boundary)  # solo sube: nunca toca la EMA 20
        bingx.candles["UP-USDT"] = rows
        sc.universe = {"UP-USDT": SymbolMeta("UP-USDT", price_prec=2)}
        sc._universe_ts = 1e18
        self.scan(sc)
        self.assertEqual(self.ema20_msgs(tg), [])


if __name__ == "__main__":
    unittest.main()
