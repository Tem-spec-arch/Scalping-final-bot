import time
import pandas as pd
from datetime import datetime
from pybit.unified_trading import HTTP

API_KEY = "YOUR_API_KEY"
API_SECRET = "YOUR_API_SECRET"

session = HTTP(api_key=API_KEY, api_secret=API_SECRET)

PAIRS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","TRXUSDT","MATICUSDT","LTCUSDT",
    "AVAXUSDT","LINKUSDT","DOTUSDT","UNIUSDT","ATOMUSDT",
    "ETCUSDT","XLMUSDT","FILUSDT","NEARUSDT","ICPUSDT",
    "APTUSDT","OPUSDT","ARBUSDT","SANDUSDT","MANAUSDT",
    "AAVEUSDT","EOSUSDT","THETAUSDT","ALGOUSDT","VETUSDT"
]

BATCH_SIZE = 4
TIMEFRAME = "3"
LEVERAGE = 70
TRADE_SIZE = 8
MAX_TRADES = 10
DAILY_DD_LIMIT = 0.40

start_balance = None
current_day = datetime.utcnow().day
trading_paused = False
last_trade_time = {}
pair_index = 0

# === INSTRUMENT INFO (Fix 1 & 2) ===
def get_instruments():
    data = session.get_instruments_info(category="linear")
    info = {}
    for item in data["result"]["list"]:
        info[item["symbol"]] = {
            "tick": float(item["priceFilter"]["tickSize"]),
            "step": float(item["lotSizeFilter"]["qtyStep"]),
            "min_qty": float(item["lotSizeFilter"]["minOrderQty"])
        }
    return info

INSTR = get_instruments()

def round_price(symbol, price):
    tick = INSTR[symbol]["tick"]
    return round(price / tick) * tick

def round_qty(symbol, qty):
    step = INSTR[symbol]["step"]
    return round(qty / step) * step

# === SAFE API CALL (Fix 3) ===
def safe_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except:
        return None

def get_balance():
    res = safe_call(session.get_wallet_balance, accountType="UNIFIED")
    if res:
        return float(res["result"]["list"][0]["totalEquity"])
    return None

def get_positions():
    res = safe_call(session.get_positions, category="linear")
    if not res:
        return {}
    return {p["symbol"]: p for p in res["result"]["list"] if float(p["size"]) > 0}

def get_data(symbol):
    res = safe_call(session.get_kline, category="linear", symbol=symbol, interval=TIMEFRAME, limit=25)
    if not res:
        return None
    df = pd.DataFrame(res["result"]["list"])
    df.columns = ["time","open","high","low","close","volume","turnover"]
    df[["open","close","high","low"]] = df[["open","close","high","low"]].astype(float)
    return df

# === SIGNAL ===
def check_signal(df):
    df["ma"] = df["close"].rolling(20).mean()
    df["std"] = df["close"].rolling(20).std()
    df["upper"] = df["ma"] + (2 * df["std"])
    df["lower"] = df["ma"] - (2 * df["std"])

    last = df.iloc[-1]
    prev = df.iloc[-2]

    if ((prev["close"] <= prev["lower"] or prev["low"] <= prev["lower"]) and
        last["close"] > last["lower"] and last["close"] > last["open"]):
        return "buy", last["close"], last["upper"]

    if ((prev["close"] >= prev["upper"] or prev["high"] >= prev["upper"]) and
        last["close"] < last["upper"] and last["close"] < last["open"]):
        return "sell", last["close"], last["lower"]

    return None, None, None

# === TRADE ===
def place_trade(symbol, side, entry, tp):
    raw_qty = (TRADE_SIZE * LEVERAGE) / entry
    qty = round_qty(symbol, raw_qty)

    if qty < INSTR[symbol]["min_qty"]:
        return  # skip invalid size

    sl = entry * (1 - 0.002) if side == "buy" else entry * (1 + 0.002)

    session.place_order(
        category="linear",
        symbol=symbol,
        side="Buy" if side == "buy" else "Sell",
        orderType="Market",
        qty=qty,
        takeProfit=round_price(symbol, tp),
        stopLoss=round_price(symbol, sl)
    )

    last_trade_time[symbol] = time.time()

# === LOOP ===
while True:
    try:
        now = datetime.utcnow()

        if now.day != current_day:
            current_day = now.day
            start_balance = get_balance()
            trading_paused = False

        if start_balance is None:
            start_balance = get_balance()

        balance = get_balance()
        if balance:
            dd = (start_balance - balance) / start_balance
            if dd >= DAILY_DD_LIMIT:
                trading_paused = True

        if trading_paused:
            time.sleep(15)
            continue

        positions = get_positions()

        if len(positions) < MAX_TRADES:
            batch = PAIRS[pair_index:pair_index+BATCH_SIZE]
            pair_index = (pair_index+BATCH_SIZE) % len(PAIRS)

            for pair in batch:
                if pair in positions:
                    continue
                if pair in last_trade_time and time.time() - last_trade_time[pair] < 180:
                    continue

                df = get_data(pair)
                if df is None:
                    continue

                signal, entry, tp = check_signal(df)

                if signal:
                    place_trade(pair, signal, entry, tp)
                    time.sleep(2)

                time.sleep(0.3)

        time.sleep(12)

    except Exception:
        time.sleep(15)