import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import time
import csv
import os
import requests
import asyncio

# ─── CONFIG ───────────────────────────────────────
TELEGRAM_TOKEN   = "8770681685:AAEKOng9yd68h4g_5K0Hyyg2AKSxQ6Gdol4"
TELEGRAM_CHAT_ID = "1349731370"
WINDOWS_BRIDGE   = "http://192.168.2.106:9999"
PAPER_TRADE_LOG  = os.path.expanduser("~/trading_bot/trades_log.csv")
REFRESH_MINUTES  = 15

print("=" * 50)
print("🥇 GOLD SMC BOT — Guardeer ICT Strategy")
print("   Mac → Windows MT5 | Telegram Active")
print("=" * 50)

last_analysis = {}

# ─── TELEGRAM ─────────────────────────────────────
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except Exception as e:
        print(f"⚠️ Telegram error: {e}")

def check_telegram_commands():
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        r = requests.get(url, timeout=5)
        data = r.json()
        if not data.get("result"):
            return
        updates = data["result"]
        if not updates:
            return
        last_id = updates[-1]["update_id"]
        # Clear updates
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_id+1}", timeout=5)
        for update in updates:
            msg = update.get("message", {})
            text = msg.get("text", "")
            if text == "/check":
                if last_analysis:
                    send_telegram(format_analysis(last_analysis))
                else:
                    send_telegram("⏳ Bot is warming up, try again in 1 minute.")
            elif text == "/status":
                send_telegram(get_trade_summary())
            elif text == "/start":
                send_telegram(
                    "🥇 Gold SMC Bot Commands:\n\n"
                    "/check — Live market analysis\n"
                    "/status — Trade summary\n\n"
                    "Auto alerts fire when signal is detected!"
                )
    except Exception as e:
        print(f"⚠️ Command check error: {e}")

# ─── WINDOWS BRIDGE ───────────────────────────────
def send_to_windows(signal_type, sl, tp):
    try:
        payload = {"signal": signal_type, "sl": sl, "tp": tp}
        r = requests.post(WINDOWS_BRIDGE, json=payload, timeout=5)
        print(f"📨 Sent to Windows MT5: {r.json()['status']}")
    except Exception as e:
        print(f"⚠️ Bridge error: {e}")

# ─── TRADE LOGGER ─────────────────────────────────
def log_trade(signal_type, sl, tp, price):
    file_exists = os.path.exists(PAPER_TRADE_LOG)
    with open(PAPER_TRADE_LOG, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["DateTime","Signal","Price","SL","TP","Result"])
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"),
                         signal_type, price, sl, tp, "OPEN"])
    print(f"📝 Trade logged")

def get_trade_summary():
    if not os.path.exists(PAPER_TRADE_LOG):
        return "📊 No trades logged yet."
    df     = pd.read_csv(PAPER_TRADE_LOG)
    total  = len(df)
    wins   = len(df[df["Result"] == "WIN"])
    losses = len(df[df["Result"] == "LOSS"])
    open_t = len(df[df["Result"] == "OPEN"])
    return (f"📊 TRADE SUMMARY\n"
            f"Total : {total}\n"
            f"Wins  : {wins}\n"
            f"Losses: {losses}\n"
            f"Open  : {open_t}")

# ─── FETCH DATA ───────────────────────────────────
def fetch_data():
    print("\n📥 Fetching XAUUSD data...")
    daily = yf.download("GC=F", period="6mo",  interval="1d",  auto_adjust=True, progress=False)
    h1    = yf.download("GC=F", period="60d",  interval="1h",  auto_adjust=True, progress=False)
    m15   = yf.download("GC=F", period="8d",   interval="15m", auto_adjust=True, progress=False)
    print(f"✅ Daily: {len(daily)} | H1: {len(h1)} | M15: {len(m15)} candles")
    return daily, h1, m15

def flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]
    return df

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
        return f"🔴 BEARISH SWEEP above {round(recent_high,2)}", True
    elif last_low < recent_low and last_close > recent_low:
        return f"🟢 BULLISH SWEEP below {round(recent_low,2)}", True
    return "⏳ No sweep yet", False

def find_fvg(m15):
    m15   = flatten(m15)
    highs = m15["High"].values
    lows  = m15["Low"].values
    n     = len(highs)
    fvgs  = []
    for i in range(2, min(n, 30)):
        idx = n - i
        if lows[idx] > highs[idx-2]:
            fvgs.append(f"🟢 Bullish FVG: {round(float(highs[idx-2]),2)}—{round(float(lows[idx]),2)}")
        if highs[idx] < lows[idx-2]:
            fvgs.append(f"🔴 Bearish FVG: {round(float(lows[idx-2]),2)}—{round(float(highs[idx]),2)}")
    return fvgs[:2]

def generate_signal(bias, bos, sweep_detected, obs, m15):
    m15        = flatten(m15)
    last_price = float(m15["Close"].iloc[-1])
    if not sweep_detected:
        return "NO_TRADE", 0, 0, "⛔ NO TRADE — Waiting for liquidity sweep (Guardeer Rule #1)"
    if bias == "BULLISH":
        bull_obs = [o for o in obs if o["type"] == "BULLISH OB"]
        if bull_obs:
            ob    = bull_obs[0]
            entry = (ob["low"] + ob["high"]) / 2
            sl    = round(ob["low"] - 2, 2)
            risk  = entry - sl
            tp    = round(entry + risk * 2, 2)
            msg   = (f"🟢 BUY SIGNAL\n"
                     f"Entry : {ob['low']} — {ob['high']}\n"
                     f"SL    : {sl}\n"
                     f"TP    : {tp}\n"
                     f"R:R   : 1:2\n"
                     f"Price : {round(last_price,2)}")
            return "BUY", sl, tp, msg
    if bias == "BEARISH":
        bear_obs = [o for o in obs if o["type"] == "BEARISH OB"]
        if bear_obs:
            ob    = bear_obs[0]
            entry = (ob["low"] + ob["high"]) / 2
            sl    = round(ob["high"] + 2, 2)
            risk  = sl - entry
            tp    = round(entry - risk * 2, 2)
            msg   = (f"🔴 SELL SIGNAL\n"
                     f"Entry : {ob['low']} — {ob['high']}\n"
                     f"SL    : {sl}\n"
                     f"TP    : {tp}\n"
                     f"R:R   : 1:2\n"
                     f"Price : {round(last_price,2)}")
            return "SELL", sl, tp, msg
    return "NO_TRADE", 0, 0, "⏳ Conditions not aligned yet"

def run_analysis():
    global last_analysis
    daily, h1, m15 = fetch_data()
    bias, mid, high, low = get_daily_bias(daily)
    last_price = float(flatten(m15)["Close"].iloc[-1])
    bos, choch = get_market_structure(h1)
    obs = find_order_blocks(h1)
    sweep_msg, sweep_detected = detect_liquidity_sweep(m15)
    fvgs = find_fvg(m15)
    sig_type, sl, tp, signal_text = generate_signal(bias, bos, sweep_detected, obs, m15)
    last_analysis = {
        "bias": bias, "price": last_price, "mid": mid,
        "bos": bos, "choch": choch, "obs": obs,
        "sweep_msg": sweep_msg, "sweep_detected": sweep_detected,
        "fvgs": fvgs, "sig_type": sig_type, "sl": sl, "tp": tp,
        "signal_text": signal_text,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    return last_analysis

def format_analysis(a):
    obs_text = "\n".join([f"  {o['type']} | {o['low']}—{o['high']}" for o in a["obs"]]) or "  None"
    fvg_text = "\n".join(a["fvgs"]) or "  None"
    return (
        f"🥇 GOLD SMC BOT\n"
        f"🕐 {a['time']}\n"
        f"{'='*28}\n"
        f"📊 BIAS  : {a['bias']}\n"
        f"💰 Price : {round(a['price'],2)}\n"
        f"📍 Mid   : {round(a['mid'],2)}\n"
        f"📊 BOS   : {a['bos']}\n"
        f"📊 CHoCH : {a['choch']}\n"
        f"📦 OBs   :\n{obs_text}\n"
        f"🌊 Sweep : {a['sweep_msg']}\n"
        f"📈 FVGs  :\n{fvg_text}\n"
        f"{'='*28}\n"
        f"🎯 {a['signal_text']}"
    )

# ─── MAIN LOOP ────────────────────────────────────
def run_bot():
    global last_analysis
    cycle       = 1
    last_signal = "NONE"

    # Send startup message
    send_telegram("🥇 Gold SMC Bot is ONLINE!\nSend /check for live analysis\n/status for trade summary")
    print("📱 Telegram active! Send /check on your phone")

    while True:
        print(f"\n{'='*50}")
        print(f"🔄 CYCLE #{cycle} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*50}")

        try:
            # Check for Telegram commands first
            check_telegram_commands()

            a = run_analysis()
            print(f"\n📊 BIAS   : {a['bias']} | Price: {round(a['price'],2)} | Mid: {round(a['mid'],2)}")
            print(f"📊 BOS    : {a['bos']} | CHoCH: {a['choch']}")
            print(f"📊 OBs    : {len(a['obs'])} found")
            for o in a["obs"]:
                print(f"   {o['type']} | {o['low']}—{o['high']} | {o['candle_ago']} candles ago")
            print(f"📊 Sweep  : {a['sweep_msg']}")
            for f in a["fvgs"]:
                print(f"   {f}")
            print(f"\n{'='*50}")
            print(f"🎯 {a['signal_text']}")
            print(f"{'='*50}")

            if a["sig_type"] in ["BUY","SELL"] and a["sig_type"] != last_signal:
                send_to_windows(a["sig_type"], a["sl"], a["tp"])
                log_trade(a["sig_type"], a["sl"], a["tp"], a["price"])
                send_telegram(f"🚨 TRADE FIRED!\n{format_analysis(a)}")
                last_signal = a["sig_type"]
            elif a["sig_type"] == "NO_TRADE":
                last_signal = "NONE"

        except Exception as e:
            print(f"⚠️ Error: {e}")

        # Check commands every minute while waiting
        print(f"\n⏰ Next scan in {REFRESH_MINUTES} mins... (Ctrl+C to stop)")
        for _ in range(REFRESH_MINUTES):
            time.sleep(60)
            check_telegram_commands()

        cycle += 1

run_bot()
