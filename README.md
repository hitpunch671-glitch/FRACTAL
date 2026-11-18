# Orderflow Trap Scanner → Telegram (Railway)

Escanea los perpetuos USDT al **cierre de cada vela** con la lógica del indicador
*Orderflow Trap Pro* (barrida de liquidez, absorción, volumen relativo, giro de delta y VWAP),
más **fractales de Williams de 30m** como liquidez, y manda las **señales nuevas a Telegram**
con entrada, stop y objetivos. Después **sigue cada señal** y te dice cómo acaba.

> ⚠️ **Solo avisa. No abre operaciones.** El delta se estima con velas (no es el delta real
> del footprint). Confirma siempre en el footprint de tradoor y usa stop.
> Esto no es asesoramiento financiero.

Por defecto escanea con datos de **Bybit** (rápido, y es lo que ves en tradoor) las monedas que
**también existen en BingX**. No necesita librerías externas.

---

## Novedades de esta versión

### Fractal de 30m (`--frac`)
Un fractal de Williams es una vela cuyo máximo (o mínimo) supera al de las **2 velas de cada lado**
(patrón clásico de 5 velas). Se **confirma 2 velas después**: no repinta, pero llega con retraso.
Los fractales **todavía sin tocar** son liquidez (ahí se acumulan stops). Si una vela perfora un
fractal bajo de 30m y **cierra de vuelta por encima** (o al revés con uno alto), es una trampa más
clara que la barrida de un mínimo cualquiera de 20 velas.

| Modo | Efecto |
|---|---|
| `--frac off` | No se usa (comportamiento anterior) |
| `--frac bonus` | Suma **+1 punto** (la puntuación pasa a 0–6) y lo marca en el aviso |
| `--frac require` | Solo avisa si además hay barrida de fractal |

Reglas que aplica (sin mirar al futuro): el fractal debe estar **confirmado**, **sin tocar** antes y
tener menos de 24 h (`--frac-bars 48`). Cada nivel cuenta **una sola vez**: se consume al primer pinchazo.
Las velas de 30m se construyen agrupando las del gráfico (2×15m), así que **no añade peticiones**.
`--frac-tf` permite otra temporalidad (15m, 1h, 2h, 4h) y `--frac-n` el nº de velas a cada lado.

> No hay evidencia de que el fractal mejore los resultados de esta estrategia: es una hipótesis.
> Por eso el bot lleva el seguimiento de abajo, que compara señales **con y sin** fractal.

### Tope de riesgo (`--max-risk-atr 2.5`)
Descarta señales cuyo stop queda a más de N ATR de la entrada (velas enormes: entras tarde y
persigues el precio). Idea tomada del CBC FAST.

### Objetivos
Cada aviso trae **TP1 (1R)** y **TP2 (2R)** (`--rr1`, `--rr2`).

### Seguimiento de resultados (forward-test)
Cada señal enviada se sigue en las velas siguientes. TP1 y TP2 se miden por separado contra el mismo
stop. Reglas conservadoras: entrada en el cierre de la vela señal; si en **una misma vela** se tocan
stop y objetivo se cuenta como **pérdida**; si pasan `TRACK_BARS` velas sin resolverse, **expira**.
No incluye comisiones ni deslizamiento. El informe diario lleva:

```
📊 Resultado de las señales (24 cerradas, 3 en curso)
TP1 (1R): 13✅ / 11❌ → 54% · +2.00 R en total (+0.08 R por señal)
TP2 (2R): 7✅ / 17❌ → 29% · -3.00 R en total (-0.13 R por señal)

Con fractal 30m (9): ...
Sin fractal (15): ...
⚠️ Muestra pequeña (24): con menos de 30 señales no saques conclusiones.
```
Para ver los resultados guardados: `python main.py --stats`.

---

## 1. Crear el bot de Telegram

1. En Telegram abre **@BotFather** → `/newbot` → elige nombre y usuario.
2. Te da un **token** (`123456:ABC...`). Es una contraseña: no lo pegues en GitHub ni en chats.
   Si se filtra, en BotFather usa `/revoke`.
3. Abre tu bot nuevo y pulsa **Start** (o escríbele algo). Para avisos en un grupo, añade el bot
   al grupo y escribe un mensaje.
4. Averigua tu chat id:
   ```powershell
   $env:TELEGRAM_BOT_TOKEN="TU_TOKEN"
   python main.py --get-chat-id
   ```
   El número de tu chat es el `TELEGRAM_CHAT_ID` (los grupos llevan un `-` delante, déjalo).

## 2. Probar en tu PC (opcional pero recomendado)

```powershell
copy .env.example .env            # rellena TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
python main.py --test --dry-run   # imprime en pantalla, no envía nada
python main.py --test             # un escaneo real y manda el resultado a tu Telegram
```
`--test` siempre contesta: con las señales o con «Sin señales ahora mismo».

## 3. Subirlo a GitHub

Crea un repositorio **privado** y sube estos ficheros.

*Con la web:* **uploading an existing file** → arrastra `main.py`, `bingx_scanner.py`, `tracker.py`,
`requirements.txt`, `README.md`, `.gitignore` y `.env.example` → **Commit changes**.

*Con git:*
```powershell
git init
git add .
git commit -m "scanner"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
git push -u origin main
```
Comprueba que **`.env` no aparece** en GitHub. Los secretos van solo en Railway.

**Si ya lo tenías desplegado:** sustituye `main.py` y `bingx_scanner.py` y **añade `tracker.py`**
(sin él el bot no arranca). Railway se redespliega solo al hacer commit.

## 4. Desplegar en Railway

1. [railway.com](https://railway.com) → **New Project** → **Deploy from GitHub repo** → elige tu repo.
2. Pestaña **Variables**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` y
   `SCANNER_ARGS` = `--source bybit --tf 15m --frac bonus --max-risk-atr 2.5`
3. **Settings → Regions**: elige **EU West (Amsterdam)** (Bybit suele dar 403 a IPs de EE. UU.).
4. Despliega. Railway detecta `main.py` y lo arranca solo. Si no, **Settings → Deploy →
   Custom Start Command**: `python main.py`.
5. En **Deployments → View logs** verás `Iniciado: Bybit (también en BingX) · 15m · N monedas`
   y te llegará «✅ Scanner iniciado» con la configuración.

**Guardar el seguimiento entre despliegues (opcional):** el disco de Railway se borra al redesplegar.
Si quieres conservar el historial, añade un **Volume** al servicio, móntalo en `/data` y pon
`STATE_FILE=/data/state.json`. Sin Volume el seguimiento funciona igual, pero empieza de cero
tras cada redeploy.

## Variables

| Variable | Por defecto | Qué hace |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token de @BotFather |
| `TELEGRAM_CHAT_ID` | — | Chat(s) destino, separados por coma |
| `SCANNER_ARGS` | `--source bybit --tf 15m --frac bonus --max-risk-atr 2.5` | Opciones del scanner |
| `ONLY_STRONG` | `0` | `1` = solo LONG+ / SHORT+ |
| `HEARTBEAT_HOURS` | `24` | Informe diario (sigo activo + resultados). `0` = nunca |
| `NOTIFY_OUTCOMES` | `0` | `1` = aviso cuando cada señal termina |
| `TRACK_BARS` | `32` | Velas máximas de seguimiento antes de expirar |
| `STATE_FILE` | `state.json` | Dónde se guarda el seguimiento |
| `TZ_NAME` | `Europe/Madrid` | Zona horaria de las horas del mensaje |

Ejemplos de `SCANNER_ARGS`:

```
--source bybit --tf 15m --frac bonus --max-risk-atr 2.5   # recomendada
--source bybit --tf 15m                                    # versión anterior (sin fractal ni tope)
--source bybit --tf 15m --frac require --max-risk-atr 2.5  # solo trampas sobre fractal 30m
--source bybit --tf 5m  --frac bonus                       # 5m (fractal de 30m = 6 velas)
--source bybit --tf 15m --min-vol 3000000                  # solo monedas líquidas
--source bybit --tf 15m --min-score 4                      # más exigente
--source bingx --tf 15m                                    # datos de BingX (más lento)
```
`--frac` necesita `--tf` igual o menor que `--frac-tf` y que lo divida (15m, 5m, 3m, 1m con 30m).
Para vigilar dos temporalidades, crea un segundo servicio con el mismo repo y otro `SCANNER_ARGS`.

## Cómo leer un aviso

```
🟢 LONG+ LONGXIAUSDT · 15m
Pts 5/6 · RVol 3.5 · vela cerrada 23:45
🔹 Barrió el fractal mínimo de 30m @ 0.01452
Entrada ~0.01457 · SL 0.01430 (riesgo 1.85% = 1.7 ATR)
TP1 0.01484 (1R) · TP2 0.01511 (2R)
24h +1.2% · Vol24h 5.0M
```
`+` = señal fuerte. La entrada es el cierre de la vela señal; el stop es orientativo
(más allá de la mecha ± 0.25 ATR). Abre la moneda en tradoor (Bybit Perp) y espera 2–3 velas:
el footprint de monedas que no son BTC/ETH solo se graba mientras tienes el gráfico abierto.

## Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| Aviso «HTTP 403» | Bybit bloquea la IP o la región. Región **EU West** en Railway; si sigue, `--source bingx`. |
| `ModuleNotFoundError: tracker` | Falta subir `tracker.py` a GitHub. |
| No llega nada a Telegram | ¿Pulsaste **Start** en el bot? ¿Token y chat id correctos? Mira los logs. |
| Pasan horas sin señales | Normal: solo avisa si se cumple la confluencia. No relajes los filtros para «forzar» señales. |
| El historial se borra tras cada redeploy | Añade un Volume y `STATE_FILE=/data/state.json`. |
| Una moneda de BingX no sale | En modo Bybit solo se escanean las que existen en ambos y con el mismo ticker. |
