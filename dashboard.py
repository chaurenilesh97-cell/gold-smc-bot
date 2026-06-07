from flask import Flask, jsonify, render_template_string
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import threading
import time
import csv
import os
import requests

app = Flask(__name__)

# ─── CONFIG ───────────────────────────────────────
TELEGRAM_TOKEN   = "8770681685:AAEKOng9yd68h4g_5K0Hyyg2AKSxQ6Gdol4"
TELEGRAM_CHAT_ID = "1349731370"
WINDOWS_BRIDGE   = "http://192.168.2.106:9999"
PAPER_TRADE_LOG  = os.path.expanduser("~/trading_bot/trades_log.csv")
REFRESH_MINUTES  = 15

# ─── SHARED STATE ─────────────────────────────────
state = {
    "running": False,
    "cycle": 0,
    "last_update": "Never",
    "bias": "-",
    "price": "-",
    "mid": "-",
    "bos": "-",
    "choch": "-",
    "sweep": "-",
    "obs": [],
    "fvgs": [],
    "signal": "Bot not started",
    "signal_type": "NONE",
    "trades": [],
    "wins": 0,
    "losses": 0,
    "open_trades": 0,
    "total": 0,
    "windows_status": "Unknown",
    "telegram_status": "Unknown",
}

# ─── HELPERS ──────────────────────────────────────
def flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]
    return df

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
        state["telegram_status"] = "✅ Connected"
    except:
        state["telegram_status"] = "❌ Failed"

def send_to_windows(signal_type, sl, tp):
    try:
        payload = {"signal": signal_type, "sl": sl, "tp": tp}
        r = requests.post(WINDOWS_BRIDGE, json=payload, timeout=5)
        state["windows_status"] = "✅ Connected"
        return r.json()["status"]
    except:
        state["windows_status"] = "❌ Offline"
        return "Windows offline"

def log_trade(signal_type, sl, tp, price):
    file_exists = os.path.exists(PAPER_TRADE_LOG)
    with open(PAPER_TRADE_LOG, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["DateTime","Signal","Price","SL","TP","Result"])
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"),
                         signal_type, price, sl, tp, "OPEN"])

def load_trades():
    if not os.path.exists(PAPER_TRADE_LOG):
        return [], 0, 0, 0, 0
    df = pd.read_csv(PAPER_TRADE_LOG)
    trades = df.tail(10).to_dict("records")
    wins   = len(df[df["Result"] == "WIN"])
    losses = len(df[df["Result"] == "LOSS"])
    open_t = len(df[df["Result"] == "OPEN"])
    return trades, wins, losses, open_t, len(df)

def fetch_data():
    daily = yf.download("GC=F", period="6mo",  interval="1d",  auto_adjust=True, progress=False)
    h1    = yf.download("GC=F", period="60d",  interval="1h",  auto_adjust=True, progress=False)
    m15   = yf.download("GC=F", period="8d",   interval="15m", auto_adjust=True, progress=False)
    return daily, h1, m15

def get_daily_bias(daily):
    daily  = flatten(daily)
    recent = daily.tail(20)
    high   = float(recent["High"].max())
    low    = float(recent["Low"].min())
    mid    = (high + low) / 2
    last   = float(daily["Close"].iloc[-1])
    bias   = "BEARISH" if last > mid else "BULLISH"
    return bias, mid, high, low

def get_market_structure(h1):
    h1     = flatten(h1)
    closes = h1["Close"].values
    n      = len(closes)
    if n < 10:
        return "NONE", "NONE"
    window     = min(50, n)
    swing_high = float(np.max(closes[-window:]))
    swing_low  = float(np.min(closes[-window:]))
    last       = float(closes[-1])
    prev_high  = float(np.max(closes[-window:-5]))
    prev_low   = float(np.min(closes[-window:-5]))
    bos = "NONE"
    choch = "NONE"
    if last > swing_high:
        bos = "BULLISH BOS"
    elif last < swing_low:
        bos = "BEARISH BOS"
    if last > prev_high and closes[-5] < prev_low:
        choch = "CHoCH UP"
    elif last < prev_low and closes[-5] > prev_high:
        choch = "CHoCH DOWN"
    return bos, choch

def find_order_blocks(h1):
    h1     = flatten(h1)
    closes = h1["Close"].values
    opens  = h1["Open"].values
    highs  = h1["High"].values
    lows   = h1["Low"].values
    n      = len(closes)
    obs    = []
    for i in range(1, min(n-1, 30)):
        idx = n - 1 - i
        if closes[idx] < opens[idx] and closes[idx+1] > highs[idx]:
            obs.append({"type":"BULLISH OB","high":round(float(highs[idx]),2),
                        "low":round(float(lows[idx]),2),"candle_ago":i})
        if closes[idx] > opens[idx] and closes[idx+1] < lows[idx]:
            obs.append({"type":"BEARISH OB","high":round(float(highs[idx]),2),
                        "low":round(float(lows[idx]),2),"candle_ago":i})
    return obs[:3]

def detect_liquidity_sweep(m15):
    m15        = flatten(m15)
    highs      = m15["High"].values
    lows       = m15["Low"].values
    closes     = m15["Close"].values
    n          = len(closes)
    if n < 10:
        return "No sweep", False
    recent_high = float(np.max(highs[-20:-3]))
    recent_low  = float(np.min(lows[-20:-3]))
    last_high   = float(highs[-1])
    last_low    = float(lows[-1])
    last_close  = float(closes[-1])
    if last_high > recent_high and last_close < recent_high:
        return f"BEARISH SWEEP above {round(recent_high,2)}", True
    elif last_low < recent_low and last_close > recent_low:
        return f"BULLISH SWEEP below {round(recent_low,2)}", True
    return "No sweep yet", False

def find_fvg(m15):
    m15   = flatten(m15)
    highs = m15["High"].values
    lows  = m15["Low"].values
    n     = len(highs)
    fvgs  = []
    for i in range(2, min(n, 30)):
        idx = n - i
        if lows[idx] > highs[idx-2]:
            fvgs.append(f"Bullish FVG: {round(float(highs[idx-2]),2)}—{round(float(lows[idx]),2)}")
        if highs[idx] < lows[idx-2]:
            fvgs.append(f"Bearish FVG: {round(float(lows[idx-2]),2)}—{round(float(highs[idx]),2)}")
    return fvgs[:2]

def generate_signal(bias, bos, sweep_detected, obs, m15):
    m15        = flatten(m15)
    last_price = float(m15["Close"].iloc[-1])
    if not sweep_detected:
        return "NO_TRADE", 0, 0, "Waiting for liquidity sweep (Guardeer Rule #1)"
    if bias == "BULLISH":
        bull_obs = [o for o in obs if o["type"] == "BULLISH OB"]
        if bull_obs:
            ob    = bull_obs[0]
            entry = (ob["low"] + ob["high"]) / 2
            sl    = round(ob["low"] - 2, 2)
            risk  = entry - sl
            tp    = round(entry + risk * 2, 2)
            return "BUY", sl, tp, f"BUY | Entry:{ob['low']}—{ob['high']} | SL:{sl} | TP:{tp} | R:R 1:2"
    if bias == "BEARISH":
        bear_obs = [o for o in obs if o["type"] == "BEARISH OB"]
        if bear_obs:
            ob    = bear_obs[0]
            entry = (ob["low"] + ob["high"]) / 2
            sl    = round(ob["high"] + 2, 2)
            risk  = sl - entry
            tp    = round(entry - risk * 2, 2)
            return "SELL", sl, tp, f"SELL | Entry:{ob['low']}—{ob['high']} | SL:{sl} | TP:{tp} | R:R 1:2"
    return "NO_TRADE", 0, 0, "Conditions not aligned yet"

def check_telegram_commands():
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        r = requests.get(url, timeout=5)
        updates = r.json().get("result", [])
        if not updates:
            return
        last_id = updates[-1]["update_id"]
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_id+1}", timeout=5)
        for update in updates:
            text = update.get("message", {}).get("text", "")
            if text == "/check":
                msg = (f"🥇 GOLD SMC BOT\n"
                       f"🕐 {state['last_update']}\n"
                       f"BIAS: {state['bias']} | Price: {state['price']}\n"
                       f"BOS: {state['bos']} | CHoCH: {state['choch']}\n"
                       f"Sweep: {state['sweep']}\n"
                       f"Signal: {state['signal']}")
                send_telegram(msg)
            elif text == "/status":
                send_telegram(f"📊 Trades: {state['total']} | Wins: {state['wins']} | Losses: {state['losses']} | Open: {state['open_trades']}")
            elif text == "/start":
                send_telegram("🥇 Commands:\n/check — Live analysis\n/status — Trade summary")
    except:
        pass

# ─── BOT LOOP ─────────────────────────────────────
last_signal = "NONE"

def bot_loop():
    global last_signal
    while state["running"]:
        try:
            check_telegram_commands()
            daily, h1, m15 = fetch_data()
            bias, mid, high, low = get_daily_bias(daily)
            last_price = float(flatten(m15)["Close"].iloc[-1])
            bos, choch = get_market_structure(h1)
            obs        = find_order_blocks(h1)
            sweep_msg, sweep_detected = detect_liquidity_sweep(m15)
            fvgs       = find_fvg(m15)
            sig_type, sl, tp, signal_text = generate_signal(bias, bos, sweep_detected, obs, m15)

            trades, wins, losses, open_t, total = load_trades()

            state.update({
                "cycle"       : state["cycle"] + 1,
                "last_update" : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "bias"        : bias,
                "price"       : round(last_price, 2),
                "mid"         : round(mid, 2),
                "bos"         : bos,
                "choch"       : choch,
                "sweep"       : sweep_msg,
                "obs"         : obs,
                "fvgs"        : fvgs,
                "signal"      : signal_text,
                "signal_type" : sig_type,
                "trades"      : trades,
                "wins"        : wins,
                "losses"      : losses,
                "open_trades" : open_t,
                "total"       : total,
            })

            if sig_type in ["BUY","SELL"] and sig_type != last_signal:
                result = send_to_windows(sig_type, sl, tp)
                log_trade(sig_type, sl, tp, last_price)
                send_telegram(f"🚨 TRADE FIRED!\n{signal_text}\nPrice:{last_price}\nSL:{sl} TP:{tp}")
                last_signal = sig_type
            elif sig_type == "NO_TRADE":
                last_signal = "NONE"

        except Exception as e:
            state["signal"] = f"Error: {e}"

        for _ in range(REFRESH_MINUTES * 6):
            if not state["running"]:
                break
            time.sleep(10)
            check_telegram_commands()

# ─── FLASK ROUTES ─────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/state")
def api_state():
    return jsonify(state)

@app.route("/api/start")
def api_start():
    if not state["running"]:
        state["running"] = True
        t = threading.Thread(target=bot_loop, daemon=True)
        t.start()
        send_telegram("🥇 Gold SMC Bot STARTED from dashboard!")
    return jsonify({"status": "started"})

@app.route("/api/stop")
def api_stop():
    state["running"] = False
    send_telegram("🛑 Gold SMC Bot STOPPED from dashboard.")
    return jsonify({"status": "stopped"})

@app.route("/api/check_now")
def api_check_now():
    if not state["running"]:
        return jsonify({"status": "Bot not running"})
    return jsonify({"status": "Analysis running, refresh in 30 seconds"})

# ─── HTML DASHBOARD ───────────────────────────────
HTML = """
<!DOCTYPE html>
<html>
<head>
<title>🥇 Gold SMC Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0a0a0f; color:#e0e0e0; font-family:'Segoe UI',sans-serif; }
  .header { background:linear-gradient(135deg,#1a1a2e,#16213e); padding:20px 30px; border-bottom:2px solid #f0b90b; display:flex; align-items:center; justify-content:space-between; }
  .header h1 { font-size:24px; color:#f0b90b; }
  .header .subtitle { color:#888; font-size:13px; margin-top:4px; }
  .controls { display:flex; gap:10px; }
  .btn { padding:10px 24px; border:none; border-radius:8px; cursor:pointer; font-size:14px; font-weight:600; transition:all 0.2s; }
  .btn-start { background:#00c851; color:#000; }
  .btn-stop  { background:#ff4444; color:#fff; }
  .btn-start:hover { background:#00a843; }
  .btn-stop:hover  { background:#cc0000; }
  .btn:disabled { opacity:0.4; cursor:not-allowed; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; padding:20px; }
  .card { background:#12121c; border:1px solid #222; border-radius:12px; padding:20px; }
  .card h3 { color:#f0b90b; font-size:13px; text-transform:uppercase; letter-spacing:1px; margin-bottom:16px; }
  .stat { display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid #1a1a2e; }
  .stat:last-child { border-bottom:none; }
  .stat .label { color:#888; font-size:13px; }
  .stat .value { font-size:14px; font-weight:600; }
  .bullish { color:#00c851; }
  .bearish { color:#ff4444; }
  .neutral { color:#888; }
  .signal-box { padding:16px; border-radius:10px; text-align:center; font-size:16px; font-weight:700; margin-top:8px; }
  .signal-buy  { background:rgba(0,200,81,0.15); border:2px solid #00c851; color:#00c851; }
  .signal-sell { background:rgba(255,68,68,0.15); border:2px solid #ff4444; color:#ff4444; }
  .signal-wait { background:rgba(240,185,11,0.1); border:2px solid #f0b90b; color:#f0b90b; }
  .ob-item { padding:8px 12px; margin:6px 0; border-radius:6px; font-size:13px; display:flex; justify-content:space-between; }
  .ob-bull { background:rgba(0,200,81,0.1); border-left:3px solid #00c851; }
  .ob-bear { background:rgba(255,68,68,0.1); border-left:3px solid #ff4444; }
  .stats-row { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; padding:0 20px 20px; }
  .stat-box { background:#12121c; border:1px solid #222; border-radius:10px; padding:16px; text-align:center; }
  .stat-box .num { font-size:28px; font-weight:700; color:#f0b90b; }
  .stat-box .lbl { font-size:12px; color:#888; margin-top:4px; }
  .status-row { display:flex; gap:12px; padding:0 20px 16px; flex-wrap:wrap; }
  .status-pill { padding:6px 14px; border-radius:20px; font-size:12px; font-weight:600; background:#1a1a2e; }
  .cycle-badge { background:#f0b90b; color:#000; padding:4px 12px; border-radius:20px; font-size:12px; font-weight:700; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th { color:#888; padding:8px; text-align:left; border-bottom:1px solid #222; }
  td { padding:8px; border-bottom:1px solid #1a1a2e; }
  .updating { animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
  .last-update { color:#888; font-size:12px; padding:0 20px 10px; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>🥇 Gold SMC Bot — Guardeer Strategy</h1>
    <div class="subtitle">XAUUSD Auto Trading | Mac → Windows MT5</div>
  </div>
  <div class="controls">
    <button class="btn btn-start" id="btnStart" onclick="startBot()">▶ Start Bot</button>
    <button class="btn btn-stop"  id="btnStop"  onclick="stopBot()" disabled>⏹ Stop Bot</button>
  </div>
</div>

<div class="status-row" style="padding-top:16px">
  <div class="status-pill" id="statusBot">⚪ Bot: Stopped</div>
  <div class="status-pill" id="statusWin">🖥 Windows: —</div>
  <div class="status-pill" id="statusTg">📱 Telegram: —</div>
  <div class="cycle-badge" id="cycleNum">Cycle #0</div>
</div>
<div class="last-update" id="lastUpdate">Last update: Never</div>

<div class="stats-row">
  <div class="stat-box"><div class="num" id="statTotal">0</div><div class="lbl">Total Trades</div></div>
  <div class="stat-box"><div class="num" style="color:#00c851" id="statWins">0</div><div class="lbl">Wins</div></div>
  <div class="stat-box"><div class="num" style="color:#ff4444" id="statLosses">0</div><div class="lbl">Losses</div></div>
  <div class="stat-box"><div class="num" style="color:#888" id="statOpen">0</div><div class="lbl">Open</div></div>
</div>

<div class="grid">
  <div class="card">
    <h3>📊 Market Analysis</h3>
    <div class="stat"><span class="label">Price (XAUUSD)</span><span class="value" id="price">—</span></div>
    <div class="stat"><span class="label">Daily Bias</span><span class="value" id="bias">—</span></div>
    <div class="stat"><span class="label">Midpoint</span><span class="value" id="mid">—</span></div>
    <div class="stat"><span class="label">BOS</span><span class="value" id="bos">—</span></div>
    <div class="stat"><span class="label">CHoCH</span><span class="value" id="choch">—</span></div>
    <div class="stat"><span class="label">Liquidity Sweep</span><span class="value" id="sweep">—</span></div>
  </div>

  <div class="card">
    <h3>🎯 Trade Signal</h3>
    <div class="signal-box signal-wait" id="signalBox">Bot not started</div>
    <div style="margin-top:16px">
      <h3 style="margin-bottom:8px">📈 Fair Value Gaps</h3>
      <div id="fvgList"><span style="color:#888;font-size:13px">—</span></div>
    </div>
  </div>

  <div class="card">
    <h3>📦 Order Blocks (H1)</h3>
    <div id="obList"><span style="color:#888;font-size:13px">No data yet</span></div>
  </div>

  <div class="card">
    <h3>📋 Recent Trades</h3>
    <div id="tradeTable"><span style="color:#888;font-size:13px">No trades yet</span></div>
  </div>
</div>

<script>
function startBot() {
  fetch('/api/start').then(r => r.json()).then(d => {
    document.getElementById('btnStart').disabled = true;
    document.getElementById('btnStop').disabled = false;
    document.getElementById('statusBot').textContent = '🟢 Bot: Running';
  });
}

function stopBot() {
  fetch('/api/stop').then(r => r.json()).then(d => {
    document.getElementById('btnStart').disabled = false;
    document.getElementById('btnStop').disabled = true;
    document.getElementById('statusBot').textContent = '🔴 Bot: Stopped';
  });
}

function colorBias(val) {
  if (val === 'BULLISH') return '<span class="bullish">🟢 BULLISH</span>';
  if (val === 'BEARISH') return '<span class="bearish">🔴 BEARISH</span>';
  return val;
}

function updateDashboard() {
  fetch('/api/state').then(r => r.json()).then(s => {
    document.getElementById('price').textContent   = s.price || '—';
    document.getElementById('bias').innerHTML      = colorBias(s.bias);
    document.getElementById('mid').textContent     = s.mid || '—';
    document.getElementById('bos').textContent     = s.bos || '—';
    document.getElementById('choch').textContent   = s.choch || '—';
    document.getElementById('sweep').textContent   = s.sweep || '—';
    document.getElementById('lastUpdate').textContent = 'Last update: ' + (s.last_update || 'Never');
    document.getElementById('cycleNum').textContent = 'Cycle #' + s.cycle;
    document.getElementById('statusWin').textContent = '🖥 Windows: ' + (s.windows_status || '—');
    document.getElementById('statusTg').textContent  = '📱 Telegram: ' + (s.telegram_status || '—');
    document.getElementById('statTotal').textContent   = s.total;
    document.getElementById('statWins').textContent    = s.wins;
    document.getElementById('statLosses').textContent  = s.losses;
    document.getElementById('statOpen').textContent    = s.open_trades;

    // Signal box
    const box = document.getElementById('signalBox');
    box.textContent = s.signal;
    box.className = 'signal-box';
    if (s.signal_type === 'BUY')  box.classList.add('signal-buy');
    else if (s.signal_type === 'SELL') box.classList.add('signal-sell');
    else box.classList.add('signal-wait');

    // Order blocks
    const obDiv = document.getElementById('obList');
    if (s.obs && s.obs.length > 0) {
      obDiv.innerHTML = s.obs.map(o =>
        `<div class="ob-item ${o.type.includes('BULL') ? 'ob-bull' : 'ob-bear'}">
          <span>${o.type}</span>
          <span>${o.low} — ${o.high}</span>
          <span style="color:#888">${o.candle_ago}c ago</span>
        </div>`
      ).join('');
    } else {
      obDiv.innerHTML = '<span style="color:#888;font-size:13px">No order blocks found</span>';
    }

    // FVGs
    const fvgDiv = document.getElementById('fvgList');
    if (s.fvgs && s.fvgs.length > 0) {
      fvgDiv.innerHTML = s.fvgs.map(f =>
        `<div style="font-size:13px;padding:4px 0;color:${f.includes('Bull') ? '#00c851' : '#ff4444'}">${f}</div>`
      ).join('');
    } else {
      fvgDiv.innerHTML = '<span style="color:#888;font-size:13px">No FVGs</span>';
    }

    // Trades table
    const tradeDiv = document.getElementById('tradeTable');
    if (s.trades && s.trades.length > 0) {
      tradeDiv.innerHTML = `<table>
        <tr><th>Time</th><th>Signal</th><th>Price</th><th>SL</th><th>TP</th><th>Result</th></tr>
        ${s.trades.map(t => `<tr>
          <td>${t.DateTime}</td>
          <td style="color:${t.Signal==='BUY'?'#00c851':'#ff4444'}">${t.Signal}</td>
          <td>${t.Price}</td>
          <td>${t.SL}</td>
          <td>${t.TP}</td>
          <td style="color:${t.Result==='WIN'?'#00c851':t.Result==='LOSS'?'#ff4444':'#888'}">${t.Result}</td>
        </tr>`).join('')}
      </table>`;
    }

    // Update buttons
    if (s.running) {
      document.getElementById('btnStart').disabled = true;
      document.getElementById('btnStop').disabled = false;
      document.getElementById('statusBot').textContent = '🟢 Bot: Running';
    }
  });
}

// Refresh every 10 seconds
updateDashboard();
setInterval(updateDashboard, 10000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("=" * 50)
    print("🥇 GOLD SMC BOT — Web Dashboard")
    print("   Open browser: http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=8080, debug=False)
