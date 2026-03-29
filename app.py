"""
Polymarket Barbell Simulator — Servidor 24/7
Versión: 1.0
Despliega en Railway (railway.app) gratis

Estrategia Barbell 80/20:
  CORE (€800): prob 55-82%, EV≥4%, Kelly 25%, BE +8%, Trail 15%
  SAT  (€200): prob 58-72%, EV≥10%, Kelly 40%, BE +6%, Trail 12%
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

# ──────────────────────────────────────────
#  CONFIGURACIÓN
# ──────────────────────────────────────────
API_URL  = "https://gamma-api.polymarket.com/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false"
INTERVAL = 30  # segundos entre ciclos API
JIT_INTERVAL = 6  # segundos entre micro-actualizaciones de precio

CORE = dict(
    label="CORE", min_prob=0.55, max_prob=0.82, min_ev=4.0,
    tp_price=0.88, sl_drop=0.12, kelly_mult=0.25, min_kelly=0.06,
    max_pos=4, min_liq=5000, min_vol=1500,
    trail_be=0.08, trail_lock=0.15, trail_gap=0.06, margin=1.08
)
SAT = dict(
    label="SAT", min_prob=0.58, max_prob=0.72, min_ev=10.0,
    tp_price=0.80, sl_drop=0.12, kelly_mult=0.40, min_kelly=0.08,
    max_pos=2, min_liq=8000, min_vol=20000,
    trail_be=0.06, trail_lock=0.12, trail_gap=0.05, margin=1.12
)

# ──────────────────────────────────────────
#  ESTADO GLOBAL (protegido por lock)
# ──────────────────────────────────────────
_lock = threading.Lock()
ST = dict(
    cash_core=800.0, cash_sat=200.0,
    pos_core=[], pos_sat=[],
    hist=[], sigs_core=[], sigs_sat=[],
    pf_hist=[1000.0],
    wins=0, loss=0, be_count=0,
    ord_core=0, ord_sat=0,
    cycle=0, status="Iniciando…",
    last_update=None
)

# ──────────────────────────────────────────
#  LÓGICA DE SIMULACIÓN
# ──────────────────────────────────────────
def portfolio_value():
    v = ST["cash_core"] + ST["cash_sat"]
    for p in ST["pos_core"]: v += p["size"] * p["cp"]
    for p in ST["pos_sat"]:  v += p["size"] * p["cp"]
    return round(v, 4)

def kelly_size(prob, mult, cash, min_kelly):
    edge = max(prob - 0.50, 0.04)
    f    = edge * mult * 2.5
    f    = max(f, min_kelly)
    f    = min(f, 0.40)
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

def check_close(p):
    if p["cp"] >= p["tp"]: return "TP"
    if p["cp"] <= p["sl"]:
        if p["phase"] == 1: return "SL-BE"
        if p["phase"] == 2: return "SL-TRAIL"
        return "SL"
    return None

def eval_mkt(m, strat):
    try:
        prices = json.loads(m.get("outcomePrices", "[0]") or "[0]")
        prob   = float(prices[0]) if prices else 0
    except Exception:
        return None
    if not prob or prob < strat["min_prob"] or prob > strat["max_prob"]:
        return None
    vol = float(m.get("volume24hr") or 0)
    liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
    if vol < strat["min_vol"] or liq < strat["min_liq"]:
        return None
    ev = (strat["margin"] - 1) * 100
    if ev < strat["min_ev"]:
        return None
    reward    = strat["tp_price"] / prob
    vol_score = min(vol / 100000, 1)
    score     = (1 - abs(prob - 0.65)) * 0.5 + vol_score * 0.3 + min(reward - 1, 0.5) * 0.2
    return dict(
        slug=m.get("slug") or m.get("id", ""),
        q=m.get("question", "—"),
        prob=prob, ev=ev, vol=vol, liq=liq, entry=prob, score=score
    )

def run_cycle(mkts):
    mkt_map = {m["slug"]: m for m in mkts if m.get("slug")}

    # Actualizar precios + trailing
    for arr in [ST["pos_core"], ST["pos_sat"]]:
        for p in arr:
            m = mkt_map.get(p["slug"])
            if m:
                try:
                    pr = json.loads(m.get("outcomePrices", "[0]") or "[0]")
                    p["cp"] = float(pr[0]) if pr else p["cp"]
                except Exception:
                    pass
            trail_update(p)

    # Cerrar posiciones
    def sweep(arr, is_sat):
        closed = []
        for i in range(len(arr) - 1, -1, -1):
            p   = arr[i]
            rsn = check_close(p)
            if rsn:
                pnl = p["size"] * (p["cp"] - p["ep"])
                if is_sat: ST["cash_sat"]  += p["size"] * p["cp"]
                else:      ST["cash_core"] += p["size"] * p["cp"]
                ST["hist"].insert(0, dict(
                    q=p["q"], rsn=rsn, pnl=round(pnl, 4),
                    ep=p["ep"], xp=p["cp"], invested=p["invested"],
                    phase=p["phase"], is_sat=is_sat,
                    ts=datetime.now(timezone.utc).isoformat()
                ))
                if   pnl >  0.001: ST["wins"]     += 1
                elif pnl < -0.001: ST["loss"]     += 1
                else:              ST["be_count"] += 1
                arr.pop(i)
        if len(ST["hist"]) > 50:
            ST["hist"] = ST["hist"][:50]

    sweep(ST["pos_core"], False)
    sweep(ST["pos_sat"],  True)

    # Señales
    c_sigs, s_sigs = [], []
    for m in mkts:
        c = eval_mkt(m, CORE); s = eval_mkt(m, SAT)
        if c: c_sigs.append(c)
        if s: s_sigs.append(s)
    c_sigs.sort(key=lambda x: -x["score"])
    s_sigs.sort(key=lambda x: -x["score"])
    ST["sigs_core"] = c_sigs[:5]
    ST["sigs_sat"]  = s_sigs[:5]

    # Abrir CORE
    slug_set = {p["slug"] for p in ST["pos_core"]}
    for s in c_sigs:
        if len(ST["pos_core"]) >= CORE["max_pos"]: break
        if ST["cash_core"] < 10: break
        if s["slug"] in slug_set: continue
        sz       = kelly_size(s["prob"], CORE["kelly_mult"], ST["cash_core"], CORE["min_kelly"])
        invested = round(sz * s["entry"], 2)
        if invested < 1 or invested > ST["cash_core"]: continue
        ST["cash_core"] -= invested
        ST["pos_core"].append(dict(
            slug=s["slug"], q=s["q"], ep=s["entry"], cp=s["entry"],
            size=sz, invested=invested, tp=CORE["tp_price"],
            sl=round(s["entry"] * (1 - CORE["sl_drop"]), 4),
            peak=s["entry"], phase=0, strat=CORE
        ))
        ST["ord_core"] += 1
        slug_set.add(s["slug"])

    # Abrir SAT
    slug_set = {p["slug"] for p in ST["pos_sat"]}
    for s in s_sigs:
        if len(ST["pos_sat"]) >= SAT["max_pos"]: break
        if ST["cash_sat"] < 5: break
        if s["slug"] in slug_set: continue
        sz       = kelly_size(s["prob"], SAT["kelly_mult"], ST["cash_sat"], SAT["min_kelly"])
        invested = round(sz * s["entry"], 2)
        if invested < 0.5 or invested > ST["cash_sat"]: continue
        ST["cash_sat"] -= invested
        ST["pos_sat"].append(dict(
            slug=s["slug"], q=s["q"], ep=s["entry"], cp=s["entry"],
            size=sz, invested=invested, tp=SAT["tp_price"],
            sl=round(s["entry"] * (1 - SAT["sl_drop"]), 4),
            peak=s["entry"], phase=0, strat=SAT
        ))
        ST["ord_sat"] += 1
        slug_set.add(s["slug"])

    pf = portfolio_value()
    ST["pf_hist"].append(round(pf, 2))
    if len(ST["pf_hist"]) > 100:
        ST["pf_hist"].pop(0)

def micro_jitter():
    """Simula pequeños movimientos de precio entre ciclos API."""
    for arr in [ST["pos_core"], ST["pos_sat"]]:
        for p in arr:
            drift  = (p["ep"] - p["cp"]) * 0.02
            noise  = (random.random() - 0.48) * 0.008
            new_cp = max(0.02, min(0.98, round(p["cp"] + drift + noise, 4)))
            p["cp"] = new_cp
            trail_update(p)

# ──────────────────────────────────────────
#  THREADS DE FONDO
# ──────────────────────────────────────────
def api_loop():
    """Ciclo principal: descarga mercados cada INTERVAL segundos."""
    while True:
        try:
            resp = requests.get(API_URL, timeout=20)
            raw  = resp.json()
            mkts = raw if isinstance(raw, list) else raw.get("markets") or raw.get("data") or []
            mkts = [m for m in mkts if m and m.get("active") and not m.get("closed")]
            with _lock:
                ST["cycle"]  += 1
                ST["status"]  = f"OK — {len(mkts)} mercados"
                ST["last_update"] = datetime.now(timezone.utc).isoformat()
                run_cycle(mkts)
        except Exception as e:
            with _lock:
                ST["status"] = f"Error: {str(e)[:60]}"
        time.sleep(INTERVAL)

def jitter_loop():
    """Micro-actualizaciones de precio cada JIT_INTERVAL segundos."""
    time.sleep(8)  # esperar el primer ciclo API
    while True:
        with _lock:
            micro_jitter()
        time.sleep(JIT_INTERVAL)

# ──────────────────────────────────────────
#  RUTAS WEB
# ──────────────────────────────────────────
@app.route("/api/state")
def api_state():
    with _lock:
        pf  = portfolio_value()
        pl  = pf - 1000
        total_inv = sum(p["invested"] for p in ST["pos_core"]) + \
                    sum(p["invested"] for p in ST["pos_sat"])
        total_ops = ST["wins"] + ST["loss"] + ST["be_count"]
        win_rate  = round(ST["wins"] / total_ops * 100, 1) if total_ops > 0 else None

        def pos_dto(p, is_sat):
            g = (p["cp"] - p["ep"]) / p["ep"] * 100
            return dict(
                slug=p["slug"], q=p["q"],
                ep=round(p["ep"]*100, 1), cp=round(p["cp"]*100, 1),
                tp=round(p["tp"]*100, 1), sl=round(p["sl"]*100, 1),
                size=round(p["size"], 2),
                invested=p["invested"],
                value_now=round(p["size"]*p["cp"], 2),
                pl_eur=round(p["size"]*(p["cp"]-p["ep"]), 2),
                gain_pct=round(g, 2),
                phase=p["phase"],
                is_sat=is_sat
            )

        return jsonify(dict(
            portfolio=round(pf, 2),
            pl=round(pl, 2),
            pl_pct=round(pl / 10, 2),
            total_invested=round(total_inv, 2),
            cash_core=round(ST["cash_core"], 2),
            cash_sat=round(ST["cash_sat"], 2),
            cycle=ST["cycle"],
            status=ST["status"],
            last_update=ST["last_update"],
            wins=ST["wins"], loss=ST["loss"], be_count=ST["be_count"],
            win_rate=win_rate,
            ord_core=ST["ord_core"], ord_sat=ST["ord_sat"],
            pos_core=[pos_dto(p, False) for p in ST["pos_core"]],
            pos_sat =[pos_dto(p, True)  for p in ST["pos_sat"]],
            hist=ST["hist"][:10],
            sigs_core=ST["sigs_core"],
            sigs_sat =ST["sigs_sat"],
            pf_hist=ST["pf_hist"][-60:]
        ))

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

# ──────────────────────────────────────────
#  DASHBOARD HTML
# ──────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Barbell Simulator</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0a0e1a; color: #e0e6f0; font-family: 'Segoe UI', monospace; font-size: 13px; }
  .header { background: linear-gradient(90deg,#0a0f20,#0d1526); border-bottom: 2px solid #00d4ff;
            padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; z-index: 10; }
  .brand { font-size: 18px; font-weight: 700; letter-spacing: 2px; }
  .brand .poly { color: #00d4ff; } .brand .market { color: #fff; }
  .brand small { color: #7a8fa8; font-size: 11px; font-weight: 400; margin-left: 10px; }
  .stats { display: flex; gap: 24px; align-items: center; }
  .stat { text-align: center; }
  .stat .val { font-size: 20px; font-weight: 700; }
  .stat .lbl { font-size: 9px; color: #7a8fa8; }
  .green { color: #00e676 !important; } .red { color: #ff4444 !important; } .gold { color: #ffd700 !important; } .orange { color: #ff9900 !important; }
  .statusbar { background: #090d1a; border-bottom: 1px solid #1a2d4a; padding: 5px 24px;
               display: flex; gap: 16px; align-items: center; font-size: 11px; flex-wrap: wrap; }
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
  .hist-row { display: flex; align-items: center; gap: 8px; padding: 4px 6px; border-bottom: 1px solid #1a2d4a;
              border-radius: 3px; margin-bottom: 2px; font-size: 10px; }
  .sig-row  { display: flex; align-items: center; gap: 6px; padding: 4px 0; border-bottom: 1px solid #1a2d4a; font-size: 10px; }
  .barbell { display: flex; gap: 10px; }
  .barbell-core { flex: 4; background: #0a0e1a; border: 1px solid #1e3a5f; border-radius: 8px; padding: 10px; text-align: center; }
  .barbell-sat  { flex: 1; background: #0a0e1a; border: 1px solid #3a2a00; border-radius: 8px; padding: 10px; text-align: center; }
  .spark { display: flex; align-items: flex-end; gap: 2px; height: 56px; }
  .spark-bar { width: 6px; border-radius: 2px; opacity: 0.85; transition: height 0.5s ease; }
  .empty { color: #7a8fa8; font-size: 11px; padding: 6px 0; }
  @media (max-width: 768px) { .body { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="header">
  <div>
    <div class="brand"><span class="poly">POLY</span><span class="market">MARKET</span><small>Barbell Simulator — Servidor 24/7</small></div>
    <div style="font-size:10px;color:#7a8fa8;margin-top:2px">Core EV≥4% Kelly25% BE+8% | Sat EV≥10% Kelly40% BE+6% | Barbell 80/20 | <span class="green">● Live</span></div>
  </div>
  <div class="stats" id="hstats">
    <div class="stat"><div class="val" id="hpf">€1000.00</div><div class="lbl">PORTFOLIO</div></div>
    <div class="stat"><div class="val" id="hpl">+€0.00</div><div class="lbl">P&L</div></div>
    <div class="stat"><div class="val" id="hpct">+0.00%</div><div class="lbl">RETORNO</div></div>
    <div class="stat"><div class="val orange" id="hinv">€0.00</div><div class="lbl">INVERTIDO</div></div>
    <div class="stat"><div class="val" id="hcyc">0</div><div class="lbl">CICLO</div></div>
  </div>
</div>
<div class="statusbar" id="statusbar">
  <span><span class="dot" id="sdot" style="background:#ffd700"></span><span id="stxt">Cargando...</span></span>
  <span style="color:#7a8fa8">Win Rate: <b id="swr" style="color:#7a8fa8">—</b></span>
  <span style="color:#7a8fa8">G:<b class="green" id="sw">0</b> P:<b class="red" id="sl">0</b> BE:<b class="gold" id="sbe">0</b></span>
  <span style="margin-left:auto;color:#7a8fa8">Actualizado: <b id="sup" style="color:#00d4ff">—</b></span>
</div>
<div class="body">
  <div class="col">
    <div class="panel" id="barbell-panel"><div class="panel-title">Barbell 80 / 20</div><div class="barbell" id="barbell"></div></div>
    <div class="panel"><div class="panel-title">Curva Portfolio</div><div class="spark" id="spark"></div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:#7a8fa8;margin-top:6px">
        <span>Inicio: <b style="color:#fff">€1000</b></span><span id="pfnow">—</span><span id="pfpct">—</span>
      </div>
    </div>
    <div class="panel" style="flex:1"><div class="panel-title">📈 Historial de Operaciones</div><div id="hist" style="max-height:240px;overflow-y:auto"></div></div>
  </div>
  <div class="col">
    <div class="panel"><div class="panel-title" style="color:#00d4ff">💼 Posiciones CORE</div><div id="pos-core" style="max-height:240px;overflow-y:auto"></div></div>
    <div class="panel sat-border"><div class="panel-title" style="color:#ffd700">⚡ Posiciones SATÉLITE</div><div id="pos-sat" style="max-height:180px;overflow-y:auto"></div></div>
    <div class="panel" style="flex:1">
      <div class="panel-title">🔎 Top Señales</div>
      <div style="color:#00d4ff;font-size:10px;font-weight:700;margin-bottom:4px">CORE (EV≥4%)</div>
      <div id="sigs-core"></div>
      <div style="color:#ffd700;font-size:10px;font-weight:700;margin:10px 0 4px">SATÉLITE (EV≥10%)</div>
      <div id="sigs-sat"></div>
    </div>
  </div>
</div>

<script>
function clr(v){ return v>0?'#00e676':v<0?'#ff4444':'#ffd700'; }
function fp(v){ return (v>=0?'+':'')+'€'+v.toFixed(2); }
function fpc(v){ return (v>=0?'+':'')+v.toFixed(2)+'%'; }
function phLabel(ph){ return ph===0?'Init':ph===1?'BE':'Trail'; }
function phDot(ph){ var c=ph===0?'#ffd700':ph===1?'#00aaff':'#00e676';
  return '<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:'+c+';margin-right:3px;vertical-align:middle"></span>'; }

function render(d){
  // Header
  var plC = clr(d.pl);
  document.getElementById('hpf').style.color=plC; document.getElementById('hpf').textContent='€'+d.portfolio.toFixed(2);
  document.getElementById('hpl').style.color=plC; document.getElementById('hpl').textContent=fp(d.pl);
  document.getElementById('hpct').style.color=plC; document.getElementById('hpct').textContent=fpc(d.pl_pct);
  document.getElementById('hinv').textContent='€'+d.total_invested.toFixed(2);
  document.getElementById('hcyc').textContent=d.cycle;

  // Statusbar
  document.getElementById('sdot').style.background=d.status.startsWith('OK')?'#00e676':'#ffd700';
  document.getElementById('stxt').textContent=d.status;
  var wr = d.win_rate !== null ? d.win_rate+'%' : '—';
  var wrEl = document.getElementById('swr'); wrEl.textContent=wr;
  wrEl.style.color = d.win_rate===null?'#7a8fa8':d.win_rate>=50?'#00e676':'#ff4444';
  document.getElementById('sw').textContent=d.wins;
  document.getElementById('sl').textContent=d.loss;
  document.getElementById('sbe').textContent=d.be_count;
  if(d.last_update){ var dt=new Date(d.last_update); document.getElementById('sup').textContent=dt.toLocaleTimeString(); }

  // Barbell
  var coreVal = d.cash_core + d.pos_core.reduce(function(a,p){return a+p.value_now;},0);
  var satVal  = d.cash_sat  + d.pos_sat.reduce(function(a,p){return a+p.value_now;},0);
  document.getElementById('barbell').innerHTML =
    '<div class="barbell-core">'+
      '<div style="color:#00d4ff;font-size:10px;font-weight:700;margin-bottom:4px">CORE — 80%</div>'+
      '<div style="font-size:20px;font-weight:700;color:#fff">€'+coreVal.toFixed(2)+'</div>'+
      '<div style="font-size:10px;color:#7a8fa8;margin-top:2px">EV≥4% | Kelly 25% | BE +8%</div>'+
      '<div style="font-size:10px;color:#7a8fa8">Pos: <b style="color:#fff">'+d.pos_core.length+'/4</b> | Cash: <b style="color:#fff">€'+d.cash_core.toFixed(2)+'</b></div>'+
    '</div>'+
    '<div class="barbell-sat">'+
      '<div style="color:#ffd700;font-size:10px;font-weight:700;margin-bottom:4px">SAT — 20%</div>'+
      '<div style="font-size:20px;font-weight:700;color:#fff">€'+satVal.toFixed(2)+'</div>'+
      '<div style="font-size:10px;color:#7a8fa8;margin-top:2px">EV≥10% | Kelly 40%</div>'+
      '<div style="font-size:10px;color:#7a8fa8">Pos: <b style="color:#fff">'+d.pos_sat.length+'/2</b> | Cash: <b style="color:#fff">€'+d.cash_sat.toFixed(2)+'</b></div>'+
    '</div>';

  // Sparkline
  var hist=d.pf_hist, mn=Math.min.apply(null,hist), mx=Math.max.apply(null,hist), rng=mx-mn||1;
  document.getElementById('spark').innerHTML=hist.map(function(v){
    var h=Math.max(3,Math.round(((v-mn)/rng)*48));
    return '<div class="spark-bar" style="height:'+h+'px;background:'+(v>=1000?'#00e676':'#ff4444')+'"></div>';
  }).join('');
  document.getElementById('pfnow').innerHTML='Ahora: <b style="color:'+(d.pl>=0?'#00e676':'#ff4444')+'">€'+d.portfolio.toFixed(2)+'</b>';
  document.getElementById('pfpct').innerHTML='<b style="color:'+(d.pl>=0?'#00e676':'#ff4444')+'">'+fpc(d.pl_pct)+'</b>';

  // Posiciones
  function renderPos(arr, isCore){
    if(!arr.length) return '<div class="empty">Sin posiciones abiertas</div>';
    return arr.map(function(p){
      var bc=isCore?'#1e3a5f':'#3a2a00', lc=isCore?'#00d4ff':'#ffd700', lt=isCore?'CORE':'SAT';
      var plBar=Math.max(0,Math.min(100,50+p.gain_pct*2));
      var plGrad=p.gain_pct>=0?'linear-gradient(90deg,#005f2d,#00e676)':'linear-gradient(90deg,#ff4444,#880000)';
      return '<div class="pos-card'+(isCore?'':' sat')+'" style="border-color:'+bc+'">'+
        '<div class="pos-row1">'+
          '<span style="color:'+lc+';font-size:10px;font-weight:700">'+lt+'</span>'+
          '<div style="display:flex;align-items:center;gap:8px">'+
            '<span style="color:'+clr(p.pl_eur)+';font-weight:700;font-size:13px">'+(p.pl_eur>=0?'+':'')+'€'+p.pl_eur.toFixed(2)+'</span>'+
            '<span style="color:'+clr(p.gain_pct)+';font-size:11px;font-weight:700">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(1)+'%</span>'+
          '</div>'+
        '</div>'+
        '<div class="pos-name">'+p.q+'</div>'+
        '<div class="pos-amounts">'+
          '<div class="amount-box"><div class="albl">Invertido</div><div class="aval" style="color:#fff">€'+p.invested.toFixed(2)+'</div></div>'+
          '<div class="amount-box"><div class="albl">Valor actual</div><div class="aval" style="color:'+clr(p.gain_pct)+'">€'+p.value_now.toFixed(2)+'</div></div>'+
          '<div class="amount-box"><div class="albl">P&L €</div><div class="aval" style="color:'+clr(p.pl_eur)+'">'+(p.pl_eur>=0?'+':'')+'€'+p.pl_eur.toFixed(2)+'</div></div>'+
        '</div>'+
        '<div class="pl-bar"><div class="pl-bar-fill" style="width:'+plBar+'%;background:'+plGrad+'"></div></div>'+
        '<div class="pos-tech">'+
          '<span>Ent: <b style="color:#fff">'+p.ep+'%</b></span>'+
          '<span>Act: <b style="color:'+clr(p.gain_pct)+'">'+p.cp+'%</b></span>'+
          '<span>TP: <b style="color:#00e676">'+p.tp+'%</b></span>'+
          '<span>SL: <b style="color:#ff4444">'+p.sl+'%</b></span>'+
          '<span>'+phDot(p.phase)+phLabel(p.phase)+'</span>'+
        '</div>'+
      '</div>';
    }).join('');
  }
  document.getElementById('pos-core').innerHTML=renderPos(d.pos_core, true);
  document.getElementById('pos-sat').innerHTML=renderPos(d.pos_sat, false);

  // Historial
  if(!d.hist.length){ document.getElementById('hist').innerHTML='<div class="empty">Sin historial</div>'; }
  else document.getElementById('hist').innerHTML=d.hist.map(function(h){
    var lc=h.is_sat?'#ffd700':'#00d4ff';
    var inv=h.invested?' (€'+h.invested.toFixed(2)+')':'';
    return '<div class="hist-row" style="background:'+(h.is_sat?'#3a2a00':'#1a2d4a')+'22">'+
      '<span style="color:'+lc+';min-width:40px">'+h.rsn+'</span>'+
      '<span style="flex:1;color:#aaa;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">'+h.q+'</span>'+
      '<span style="color:#7a8fa8;min-width:52px;text-align:right">'+inv+'</span>'+
      '<span style="color:'+clr(h.pnl)+';font-weight:700;min-width:60px;text-align:right">'+(h.pnl>=0?'+':'')+'€'+h.pnl.toFixed(2)+'</span>'+
    '</div>';
  }).join('');

  // Señales
  function renderSigs(sigs){
    if(!sigs.length) return '<div class="empty">Sin señales</div>';
    return sigs.slice(0,4).map(function(s,i){
      return '<div class="sig-row">'+
        '<span style="color:#7a8fa8;min-width:14px">'+(i+1)+'</span>'+
        '<span style="flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">'+s.q+'</span>'+
        '<span style="color:#00d4ff;min-width:34px;text-align:right">'+Math.round(s.prob*100)+'%</span>'+
        '<span style="color:#00e676;min-width:40px;text-align:right">+'+s.ev.toFixed(1)+'%</span>'+
        '<span style="color:#ffd700;min-width:36px;text-align:right">'+s.score.toFixed(3)+'</span>'+
      '</div>';
    }).join('');
  }
  document.getElementById('sigs-core').innerHTML=renderSigs(d.sigs_core);
  document.getElementById('sigs-sat').innerHTML=renderSigs(d.sigs_sat);
}

function refresh(){
  fetch('/api/state').then(function(r){return r.json();}).then(render).catch(console.error);
}
refresh();
setInterval(refresh, 6000); // actualizar UI cada 6 segundos
</script>
</body>
</html>"""

# ──────────────────────────────────────────
#  ARRANQUE
# ──────────────────────────────────────────
# Iniciar threads de fondo al importar (gunicorn no ejecuta __main__)
threading.Thread(target=api_loop,    daemon=True).start()
threading.Thread(target=jitter_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
