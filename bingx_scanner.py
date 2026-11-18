#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BINGX SCANNER - ORDERFLOW TRAP PRO
==================================
Escanea los perpetuos USDT de BingX (API pública, sin API key) y te dice qué
monedas tienen señal LONG / SHORT con la MISMA lógica del indicador de Frost
Script "orderflow_trap_pro.txt": barrida de liquidez + absorción + delta
estimado + VWAP + tendencia, con puntuación de 0 a 5.

Solo necesita Python 3.8 o superior (no hay que instalar nada):

    python bingx_scanner.py                        # 15m, última vela cerrada
    python bingx_scanner.py --tf 5m --lookback 3   # 5m, últimas 3 velas
    python bingx_scanner.py --loop --beep          # repite al cierre de cada vela
    python bingx_scanner.py --symbols LONGXIA,BTC  # solo esas monedas
    python bingx_scanner.py --source bybit --loop  # MODO RAPIDO (ver abajo)
    python bingx_scanner.py --help                 # todas las opciones

FRACTALES DE 30m (--frac bonus | require):
  Un fractal de Williams es una vela cuyo maximo (o minimo) supera al de las 2 velas de cada
  lado. Se confirma 2 velas despues (no repinta, pero llega con retraso). Los fractales aun
  SIN TOCAR son liquidez: ahi se acumulan stops. Si una vela perfora un fractal bajo de 30m y
  cierra de vuelta por encima (o al reves con uno alto), es una trampa mas clara que una
  barrida del minimo "cualquiera" de 20 velas.
    --frac bonus    suma +1 punto (la puntuacion pasa a 0-6)
    --frac require  solo avisa si ademas hay barrida de fractal 30m
  Las velas de 30m se construyen agrupando las del grafico (15m = 2 velas): sin peticiones extra.

MODO RAPIDO (--source bybit):
  BingX limita a ~10 peticiones/s, asi que escanear 400+ monedas tarda ~1 min.
  Bybit permite 600 peticiones cada 5 s por IP, asi que el mismo escaneo tarda
  unos segundos. Por defecto solo se escanean las monedas que TAMBIEN estan en
  BingX (--all-bybit para quitar ese filtro). Ventaja extra: tradoor muestra
  Bybit Perp, asi que las velas del escaner son las mismas que ves en el grafico
  y todas las senales se pueden abrir en tradoor con footprint.
  (BingX no esta entre los 14 exchanges de tradoor.)

OJO:
  * El delta es una ESTIMACIÓN con velas (no el delta real del footprint).
  * Los datos son los de BingX; el gráfico de tradoor puede usar otro exchange,
    así que las señales pueden diferir un poco.
  * Úsalo para filtrar candidatas y confirma en el footprint/DOM antes de entrar.
  * Ningún escáner garantiza resultados. No es asesoramiento financiero.
"""

import argparse
import bisect
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

BASE_URL = "https://open-api.bingx.com"
BYBIT_URL = "https://api.bybit.com"

TF_MS = {
    "1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000, "30m": 1800000,
    "1h": 3600000, "2h": 7200000, "4h": 14400000, "6h": 21600000,
    "8h": 28800000, "12h": 43200000, "1d": 86400000,
}

# Bybit no tiene 8h; el resto coincide con las temporalidades de BingX.
BYBIT_INTERVAL = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720", "1d": "D",
}

ATR_LEN = 14      # igual que en el indicador
NEAR_ATR = 0.25   # "cerca del extremo" para la absorción (x ATR)
STOP_ATR = 0.25   # separación del stop orientativo respecto a la mecha (x ATR)

COLORS = {"g": "\033[92m", "r": "\033[91m", "y": "\033[93m", "d": "\033[2m", "0": "\033[0m"}


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def normalize(sym):
    """LONGXIA, LONGXIAUSDT o LONGXIA-USDT -> LONGXIA-USDT"""
    s = sym.strip().upper().replace("/", "-").replace("_", "-")
    if not s:
        return ""
    if s.endswith("-USDT"):
        return s
    if s.endswith("USDT"):
        return s[:-4] + "-USDT"
    return s + "-USDT"


def is_tradfi(sym):
    """Contratos de acciones / materias primas / divisas / índices de BingX (NCSK..., NCCO...)."""
    base = sym.split("-")[0]
    return re.match(r"^NC(SK|CO|FX|SI)", base) is not None


def rolling_sum(values, n):
    out = [None] * len(values)
    acc = 0.0
    for i, x in enumerate(values):
        acc += x
        if i >= n:
            acc -= values[i - n]
        if i >= n - 1:
            out[i] = acc
    return out


def rolling_min(values, n):
    return [min(values[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(values))]


def rolling_max(values, n):
    return [max(values[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(values))]


def ema(values, length):
    alpha = 2.0 / (length + 1)
    out = []
    prev = None
    for x in values:
        prev = x if prev is None else prev + alpha * (x - prev)
        out.append(prev)
    return out


# ----------------------------------------------------------------------------
# API de BingX (pública)
# ----------------------------------------------------------------------------
class ApiError(Exception):
    pass


class RateLimiter:
    """Reparte las peticiones en el tiempo para no pasar del límite de BingX."""

    def __init__(self, rps):
        self.interval = 1.0 / max(rps, 0.1)
        self.lock = threading.Lock()
        self.next_free = time.monotonic()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_free)
            self.next_free = start + self.interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


class Api:
    """Cliente HTTP para BingX. BybitApi (abajo) cambia solo la URL y el formato de la respuesta."""
    base_url = BASE_URL
    name = "BingX"

    def __init__(self, rps):
        self.limiter = RateLimiter(rps)
        self.blocked = None  # texto del error si el servidor nos ha bloqueado la IP

    def unwrap(self, body):
        """-> (datos, error, reintentar) segun el formato de respuesta de BingX."""
        code = body.get("code", 0)
        if str(code) in ("0", "None", ""):
            return body.get("data"), None, False
        return None, "código %s: %s" % (code, body.get("msg", "")), str(code) == "100410"

    def get(self, path, params=None, retries=4):
        if self.blocked:
            raise ApiError(self.blocked)
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last = "sin respuesta"
        for attempt in range(retries):
            self.limiter.wait()
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 (bingx-scanner)", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last = "HTTP %s" % e.code
                if e.code == 403 and self.name == "Bybit":
                    # Bybit: 403 = IP bloqueada (demasiadas peticiones o region no permitida).
                    # Se corta aqui para no seguir golpeando el servidor.
                    self.blocked = ("HTTP 403 de Bybit: IP bloqueada unos minutos (o region no permitida). "
                                    "Espera 10 min, baja --rps o usa --source bingx.")
                    raise ApiError(self.blocked)
                if e.code in (400, 401, 403, 404, 451):
                    raise ApiError(last)
                time.sleep(1.5 * (attempt + 1))
                continue
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
                last = str(e)
                time.sleep(1.5 * (attempt + 1))
                continue
            if not isinstance(body, dict):
                return body
            data, err, retry = self.unwrap(body)
            if err is None:
                return data
            last = err
            if retry:  # límite de peticiones superado
                time.sleep(2.0 * (attempt + 1))
                continue
            raise ApiError(last)
        raise ApiError(last)


class BybitApi(Api):
    base_url = BYBIT_URL
    name = "Bybit"

    def unwrap(self, body):
        code = body.get("retCode", 0)
        if str(code) == "0":
            return body.get("result"), None, False
        # 10006 / 10018: límite de peticiones
        return None, "código %s: %s" % (code, body.get("retMsg", "")), str(code) in ("10006", "10018")


def server_offset_ms(api):
    """Diferencia entre el reloj del exchange y el tuyo (para saber qué vela está cerrada)."""
    try:
        if api.name == "Bybit":
            data = api.get("/v5/market/time", retries=2)
            ms = int(data["timeNano"]) // 1000000 if data.get("timeNano") else int(data["timeSecond"]) * 1000
            return ms - int(time.time() * 1000)
        data = api.get("/openApi/swap/v2/server/time", retries=2)
        return int(data["serverTime"]) - int(time.time() * 1000)
    except Exception:
        return 0


def load_universe(api, a):
    """Lista de monedas a escanear: [{'symbol', 'qv' (vol 24h USDT), 'chg' (% 24h)}]."""
    if a.symbols:
        syms = [normalize(s) for s in a.symbols.split(",")]
        return [{"symbol": s, "qv": None, "chg": None} for s in syms if s]

    items = []
    tickers = None
    try:
        tickers = api.get("/openApi/swap/v2/quote/ticker")
    except ApiError as e:
        print("Aviso: no pude leer los tickers (%s). Uso la lista de contratos." % e)

    if tickers:
        if isinstance(tickers, dict):
            tickers = [tickers]
        for t in tickers:
            sym = str(t.get("symbol", ""))
            if sym.endswith("-USDT"):
                items.append({"symbol": sym,
                              "qv": to_float(t.get("quoteVolume")),
                              "chg": to_float(t.get("priceChangePercent"))})
    else:
        contracts = api.get("/openApi/swap/v2/quote/contracts") or []
        for ct in contracts:
            sym = str(ct.get("symbol", ""))
            if sym.endswith("-USDT") and str(ct.get("status", 1)) == "1":
                items.append({"symbol": sym, "qv": None, "chg": None})

    if not a.tradfi:
        items = [x for x in items if not is_tradfi(x["symbol"])]

    if any(x["qv"] for x in items):
        items = [x for x in items if (x["qv"] or 0) >= a.min_vol]
        items.sort(key=lambda x: -(x["qv"] or 0))
    else:
        print("Aviso: no hay datos de volumen 24h; no filtro por liquidez.")

    if a.top > 0:
        items = items[:a.top]
    return items


def bingx_listed(bingx_api, a):
    """Conjunto de símbolos (formato Bybit, ej. LONGXIAUSDT) que cotizan como perpetuo USDT en BingX."""
    contracts = bingx_api.get("/openApi/swap/v2/quote/contracts") or []
    out = set()
    for ct in contracts:
        sym = str(ct.get("symbol", ""))
        if sym.endswith("-USDT") and str(ct.get("status", 1)) == "1" and (a.tradfi or not is_tradfi(sym)):
            out.add(sym.replace("-", ""))
    return out


def load_universe_bybit(api, a, bingx_api):
    """Lista de monedas (Bybit linear USDT), por defecto solo las que también están en BingX."""
    if a.symbols:
        syms = [normalize(s).replace("-", "") for s in a.symbols.split(",")]
        return [{"symbol": s, "qv": None, "chg": None} for s in syms if s]

    data = api.get("/v5/market/tickers", {"category": "linear"}) or {}
    items = []
    for t in data.get("list", []):
        sym = str(t.get("symbol", ""))
        if not sym.endswith("USDT"):  # fuera USDC, futuros con fecha, etc.
            continue
        pct = to_float(t.get("price24hPcnt"))
        items.append({"symbol": sym,
                      "qv": to_float(t.get("turnover24h")),
                      "chg": pct * 100.0 if pct is not None else None})

    if not a.all_bybit:
        try:
            listed = bingx_listed(bingx_api, a)
            if listed:
                items = [x for x in items if x["symbol"] in listed]
            else:
                print("Aviso: BingX no devolvió contratos; escaneo todo Bybit.")
        except ApiError as e:
            print("Aviso: no pude leer la lista de BingX (%s); escaneo todo Bybit." % e)

    items = [x for x in items if (x["qv"] or 0) >= a.min_vol]
    items.sort(key=lambda x: -(x["qv"] or 0))
    if a.top > 0:
        items = items[:a.top]
    return items


def fetch_klines_bybit(api, symbol, interval, limit):
    """Misma salida que fetch_klines. Bybit devuelve [inicio_ms, open, high, low, close, volumen, turnover], la más nueva primero."""
    data = api.get("/v5/market/kline", {"category": "linear", "symbol": symbol,
                                        "interval": BYBIT_INTERVAL[interval], "limit": min(limit, 1000)})
    rows = {}
    for k in (data or {}).get("list", []):
        try:
            row = (int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
        except (TypeError, ValueError, IndexError):
            continue
        rows[row[0]] = row
    return [rows[t] for t in sorted(rows)]


def fetch_klines(api, symbol, interval, limit):
    """Velas ordenadas de la más antigua a la más reciente: (t_ms, open, high, low, close, volume)."""
    data = api.get("/openApi/swap/v3/quote/klines",
                   {"symbol": symbol, "interval": interval, "limit": limit})
    rows = {}
    for k in data or []:
        try:
            if isinstance(k, dict):
                t = int(k.get("time", k.get("openTime", k.get("t"))))
                row = (t, float(k["open"]), float(k["high"]), float(k["low"]),
                       float(k["close"]), float(k["volume"]))
            else:
                row = (int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        rows[row[0]] = row
    return [rows[t] for t in sorted(rows)]


# ----------------------------------------------------------------------------
# Fractales de Williams en una temporalidad mayor (liquidez)
# ----------------------------------------------------------------------------
def aggregate_bars(bars, tf_ms, big_ms):
    """Agrupa velas de tf_ms en velas de big_ms alineadas al reloj UTC (como las de Bybit).
    Solo devuelve grupos COMPLETOS y sin huecos. `bars` deben ser velas ya cerradas."""
    need = big_ms // tf_ms
    out, grp, key = [], [], None

    def flush():
        if len(grp) == need and all(grp[k + 1][0] - grp[k][0] == tf_ms for k in range(need - 1)):
            out.append((key * big_ms, grp[0][1], max(b[2] for b in grp),
                        min(b[3] for b in grp), grp[-1][4], sum(b[5] for b in grp)))

    for b in bars:
        k = b[0] // big_ms
        if k != key:
            if grp:
                flush()
            key, grp = k, []
        grp.append(b)
    if grp:
        flush()
    return out


def fractal_sweeps(bars, tf_ms, big_ms, nfr, max_age):
    """Para cada vela del gráfico, el nivel del fractal de `big_ms` que ha BARRIDO y RECUPERADO.

    - Fractal bajo (liquidez de compradores): la vela perfora el nivel y cierra por encima.
    - Fractal alto: la vela perfora el nivel y cierra por debajo.
    - Solo cuenta si el fractal ya estaba confirmado (nfr velas grandes cerradas a su derecha)
      y NO se había tocado antes: cada nivel se usa una sola vez (se "consume" al primer pinchazo).
    - Caduca a los `max_age` periodos grandes.
    Devuelve (nivel_largo[i], nivel_corto[i]) con None donde no hay barrida."""
    n = len(bars)
    out_l, out_s = [None] * n, [None] * n
    if big_ms < tf_ms or big_ms % tf_ms or n == 0:
        return out_l, out_s
    big = aggregate_bars(bars, tf_ms, big_ms) if big_ms > tf_ms else list(bars)
    t = [b[0] for b in bars]
    h = [b[2] for b in bars]
    l = [b[3] for b in bars]
    c = [b[4] for b in bars]

    for j in range(nfr, len(big) - nfr):
        win = range(j - nfr, j + nfr + 1)
        if any(big[k + 1][0] - big[k][0] != big_ms for k in range(j - nfr, j + nfr)):
            continue  # hueco en los datos: no es un patrón válido
        is_lo = all(big[j][3] < big[k][3] for k in win if k != j)
        is_hi = all(big[j][2] > big[k][2] for k in win if k != j)
        if not (is_lo or is_hi):
            continue
        confirm_t = big[j + nfr][0] + big_ms          # instante en que se cierra la vela que lo confirma
        i0 = bisect.bisect_left(t, confirm_t)           # primera vela del gráfico POSTERIOR a la confirmación
        level_lo, level_hi = big[j][3], big[j][2]
        for i in range(i0, n):
            if t[i] - big[j][0] > max_age * big_ms:
                break
            if is_lo and l[i] < level_lo:
                if c[i] > level_lo:
                    out_l[i] = level_lo if out_l[i] is None else max(out_l[i], level_lo)
                is_lo = False                           # consumido
            if is_hi and h[i] > level_hi:
                if c[i] < level_hi:
                    out_s[i] = level_hi if out_s[i] is None else min(out_s[i], level_hi)
                is_hi = False
            if not (is_lo or is_hi):
                break
    return out_l, out_s


# ----------------------------------------------------------------------------
# Lógica de la señal (réplica del indicador de Frost Script)
# ----------------------------------------------------------------------------
def analyze(bars, a):
    """Devuelve las señales finales de las últimas `a.lookback` velas cerradas."""
    n = len(bars)
    if n < 3:
        return []
    t = [b[0] for b in bars]
    o = [b[1] for b in bars]
    h = [b[2] for b in bars]
    l = [b[3] for b in bars]
    c = [b[4] for b in bars]
    v = [b[5] for b in bars]

    # Anatomía de la vela
    rng = [h[i] - l[i] for i in range(n)]
    cpos = [(c[i] - l[i]) / rng[i] if rng[i] > 0 else 0.5 for i in range(n)]
    body_hi = [max(o[i], c[i]) for i in range(n)]
    body_lo = [min(o[i], c[i]) for i in range(n)]
    wick_dn = [(body_lo[i] - l[i]) / rng[i] if rng[i] > 0 else 0.0 for i in range(n)]
    wick_up = [(h[i] - body_hi[i]) / rng[i] if rng[i] > 0 else 0.0 for i in range(n)]

    # ATR (EMA del rango verdadero)
    tr = [rng[0]] + [max(rng[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    atr = ema(tr, ATR_LEN)

    # Volumen relativo y delta estimado
    vs = rolling_sum(v, a.rvol_len)
    rvol = [v[i] / (vs[i] / a.rvol_len) if (vs[i] is not None and vs[i] > 0) else 0.0 for i in range(n)]
    delta = [v[i] * (2 * cpos[i] - 1) for i in range(n)]
    cvd = rolling_sum(delta, a.delta_len)

    # VWAP móvil
    tp = [(h[i] + l[i] + c[i]) / 3.0 for i in range(n)]
    vol_sum = rolling_sum(v, a.vwap_len)
    tpv_sum = rolling_sum([tp[i] * v[i] for i in range(n)], a.vwap_len)
    vwap = [tpv_sum[i] / vol_sum[i] if (vol_sum[i] is not None and vol_sum[i] > 0) else c[i]
            for i in range(n)]

    # Tendencia y liquidez previa
    ema_f = ema(c, a.ema_fast)
    ema_s = ema(c, a.ema_slow)
    ll = rolling_min(l, a.swing)
    hh = rolling_max(h, a.swing)

    # Fractales de Williams de la temporalidad mayor: barridas de liquidez sin tocar
    if a.frac != "off":
        frac_l, frac_s = fractal_sweeps(bars, TF_MS[a.tf], TF_MS[a.frac_tf], a.frac_n, a.frac_bars)
    else:
        frac_l, frac_s = [None] * n, [None] * n
    max_pts = 6 if a.frac == "bonus" else 5

    raw_l = [False] * n
    raw_s = [False] * n
    sc_l = [0] * n
    sc_s = [0] * n

    for i in range(1, n):
        prev_lo = ll[i - 1]
        prev_hi = hh[i - 1]
        if prev_lo is None or prev_hi is None:
            continue
        ai = atr[i]

        # Disparador 1: barrida de liquidez + recuperación
        big_enough = rng[i] >= a.min_range_atr * ai
        sweep_long = l[i] < prev_lo and c[i] > prev_lo and big_enough
        sweep_short = h[i] > prev_hi and c[i] < prev_hi and big_enough

        # Disparador 2: absorción en el extremo
        near_lo = l[i] <= prev_lo + NEAR_ATR * ai
        near_hi = h[i] >= prev_hi - NEAR_ATR * ai
        absorb_long = near_lo and rvol[i] >= a.rvol_min and wick_dn[i] >= a.wick_min
        absorb_short = near_hi and rvol[i] >= a.rvol_min and wick_up[i] >= a.wick_min

        close_ok_l = cpos[i] >= a.close_pos
        close_ok_s = cpos[i] <= 1 - a.close_pos

        prev_cvd = cvd[i - 1]
        flip_l = prev_cvd is not None and prev_cvd < 0 and delta[i] > 0
        flip_s = prev_cvd is not None and prev_cvd > 0 and delta[i] < 0
        vol_pt = 1 if rvol[i] >= a.rvol_min else 0

        sc_l[i] = (int(sweep_long) + int(absorb_long) + vol_pt + int(flip_l) + int(c[i] < vwap[i])
                   + int(a.frac == "bonus" and frac_l[i] is not None))
        sc_s[i] = (int(sweep_short) + int(absorb_short) + vol_pt + int(flip_s) + int(c[i] > vwap[i])
                   + int(a.frac == "bonus" and frac_s[i] is not None))

        # Riesgo máximo: descarta velas con el stop demasiado lejos (entrada tardía / perseguir el precio)
        risk_ok_l = a.max_risk_atr <= 0 or (c[i] - (l[i] - STOP_ATR * ai)) <= a.max_risk_atr * ai
        risk_ok_s = a.max_risk_atr <= 0 or ((h[i] + STOP_ATR * ai) - c[i]) <= a.max_risk_atr * ai
        frac_ok_l = a.frac != "require" or frac_l[i] is not None
        frac_ok_s = a.frac != "require" or frac_s[i] is not None

        trend_up = ema_f[i] > ema_s[i]
        trend_dn = ema_f[i] < ema_s[i]
        raw_l[i] = ((sweep_long or absorb_long) and close_ok_l and sc_l[i] >= a.min_score
                    and (not a.require_trend or trend_up) and risk_ok_l and frac_ok_l)
        raw_s[i] = ((sweep_short or absorb_short) and close_ok_s and sc_s[i] >= a.min_score
                    and (not a.require_trend or trend_dn) and risk_ok_s and frac_ok_s)

    # Pausa: una señal por racimo
    cnt_l = rolling_sum([1 if x else 0 for x in raw_l], a.cooldown)
    cnt_s = rolling_sum([1 if x else 0 for x in raw_s], a.cooldown)

    out = []
    for i in range(max(1, n - a.lookback), n):
        for side, raw, cnt, sc, fr in (("LONG", raw_l, cnt_l, sc_l, frac_l), ("SHORT", raw_s, cnt_s, sc_s, frac_s)):
            if not raw[i] or cnt[i - 1] is None or cnt[i - 1] >= 1:
                continue
            if side == "LONG":
                stop = l[i] - STOP_ATR * atr[i]
            else:
                stop = h[i] + STOP_ATR * atr[i]
            r_abs = abs(c[i] - stop)
            sign = 1 if side == "LONG" else -1
            out.append({
                "dir": side,
                "strong": sc[i] >= a.min_score + 1,
                "score": sc[i],
                "max_score": max_pts,
                "bars_ago": n - 1 - i,
                "time": t[i],
                "price": c[i],
                "stop": stop,
                "risk": r_abs / c[i] * 100.0 if c[i] > 0 else 0.0,
                "risk_atr": r_abs / atr[i] if atr[i] > 0 else 0.0,
                "tp1": c[i] + sign * a.rr1 * r_abs,
                "tp2": c[i] + sign * a.rr2 * r_abs,
                "frac": fr[i],          # nivel del fractal barrido (None si no hubo)
                "rvol": rvol[i],
            })
    return out


# ----------------------------------------------------------------------------
# Escaneo y salida por pantalla
# ----------------------------------------------------------------------------
def scan_once(api, a, universe, now_ms):
    tf_ms = TF_MS[a.tf]
    results = []
    errors = []

    def work(item):
        get_klines = fetch_klines_bybit if a.source == "bybit" else fetch_klines
        rows = get_klines(api, item["symbol"], a.tf, a.bars)
        closed = [r for r in rows if r[0] + tf_ms <= now_ms]  # descarta la vela en formación
        if len(closed) < a.min_bars:
            return item, []
        return item, analyze(closed, a)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futures = {ex.submit(work, it): it for it in universe}
        for fut in as_completed(futures):
            done += 1
            try:
                item, sigs = fut.result()
                for s in sigs:
                    s["symbol"] = item["symbol"]
                    s["qv"] = item["qv"]
                    s["chg"] = item["chg"]
                    results.append(s)
            except ApiError as e:
                errors.append((futures[fut]["symbol"], str(e)))
            if sys.stderr.isatty() and (done % 10 == 0 or done == len(universe)):
                sys.stderr.write("\r  escaneando %d/%d " % (done, len(universe)))
                sys.stderr.flush()
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 40 + "\r")
    results.sort(key=lambda r: (-r["score"], r["bars_ago"], -r["rvol"]))
    return results, errors


def fmt_price(p):
    return "%.6g" % p


def fmt_vol(qv):
    if qv is None:
        return "-"
    return "%.1fM" % (qv / 1e6) if qv >= 1e6 else "%.0fK" % (qv / 1e3)


def paint(text, color, a):
    if a.no_color:
        return text
    return COLORS[color] + text + COLORS["0"]


def print_results(results, errors, a, n_universe, elapsed):
    tf_ms = TF_MS[a.tf]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print()
    src = "BingX perpetuos" if a.source == "bingx" else (
        "Bybit perpetuos" + ("" if a.all_bybit else " (también en BingX)"))
    print("%s | %s | %d monedas | %.0fs | %s" % (src, a.tf, n_universe, elapsed, stamp))
    if errors:
        print(paint("  %d monedas con error (ej.: %s: %s)" % (len(errors), errors[0][0], errors[0][1]), "y", a))
    if not results:
        print("  Sin señales en las últimas %d vela(s) cerrada(s)." % a.lookback)
        return

    header = "%3s  %-16s %-7s %4s %4s  %12s %7s %8s %5s  %12s %7s %12s %12s %4s" % (
        "#", "Par", "Señal", "Pts", "Hace", "Precio", "24h", "Vol24h", "RVol", "Stop", "Riesgo",
        "TP1", "TP2", "Frac")
    print(paint(header, "d", a))
    shown = results[:a.max]
    for idx, r in enumerate(shown, 1):
        label = r["dir"] + ("+" if r["strong"] else "")
        chg = "%+.1f%%" % r["chg"] if r["chg"] is not None else "-"
        line = "%3d  %-16s %-7s %4s %4d  %12s %7s %8s %5.1f  %12s %6.2f%% %12s %12s %4s" % (
            idx, r["symbol"], label, "%d/%d" % (r["score"], r["max_score"]), r["bars_ago"],
            fmt_price(r["price"]), chg, fmt_vol(r["qv"]), r["rvol"], fmt_price(r["stop"]), r["risk"],
            fmt_price(r["tp1"]), fmt_price(r["tp2"]), "Sí" if r["frac"] is not None else "-")
        print(paint(line, "g" if r["dir"] == "LONG" else "r", a))
    if len(results) > len(shown):
        print(paint("  (+%d más; sube --max)" % (len(results) - len(shown)), "d", a))

    syms = []
    for r in shown:
        s = r["symbol"].replace("-", "")
        if s not in syms:
            syms.append(s)
    print()
    print("Para buscar en tradoor: " + ", ".join(syms))
    print(paint("Pts = puntuación ('+' = fuerte) | Hace = velas cerradas atrás (0 = la última) | "
                "Stop orientativo = mecha +/- %.2f ATR | TP1/TP2 = %g R y %g R | "
                "Frac = barrió un fractal de %s" % (STOP_ATR, a.rr1, a.rr2, a.frac_tf), "d", a))


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Escáner de señales ORDERFLOW TRAP PRO para perpetuos USDT de BingX.")
    g = p.add_argument_group("qué escanear")
    g.add_argument("--tf", default="15m", choices=sorted(TF_MS, key=TF_MS.get),
                   help="temporalidad (por defecto 15m)")
    g.add_argument("--source", default="bingx", choices=["bingx", "bybit"],
                   help="de dónde bajar las velas: bingx (por defecto) o bybit (mucho más rápido; "
                        "es el mismo exchange que ves en tradoor)")
    g.add_argument("--all-bybit", action="store_true",
                   help="con --source bybit: escanear todo Bybit, no solo lo que también está en BingX")
    g.add_argument("--lookback", type=int, default=1,
                   help="velas cerradas a revisar; 1 = solo la última (por defecto 1)")
    g.add_argument("--symbols", default="",
                   help="solo estas monedas, separadas por coma (ej. LONGXIA,BTC,ETH)")
    g.add_argument("--min-vol", type=float, default=500000,
                   help="volumen 24h mínimo en USDT (por defecto 500000)")
    g.add_argument("--top", type=int, default=0, help="solo las N monedas con más volumen (0 = todas)")
    g.add_argument("--tradfi", action="store_true",
                   help="incluir contratos de acciones/materias primas/divisas (NC...)")

    s = p.add_argument_group("ajustes de la señal (mismos valores por defecto que el indicador)")
    s.add_argument("--min-score", type=int, default=3, help="puntuación mínima (por defecto 3)")
    s.add_argument("--swing", type=int, default=20, help="velas atrás para la liquidez (20)")
    s.add_argument("--rvol-len", type=int, default=20, help="velas del volumen medio (20)")
    s.add_argument("--rvol-min", type=float, default=1.5, help="volumen relativo mínimo (1.5)")
    s.add_argument("--wick-min", type=float, default=0.35, help="mecha de rechazo mínima, %% del rango (0.35)")
    s.add_argument("--close-pos", type=float, default=0.6, help="cierre en zona fuerte (0.6)")
    s.add_argument("--min-range-atr", type=float, default=0.6, help="rango mínimo de la vela en ATR (0.6)")
    s.add_argument("--delta-len", type=int, default=8, help="velas del delta acumulado (8)")
    s.add_argument("--vwap-len", type=int, default=96, help="velas del VWAP móvil (96)")
    s.add_argument("--ema-fast", type=int, default=21, help="EMA rápida (21)")
    s.add_argument("--ema-slow", type=int, default=55, help="EMA lenta (55)")
    s.add_argument("--cooldown", type=int, default=6, help="pausa entre señales, en velas (6)")
    s.add_argument("--no-trend", action="store_true", help="no exigir tendencia a favor")
    s.add_argument("--max-risk-atr", type=float, default=0.0,
                   help="descarta señales con el stop a más de N ATR de la entrada (0 = desactivado; sugerido 2.5)")
    s.add_argument("--rr1", type=float, default=1.0, help="objetivo TP1, en múltiplos del riesgo (1)")
    s.add_argument("--rr2", type=float, default=2.0, help="objetivo TP2, en múltiplos del riesgo (2)")

    f = p.add_argument_group("fractales de Williams (liquidez de una temporalidad mayor)")
    f.add_argument("--frac", default="off", choices=["off", "bonus", "require"],
                   help="off = no usar; bonus = +1 punto si barre un fractal (puntuación 0-6); "
                        "require = solo avisa si además barre un fractal")
    f.add_argument("--frac-tf", default="30m", choices=["15m", "30m", "1h", "2h", "4h"],
                   help="temporalidad de los fractales (30m); debe ser múltiplo de --tf")
    f.add_argument("--frac-n", type=int, default=2, help="velas a cada lado del fractal (2 = patrón clásico de 5)")
    f.add_argument("--frac-bars", type=int, default=48,
                   help="antigüedad máxima del fractal, en velas de su temporalidad (48 = 24 h en 30m)")

    o = p.add_argument_group("otros")
    o.add_argument("--bars", type=int, default=300, help="velas que se descargan por moneda (300)")
    o.add_argument("--rps", type=float, default=None,
                   help="peticiones por segundo (BingX: 8, permite ~10; Bybit: 25, permite 120)")
    o.add_argument("--workers", type=int, default=None,
                   help="descargas en paralelo (BingX: 6; Bybit: 12)")
    o.add_argument("--max", type=int, default=30, help="máximo de filas a mostrar (30)")
    o.add_argument("--loop", action="store_true", help="repetir al cierre de cada vela")
    o.add_argument("--delay", type=float, default=4.0, help="segundos de espera tras el cierre en --loop (4)")
    o.add_argument("--beep", action="store_true", help="pitido cuando haya señales nuevas")
    o.add_argument("--no-color", action="store_true", help="sin colores")

    a = p.parse_args(argv)
    a.require_trend = not a.no_trend
    if a.rps is None:
        a.rps = 25.0 if a.source == "bybit" else 8.0
    if a.workers is None:
        a.workers = 12 if a.source == "bybit" else 6
    if a.source == "bybit":
        if a.tf not in BYBIT_INTERVAL:
            p.error("Bybit no tiene la temporalidad %s; usa una de: %s" % (a.tf, ", ".join(BYBIT_INTERVAL)))
        a.rps = min(a.rps, 60.0)  # tope de seguridad: el límite de Bybit es 120/s por IP
    a.lookback = max(1, a.lookback)
    a.frac_n = max(1, a.frac_n)
    a.min_bars = max(150, a.ema_slow * 3, a.vwap_len + 10)
    need_bars = 0
    if a.frac != "off":
        if TF_MS[a.frac_tf] < TF_MS[a.tf] or TF_MS[a.frac_tf] % TF_MS[a.tf]:
            p.error("--frac-tf %s tiene que ser igual o múltiplo de --tf %s (ej. --tf 15m con --frac-tf 30m)"
                    % (a.frac_tf, a.tf))
        factor = TF_MS[a.frac_tf] // TF_MS[a.tf]
        need_bars = (a.frac_bars + 2 * a.frac_n + 2) * factor + 30   # historial para ver los fractales de 24 h
    a.bars = min(1440, max(a.bars, a.min_bars + 30, need_bars))
    return a


def main():
    a = parse_args()
    if os.name == "nt":
        os.system("")  # activa los colores ANSI en la consola de Windows
    if not sys.stdout.isatty():
        a.no_color = True

    if a.source == "bybit":
        api = BybitApi(a.rps)
        bingx_api = Api(8.0)  # solo para leer la lista de contratos de BingX (1 petición)
    else:
        api = Api(a.rps)
        bingx_api = None
    tf_ms = TF_MS[a.tf]
    seen = set()

    try:
        while True:
            t0 = time.time()
            offset = server_offset_ms(api)
            now_ms = int(time.time() * 1000) + offset
            try:
                if a.source == "bybit":
                    universe = load_universe_bybit(api, a, bingx_api)
                else:
                    universe = load_universe(api, a)
            except ApiError as e:
                print("No pude conectar con %s: %s" % (api.name, e))
                print("Revisa tu conexión / VPN y vuelve a intentarlo.")
                if not a.loop:
                    return 1
                universe = []
            if universe:
                results, errors = scan_once(api, a, universe, now_ms)
                if a.loop:
                    results = [r for r in results if (r["symbol"], r["time"], r["dir"]) not in seen]
                    for r in results:
                        seen.add((r["symbol"], r["time"], r["dir"]))
                print_results(results, errors, a, len(universe), time.time() - t0)
                if a.beep and results:
                    sys.stdout.write("\a")
                    sys.stdout.flush()
            if not a.loop:
                break
            now_ms = int(time.time() * 1000) + offset
            next_close = (now_ms // tf_ms + 1) * tf_ms
            wait_s = max(1.0, (next_close - now_ms) / 1000.0 + a.delay)
            when = datetime.fromtimestamp(time.time() + wait_s).strftime("%H:%M:%S")
            print("\nPróximo escaneo a las %s (Ctrl+C para parar)" % when)
            time.sleep(wait_s)
    except KeyboardInterrupt:
        print("\nDetenido.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
