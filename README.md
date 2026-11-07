# DAYDRIVE v1

Bot **complementario a IPDA**: no busca GRAB/Asia. Opera **continuación de sesión** con:

1. **Ranking residual** (alt − BTC) en ventana Europa 08:00–13:00 UTC  
2. Entrada **pullback a EMA 9/21** a favor de tendencia (o **squeeze → expansión**)  
3. Confirmación **5m** de rechazo en el pullback  
4. Solo **08:00–20:00 UTC** · evita setups tipo barrido Asia (deja esos a IPDA)

## Señales

| Origen | Qué es |
|--------|--------|
| **PB** | Toque EMA + cierre a favor + RVOL |
| **SQZ** | Compresión ATR + vela de expansión en dirección del residual |

## Railway

1. Repo nuevo (no mezclar proceso con IPDA)  
2. Volumen en `/data`  
3. Pegar `railway_vars.txt`  
4. Rellenar `TELEGRAM_*` (BingX si `MODE=LIVE`)  
5. Deploy → mensaje **DAYDRIVE v1 arrancado**

## Modo

- `MODE=SIGNAL` — solo Telegram (recomendado al inicio)  
- `MODE=LIVE` + `LIVE_CONFIRMED=true` — orden market + SL/TP en BingX  

## Archivos

```
bot.py  strategy.py  confirm_5m.py  bingx.py  notify.py
config.py  railway_vars.txt  Procfile  requirements.txt
```

## Convivencia con IPDA

- Worker Railway **aparte**  
- `STATE_PATH=/data/daydrive_state.json` (distinto de IPDA)  
- Mismos límites de riesgo manuales si compartes cuenta  
- `SKIP_ASIA_GRAB=true` reduce solape con IPDA  
