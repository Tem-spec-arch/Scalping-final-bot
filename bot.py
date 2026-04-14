import time
import pandas as pd
from datetime import datetime
from pybit.unified_trading import HTTP

# === CONFIG ===
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

TIMEFRAME = "3"
LEVERAGE = 70
TRADE_SIZE = 2
MAX_TRADES = 10
DAILY_DD_LIMIT = 0.40

# === STATE ===
start_balance = None
current_day = datetime.utcnow().day
trading_paused = False
last_trade_time = {}

# === HELPERS ===
def get_balance():
    bal = session.get_wallet_balance(accountType="UNIFIED")
    return float(bal["result"]["list"][0]["totalEquity"])

def get_positions():
    pos = session.get_positions(category="linear")
    return {p["symbol"]: p for p in pos["result"]["list"] if float(p["size"]) > 0}

def get_instruments():
    data = session.get_instruments_info(category="linear")
    info = {}
    for item in data["result"]["list"]:
        info[item["symbol"]] = {
            "qty_step": float(item["lotSizeFilter"]["qtyStep"]),
            "tick_size": float(item["priceFilter"]["tickSize"])
        }
    return info

INSTRUMENTS = get_instruments()

def format_qty(symbol, qty):
    step = INSTRUMENTS[symbol]["qty_step"]
    return round(qty / step) * step

def format_price(symbol, price):
    tick = INSTRUMENTS[symbol]["tick_size"]
    return round(price / tick) * tick

def get_data(symbol):
    k = session.get_kline(category="linear", symbol=symbol, interval=TIMEFRAME, limit=50)
    df = pd.DataFrame(k["result"]["list"])
    df.columns = ["time","open","high","low","close","volume","turnover"]
    df[["open","close"]] = df[["open","close"]].astype(float)
    return df

def bollinger(df):
    df["ma"] = df["close"].rolling(20).mean()
    df["std"] = df["close"].rolling(20).std()
    df["upper"] = df["ma"] + (2 * df["std"])
    df["lower"] = df["ma"] - (2 * df["std"])
    return df

def check_signal(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    if prev["close"] < prev["lower"] and last["close"] > last["lower"]:
        if last["close"] > last["open"]:
            return "buy", last["close"], last["upper"]

    if prev["close"] > prev["upper"] and last["close"] < last["upper"]:
        if last["close"] < last["open"]:
            return "sell", last["close"], last["lower"]

    return None, None, None

def set_leverage(symbol):
    try:
        session.set_leverage(
            category="linear",
            symbol=symbol,
            buyLeverage=str(LEVERAGE),
            sellLeverage=str(LEVERAGE)
        )
    except:
        pass

def place_trade(symbol, side, entry, tp):
    set_leverage(symbol)

    position_value = TRADE_SIZE * LEVERAGE
    qty = position_value / entry
    qty = format_qty(symbol, qty)

    if side == "buy":
        sl = entry * (1 - 0.0025)
    else:
        sl = entry * (1 + 0.0025)

    sl = format_price(symbol, sl)
    tp = format_price(symbol, tp)

    session.place_order(
        category="linear",
        symbol=symbol,
        side="Buy" if side == "buy" else "Sell",
        orderType="Market",
        qty=qty,
        takeProfit=tp,
        stopLoss=sl
    )

    last_trade_time[symbol] = time.time()

    print(f"{symbol} | {side.upper()} | Qty:{qty} | TP:{tp} | SL:{sl}")

# === MAIN LOOP ===
while True:
    try:
        now = datetime.utcnow()

        # New day reset
        if now.day != current_day:
            current_day = now.day
            start_balance = get_balance()
            trading_paused = False
            print("New trading day")

        if start_balance is None:
            start_balance = get_balance()

        current_balance = get_balance()

        # Drawdown control
        dd = (start_balance - current_balance) / start_balance
        if dd >= DAILY_DD_LIMIT:
            trading_paused = True
            print("🚨 40% DD reached. Trading paused.")

        if trading_paused:
            time.sleep(30)
            continue

        positions = get_positions()

        if len(positions) < MAX_TRADES:

            for pair in PAIRS:

                positions = get_positions()
                if len(positions) >= MAX_TRADES:
                    break

                if pair in positions:
                    continue

                df = get_data(pair)
                df = bollinger(df)

                signal, entry, tp = check_signal(df)

                if signal:
                    if pair in last_trade_time and time.time() - last_trade_time[pair] < 60:
                        continue

                    place_trade(pair, signal, entry, tp)
                    time.sleep(2)

        time.sleep(10)

    except Exception as e:
        print("Error:", e)
        time.sleep(10)