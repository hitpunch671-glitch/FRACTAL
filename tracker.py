# -*- coding: utf-8 -*-
"""
Seguimiento de señales (forward-test)
=====================================
Cada señal que el bot envía se sigue en las velas siguientes y se anota si llegó antes
al TP1, al TP2 o al stop. Así, en unos días, tienes datos REALES de cómo funciona la
estrategia en lugar de fiarte de la intuición.

Reglas (conservadoras y simples):
  - Entrada = cierre de la vela señal (no hay deslizamiento ni comisiones).
  - TP1 y TP2 se evalúan por separado, cada uno contra el mismo stop:
        TP1: gana +rr1 R si lo toca antes que el stop, pierde -1 R si no.
        TP2: gana +rr2 R si lo toca antes que el stop, pierde -1 R si no.
  - Si en UNA misma vela se tocan el stop y el objetivo, se cuenta como pérdida
    (no se sabe cuál fue primero; se asume lo peor).
  - Si pasan `max_bars` velas sin resolverse, queda "expirada" y no cuenta ni ganada ni perdida.
"""

import html
import json
import os
import tempfile


def evaluate_trade(sig, bars, tf_ms, max_bars):
    """Recorre las velas CERRADAS posteriores a la señal.

    sig: dict con dir, time (inicio de la vela señal, ms), stop, tp1, tp2.
    bars: velas (t_ms, o, h, l, c, v) cerradas y ordenadas.
    Devuelve (res, n): res = {"tp1": ..., "tp2": ...} con "win" | "loss" | "exp" | None (pendiente);
    n = nº de velas posteriores evaluadas.
    """
    is_long = sig["dir"] == "LONG"
    res = {"tp1": None, "tp2": None}
    n = 0
    for b in bars:
        if b[0] <= sig["time"]:
            continue
        n += 1
        hi, lo = b[2], b[3]
        sl_hit = lo <= sig["stop"] if is_long else hi >= sig["stop"]
        for k in ("tp1", "tp2"):
            if res[k] is not None:
                continue
            tp_hit = hi >= sig[k] if is_long else lo <= sig[k]
            if sl_hit:
                res[k] = "loss"      # misma vela o stop primero: pérdida
            elif tp_hit:
                res[k] = "win"
        if res["tp1"] is not None and res["tp2"] is not None:
            break
        if n >= max_bars:
            break
    if n >= max_bars:
        for k in ("tp1", "tp2"):
            if res[k] is None:
                res[k] = "exp"
    return res, n


def _tally(records, key):
    w = sum(1 for r in records if r["res"][key] == "win")
    l = sum(1 for r in records if r["res"][key] == "loss")
    e = sum(1 for r in records if r["res"][key] == "exp")
    r_win = sum(r["r" + key[-1]] for r in records if r["res"][key] == "win")
    total_r = r_win - l
    return w, l, e, total_r


def _line(label, records, key):
    w, l, e, total_r = _tally(records, key)
    dec = w + l
    if dec == 0:
        return "%s: sin resultados todavía" % label
    return "%s: %d✅ / %d❌ → %d%% · %+.2f R en total (%+.2f R por señal)" % (
        label, w, l, round(100.0 * w / dec), total_r, total_r / dec)


class Tracker:
    def __init__(self, path=None, max_bars=32, keep=500):
        self.path = path
        self.max_bars = max_bars
        self.keep = keep
        self.open = []
        self.done = []
        self.load()

    # -- persistencia (opcional: en Railway solo sobrevive a un redeploy con un Volume) ----------
    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.open = list(data.get("open", []))
            self.done = list(data.get("done", []))[-self.keep:]
        except (OSError, ValueError):
            self.open, self.done = [], []

    def save(self):
        if not self.path:
            return
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"open": self.open, "done": self.done[-self.keep:]}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass  # no poder guardar no debe parar el bot

    # -- altas y seguimiento ------------------------------------------------------------------------
    def add(self, sig, tf):
        """Registra una señal enviada (dict de analyze + symbol)."""
        entry = sig["price"]
        risk = abs(entry - sig["stop"]) or 1e-12
        rec = {
            "symbol": sig["symbol"].replace("-", ""), "api_symbol": sig["symbol"], "dir": sig["dir"], "tf": tf, "time": sig["time"],
            "entry": entry, "stop": sig["stop"], "tp1": sig["tp1"], "tp2": sig["tp2"],
            "r1": abs(sig["tp1"] - entry) / risk, "r2": abs(sig["tp2"] - entry) / risk,
            "score": sig["score"], "strong": bool(sig["strong"]), "frac": sig.get("frac") is not None,
            "res": {"tp1": None, "tp2": None},
        }
        if any(o["symbol"] == rec["symbol"] and o["time"] == rec["time"] and o["dir"] == rec["dir"]
               for o in self.open + self.done):
            return None
        self.open.append(rec)
        return rec

    def update(self, fetch, tf_ms):
        """fetch(rec) -> velas cerradas de esa moneda. Devuelve las señales que acaban de cerrarse."""
        finished, still_open = [], []
        for rec in self.open:
            try:
                bars = fetch(rec)
            except Exception:
                still_open.append(rec)      # sin datos esta vez: se reintenta en el siguiente ciclo
                continue
            rec["res"], n = evaluate_trade(rec, bars, tf_ms, self.max_bars)
            if rec["res"]["tp1"] is not None and rec["res"]["tp2"] is not None:
                finished.append(rec)
            else:
                still_open.append(rec)
        self.open = still_open
        self.done.extend(finished)
        self.done = self.done[-self.keep:]
        return finished

    # -- informes ------------------------------------------------------------------------------------
    @staticmethod
    def outcome_text(rec):
        mark = {"win": "✅", "loss": "❌", "exp": "⌛"}
        return "%s %s <code>%s</code>: TP1 %s · TP2 %s" % (
            "🟢" if rec["dir"] == "LONG" else "🔴", rec["dir"], html.escape(rec["symbol"]),
            mark[rec["res"]["tp1"]], mark[rec["res"]["tp2"]])

    def summary(self):
        """Texto HTML con los resultados, o None si todavía no hay señales cerradas."""
        if not self.done:
            return None
        recs = self.done
        lines = ["📊 <b>Resultado de las señales</b> (%d cerradas, %d en curso)" % (len(recs), len(self.open)),
                 _line("TP1 (%gR)" % round(recs[-1]["r1"], 2), recs, "tp1"),
                 _line("TP2 (%gR)" % round(recs[-1]["r2"], 2), recs, "tp2")]
        exp = sum(1 for r in recs if r["res"]["tp1"] == "exp" and r["res"]["tp2"] == "exp")
        if exp:
            lines.append("Expiradas sin resolver: %d" % exp)
        with_f = [r for r in recs if r["frac"]]
        no_f = [r for r in recs if not r["frac"]]
        if with_f and no_f:
            lines.append("")
            lines.append("<b>Con fractal 30m</b> (%d): %s" % (len(with_f), _line("TP1", with_f, "tp1").replace("TP1: ", "")))
            lines.append("<b>Sin fractal</b> (%d): %s" % (len(no_f), _line("TP1", no_f, "tp1").replace("TP1: ", "")))
        if len(recs) < 30:
            lines.append("")
            lines.append("⚠️ Muestra pequeña (%d): con menos de 30 señales no saques conclusiones. "
                         "No incluye comisiones ni deslizamiento." % len(recs))
        return "\n".join(lines)
