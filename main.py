#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ORDERFLOW TRAP PRO - worker para Railway
========================================
Escanea los perpetuos al CIERRE de cada vela y manda las señales NUEVAS a Telegram.
NO opera por ti: solo avisa. Confirma siempre en el footprint antes de entrar.

Configuración por variables de entorno (ver .env.example y README.md):
    TELEGRAM_BOT_TOKEN   token que te da @BotFather
    TELEGRAM_CHAT_ID     tu chat id (varios, separados por coma)
    SCANNER_ARGS         opciones del scanner, p. ej. "--source bybit --tf 15m"
    ONLY_STRONG          1 = solo avisar de señales fuertes (LONG+ / SHORT+)
    HEARTBEAT_HOURS      informe diario cada N horas (0 = desactivado, por defecto 24)
    TZ_NAME              zona horaria para las horas del mensaje (Europe/Madrid)
    NOTIFY_OUTCOMES      1 = avisar también cuando cada señal termina (TP/stop). Por defecto 0
    TRACK_BARS           velas máximas que se sigue una señal antes de darla por expirada (32)
    STATE_FILE           fichero donde guardar el seguimiento (state.json). En Railway solo
                         sobrevive a un redeploy si lo pones en un Volume, p. ej. /data/state.json

Uso:
    python main.py                    # modo normal (lo que ejecuta Railway)
    python main.py --test             # un escaneo ahora, manda el resultado y termina
    python main.py --test --dry-run   # igual, pero imprime en pantalla (no necesita token)
    python main.py --get-chat-id      # muestra tu chat id (escribe antes algo a tu bot)
    python main.py --stats            # imprime los resultados guardados de las señales
"""

import argparse
import html
import json
import os
import re
import shlex
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

import bingx_scanner as sc
from tracker import Tracker

DEFAULT_ARGS = "--source bybit --tf 15m --frac bonus --max-risk-atr 2.5"
MAX_MSG = 3800  # Telegram admite 4096 caracteres por mensaje


def log(msg):
    print("[%s UTC] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), msg), flush=True)


def load_dotenv(path=".env"):
    """Carga un .env local (solo para pruebas en tu PC; en Railway se usan las Variables)."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "si", "sí", "on")


def get_tz():
    name = os.environ.get("TZ_NAME", "Europe/Madrid")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
class Telegram:
    API = "https://api.telegram.org"

    def __init__(self, token, chat_ids, dry_run=False):
        self.token = token or ""
        self.chat_ids = chat_ids
        self.dry_run = dry_run

    def call(self, method, payload=None, timeout=20):
        # OJO: la URL lleva el token; no se imprime nunca.
        url = "%s/bot%s/%s" % (self.API, self.token, method)
        req = urllib.request.Request(url, data=json.dumps(payload or {}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send(self, text):
        """Manda el texto (HTML) a todos los chats. Devuelve True si llegó a todos."""
        if self.dry_run:
            print("\n----- TELEGRAM (simulado) -----\n" + re.sub(r"</?[a-z]+>", "", html.unescape(text)) +
                  "\n-------------------------------", flush=True)
            return True
        ok_all = True
        for chat in self.chat_ids:
            ok = False
            for attempt in range(3):
                try:
                    self.call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                              "disable_web_page_preview": True})
                    ok = True
                    break
                except urllib.error.HTTPError as e:
                    detail = ""
                    retry_after = 0
                    try:
                        body = json.loads(e.read().decode("utf-8"))
                        detail = body.get("description", "")
                        retry_after = int((body.get("parameters") or {}).get("retry_after", 0))
                    except Exception:
                        pass
                    log("Telegram: HTTP %s %s (chat %s)" % (e.code, detail, chat))
                    if e.code == 429 and attempt < 2:
                        time.sleep(min(max(retry_after, 1), 30))
                        continue
                    if e.code in (400, 401, 403, 404):
                        break  # token/chat mal o el bot no puede escribirte: reintentar no sirve
                    time.sleep(2 * (attempt + 1))
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                    log("Telegram: fallo de red (%s)" % type(e).__name__)
                    time.sleep(2 * (attempt + 1))
            ok_all = ok_all and ok
        return ok_all


def get_chat_ids(token):
    tg = Telegram(token, [])
    try:
        data = tg.call("getUpdates", {"timeout": 0})
    except urllib.error.HTTPError as e:
        print("Telegram respondió HTTP %s: ¿el token es correcto?" % e.code)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print("No pude conectar con Telegram (%s)." % type(e).__name__)
        return 1
    chats = {}
    for u in data.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            m = u.get(key)
            if m and "chat" in m:
                c = m["chat"]
                chats[c["id"]] = c.get("title") or c.get("username") or c.get("first_name") or ""
    if not chats:
        print("No hay mensajes todavía. Abre tu bot en Telegram, pulsa Start (o escríbele algo) y repite.")
        return 1
    print("Chats encontrados (usa el número en TELEGRAM_CHAT_ID):")
    for cid, name in chats.items():
        print("  %s   %s" % (cid, name))
    return 0


# ----------------------------------------------------------------------------
# Mensajes
# ----------------------------------------------------------------------------
def source_label(a):
    if a.source == "bybit":
        return "Bybit" + ("" if a.all_bybit else " (también en BingX)")
    return "BingX"


def fmt_signal(r, a, tz):
    tf_ms = sc.TF_MS[a.tf]
    icon = "🟢" if r["dir"] == "LONG" else "🔴"
    label = r["dir"] + ("+" if r["strong"] else "")
    close = datetime.fromtimestamp((r["time"] + tf_ms) / 1000.0, tz)
    ago = "" if r["bars_ago"] == 0 else " (hace %d velas)" % r["bars_ago"]
    lines = [
        "%s <b>%s</b> <code>%s</code> · %s" % (icon, label, html.escape(r["symbol"].replace("-", "")), a.tf),
        "Pts %d/%d · RVol %.1f · vela cerrada %s%s" % (r["score"], r["max_score"], r["rvol"], close.strftime("%H:%M"), ago),
    ]
    if r.get("frac") is not None:
        verb = "mínimo" if r["dir"] == "LONG" else "máximo"
        lines.append("🔹 Barrió el fractal %s de %s @ %s" % (verb, a.frac_tf, sc.fmt_price(r["frac"])))
    lines += [
        "Entrada ~%s · SL %s (riesgo %.2f%% = %.1f ATR)" % (
            sc.fmt_price(r["price"]), sc.fmt_price(r["stop"]), r["risk"], r["risk_atr"]),
        "TP1 %s (%gR) · TP2 %s (%gR)" % (sc.fmt_price(r["tp1"]), a.rr1, sc.fmt_price(r["tp2"]), a.rr2),
    ]
    extra = []
    if r.get("chg") is not None:
        extra.append("24h %+.1f%%" % r["chg"])
    if r.get("qv") is not None:
        extra.append("Vol24h %s" % sc.fmt_vol(r["qv"]))
    if extra:
        lines.append(" · ".join(extra))
    return "\n".join(lines)


def pack(header, blocks, footer):
    """Reparte los bloques en mensajes de como mucho MAX_MSG caracteres."""
    msgs, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > MAX_MSG and cur != header:
            msgs.append(cur)
            cur = header
        cur += "\n\n" + b
    msgs.append(cur)
    msgs[-1] += "\n\n" + footer
    return msgs


# ----------------------------------------------------------------------------
# El bot
# ----------------------------------------------------------------------------
class Bot:
    BLOCK_COOLDOWN = 900  # si el exchange nos bloquea la IP, esperamos 15 min antes de reintentar

    def __init__(self, a, tg, only_strong, heartbeat_hours, tz, tracker=None, notify_outcomes=False):
        self.a, self.tg, self.only_strong, self.tz = a, tg, only_strong, tz
        self.tracker = tracker or Tracker(None)
        self.notify_outcomes = notify_outcomes
        self.heartbeat_s = heartbeat_hours * 3600.0
        self.seen = set()
        self.fail_streak = 0
        self.blocked_until = 0.0
        self.last_heartbeat = time.time()
        self.last_info = ""
        self.make_apis()

    def make_apis(self):
        if self.a.source == "bybit":
            self.api = sc.BybitApi(self.a.rps)
            self.bingx_api = sc.Api(8.0)
        else:
            self.api = sc.Api(self.a.rps)
            self.bingx_api = None

    def load_universe(self):
        if self.a.source == "bybit":
            return sc.load_universe_bybit(self.api, self.a, self.bingx_api)
        return sc.load_universe(self.api, self.a)

    # -- fallos ---------------------------------------------------------------
    def on_failure(self, msg):
        self.fail_streak += 1
        log("ERROR: %s" % msg)
        if self.api.blocked:
            self.blocked_until = time.time() + self.BLOCK_COOLDOWN
        if self.fail_streak == 1:
            text = "⚠️ <b>Scanner con problemas</b>\n%s" % html.escape(msg)
            if "403" in msg or "451" in msg:
                text += ("\n\nSi corre en Railway, pon la región en <b>EU West (Ámsterdam)</b>: "
                         "los exchanges suelen bloquear IPs de EE. UU. Reintento solo en unos minutos.")
            text += "\nTe aviso de nuevo cuando se recupere."
            self.tg.send(text)

    def on_success(self):
        if self.fail_streak:
            log("Recuperado tras %d fallo(s)." % self.fail_streak)
            self.tg.send("✅ Scanner recuperado.")
        self.fail_streak = 0

    # -- arranque ------------------------------------------------------------
    def startup(self):
        try:
            n = len(self.load_universe())
        except sc.ApiError as e:
            self.on_failure("al arrancar: %s" % e)
            return
        self.on_success()
        self.last_info = "%s · %s · %d monedas" % (source_label(self.a), self.a.tf, n)
        log("Iniciado: %s" % self.last_info)
        self.tg.send("✅ <b>Scanner iniciado</b>\n%s\n%s\nTe aviso al cierre de cada vela. "
                     "Solo aviso, no opero por ti." % (html.escape(self.last_info), html.escape(self.config_text())))

    def config_text(self):
        a = self.a
        frac = {"off": "fractal %s: no", "bonus": "fractal %s: +1 punto (0-6)",
                "require": "fractal %s: obligatorio"}[a.frac]
        frac = frac % a.frac_tf if a.frac != "off" else "fractal: no"
        risk = "riesgo máx. %g ATR" % a.max_risk_atr if a.max_risk_atr > 0 else "sin tope de riesgo"
        return "%s · %s · mín. %d pts%s" % (frac, risk, a.min_score, " · solo fuertes" if self.only_strong else "")

    # -- un escaneo ------------------------------------------------------------
    def cycle(self, test=False):
        """Un escaneo completo. Devuelve nº de señales enviadas, o None si falló."""
        a = self.a
        if self.api.blocked:
            if time.time() < self.blocked_until:
                log("IP bloqueada por el exchange; espero antes de reintentar.")
                return None
            self.make_apis()
        t0 = time.time()
        try:
            offset = sc.server_offset_ms(self.api)
            now_ms = int(time.time() * 1000) + offset
            universe = self.load_universe()
            if not universe:
                raise sc.ApiError("la lista de monedas salió vacía (revisa --min-vol / --symbols)")
            results, errors = sc.scan_once(self.api, a, universe, now_ms)
            if errors and len(errors) >= max(3, len(universe) // 2):
                raise sc.ApiError("fallaron %d de %d monedas (ej. %s: %s)" %
                                  (len(errors), len(universe), errors[0][0], errors[0][1]))
        except sc.ApiError as e:
            self.on_failure(str(e))
            return None
        self.on_success()
        elapsed = time.time() - t0
        self.last_info = "%s · %s · %d monedas · %.0fs" % (source_label(a), a.tf, len(universe), elapsed)

        key = lambda r: (r["symbol"], r["time"], r["dir"])
        new = [r for r in results if key(r) not in self.seen]
        for r in new:
            self.seen.add(key(r))
        horizon = now_ms - 60 * sc.TF_MS[a.tf]  # no acumular claves viejas
        self.seen = {k for k in self.seen if k[1] >= horizon}

        to_send = [r for r in new if r["strong"]] if self.only_strong else new
        log("Escaneo: %s · %d señal(es) nueva(s), %d a enviar" % (self.last_info, len(new), len(to_send)))
        if to_send:
            self.send_signals(to_send, len(universe))
            for r in to_send[:a.max]:
                self.tracker.add(r, a.tf)
        elif test:
            self.tg.send("📭 Sin señales ahora mismo\n%s" % html.escape(self.last_info))

        self.update_tracker(now_ms)

        if self.heartbeat_s > 0 and time.time() - self.last_heartbeat >= self.heartbeat_s:
            self.last_heartbeat = time.time()
            text = "💓 <b>Sigo activo</b>\n%s" % html.escape(self.last_info)
            summ = self.tracker.summary()
            self.tg.send(text + ("\n\n" + summ if summ else ""))
        return len(to_send)

    def update_tracker(self, now_ms):
        """Sigue las señales abiertas y anota cómo terminan (TP1 / TP2 / stop)."""
        a = self.a
        tf_ms = sc.TF_MS[a.tf]
        get = sc.fetch_klines_bybit if a.source == "bybit" else sc.fetch_klines

        def fetch(rec):
            need = min(1000, max(40, int((now_ms - rec["time"]) // tf_ms) + 5))
            rows = get(self.api, rec["api_symbol"], a.tf, need)
            return [r for r in rows if r[0] + tf_ms <= now_ms]

        if not self.tracker.open:
            return
        try:
            finished = self.tracker.update(fetch, tf_ms)
        except sc.ApiError as e:
            log("Seguimiento: %s" % e)
            return
        for rec in finished:
            log("Señal cerrada: %s %s TP1=%s TP2=%s" % (rec["symbol"], rec["dir"], rec["res"]["tp1"], rec["res"]["tp2"]))
            if self.notify_outcomes:
                self.tg.send(Tracker.outcome_text(rec))
        self.tracker.save()

    def send_signals(self, sigs, n_universe):
        a = self.a
        header = "📡 <b>%s · %s</b> · %d señal(es)" % (html.escape(source_label(a)), a.tf, len(sigs))
        footer = "⚠️ Aviso, no recomendación. Confirma en el footprint de tradoor antes de entrar y usa stop."
        for m in pack(header, [fmt_signal(r, a, self.tz) for r in sigs[:a.max]], footer):
            self.tg.send(m)

    def seconds_to_next_close(self):
        tf_ms = sc.TF_MS[self.a.tf]
        offset = sc.server_offset_ms(self.api)
        now_ms = int(time.time() * 1000) + offset
        next_close = (now_ms // tf_ms + 1) * tf_ms
        return max(1.0, (next_close - now_ms) / 1000.0 + self.a.delay)


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description="Scanner de señales -> Telegram (para Railway).")
    p.add_argument("--test", action="store_true", help="un escaneo ahora, manda el resultado y termina")
    p.add_argument("--dry-run", action="store_true", help="no envía a Telegram: imprime los mensajes")
    p.add_argument("--get-chat-id", action="store_true", help="muestra tu chat id y termina")
    p.add_argument("--stats", action="store_true", help="imprime los resultados guardados de las señales y termina")
    cli = p.parse_args()

    state_file = os.environ.get("STATE_FILE", "state.json").strip() or None
    if cli.stats:
        summ = Tracker(state_file).summary()
        print(re.sub(r"</?[a-z]+>", "", html.unescape(summ)) if summ else
              "Todavía no hay señales cerradas en %s." % state_file)
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]

    if cli.get_chat_id:
        if not token:
            print("Falta TELEGRAM_BOT_TOKEN (ponlo en .env o como variable de entorno).")
            return 2
        return get_chat_ids(token)

    try:
        a = sc.parse_args(shlex.split(os.environ.get("SCANNER_ARGS", "").strip() or DEFAULT_ARGS))
    except SystemExit:
        log("SCANNER_ARGS no es válido (mira el mensaje de arriba).")
        return 2

    if not cli.dry_run and (not token or not chats):
        log("Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID. Defínelos en Railway > Variables.")
        return 2

    try:
        hb = float(os.environ.get("HEARTBEAT_HOURS", "24") or 0)
    except ValueError:
        hb = 24.0
    try:
        track_bars = int(os.environ.get("TRACK_BARS", "32") or 32)
    except ValueError:
        track_bars = 32
    tracker = Tracker(state_file, max_bars=max(4, track_bars))
    bot = Bot(a, Telegram(token, chats, cli.dry_run), env_bool("ONLY_STRONG"), hb, get_tz(),
              tracker, env_bool("NOTIFY_OUTCOMES"))

    if cli.test:
        res = bot.cycle(test=True)
        return 0 if res is not None else 1

    bot.startup()
    while True:
        wait = bot.seconds_to_next_close()
        log("Próximo escaneo en %.0f s" % wait)
        time.sleep(wait)
        try:
            bot.cycle()
        except Exception:  # un fallo raro no debe matar el servicio
            traceback.print_exc()
            bot.on_failure("error interno (mira los logs de Railway)")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nDetenido.")
