"""
Polymarket Barbell Simulator â Servidor 24/7
VersiÃ³n: 2.0 â Estrategia mejorada con aprendizaje adaptativo
Despliega en Railway (railway.app) gratis

Mejoras v2:
  - EV real calculado desde precio de entrada vs TP dinÃ¡mico
  - TP dinÃ¡mico: entrada + 15pp (mÃ¡s realista)
  - SL ajustado: 7% (mÃ¡s rÃ¡pido en cortar pÃ©rdidas)
  - Filtro de momentum: no entra en mercados con precio cayendo
  - Anti-correlaciÃ³n: evita posiciones sobre el mismo evento
  - Aprendizaje por categorÃ­a: ajusta Kelly segÃºn win rate histÃ³rico
  - Kelly dinÃ¡mico: reduce posiciÃ³n tras pÃ©rdidas consecutivas
  - Salida temporal: cierra posiciones estancadas tras N ciclos
  - Filtro de spread: evita mercados ilÃ­quidos con spread alto
"""
import os
import time
import json
import math
import random
import threading
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# ââââââââââââââââââââââââââââââââââââââââââ
# CONFIGURACIÃN v2
# ââââââââââââââââââââââââââââââââââââââââââ
API_URL      = "https://gamma-api.polymarket.com/markets?limit=300&active=true&closed=false&order=volume24hr&ascending=false"
INTERVAL     = 25   # segundos entre ciclos API
JIT_INTERVAL = 6    # micro-actualizaciones de precio

CORE = dict(
    label="CORE",
    # Alta selectividad: de 300 mercados solo entran los mejores
    min_prob=0.60, max_prob=0.82,
    min_ev=3.5,           # solo movimientos con buen retorno esperado
    tp_offset=0.15,       # TP = entrada + 15pp
    tp_max=0.92,
    sl_drop=0.05,         # SL mÃ¡s ajustado: cortar rÃ¡pido si falla
    kelly_mult=0.30,      # posiciones mÃ¡s grandes para llegar al 30%
    min_kelly=0.10,       # mÃ­nimo garantizado: 10% de caja por posiciÃ³n
    max_pos=4,            # pocos movimientos simultÃ¡neos, pero de calidad
    min_liq=15000,
    min_vol=3000,
    max_spread=0.04,
    trail_be=0.05,
    trail_lock=0.10,
    trail_gap=0.04,
    max_cycles_open=90,   # salida rÃ¡pida si se estanca
)
SAT = dict(
    label="SAT",
    # Muy selectivo: solo los mejores de alta liquidez
    min_prob=0.60, max_prob=0.75,
    min_ev=4.0,
    tp_offset=0.15,
    tp_max=0.91,
    sl_drop=0.05,
    kelly_mult=0.30,      # posiciÃ³n grande en los 2 slots SAT
    min_kelly=0.10,
    max_pos=2,            # solo 2 posiciones SAT, muy selectivas
    min_liq=20000,
    min_vol=20000,
    max_spread=0.04,
    trail_be=0.04,
    trail_lock=0.09,
    trail_gap=0.04,
    max_cycles_open=90,
)

# ââââââââââââââââââââââââââââââââââââââââââ
# ESTADO GLOBAL
# ââââââââââââââââââââââââââââââââââââââââââ
_lock = threading.Lock()

ST = dict(
    cash_core=800.0,
    cash_sat=200.0,
    pos_core=[],
    pos_sat=[],
    hist=[],
    sigs_core=[],
    sigs_sat=[],
    pf_hist=[1000.0],
    wins=0, loss=0, be_count=0,
    ord_core=0, ord_sat=0,
    cycle=0,
    status="Iniciandoâ¦",
    last_update=None,
)

# Aprendizaje adaptativo (fuera del lock, se actualiza con GIL)
PRICE_HISTORY  = {}   # {slug: [p1,p2,...]} Ãºltimos 8 precios
CAT_STATS      = {}   # {cat: {w,l,be}} win rate por categorÃ­a
CONSEC_LOSSES  = 0    # pÃ©rdidas consecutivas â reduce Kelly

# ââââââââââââââââââââââââââââââââââââââââââ
# UTILIDADES DE APRENDIZAJE
# ââââââââââââââââââââââââââââââââââââââââââ
def get_category(slug, question):
    """Clasifica el mercado para aprendizaje adaptativo."""
    s = (slug + " " + question).lower()
    if any(k in s for k in ['nba','nfl','mlb','nhl','cbb','spread',
                             'over-under','total','pts','score','soccer',
                             'mls','tennis','golf','ufc','boxing']):
        return 'sports'
    if any(k in s for k in ['bitcoin','btc','eth','crypto','token','coin','defi']):
        return 'crypto'
    if any(k in s for k in ['trump','election','president','senator',
                             'congress','vote','poll','candidate','governor']):
        return 'politics'
    if any(k in s for k in ['iran','war','military','nuclear','forces',
                             'missile','attack','conflict','ukraine','russia']):
        return 'geopolitics'
    if any(k in s for k in ['fed','rate','inflation','gdp','recession',
                             'economy','market','stock','oil','gold']):
        return 'macro'
    return 'other'

def cat_kelly_multiplier(category):
    """Ajusta Kelly dinÃ¡micamente segÃºn rendimiento histÃ³rico de la categorÃ­a."""
    if category not in CAT_STATS:
        return 1.0
    s   = CAT_STATS[category]
    tot = s['w'] + s['l'] + s['be']
    if tot < 4:
        return 1.0   # sin datos suficientes, neutro
    wr = s['w'] / tot
    if wr < 0.25: return 0.20   # muy mala: reducir 80%
    if wr < 0.35: return 0.45   # mala: reducir 55%
    if wr < 0.45: return 0.70   # algo mala: reducir 30%
    if wr > 0.65: return 1.30   # buena: aumentar 30%
    if wr > 0.55: return 1.10   # decente: aumentar 10%
    return 1.0

def price_momentum(slug):
    """Tendencia de precio: +1 alcista, 0 neutro, -1 bajista."""
    h = PRICE_HISTORY.get(slug, [])
    if len(h) < 4:
        return 0
    recent = h[-4:]
    slope  = (recent[-1] - recent[0]) / 3.0
    if slope > 0.008:  return  1
    if slope < -0.008: return -1
    return 0

def update_price_history(mkts):
    """Actualiza historial de precios para filtro de momentum."""
    for m in mkts:
        slug = m.get("slug", "")
        if not slug:
            continue
        try:
            pr = json.loads(m.get("outcomePrices", "[0]") or "[0]")
            p  = float(pr[0]) if pr else 0.0
        except Exception:
            p  = 0.0
        if p > 0:
            if slug not in PRICE_HISTORY:
                PRICE_HISTORY[slug] = []
            PRICE_HISTORY[slug].append(p)
            if len(PRICE_HISTORY[slug]) > 8:
                PRICE_HISTORY[slug].pop(0)

def keywords_from_q(q):
    """Extrae palabras clave de una pregunta (ignora stopwords cortas)."""
    stop = {'the','a','an','by','to','in','of','and','or','is','will',
            'for','at','on','before','after','does','this','that','be',
            'by','do','it','its','was','has','have','had','not','are'}
    return set(w.lower().strip('?.,!') for w in q.split()
               if w.lower().strip('?.,!') not in stop and len(w) > 3)

def is_correlated(q_new, existing_positions, threshold=2):
    """True si el nuevo mercado comparte â¥threshold palabras clave con posiciones abiertas."""
    kw_new = keywords_from_q(q_new)
    for p in existing_positions:
        kw_ex  = keywords_from_q(p["q"])
        if len(kw_new & kw_ex) >= threshold:
            return True
    return False

# ââââââââââââââââââââââââââââââââââââââââââ
# LÃGICA DE SIMULACIÃN
# ââââââââââââââââââââââââââââââââââââââââââ
def portfolio_value():
    v = ST["cash_core"] + ST["cash_sat"]
    for p in ST["pos_core"]: v += p["size"] * p["cp"]
    for p in ST["pos_sat"]:  v += p["size"] * p["cp"]
    return round(v, 4)

def kelly_size(prob, mult, cash, min_kelly):
    """Kelly con ajuste dinÃ¡mico por pÃ©rdidas consecutivas."""
    global CONSEC_LOSSES
    edge = max(prob - 0.50, 0.04)
    f    = edge * mult * 2.5
    f    = max(f, min_kelly)
    f    = min(f, 0.22)                          # techo absoluto 22%
    # PenalizaciÃ³n por racha de pÃ©rdidas
    loss_pen = min(CONSEC_LOSSES * 0.12, 0.55)
    f = f * (1.0 - loss_pen)
    f = max(f, min_kelly * 0.4)
    return round(f * cash, 2)

def trail_update(p):
    st   = p["strat"]
    gain = (p["cp"] - p["ep"]) / p["ep"]
    if p["phase"] == 0 and gain >= st["trail_be"]:
        p["sl"] = p["ep"]; p["phase"] = 1
    elif p["phase"] >= 1 and gain >= st["trail_lock"]:
        ns = p["cp"] * (1 - st["trail_gap"])
        if ns > p["sl"]:
            p["sl"] = round(ns, 4); p["phase"] = 2
    if p["cp"] > p["peak"]:
        p["peak"] = p["cp"]

def check_close(p, current_cycle):
    """Criterios de cierre: TP, SL, SL-BE, SL-TRAIL, TIME."""
    if p["cp"] >= p["tp"]:
        return "TP"
    if p["cp"] <= p["sl"]:
        if p["phase"] == 1: return "SL-BE"
        if p["phase"] == 2: return "SL-TRAIL"
        return "SL"
    # Salida temporal: si lleva mucho tiempo sin avanzar
    cycles_open = current_cycle - p.get("entry_cycle", current_cycle)
    if cycles_open >= p["strat"].get("max_cycles_open", 200):
        gain = (p["cp"] - p["ep"]) / p["ep"]
        if gain < 0.03:   # < 3% ganancia tras N ciclos â salir
            return "TIME"
    return None

def eval_mkt(m, strat):
    """
    EvalÃºa un mercado con criterios v2:
    - EV real desde TP dinÃ¡mico
    - Filtro de spread (bid-ask)
    - Filtro de momentum (no entrar en caÃ­da)
    - Score ponderado con aprendizaje por categorÃ­a
    """
    try:
        prices = json.loads(m.get("outcomePrices", "[0,0]") or "[0,0]")
        yes_p  = float(prices[0]) if prices        else 0.0
        no_p   = float(prices[1]) if len(prices) > 1 else (1.0 - yes_p)
    except Exception:
        return None

    if not yes_p or yes_p < strat["min_prob"] or yes_p > strat["max_prob"]:
        return None

    vol = float(m.get("volume24hr") or 0)
    liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
    if vol < strat["min_vol"] or liq < strat["min_liq"]:
        return None

    # Filtro de spread: evitar mercados con mucho "juice"
    spread = max(0.0, yes_p + no_p - 1.0)
    if spread > strat["max_spread"]:
        return None

    # TP y SL dinÃ¡micos
    tp = min(strat["tp_max"], yes_p + strat["tp_offset"])
    sl = max(0.08, yes_p - strat["sl_drop"])

    # EV real: retorno esperado hacia TP
    ev = (tp - yes_p) / yes_p * 100.0
    if ev < strat["min_ev"]:
        return None

    slug = m.get("slug") or m.get("id", "")
    q    = m.get("question", "â")

    # Filtro de momentum: no entrar en tendencia bajista
    mom = price_momentum(slug)
    if mom == -1:
        return None

    # Score con aprendizaje por categorÃ­a
    category  = get_category(slug, q)
    cat_mult  = cat_kelly_multiplier(category)

    prob_qual  = 1.0 - abs(yes_p - 0.67) * 2.0   # Ã³ptimo en 0.67
    liq_score  = min(liq  / 300000.0, 1.0)
    vol_score  = min(vol  / 2000000.0, 1.0)
    spread_q   = max(0.0, 1.0 - spread * 25.0)
    mom_bonus  = 0.08 if mom == 1 else 0.0

    base_score = (prob_qual * 0.35 + liq_score * 0.30 +
                  vol_score * 0.20 + spread_q  * 0.15 + mom_bonus)
    score = base_score * max(cat_mult, 0.2)  # nunca anular completamente

    return dict(
        slug=slug, q=q,
        prob=yes_p, ev=round(ev, 2),
        vol=vol, liq=liq, entry=yes_p,
        tp=round(tp, 4), sl=round(sl, 4),
        score=round(score, 4),
        category=category, momentum=mom,
        spread=round(spread, 4),
    )

# ââââââââââââââââââââââââââââââââââââââââââ
# CICLO PRINCIPAL
# ââââââââââââââââââââââââââââââââââââââââââ
def run_cycle(mkts):
    global CONSEC_LOSSES

    mkt_map = {m["slug"]: m for m in mkts if m.get("slug")}

    # Actualizar precios + trailing
    for arr in [ST["pos_core"], ST["pos_sat"]]:
        for p in arr:
            m = mkt_map.get(p["slug"])
            if m:
                try:
                    pr     = json.loads(m.get("outcomePrices", "[0]") or "[0]")
                    p["cp"] = float(pr[0]) if pr else p["cp"]
                except Exception:
                    pass
            trail_update(p)

    # Cerrar posiciones
    def sweep(arr, is_sat):
        global CONSEC_LOSSES
        for i in range(len(arr) - 1, -1, -1):
            p   = arr[i]
            rsn = check_close(p, ST["cycle"])
            if rsn:
                pnl = p["size"] * (p["cp"] - p["ep"])
                if is_sat:
                    ST["cash_sat"]  += p["size"] * p["cp"]
                else:
                    ST["cash_core"] += p["size"] * p["cp"]

                ST["hist"].insert(0, dict(
                    q=p["q"], rsn=rsn, pnl=round(pnl, 4),
                    ep=p["ep"], xp=p["cp"],
                    invested=p["invested"], phase=p["phase"],
                    category=p.get("category","other"),
                    is_sat=is_sat,
                    ts=datetime.now(timezone.utc).isoformat()
                ))

                cat = p.get("category", "other")
                if cat not in CAT_STATS:
                    CAT_STATS[cat] = {"w": 0, "l": 0, "be": 0}

                if pnl > 0.001:
                    ST["wins"]   += 1
                    CAT_STATS[cat]["w"]  += 1
                    CONSEC_LOSSES = max(0, CONSEC_LOSSES - 1)
                elif pnl < -0.001:
                    ST["loss"]   += 1
                    CAT_STATS[cat]["l"]  += 1
                    CONSEC_LOSSES += 1
                else:
                    ST["be_count"] += 1
                    CAT_STATS[cat]["be"] += 1

                arr.pop(i)
                if len(ST["hist"]) > 50:
                    ST["hist"] = ST["hist"][:50]

    sweep(ST["pos_core"], False)
    sweep(ST["pos_sat"],  True)

    # Calcular seÃ±ales
    c_sigs, s_sigs = [], []
    for m in mkts:
        c = eval_mkt(m, CORE)
        s = eval_mkt(m, SAT)
        if c: c_sigs.append(c)
        if s: s_sigs.append(s)

    c_sigs.sort(key=lambda x: -x["score"])
    s_sigs.sort(key=lambda x: -x["score"])

    # Filtrar seÃ±ales de mercados ya abiertos (evita duplicados en panel)
    open_slugs = {p["slug"] for p in ST["pos_core"] + ST["pos_sat"]}
    ST["sigs_core"] = [s for s in c_sigs if s["slug"] not in open_slugs][:5]
    ST["sigs_sat"]  = [s for s in s_sigs if s["slug"] not in open_slugs][:5]

    # Abrir posiciones CORE
    all_pos = ST["pos_core"] + ST["pos_sat"]
    # slug_set incluye CORE + SAT para evitar misma apuesta en ambas estrategias
    slug_set_c = {p["slug"] for p in ST["pos_core"] + ST["pos_sat"]}
    for s in c_sigs:
        if len(ST["pos_core"]) >= CORE["max_pos"]: break
        if ST["cash_core"] < 10:                  break
        if s["slug"] in slug_set_c:               continue
        if is_correlated(s["q"], all_pos):         continue
        cat_mult = cat_kelly_multiplier(s["category"])
        adj_mult = CORE["kelly_mult"] * cat_mult
        sz       = kelly_size(s["prob"], adj_mult, ST["cash_core"], CORE["min_kelly"])
        invested = round(sz * s["entry"], 2)
        if invested < 1 or invested > ST["cash_core"]: continue
        ST["cash_core"] -= invested
        pos = dict(
            slug=s["slug"], q=s["q"],
            ep=s["entry"], cp=s["entry"],
            size=sz, invested=invested,
            tp=s["tp"],
            sl=s["sl"],
            peak=s["entry"], phase=0,
            strat=CORE,
            category=s["category"],
            entry_cycle=ST["cycle"],
        )
        ST["pos_core"].append(pos)
        ST["ord_core"] += 1
        slug_set_c.add(s["slug"])
        all_pos.append(pos)

    # Abrir posiciones SAT
    # slug_set incluye CORE + SAT â impide misma apuesta en las dos estrategias
    slug_set_s = {p["slug"] for p in ST["pos_core"] + ST["pos_sat"]}
    for s in s_sigs:
        if len(ST["pos_sat"]) >= SAT["max_pos"]: break
        if ST["cash_sat"] < 5:                   break
        if s["slug"] in slug_set_s:              continue
        if is_correlated(s["q"], all_pos):        continue
        cat_mult = cat_kelly_multiplier(s["category"])
        adj_mult = SAT["kelly_mult"] * cat_mult
        sz       = kelly_size(s["prob"], adj_mult, ST["cash_sat"],  SAT["min_kelly"])
        invested = round(sz * s["entry"], 2)
        if invested < 0.5 or invested > ST["cash_sat"]: continue
        ST["cash_sat"] -= invested
        pos = dict(
            slug=s["slug"], q=s["q"],
            ep=s["entry"], cp=s["entry"],
            size=sz, invested=invested,
            tp=s["tp"],
            sl=s["sl"],
            peak=s["entry"], phase=0,
            strat=SAT,
            category=s["category"],
            entry_cycle=ST["cycle"],
        )
        ST["pos_sat"].append(pos)
        ST["ord_sat"] += 1
        slug_set_s.add(s["slug"])
        all_pos.append(pos)

    pf = portfolio_value()
    ST["pf_hist"].append(round(pf, 2))
    if len(ST["pf_hist"]) > 100:
        ST["pf_hist"].pop(0)

def micro_jitter():
    """Simula pequeÃ±os movimientos de precio entre ciclos API."""
    for arr in [ST["pos_core"], ST["pos_sat"]]:
        for p in arr:
            drift   = (p["ep"] - p["cp"]) * 0.015
            noise   = (random.random() - 0.48) * 0.006
            new_cp  = max(0.02, min(0.97, round(p["cp"] + drift + noise, 4)))
            p["cp"] = new_cp
            trail_update(p)

# ââââââââââââââââââââââââââââââââââââââââââ
# THREADS DE FONDO
# ââââââââââââââââââââââââââââââââââââââââââ
def api_loop():
    """Ciclo principal: descarga mercados y ejecuta estrategia."""
    while True:
        try:
            resp = requests.get(API_URL, timeout=20)
            raw  = resp.json()
            mkts = raw if isinstance(raw, list) else (
                       raw.get("markets") or raw.get("data") or [])
            mkts = [m for m in mkts if m and m.get("active") and not m.get("closed")]
            # Actualizar historial de precios antes del ciclo
            update_price_history(mkts)
            with _lock:
                ST["cycle"]       += 1
                ST["status"]       = f"OK â {len(mkts)} mercados"
                ST["last_update"]  = datetime.now(timezone.utc).isoformat()
                run_cycle(mkts)
        except Exception as e:
            with _lock:
                ST["status"] = f"Error: {str(e)[:60]}"
        time.sleep(INTERVAL)

def jitter_loop():
    """Micro-actualizaciones de precio cada JIT_INTERVAL segundos."""
    time.sleep(8)
    while True:
        with _lock:
            micro_jitter()
        time.sleep(JIT_INTERVAL)

# ââââââââââââââââââââââââââââââââââââââââââ
# RUTAS WEB
# ââââââââââââââââââââââââââââââââââââââââââ
@app.route("/api/state")
def api_state():
    with _lock:
        pf        = portfolio_value()
        pl        = pf - 1000.0
        total_inv = (sum(p["invested"] for p in ST["pos_core"]) +
                     sum(p["invested"] for p in ST["pos_sat"]))
        total_ops = ST["wins"] + ST["loss"] + ST["be_count"]
        win_rate  = round(ST["wins"] / total_ops * 100, 1) if total_ops > 0 else None

        def pos_dto(p, is_sat):
            g = (p["cp"] - p["ep"]) / p["ep"] * 100
            return dict(
                slug=p["slug"], q=p["q"],
                ep=round(p["ep"]*100, 1), cp=round(p["cp"]*100, 1),
                tp=round(p["tp"]*100, 1), sl=round(p["sl"]*100, 1),
                size=round(p["size"], 2), invested=p["invested"],
                value_now=round(p["size"]*p["cp"], 2),
                pl_eur=round(p["size"]*(p["cp"]-p["ep"]), 2),
                gain_pct=round(g, 2), phase=p["phase"],
                category=p.get("category","other"),
                cycles_open=ST["cycle"] - p.get("entry_cycle", ST["cycle"]),
                is_sat=is_sat,
            )

        return jsonify(dict(
            portfolio=round(pf, 2), pl=round(pl, 2),
            pl_pct=round(pl / 10, 2),
            total_invested=round(total_inv, 2),
            cash_core=round(ST["cash_core"], 2),
            cash_sat=round(ST["cash_sat"], 2),
            cycle=ST["cycle"], status=ST["status"],
            last_update=ST["last_update"],
            wins=ST["wins"], loss=ST["loss"], be_count=ST["be_count"],
            win_rate=win_rate,
            ord_core=ST["ord_core"], ord_sat=ST["ord_sat"],
            pos_core=[pos_dto(p, False) for p in ST["pos_core"]],
            pos_sat =[pos_dto(p, True)  for p in ST["pos_sat"]],
            hist=ST["hist"][:10],
            sigs_core=ST["sigs_core"],
            sigs_sat =ST["sigs_sat"],
            pf_hist=ST["pf_hist"][-60:],
            cat_stats=CAT_STATS,
            consec_losses=CONSEC_LOSSES,
        ))

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

# ââââââââââââââââââââââââââââââââââââââââââ
# DASHBOARD HTML
# ââââââââââââââââââââââââââââââââââââââââââ
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Barbell Simulator</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0e1a; color: #e0e6f0; font-family: 'Segoe UI', monospace; font-size: 13px; }
.header { background: linear-gradient(90deg,#0a0f20,#0d1526); border-bottom: 2px solid #00d4ff; padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; z-index: 10; }
.brand { font-size: 18px; font-weight: 700; letter-spacing: 2px; }
.brand .poly { color: #00d4ff; } .brand .market { color: #fff; }
.brand small { color: #7a8fa8; font-size: 11px; font-weight: 400; margin-left: 10px; }
.stats { display: flex; gap: 24px; align-items: center; }
.stat { text-align: center; }
.stat .val { font-size: 20px; font-weight: 700; }
.stat .lbl { font-size: 9px; color: #7a8fa8; }
.green { color: #00e676 !important; } .red { color: #ff4444 !important; }
.gold { color: #ffd700 !important; } .orange { color: #ff9900 !important; }
.statusbar { background: #090d1a; border-bottom: 1px solid #1a2d4a; padding: 5px 24px; display: flex; gap: 16px; align-items: center; font-size: 11px; flex-wrap: wrap; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
.body { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; padding: 14px 24px; }
.panel { background: #0d1526; border: 1px solid #1e3a5f; border-radius: 10px; padding: 14px; }
.panel.sat-border { border-color: #3a2a00; }
.panel-title { font-size: 10px; color: #7a8fa8; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
.col { display: flex; flex-direction: column; gap: 12px; }
.pos-card { background: #0a0e1a; border-radius: 6px; padding: 9px 11px; margin-bottom: 6px; border: 1px solid #1e3a5f; }
.pos-card.sat { border-color: #3a2a00; }
.pos-row1 { display: flex; justify-content: space-between; align-items: center; margin-bottom: 3px; }
.pos-name { color: #dde; font-size: 10px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; margin-bottom: 5px; }
.pos-amounts { display: flex; gap: 8px; margin-bottom: 5px; }
.amount-box { flex: 1; background: #0d1526; border-radius: 4px; padding: 4px 7px; text-align: center; }
.amount-box .albl { font-size: 9px; color: #7a8fa8; text-transform: uppercase; letter-spacing: 0.5px; }
.amount-box .aval { font-size: 13px; font-weight: 700; }
.pl-bar { background: #1a2d4a; border-radius: 3px; height: 3px; margin-bottom: 5px; overflow: hidden; }
.pl-bar-fill { height: 100%; border-radius: 3px; transition: width 0.8s ease; }
.pos-tech { font-size: 10px; color: #7a8fa8; display: flex; gap: 8px; flex-wrap: wrap; }
.hist-row { display: flex; align-items: center; gap: 8px; padding: 4px 6px; border-bottom: 1px solid #1a2d4a; border-radius: 3px; margin-bottom: 2px; font-size: 10px; }
.sig-row { display: flex; align-items: center; gap: 6px; padding: 4px 0; border-bottom: 1px solid #1a2d4a; font-size: 10px; }
.barbell { display: flex; gap: 10px; }
.barbell-core { flex: 4; background: #0a0e1a; border: 1px solid #1e3a5f; border-radius: 8px; padding: 10px; text-align: center; }
.barbell-sat  { flex: 1; background: #0a0e1a; border: 1px solid #3a2a00; border-radius: 8px; padding: 10px; text-align: center; }
.spark { display: flex; align-items: flex-end; gap: 2px; height: 56px; }
.spark-bar { width: 6px; border-radius: 2px; opacity: 0.85; transition: height 0.5s ease; }
.empty { color: #7a8fa8; font-size: 11px; padding: 6px 0; }
.cat-grid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.cat-chip { padding: 3px 8px; border-radius: 10px; font-size: 9px; font-weight: 700; border: 1px solid; }
@media (max-width: 768px) { .body { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="header">
  <div>
    <div class="brand"><span class="poly">POLY</span><span class="market">MARKET</span><small>Barbell Simulator v2 â 24/7</small></div>
    <div style="font-size:10px;color:#7a8fa8;margin-top:2px">TP dinÃ¡mico +15pp | SL 7% | Momentum + Anti-correlaciÃ³n | Aprendizaje adaptativo | <span class="green">â Live</span></div>
  </div>
  <div class="stats" id="hstats">
    <div class="stat"><div class="val" id="hpf">â¬1000.00</div><div class="lbl">PORTFOLIO</div></div>
    <div class="stat"><div class="val" id="hpl">+â¬0.00</div><div class="lbl">P&L</div></div>
    <div class="stat"><div class="val" id="hpct">+0.00%</div><div class="lbl">RETORNO</div></div>
    <div class="stat"><div class="val orange" id="hinv">â¬0.00</div><div class="lbl">INVERTIDO</div></div>
    <div class="stat"><div class="val" id="hcyc">0</div><div class="lbl">CICLO</div></div>
  </div>
</div>
<div class="statusbar" id="statusbar">
  <span><span class="dot" id="sdot" style="background:#ffd700"></span><span id="stxt">Cargando...</span></span>
  <span style="color:#7a8fa8">Win Rate: <b id="swr" style="color:#7a8fa8">â</b></span>
  <span style="color:#7a8fa8">G:<b class="green" id="sw">0</b> P:<b class="red" id="sl2">0</b> BE:<b class="gold" id="sbe">0</b></span>
  <span style="color:#7a8fa8">Racha pÃ©rdidas: <b id="scl" style="color:#ff9900">0</b></span>
  <span style="margin-left:auto;color:#7a8fa8">Actualizado: <b id="sup" style="color:#00d4ff">â</b></span>
</div>
<div class="body">
  <div class="col">
    <div class="panel" id="barbell-panel"><div class="panel-title">Barbell 80 / 20</div><div class="barbell" id="barbell"></div></div>
    <div class="panel">
      <div class="panel-title">Curva Portfolio</div>
      <div class="spark" id="spark"></div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:#7a8fa8;margin-top:6px">
        <span>Inicio: <b style="color:#fff">â¬1000</b></span><span id="pfnow">â</span><span id="pfpct">â</span>
      </div>
    </div>
    <div class="panel"><div class="panel-title">ð§  Aprendizaje por CategorÃ­a</div><div id="cat-panel" class="cat-grid"></div></div>
    <div class="panel" style="flex:1"><div class="panel-title">ð Historial de Operaciones</div><div id="hist" style="max-height:240px;overflow-y:auto"></div></div>
  </div>
  <div class="col">
    <div class="panel"><div class="panel-title" style="color:#00d4ff">ð¼ Posiciones CORE</div><div id="pos-core" style="max-height:260px;overflow-y:auto"></div></div>
    <div class="panel sat-border"><div class="panel-title" style="color:#ffd700">â¡ Posiciones SATÃLITE</div><div id="pos-sat" style="max-height:180px;overflow-y:auto"></div></div>
    <div class="panel" style="flex:1">
      <div class="panel-title">ð Top SeÃ±ales</div>
      <div style="color:#00d4ff;font-size:10px;font-weight:700;margin-bottom:4px">CORE (EVâ¥3%)</div>
      <div id="sigs-core"></div>
      <div style="color:#ffd700;font-size:10px;font-weight:700;margin:10px 0 4px">SATÃLITE (EVâ¥4%)</div>
      <div id="sigs-sat"></div>
    </div>
  </div>
</div>
<script>
function clr(v){ return v>0?'#00e676':v<0?'#ff4444':'#ffd700'; }
function fp(v){ return (v>=0?'+':'')+'â¬'+v.toFixed(2); }
function fpc(v){ return (v>=0?'+':'')+v.toFixed(2)+'%'; }
function phLabel(ph){ return ph===0?'Init':ph===1?'BE':'Trail'; }
function phDot(ph){ var c=ph===0?'#ffd700':ph===1?'#00aaff':'#00e676'; return '<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:'+c+';margin-right:3px;vertical-align:middle"></span>'; }
function momIcon(m){ return m===1?'â':m===-1?'â':'â'; }
var CAT_COLORS = {sports:'#ff7043',crypto:'#ab47bc',politics:'#42a5f5',geopolitics:'#ef5350',macro:'#ffca28',other:'#78909c'};
function render(d){
  var plC = clr(d.pl);
  document.getElementById('hpf').style.color=plC;
  document.getElementById('hpf').textContent='â¬'+d.portfolio.toFixed(2);
  document.getElementById('hpl').style.color=plC;
  document.getElementById('hpl').textContent=fp(d.pl);
  document.getElementById('hpct').style.color=plC;
  document.getElementById('hpct').textContent=fpc(d.pl_pct);
  document.getElementById('hinv').textContent='â¬'+d.total_invested.toFixed(2);
  document.getElementById('hcyc').textContent=d.cycle;
  document.getElementById('sdot').style.background=d.status.startsWith('OK')?'#00e676':'#ffd700';
  document.getElementById('stxt').textContent=d.status;
  var wr=d.win_rate!==null?d.win_rate+'%':'â';
  var wrEl=document.getElementById('swr');
  wrEl.textContent=wr; wrEl.style.color=d.win_rate===null?'#7a8fa8':d.win_rate>=50?'#00e676':'#ff4444';
  document.getElementById('sw').textContent=d.wins;
  document.getElementById('sl2').textContent=d.loss;
  document.getElementById('sbe').textContent=d.be_count;
  var cl=document.getElementById('scl');
  cl.textContent=d.consec_losses||0;
  cl.style.color=d.consec_losses>3?'#ff4444':d.consec_losses>1?'#ff9900':'#00e676';
  if(d.last_update){ var dt=new Date(d.last_update); document.getElementById('sup').textContent=dt.toLocaleTimeString(); }
  // Barbell
  var coreVal=d.cash_core+d.pos_core.reduce(function(a,p){return a+p.value_now;},0);
  var satVal=d.cash_sat+d.pos_sat.reduce(function(a,p){return a+p.value_now;},0);
  document.getElementById('barbell').innerHTML=
    '<div class="barbell-core">'+
    '<div style="color:#00d4ff;font-size:10px;font-weight:700;margin-bottom:4px">CORE â 80%</div>'+
    '<div style="font-size:20px;font-weight:700;color:#fff">â¬'+coreVal.toFixed(2)+'</div>'+
    '<div style="font-size:10px;color:#7a8fa8;margin-top:2px">EVâ¥3% | Kelly ~12% | SL 7%</div>'+
    '<div style="font-size:10px;color:#7a8fa8">Pos: <b style="color:#fff">'+d.pos_core.length+'/4</b> | Cash: <b style="color:#fff">â¬'+d.cash_core.toFixed(2)+'</b></div>'+
    '</div>'+
    '<div class="barbell-sat">'+
    '<div style="color:#ffd700;font-size:10px;font-weight:700;margin-bottom:4px">SAT â 20%</div>'+
    '<div style="font-size:20px;font-weight:700;color:#fff">â¬'+satVal.toFixed(2)+'</div>'+
    '<div style="font-size:10px;color:#7a8fa8;margin-top:2px">EVâ¥4% | Kelly ~14%</div>'+
    '<div style="font-size:10px;color:#7a8fa8">Pos: <b style="color:#fff">'+d.pos_sat.length+'/2</b> | Cash: <b style="color:#fff">â¬'+d.cash_sat.toFixed(2)+'</b></div>'+
    '</div>';
  // Sparkline
  var hist=d.pf_hist,mn=Math.min.apply(null,hist),mx=Math.max.apply(null,hist),rng=mx-mn||1;
  document.getElementById('spark').innerHTML=hist.map(function(v){
    var h=Math.max(3,Math.round(((v-mn)/rng)*48));
    return '<div class="spark-bar" style="height:'+h+'px;background:'+(v>=1000?'#00e676':'#ff4444')+'"></div>';
  }).join('');
  document.getElementById('pfnow').innerHTML='Ahora: <b style="color:'+(d.pl>=0?'#00e676':'#ff4444')+'">â¬'+d.portfolio.toFixed(2)+'</b>';
  document.getElementById('pfpct').innerHTML='<b style="color:'+(d.pl>=0?'#00e676':'#ff4444')+'">'+fpc(d.pl_pct)+'</b>';
  // CategorÃ­as aprendizaje
  var cats=d.cat_stats||{};
  var catHtml=Object.keys(cats).map(function(cat){
    var s=cats[cat]; var tot=s.w+s.l+s.be||1; var wr=Math.round(s.w/tot*100);
    var col=CAT_COLORS[cat]||'#78909c';
    return '<div class="cat-chip" style="color:'+col+';border-color:'+col+'22;background:'+col+'11">'+
      cat+' '+wr+'% ('+s.w+'W/'+s.l+'L)</div>';
  }).join('');
  document.getElementById('cat-panel').innerHTML=catHtml||'<div class="empty">Sin datos aÃºn</div>';
  // Posiciones
  function renderPos(arr,isCore){
    if(!arr.length) return '<div class="empty">Sin posiciones abiertas</div>';
    return arr.map(function(p){
      var bc=isCore?'#1e3a5f':'#3a2a00',lc=isCore?'#00d4ff':'#ffd700',lt=isCore?'CORE':'SAT';
      var plBar=Math.max(0,Math.min(100,50+p.gain_pct*2));
      var plGrad=p.gain_pct>=0?'linear-gradient(90deg,#005f2d,#00e676)':'linear-gradient(90deg,#ff4444,#880000)';
      var catC=CAT_COLORS[p.category]||'#78909c';
      return '<div class="pos-card'+(isCore?'':' sat')+'" style="border-color:'+bc+'">'+
        '<div class="pos-row1">'+
        '<span style="color:'+lc+';font-size:10px;font-weight:700">'+lt+'</span>'+
        '<div style="display:flex;align-items:center;gap:8px">'+
        '<span style="color:'+catC+';font-size:9px;background:'+catC+'11;border:1px solid '+catC+'33;padding:1px 5px;border-radius:8px">'+p.category+'</span>'+
        '<span style="color:'+clr(p.pl_eur)+';font-weight:700;font-size:13px">'+(p.pl_eur>=0?'+':'')+'â¬'+p.pl_eur.toFixed(2)+'</span>'+
        '<span style="color:'+clr(p.gain_pct)+';font-size:11px;font-weight:700">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(1)+'%</span>'+
        '</div></div>'+
        '<div class="pos-name">'+p.q+'</div>'+
        '<div class="pos-amounts">'+
        '<div class="amount-box"><div class="albl">Invertido</div><div class="aval" style="color:#fff">â¬'+p.invested.toFixed(2)+'</div></div>'+
        '<div class="amount-box"><div class="albl">Valor actual</div><div class="aval" style="color:'+clr(p.gain_pct)+'">â¬'+p.value_now.toFixed(2)+'</div></div>'+
        '<div class="amount-box"><div class="albl">P&L â¬</div><div class="aval" style="color:'+clr(p.pl_eur)+'">'+(p.pl_eur>=0?'+':'')+'â¬'+p.pl_eur.toFixed(2)+'</div></div>'+
        '</div>'+
        '<div class="pl-bar"><div class="pl-bar-fill" style="width:'+plBar+'%;background:'+plGrad+'"></div></div>'+
        '<div class="pos-tech">'+
        '<span>Ent: <b style="color:#fff">'+p.ep+'%</b></span>'+
        '<span>Act: <b style="color:'+clr(p.gain_pct)+'">'+p.cp+'%</b></span>'+
        '<span>TP: <b style="color:#00e676">'+p.tp+'%</b></span>'+
        '<span>SL: <b style="color:#ff4444">'+p.sl+'%</b></span>'+
        '<span>'+phDot(p.phase)+phLabel(p.phase)+'</span>'+
        '<span style="color:#7a8fa8">â±'+p.cycles_open+'c</span>'+
        '</div></div>';
    }).join('');
  }
  document.getElementById('pos-core').innerHTML=renderPos(d.pos_core,true);
  document.getElementById('pos-sat').innerHTML=renderPos(d.pos_sat,false);
  // Historial
  if(!d.hist.length){ document.getElementById('hist').innerHTML='<div class="empty">Sin historial</div>'; }
  else document.getElementById('hist').innerHTML=d.hist.map(function(h){
    var lc=h.is_sat?'#ffd700':'#00d4ff';
    var catC=CAT_COLORS[h.category]||'#78909c';
    var inv=h.invested?' (â¬'+h.invested.toFixed(2)+')':'';
    return '<div class="hist-row" style="background:'+(h.is_sat?'#3a2a00':'#1a2d4a')+'22">'+
      '<span style="color:'+lc+';min-width:50px">'+h.rsn+'</span>'+
      '<span style="color:'+catC+';font-size:9px;min-width:60px">'+h.category+'</span>'+
      '<span style="flex:1;color:#aaa;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">'+h.q+'</span>'+
      '<span style="color:#7a8fa8;min-width:52px;text-align:right">'+inv+'</span>'+
      '<span style="color:'+clr(h.pnl)+';font-weight:700;min-width:60px;text-align:right">'+(h.pnl>=0?'+':'')+'â¬'+h.pnl.toFixed(2)+'</span>'+
      '</div>';
  }).join('');
  // SeÃ±ales
  function renderSigs(sigs){
    if(!sigs.length) return '<div class="empty">Sin seÃ±ales</div>';
    return sigs.slice(0,4).map(function(s,i){
      var catC=CAT_COLORS[s.category]||'#78909c';
      var mom=s.momentum===1?'<span style="color:#00e676">â</span>':s.momentum===-1?'<span style="color:#ff4444">â</span>':'<span style="color:#7a8fa8">â</span>';
      return '<div class="sig-row">'+
        '<span style="color:#7a8fa8;min-width:14px">'+(i+1)+'</span>'+
        '<span style="color:'+catC+';min-width:60px;font-size:9px">'+s.category+'</span>'+
        '<span style="flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">'+s.q+'</span>'+
        mom+
        '<span style="color:#00d4ff;min-width:34px;text-align:right">'+Math.round(s.prob*100)+'%</span>'+
        '<span style="color:#00e676;min-width:44px;text-align:right">+'+s.ev.toFixed(1)+'%</span>'+
        '<span style="color:#ffd700;min-width:36px;text-align:right">'+s.score.toFixed(3)+'</span>'+
        '</div>';
    }).join('');
  }
  document.getElementById('sigs-core').innerHTML=renderSigs(d.sigs_core);
  document.getElementById('sigs-sat').innerHTML=renderSigs(d.sigs_sat);
}
function refresh(){ fetch('/api/state').then(function(r){return r.json();}).then(render).catch(console.error); }
refresh();
setInterval(refresh, 6000);
</script>
</body>
</html>"""

# ââââââââââââââââââââââââââââââââââââââââââ
# ARRANQUE
# ââââââââââââââââââââââââââââââââââââââââââ
# Iniciar threads de fondo al importar (gunicorn no ejecuta __main__)
threading.Thread(target=api_loop,    daemon=True).start()
threading.Thread(target=jitter_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
