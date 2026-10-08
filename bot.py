import pandas as pd
import requests
import os
import json
from datetime import datetime, timezone

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "XAU/USD"
RISK_PER_TRADE = 0.01
REWARD_RISK = 2.0
SPREAD = 0.35
CAPITAL = 10000
POSITION_FILE = "position.json"

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        print(f"Telegram sent: {msg[:100]}")
    except Exception as e:
        print(f"Telegram error: {e}")

def fetch(symbol, interval):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": 500, "apikey": TWELVEDATA_API_KEY, "format": "JSON"}
    r = requests.get(url, params=params, timeout=15)
    data = r.json()
    if "values" not in data:
        raise Exception(f"TwelveData error: {data}")
    df = pd.DataFrame(data["values"])
    df = df.iloc[::-1].reset_index(drop=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    df = df.rename(columns={"open":"Open","high":"High","low":"Low","close":"Close"})
    for c in ["Open","High","Low","Close"]:
        df[c] = df[c].astype(float)
    return df

def load_position():
    if os.path.exists(POSITION_FILE):
        with open(POSITION_FILE, "r") as f:
            return json.load(f)
    return {"position": 0, "entry": 0, "sl": 0, "tp": 0, "capital": CAPITAL}

def save_position(state):
    with open(POSITION_FILE, "w") as f:
        json.dump(state, f)

state = load_position()
position = state["position"]
entry = state["entry"]
sl = state["sl"]
tp = state["tp"]
capital = state["capital"]

try:
    df_1h = fetch(SYMBOL, "1h")
    df_5m = fetch(SYMBOL, "5min")

    df_1h["EMA50"] = df_1h["Close"].ewm(span=50).mean()
    df_1h["EMA200"] = df_1h["Close"].ewm(span=200).mean()
    df_5m["ATR"] = (df_5m["High"] - df_5m["Low"]).rolling(14).mean()
    df_5m["TradeDate"] = df_5m.index.date
    df_5m["Hour"] = df_5m.index.hour

    daily = df_5m.groupby("TradeDate").agg({"High":"max","Low":"min"}).shift(1)
    daily.columns = ["PDH","PDL"]
    asia = df_5m[(df_5m["Hour"] >= 0) & (df_5m["Hour"] < 6)].groupby("TradeDate").agg({"High":"max","Low":"min"})
    asia.columns = ["AsiaHigh","AsiaLow"]
    df_5m = df_5m.join(daily, on="TradeDate")
    df_5m = df_5m.join(asia, on="TradeDate")
    df_5m.dropna(inplace=True)

    last = df_5m.iloc[-1]
    time_now = df_5m.index[-1]
    price = float(last["Close"])
    atr = float(last["ATR"])

    b = df_1h.iloc[-1]
    bullish_bias = float(b["Close"]) > float(b["EMA50"]) > float(b["EMA200"])
    bearish_bias = float(b["Close"]) < float(b["EMA50"]) < float(b["EMA200"])
    bias_txt = "BULL" if bullish_bias else "BEAR" if bearish_bias else "NEUTRAL"

    now_utc = datetime.now(timezone.utc)
    is_eod = time_now.hour == 19 and time_now.minute >= 55

    msg = ""
    # EOD Close
    if position != 0 and is_eod:
        pnl = (price - entry) * position - SPREAD * abs(position) if position > 0 else (entry - price) * abs(position) - SPREAD * abs(position)
        capital += pnl
        msg = f"EOD CLOSE {time_now.strftime(\'%H:%M UTC\')} Price {price:.2f} PnL {pnl:+.2f} Cap {capital:.2f}"
        position = 0
        entry = 0
        sl = 0
        tp = 0
    # Check SL/TP
    elif position != 0:
        if position > 0 and (price <= sl or price >= tp):
            pnl = (price - entry) * position - SPREAD * abs(position)
            capital += pnl
            msg = f"EXIT LONG {price:.2f} PnL {pnl:+.2f} Cap {capital:.2f} | SL {sl:.2f} TP {tp:.2f}"
            position = 0
        elif position < 0 and (price >= sl or price <= tp):
            pnl = (entry - price) * abs(position) - SPREAD * abs(position)
            capital += pnl
            msg = f"EXIT SHORT {price:.2f} PnL {pnl:+.2f} Cap {capital:.2f} | SL {sl:.2f} TP {tp:.2f}"
            position = 0
        else:
            msg = f"HOLD IN TRADE {bias_txt} Price {price:.2f} Entry {entry:.2f} SL {sl:.2f} TP {tp:.2f} Cap {capital:.2f}"
    # Look for new entry
    elif position == 0:
        recent = df_5m.iloc[-30:]
        swept_low = any((float(r["Low"]) < float(r["PDL"])*0.9995 or float(r["Low"]) < float(r["AsiaLow"])*0.9995) and float(r["Close"]) > min(float(r["PDL"]), float(r["AsiaLow"])) for _, r in recent.iterrows())
        swept_high = any((float(r["High"]) > float(r["PDH"])*1.0005 or float(r["High"]) > float(r["AsiaHigh"])*1.0005) and float(r["Close"]) < max(float(r["PDH"]), float(r["AsiaHigh"])) for _, r in recent.iterrows())
        is_bull_fvg = float(df_5m["Low"].iloc[-1]) > float(df_5m["High"].iloc[-3])
        is_bear_fvg = float(df_5m["High"].iloc[-1]) < float(df_5m["Low"].iloc[-3])

        def get_ob(direction):
            for k in range(len(df_5m)-2, max(len(df_5m)-7, -1), -1):
                o = float(df_5m["Open"].iloc[k])
                c = float(df_5m["Close"].iloc[k])
                if direction == "bull" and c < o:
                    return float(df_5m["Low"].iloc[k]), float(df_5m["High"].iloc[k])
                if direction == "bear" and c > o:
                    return float(df_5m["Low"].iloc[k]), float(df_5m["High"].iloc[k])
            return None

        entered = False
        if bullish_bias and swept_low:
            poi = None
            poi_type = ""
            if is_bull_fvg and float(df_5m["High"].iloc[-3]) <= price <= float(df_5m["Low"].iloc[-1]):
                poi = (float(df_5m["High"].iloc[-3]), float(df_5m["Low"].iloc[-1]))
                poi_type = "FVG"
            else:
                ob = get_ob("bull")
                if ob and ob[0] <= price <= ob[1]:
                    poi = ob
                    poi_type = "OB"
            if poi:
                sl = poi[0] - 0.3*atr
                risk = price - sl
                if 0 < risk < price*0.01:
                    qty = (capital * RISK_PER_TRADE) / risk
                    tp = price + risk * REWARD_RISK
                    position = qty
                    entry = price
                    msg = f"BUY SIGNAL {poi_type} {poi[0]:.2f}-{poi[1]:.2f} Price {price:.2f} SL {sl:.2f} TP {tp:.2f} Bias {bias_txt}"
                    entered = True

        if not entered and bearish_bias and swept_high:
            poi = None
            poi_type = ""
            if is_bear_fvg and float(df_5m["Low"].iloc[-3]) <= price <= float(df_5m["High"].iloc[-1]):
                poi = (float(df_5m["Low"].iloc[-3]), float(df_5m["High"].iloc[-1]))
                poi_type = "FVG"
            else:
                ob = get_ob("bear")
                if ob and ob[0] <= price <= ob[1]:
                    poi = ob
                    poi_type = "OB"
            if poi:
                sl = poi[1] + 0.3*atr
                risk = sl - price
                if 0 < risk < price*0.01:
                    qty = (capital * RISK_PER_TRADE) / risk
                    tp = price - risk * REWARD_RISK
                    position = -qty
                    entry = price
                    msg = f"SELL SIGNAL {poi_type} {poi[0]:.2f}-{poi[1]:.2f} Price {price:.2f} SL {sl:.2f} TP {tp:.2f} Bias {bias_txt}"
                    entered = True

        if not entered:
            msg = f"HOLD {bias_txt} Price {price:.2f} PDH {float(last[\'PDH\']):.2f} PDL {float(last[\'PDL\']):.2f} ATR {atr:.2f} Cap {capital:.2f}"

    print(msg)
    # Only send Telegram for signals/exits or every 30 mins
    if "SIGNAL" in msg or "EXIT" in msg or "EOD" in msg or now_utc.minute % 30 == 0:
        send_telegram(f"[XAUUSD Spot] {msg}")

    save_position({"position": position, "entry": entry, "sl": sl, "tp": tp, "capital": capital})

except Exception as e:
    print(f"Error: {e}")
    send_telegram(f"Bot Error: {e}")
