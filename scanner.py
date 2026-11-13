"""Bucle principal: escanea todos los símbolos al cierre de cada vela LTF."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

from bingx import BingXClient
from config import Config
from messages import format_ema20_alerts, format_event, format_signal, format_top
from models import Candle, Ema20Alert, Signal, SymbolMeta
from strategy import (MIN_BARS, base_of, build_series, ema20_pullback, evaluate, market_bias,
                      select_top)
from telegram import Telegram
from tracker import Tracker
from universe import build_universe

log = logging.getLogger("scanner")


class Scanner:
    def __init__(self, cfg: Config, bingx: BingXClient, tg: Telegram, tracker: Tracker):
        self.cfg = cfg
        self.bingx = bingx
        self.tg = tg
        self.tracker = tracker
        self.universe: Dict[str, SymbolMeta] = {}
        self._universe_ts = 0.0
        self._has_volumes = False  # ¿hay ticker 24h? (si no, la liquidez puntúa neutra en el ranking)
        self.ltf_ms = cfg.ltf_sec * 1000
        # Velas necesarias: ~2.3 días (para PDH/PDL) y mínimo 500, máx. 1440 (límite BingX)
        self.limit = min(1440, max(500, int(2.3 * 86400 / cfg.ltf_sec) + 40))
        self._sem = asyncio.Semaphore(cfg.concurrency)

    # ---- universo ------------------------------------------------------------------------
    async def refresh_universe(self) -> None:
        contracts = await self.bingx.contracts()
        try:
            volumes = await self.bingx.tickers()
        except Exception as e:
            log.warning("No se pudo leer el ticker 24h (%s): sin filtro de volumen", e)
            volumes = {}  # sin datos de volumen: build_universe omite el filtro
        uni = build_universe(contracts, volumes, self.cfg)
        if not uni:
            raise RuntimeError("Universo vacío: revisa la conexión con BingX / filtros")
        self.universe = uni
        self._has_volumes = bool(volumes)
        self._universe_ts = time.time()
        n_c = sum(1 for m in uni.values() if m.kind == "crypto")
        oil = [m.symbol for m in uni.values() if m.kind == "oil"]
        log.info("Universo: %d criptos, petróleo=%s, otros=%d", n_c, oil or "NO ENCONTRADO",
                 len(uni) - n_c - len(oil))
        if self.cfg.include_oil and not oil:
            log.warning("No se encontró ningún contrato de petróleo. Define EXTRA_SYMBOLS con el símbolo exacto de BingX.")

    def universe_summary(self) -> Tuple[int, List[str], int]:
        n_c = sum(1 for m in self.universe.values() if m.kind == "crypto")
        oil = [m.symbol for m in self.universe.values() if m.kind == "oil"]
        return n_c, oil, len(self.universe) - n_c - len(oil)

    # ---- datos ---------------------------------------------------------------------------
    async def _load(self, sym: str) -> Optional[List[Candle]]:
        async with self._sem:
            try:
                return await self.bingx.klines(sym, self.cfg.ltf, self.limit)
            except Exception as e:
                log.debug("klines %s falló: %s", sym, e)
                return None

    def market_bias_for(self, candles: Optional[List[Candle]], now_ms: int) -> int:
        """+1 / -1 / 0 según CBC + nube EMA 9/20 del mercado de referencia (BTC) en su última vela cerrada."""
        if not candles:
            return 0
        closed = [c for c in candles if c.t + self.ltf_ms <= now_ms]
        if len(closed) < 60:
            return 0
        S = build_series(closed, self.cfg)
        return market_bias(S, len(closed) - 1)

    # ---- un ciclo de análisis --------------------------------------------------------------
    async def scan_once(self, once: bool = False) -> None:
        cfg = self.cfg
        t0 = time.time()
        if time.time() - self._universe_ts > cfg.universe_refresh_min * 60:
            try:
                await self.refresh_universe()
            except Exception as e:
                log.warning("No se pudo refrescar el universo: %s", e)

        # Símbolos con operación abierta que ya no están en el universo (bajó el volumen, etc.)
        # se siguen igualmente hasta cerrar la operación; solo NO generan señales nuevas.
        syms = list(self.universe) + [s for s in self.tracker.trades if s not in self.universe]
        results = await asyncio.gather(*(self._load(s) for s in syms))
        now_ms = int(time.time() * 1000)

        # Mercado de referencia (BTC): el ranking premia las señales alineadas con él
        bias = 0
        if cfg.market_bias:
            ref = cfg.market_symbol
            ref_candles = results[syms.index(ref)] if ref in syms else await self._load(ref)
            bias = self.market_bias_for(ref_candles, now_ms)

        failures = 0
        rejects: Counter = Counter()
        candidates: List[Signal] = []
        notifications: List[str] = []
        ema20_found: List[Ema20Alert] = []  # avisos "retroceso alcista a la EMA 20" de este cierre
        ema20_dups = 0

        for sym, candles in zip(syms, results):
            if not candles:
                failures += 1
                continue
            closed = [c for c in candles if c.t + self.ltf_ms <= now_ms]  # descarta la vela en formación
            if len(closed) < MIN_BARS:
                rejects["pocas_velas"] += 1
                continue
            last_close_ms = closed[-1].t + self.ltf_ms
            stale = (not once) and now_ms - last_close_ms > 2 * self.ltf_ms
            tracked = self.tracker.has_open(sym) is not None
            if stale and not tracked:
                rejects["sin_datos_recientes"] += 1  # mercado parado / contrato ilíquido
                continue

            S = build_series(closed, cfg)
            # 1) seguimiento de operaciones abiertas (TP / SL / CBC cambia de color / tiempo máx.)
            for tr, ev in self.tracker.update(sym, closed, S.cbc, now_ms):
                notifications.append(format_event(tr, ev, cfg, self.tracker.stats_line()))
            if sym not in self.universe:
                continue  # solo seguimiento: fuera del universo no se abren operaciones nuevas
            if stale:
                rejects["sin_datos_recientes"] += 1
                continue

            # 1b) aviso independiente: retroceso alcista a la EMA 20 (no depende del TOP-N ni del ranking)
            if cfg.ema20_alert:
                al, _ = ema20_pullback(S, len(closed) - 1, cfg, sym)
                if al is not None:
                    if once or not self.tracker.ema20_recent(sym, al.t):
                        ema20_found.append(al)
                    else:
                        ema20_dups += 1

            # 2) señal nueva en la última vela cerrada
            meta = self.universe[sym]
            liq = meta.quote_volume if (self._has_volumes or meta.quote_volume > 0) else None
            sig, why = evaluate(S, len(closed) - 1, cfg, sym, liq=liq,
                                bias=bias if meta.kind == "crypto" else 0)  # BTC no dice nada de oro/forex/acciones
            if sig is None:
                rejects[why] += 1
                continue
            if self.tracker.already_alerted(sym, sig.side, sig.t) and not once:
                rejects["duplicada"] += 1
                continue
            if self.tracker.in_cooldown(sym, sig.t):
                rejects["cooldown"] += 1  # el par acaba de cerrar una operación no ganadora
                continue
            open_tr = self.tracker.has_open(sym)
            if open_tr is not None:
                if open_tr.side != sig.side and cfg.exit_on_opposite:
                    res = self.tracker.close_opposite(sym, sig.entry, end_ms=sig.t + self.ltf_ms)
                    if res:
                        notifications.append(format_event(res[0], res[1], cfg, self.tracker.stats_line()))
                else:
                    rejects["ya_en_operacion"] += 1
                    continue
            candidates.append(sig)

        # TOP-N: de todas las señales válidas de este cierre solo se envían las N de mayor ranking
        busy = {base_of(s) for s in self.tracker.trades}  # subyacentes con operación abierta
        chosen, low_rank, cut = select_top(candidates, cfg, busy_bases=busy)
        if low_rank:
            rejects["rank_bajo"] += low_rank
        if cut:
            rejects["fuera_del_top"] += cut

        for text in notifications:
            await self.tg.send(text)
        sent = 0
        if len(chosen) >= 2:
            await self.tg.send(format_top(chosen, cfg, n_valid=len(candidates), n_eval=len(syms)))
        for pos, sig in enumerate(chosen, 1):
            meta = self.universe[sig.symbol]
            if await self.tg.send(format_signal(sig, meta, cfg, pos=pos, total=len(chosen))):
                self.tracker.register(sig, meta.price_prec)
                sent += 1
                log.info("TOP %d/%d %s %s rank=%.1f entrada=%s sl=%s score=%d/%d", pos, len(chosen),
                         sig.side, sig.symbol, sig.rank, sig.entry, sig.sl, sig.score, sig.max_score)

        # Avisos EMA 20: uno por subyacente (el más líquido), agrupados en un resumen por cierre de vela
        ema20_sent = 0
        if ema20_found:
            ema20_found.sort(key=lambda a: self.universe[a.symbol].quote_volume, reverse=True)
            uniq: List[Ema20Alert] = []
            seen = set()
            for a in ema20_found:
                if base_of(a.symbol) not in seen:
                    seen.add(base_of(a.symbol))
                    uniq.append(a)
            if cfg.ema20_max_alerts > 0:
                uniq = uniq[:cfg.ema20_max_alerts]
            for text, group in format_ema20_alerts(uniq, cfg, self.universe):
                if await self.tg.send(text):
                    for a in group:
                        self.tracker.register_ema20(a.symbol, a.t)  # solo se da por avisado lo ENVIADO
                    ema20_sent += len(group)
                    log.info("EMA20 retroceso: %s", ", ".join(a.symbol for a in group))
        self.tracker.save()

        log.info("Scan: %d símbolos en %.1fs · candidatas=%d · enviadas=%d · ema20=%d (repetidas=%d) · "
                 "avisos=%d · fallos=%d · mercado=%+d · descartes=%s", len(syms), time.time() - t0,
                 len(candidates), sent, ema20_sent, ema20_dups, len(notifications), failures, bias,
                 dict(rejects))
        if syms and failures > len(syms) * 0.5:
            log.warning("Más del 50%% de peticiones fallaron: ¿límite de BingX o red? Baja REQUESTS_PER_SECOND.")

    # ---- bucle -------------------------------------------------------------------------------
    async def run(self) -> None:
        cfg = self.cfg
        while True:
            now = time.time()
            nxt = (int(now // cfg.ltf_sec) + 1) * cfg.ltf_sec + cfg.scan_delay_sec
            await asyncio.sleep(max(0.0, nxt - now))
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error en el ciclo de análisis (se reintenta en la próxima vela)")
