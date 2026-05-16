# bybit_scalper_3m_live.py
# Bybit V5 UTA 3m Scalper | Crossover + Pullback | 2-Entry Flow | FINAL + INDEXERROR FIX
# FIX: Safe position list access in attach_stops + close_position

import os, time, json, logging, math, tempfile, shutil
from dotenv import load_dotenv
import pandas as pd
from pybit.unified_trading import HTTP
from ta.trend import ema_indicator, MACD, PSARIndicator
from ta.volatility import AverageTrueRange

# ================= CONFIG =================
load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "LINKUSDT"]
LEVERAGE = 70
ENTRY_SIZE_PCT = 0.5
ORDER1_TP_MULT = 1.3
ORDER1_SL_MULT = 1.0
ORDER2_SL_MULT = 0.7
DRY_RUN = False
STATE_FILE = "bot_state.json"
LOG_FILE = "scalper_live.log"

# Bybit V5 Precision & Constraints
QTY_STEP = {"BTCUSDT": 0.001, "ETHUSDT": 0.001, "XRPUSDT": 1.0, "SOLUSDT": 0.1, "LINKUSDT": 0.1}
TICK_SIZE = {"BTCUSDT": 0.1, "ETHUSDT": 0.01, "XRPUSDT": 0.0001, "SOLUSDT": 0.001, "LINKUSDT": 0.001}
MIN_NOTIONAL = 5.0

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ================= API CLIENT =================
session = HTTP(api_key=API_KEY if not DRY_RUN else "", api_secret=API_SECRET if not DRY_RUN else "", 
               testnet=False, recv_window=5000)

def api_call(func, *args, max_retries=3, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if "429" in str(e) or "500" in str(e):
                wait = 1.0 * (2 ** attempt)
                logger.warning(f"⚠️ Rate/Server error. Retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise
    raise Exception("Max API retries exceeded")

# ================= STATE (Atomic) =================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: pass
    return {s: {"status":"idle","side":None,"entry_idx":0,"entry_price":0.0,"tp_price":0.0,"sl_price":0.0,"qty":0.0} for s in SYMBOLS}

def save_state(state):
    tmp_fd, tmp_path = tempfile.mkstemp()
    try:
        with os.fdopen(tmp_fd, 'w') as f: json.dump(state, f, indent=2)
        shutil.move(tmp_path, STATE_FILE)
    except Exception as e: logger.error(f"State save failed: {e}")

def sync_positions(state):
    if DRY_RUN: return state
    try:
        pos_resp = api_call(session.get_positions, category="linear", symbol="")
        if pos_resp["retCode"] != 0: return state
        for sym in SYMBOLS:
            sym_pos = next((p for p in pos_resp["result"]["list"] if p["symbol"] == sym), None)
            size = float(sym_pos.get("size", 0)) if sym_pos else 0.0
            if size > 0 and sym_pos.get("side"):
                state[sym].update({"status":"entry1_open","side":sym_pos["side"],"qty":size})
                logger.info(f"🔄 Synced {sym}: {sym_pos['side']} {size}")
            elif state[sym]["status"] != "idle":
                state[sym] = {"status":"idle","side":None,"entry_idx":0,"entry_price":0.0,"tp_price":0.0,"sl_price":0.0,"qty":0.0}
    except Exception as e: logger.warning(f"Position sync failed: {e}")
    return state

# Leverage map per symbol
LEVERAGE_MAP = {}
def fetch_leverage_map():
    global LEVERAGE_MAP
    try:
        resp = api_call(session.get_instruments_info, category="linear")
        if resp["retCode"] != 0: return
        for inst in resp["result"]["list"]:
            sym = inst["symbol"]
            if sym in SYMBOLS:
                max_lev = int(inst.get("leverageFilter", {}).get("maxLeverage", LEVERAGE))
                LEVERAGE_MAP[sym] = min(LEVERAGE, max_lev)
    except Exception as e:
        logger.warning(f"⚠️ Leverage map fetch failed: {e}")
        for sym in SYMBOLS: LEVERAGE_MAP[sym] = LEVERAGE

# ================= PRECISION HELPERS =================
def round_to_step(val: float, sym: str) -> str:
    step = QTY_STEP.get(sym, 0.001)
    rounded = math.floor(val / step) * step
    return f"{max(step, rounded):.6f}".rstrip('0').rstrip('.')

def round_to_tick(val: float, sym: str) -> str:
    tick = TICK_SIZE.get(sym, 0.01)
    rounded = round(val / tick) * tick
    decimals = int(math.log10(1/tick)) if tick < 1 else 0
    return f"{rounded:.{decimals}f}"

def calc_qty(balance: float, price: float, sym: str) -> str:
    if balance <= 0 or price <= 0: return "0"
    effective_lev = LEVERAGE_MAP.get(sym, LEVERAGE)
    notional = balance * ENTRY_SIZE_PCT * effective_lev
    if notional < MIN_NOTIONAL:
        logger.warning(f"⚠️ Notional {notional:.2f} < {MIN_NOTIONAL}. Skipping {sym}")
        return "0"
    qty = notional / price
    return round_to_step(qty, sym)

# ================= EXECUTION =================
def place_market(sym: str, side: str, qty: str) -> bool:
    if DRY_RUN:
        logger.info(f"[DRY RUN] Market {side} {qty} {sym}")
        return True
    try:
        resp = api_call(session.place_order, category="linear", symbol=sym, side=side,
                        orderType="Market", qty=qty, timeInForce="GTC", positionIdx=0)
        if resp["retCode"] == 0: return True
        logger.error(f"❌ Market order failed {sym}: {resp['retMsg']} (code:{resp['retCode']})")
        return False
    except Exception as e:
        logger.error(f"❌ Market exception {sym}: {e}")
        return False

# ✅ FIX: Safe position list access to prevent IndexError
def attach_stops(sym: str, tp: float, sl: float, side: str, max_wait=3.0) -> bool:
    if DRY_RUN: return True
    if not tp and not sl: return True
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            pos_resp = api_call(session.get_positions, category="linear", symbol=sym)
            # ✅ FIX: Check retCode AND empty list before accessing [0]
            if pos_resp["retCode"] != 0 or not pos_resp["result"]["list"]:
                time.sleep(0.3)
                continue
            pos = pos_resp["result"]["list"][0]
            if float(pos.get("size", 0)) > 0:
                resp = api_call(session.set_trading_stop, category="linear", symbol=sym,
                                takeProfit=str(round_to_tick(tp, sym)) if tp else "",
                                stopLoss=str(round_to_tick(sl, sym)) if sl else "",
                                tpTriggerBy="MarkPrice", slTriggerBy="MarkPrice", positionIdx=0)
                if resp["retCode"] == 0: return True
                logger.warning(f"⚠️ set_trading_stop failed {sym}: {resp['retMsg']}")
                if not DRY_RUN:
                    logger.warning(f"🚨 Closing unprotected position {sym} due to stop attach failure")
                    close_position(sym, side)
                return False
        except Exception as e:
            logger.warning(f"⚠️ attach_stops exception {sym}: {e}")
            pass
        time.sleep(0.3)
    logger.error(f"❌ Position not found to attach stops for {sym}")
    if not DRY_RUN:
        logger.warning(f"🚨 Closing unprotected position {sym} due to timeout")
        close_position(sym, side)
    return False

# ✅ FIX: Safe position list access to prevent IndexError
def close_position(sym: str, side: str) -> bool:
    if DRY_RUN: return True
    try:
        pos_resp = api_call(session.get_positions, category="linear", symbol=sym)
        # ✅ FIX: Check retCode AND empty list before accessing [0]
        if pos_resp["retCode"] != 0 or not pos_resp["result"]["list"]:
            return True  # No position to close
        pos = pos_resp["result"]["list"][0]
        size = float(pos.get("size", 0))
        if size <= 0: return True
        opp = "Sell" if side == "Buy" else "Buy"
        resp = api_call(session.place_order, category="linear", symbol=sym, side=opp,
                        orderType="Market", qty=round_to_step(size, sym),
                        reduceOnly=True, timeInForce="GTC", positionIdx=0)
        if resp["retCode"] == 0:
            logger.info(f"🚪 Closed {sym} {side} {size}")
            return True
        logger.warning(f"⚠️ Close failed {sym}: {resp['retMsg']}")
        return False
    except Exception as e:
        logger.error(f"❌ Close exception {sym}: {e}")
        return False

def set_leverage_safe():
    for sym in SYMBOLS:
        try:
            if not DRY_RUN:
                api_call(session.set_leverage, category="linear", symbol=sym, 
                         buyLeverage=str(LEVERAGE_MAP.get(sym, LEVERAGE)), 
                         sellLeverage=str(LEVERAGE_MAP.get(sym, LEVERAGE)))
            logger.info(f"✅ Leverage {LEVERAGE_MAP.get(sym, LEVERAGE)}x set for {sym}")
        except Exception as e:
            logger.warning(f"⚠️ Leverage capped/failed {sym}: {e}")
        time.sleep(0.2)

# ================= INDICATORS =================
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema9"] = ema_indicator(df["close"], window=9)
    df["ema21"] = ema_indicator(df["close"], window=21)
    macd_obj = MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd_hist"] = macd_obj.macd_diff()
    df["atr"] = AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    df["sar"] = PSARIndicator(df["high"], df["low"], df["close"], step=0.02, max_step=0.2).psar()
    return df.dropna().reset_index(drop=True)

# ================= SIGNALS =================
def check_crossover(df, idx, side):
    if idx < 2: return False
    e9p, e9c = df["ema9"].iloc[idx-1], df["ema9"].iloc[idx]
    e21p, e21c = df["ema21"].iloc[idx-1], df["ema21"].iloc[idx]
    cc = df["close"].iloc[idx]
    hp, hc = df["macd_hist"].iloc[idx-1], df["macd_hist"].iloc[idx]
    cross = (e9p <= e21p and e9c > e21c) if side=="Buy" else (e9p >= e21p and e9c < e21c)
    mom = hc > hp if side=="Buy" else hc < hp
    close_cond = cc > e9c if side=="Buy" else cc < e9c
    return cross and close_cond and mom

def check_pullback(df, idx, side):
    if idx < 6: return False
    e9, e21 = df["ema9"].iloc[idx], df["ema21"].iloc[idx]
    cc, lc, hc = df["close"].iloc[idx], df["low"].iloc[idx], df["high"].iloc[idx]
    hp, hc_hist = df["macd_hist"].iloc[idx-1], df["macd_hist"].iloc[idx]
    recent = df.iloc[max(0, idx-5):idx]
    if side == "Buy":
        touched = (recent["low"] <= e21).any() or (recent["low"] <= e9).any()
        return touched and (lc <= e9 or lc <= e21) and cc > e9 and hc_hist > hp
    touched = (recent["high"] >= e21).any() or (recent["high"] >= e9).any()
    return touched and (hc >= e9 or hc >= e21) and cc < e9 and hc_hist < hp

def macd_supports_trend(df, idx, side):
    if idx < 1: return False
    hp, hc = df["macd_hist"].iloc[idx-1], df["macd_hist"].iloc[idx]
    return hc > hp if side=="Buy" else hc < hp

# ================= MAIN LOOP =================
def run_bot():
    logger.info("🤖 Starting Bybit 3m Scalper (FINAL + INDEXERROR FIX)")
    logger.info(f"Pairs: {SYMBOLS} | Lev: {LEVERAGE}x | Size: {ENTRY_SIZE_PCT*100}% | TP1:×{ORDER1_TP_MULT} SL1:×{ORDER1_SL_MULT}")
    
    fetch_leverage_map()
    
    state = load_state()
    state = sync_positions(state)
    save_state(state)
    if not DRY_RUN: set_leverage_safe()
    time.sleep(2)
    
    while True:
        try:
            balance = api_call(session.get_wallet_balance, accountType="UNIFIED")
            bal_usdt = 0.0
            if balance["retCode"] == 0:
                bal_usdt = float(next((c["walletBalance"] for c in balance["result"]["list"][0]["coin"] if c["coin"]=="USDT"), 0))
            if bal_usdt < MIN_NOTIONAL:
                logger.warning(f"⚠️ Balance ${bal_usdt:.2f}. Waiting...")
                time.sleep(300); continue
            
            for sym in SYMBOLS:
                st = state[sym]
                try:
                    klines = api_call(session.get_kline, category="linear", symbol=sym, interval="3", limit=50)
                    if klines["retCode"] != 0: continue
                    data = klines["result"]["list"]
                except Exception as e:
                    logger.warning(f"⚠️ Kline fetch {sym}: {e}"); continue
                
                df = pd.DataFrame(data, columns=["ts","o","h","l","c","v","t"])
                df[["o","h","l","c","v"]] = df[["o","h","l","c","v"]].astype(float)
                df = df.sort_values("ts").reset_index(drop=True)
                df = calc_indicators(df)
                if len(df) < 25: continue
                
                idx = len(df) - 1
                price, atr, sar = df["close"].iloc[idx], df["atr"].iloc[idx], df["sar"].iloc[idx]
                if atr <= 0 or math.isnan(atr): continue
                
                # EMERGENCY EXIT
                if st["status"] != "idle" and st["side"]:
                    if (st["side"]=="Buy" and price < sar) or (st["side"]=="Sell" and price > sar):
                        logger.warning(f"🚨 SAR FLIP EXIT: {sym} {st['side']}")
                        close_position(sym, st["side"])
                        state[sym] = {"status":"idle","side":None,"entry_idx":0,"entry_price":0.0,"tp_price":0.0,"sl_price":0.0,"qty":0.0}
                        save_state(state)
                        continue
                
                # IDLE -> CHECK STRATEGY 1 THEN 2
                if st["status"] == "idle":
                    side = None
                    if check_crossover(df, idx, "Buy"): side = "Buy"
                    elif check_crossover(df, idx, "Sell"): side = "Sell"
                    elif check_pullback(df, idx, "Buy"): side = "Buy"
                    elif check_pullback(df, idx, "Sell"): side = "Sell"
                    
                    if side:
                        ep = price * (1.0005 if side=="Buy" else 0.9995)
                        qty_str = calc_qty(bal_usdt, ep, sym)
                        if qty_str == "0": continue
                        tp_val = ep + (atr * ORDER1_TP_MULT if side=="Buy" else -atr * ORDER1_TP_MULT)
                        sl_val = ep - (atr * ORDER1_SL_MULT if side=="Buy" else -atr * ORDER1_SL_MULT)
                        
                        if place_market(sym, side, qty_str) and attach_stops(sym, tp_val, sl_val, side):
                            state[sym] = {"status":"entry1_open","side":side,"entry_idx":idx,"entry_price":ep,"tp_price":tp_val,"sl_price":sl_val,"qty":float(qty_str)}
                            logger.info(f"✅ Entry1 {side.upper()} {sym} @ {ep:.{4 if sym.startswith('XRP') else 2}f} | TP:{tp_val:.4f} SL:{sl_val:.4f}")
                            save_state(state)
                
                # ENTRY 1 TP/SL -> ENTRY 2
                elif st["status"] == "entry1_open":
                    tp_hit = (st["side"]=="Buy" and price >= st["tp_price"]) or (st["side"]=="Sell" and price <= st["tp_price"])
                    sl_hit = (st["side"]=="Buy" and price <= st["sl_price"]) or (st["side"]=="Sell" and price >= st["sl_price"])
                    
                    if tp_hit or sl_hit:
                        logger.info(f"{'🎯 TP1' if tp_hit else '🛑 SL1'} hit: {sym}")
                        close_position(sym, st["side"])
                        
                        if tp_hit and macd_supports_trend(df, idx, st["side"]):
                            logger.info(f"📈 MACD confirms -> Entry2 prep {sym}")
                            time.sleep(1.0)
                            pos_size = 0.0
                            try: 
                                pos_resp = api_call(session.get_positions, category="linear", symbol=sym)
                                if pos_resp["retCode"] == 0 and pos_resp["result"]["list"]:
                                    pos_size = float(pos_resp["result"]["list"][0].get("size", 0))
                            except: pass
                            if pos_size <= 1e-8:
                                ep2 = price * (1.0005 if st["side"]=="Buy" else 0.9995)
                                qty2 = calc_qty(bal_usdt, ep2, sym)
                                if qty2 != "0":
                                    sl2 = ep2 - (atr * ORDER2_SL_MULT if st["side"]=="Buy" else -atr * ORDER2_SL_MULT)
                                    if place_market(sym, st["side"], qty2) and attach_stops(sym, 0.0, sl2, st["side"]):
                                        state[sym] = {"status":"entry2_open","side":st["side"],"entry_idx":idx,"entry_price":ep2,"tp_price":0.0,"sl_price":sl2,"qty":float(qty2)}
                                        logger.info(f"✅ Entry2 {st['side'].upper()} {sym} @ {ep2:.{4 if sym.startswith('XRP') else 2}f} | SL:{sl2:.4f} TP:SAR")
                                        save_state(state)
                                    else:
                                        logger.warning(f"⚠️ Entry2 failed {sym}. Resetting to idle.")
                                        state[sym] = {"status":"idle","side":None,"entry_idx":0,"entry_price":0.0,"tp_price":0.0,"sl_price":0.0,"qty":0.0}
                                        save_state(state)
                                else:
                                    logger.warning(f"⚠️ Entry2 qty=0 {sym}. Resetting to idle.")
                                    state[sym] = {"status":"idle","side":None,"entry_idx":0,"entry_price":0.0,"tp_price":0.0,"sl_price":0.0,"qty":0.0}
                                    save_state(state)
                            else:
                                logger.warning(f"⚠️ Pos still open {sym}. Skip Entry2.")
                                state[sym] = {"status":"idle","side":None,"entry_idx":0,"entry_price":0.0,"tp_price":0.0,"sl_price":0.0,"qty":0.0}
                                save_state(state)
                        else:
                            logger.info(f"🔄 Reset to idle: {sym}")
                            state[sym] = {"status":"idle","side":None,"entry_idx":0,"entry_price":0.0,"tp_price":0.0,"sl_price":0.0,"qty":0.0}
                            save_state(state)
                
                # ENTRY 2 SAR EXIT
                elif st["status"] == "entry2_open":
                    if (st["side"]=="Buy" and price < sar) or (st["side"]=="Sell" and price > sar):
                        logger.info(f"🎯 SAR TP hit: {sym}")
                        close_position(sym, st["side"])
                        state[sym] = {"status":"idle","side":None,"entry_idx":0,"entry_price":0.0,"tp_price":0.0,"sl_price":0.0,"qty":0.0}
                        save_state(state)
                
                time.sleep(0.2)
            
            # Sync to 3m UTC boundary
            now = time.time()
            next_candle = now - (now % 180) + 180
            time.sleep(max(1.0, next_candle - now - 2.0))
            
        except KeyboardInterrupt:
            logger.info("🛑 Bot stopped")
            break
        except Exception as e:
            logger.critical(f"💥 Crash: {e}. Restarting in 30s...", exc_info=True)
            time.sleep(30)

if __name__ == "__main__":
    run_bot()