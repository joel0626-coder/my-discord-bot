import discord
from discord.ext import commands, tasks
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web
from datetime import datetime, time, timezone, timedelta
import requests
import re
from io import StringIO
import logging

# 強制關閉 yfinance 煩人的紅字報錯
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

TOKEN = os.environ.get('DISCORD_TOKEN')
PORTFOLIO_FILE = "my_portfolio.json"

# ========= 🚨 你的專屬設定 =========
PUSH_CHANNEL_ID = 1509058179458404495
FINMIND_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiam9lbDA2MjYiLCJlbWFpbCI6ImpvZWwwNjI2QG1zbi5jb20iLCJ0b2tlbl92ZXJzaW9uIjowfQ.j1KeK6JfXNUX2WlEKYmdMctQV_9_xfwpzVlANplYafs"
# ============================================

STRAT_MAP = {
    "1": "1. 布林壓縮突破 (動能)",
    "2": "2. 雙均線+MACD (趨勢)",
    "3": "3. RSI超賣反彈 (逆勢)"
}

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: 
        return json.load(f)

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: 
        json.dump(data, f, indent=4, ensure_ascii=False)

# =====================================================================
# 🔍 核心 1：大數據智能選股雷達 (解開限制，滿血版)
# =====================================================================
_TICKER_CACHE = {}
def get_all_taiwan_tickers():
    global _TICKER_CACHE
    if _TICKER_CACHE: return _TICKER_CACHE
    
    tickers_dict = {}
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {"dataset": "TaiwanStockInfo", "token": FINMIND_TOKEN}
    
    try:
        res = requests.get(url, params=params, timeout=15)
        data = res.json()
        if data.get("status") == 200:
            for item in data.get("data", []):
                stock_id = str(item.get("stock_id", ""))
                if len(stock_id) == 4 and stock_id.isdigit():
                    stock_type = item.get("type", "")
                    suffix = ".TWO" if stock_type == "tpex" else ".TW"
                    tickers_dict[f"{stock_id}{suffix}"] = {
                        "name": item.get("stock_name", ""),
                        "sector": item.get("industry_category", "")
                    }
    except Exception as e:
        print(f"FinMind 股票清單下載失敗: {e}")
        
    _TICKER_CACHE = tickers_dict
    return _TICKER_CACHE

def get_finmind_chip_5d(stock_code):
    start_date = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": str(stock_code), "start_date": start_date, "token": FINMIND_TOKEN}
    try:
        res = requests.get(url, params=params, timeout=5)
        data = res.json()
        if data.get("status") == 200 and len(data.get("data", [])) > 0:
            df = pd.DataFrame(data["data"])
            recent_dates = sorted(df['date'].unique())[-5:]
            df_5d = df[df['date'].isin(recent_dates)]
            foreign, trust = 0, 0
            for _, row in df_5d.iterrows():
                net_buy = (row.get('buy', 0) - row.get('sell', 0)) // 1000
                name = row.get('name', '')
                if 'Foreign_Investor' in name: foreign += net_buy
                elif 'Investment_Trust' in name: trust += net_buy
            return {"外資": int(foreign), "投信": int(trust)}
    except: pass
    return {"外資": 0, "投信": 0}

def calculate_screener_indicators(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df['SMA_10'] = df['Close'].rolling(window=10).mean()
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    df['SMA_60'] = df['Close'].rolling(window=60).mean()
    df['Vol_SMA_5'] = df['Volume'].rolling(window=5).mean()
    df['Vol_SMA_20'] = df['Volume'].rolling(window=20).mean()
    
    std = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['SMA_20'] + (2 * std)
    df['BB_Lower'] = df['SMA_20'] - (2 * std)
    df['BB_Width'] = (df['BB_Upper'] - df['BB_Lower']) / df['SMA_20']
    
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['Signal_Line'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist'] = df['MACD'] - df['Signal_Line']
    
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    rs = gain.ewm(alpha=1/14, adjust=False).mean() / loss.ewm(alpha=1/14, adjust=False).mean()
    df['RSI_14'] = 100 - (100 / (1 + rs))
    return df

def run_screener_for_discord(mode="嚴格"):
    if mode == "放寬":
        vol_amount_min = 20000000
        bb_width_max = 0.12
        vol_multiple = 1.5
        rsi_bottom = 35
    elif mode == "極限":
        vol_amount_min = 10000000
        bb_width_max = 0.15
        vol_multiple = 1.2
        rsi_bottom = 40
    else: 
        vol_amount_min = 50000000
        bb_width_max = 0.08
        vol_multiple = 2.0
        rsi_bottom = 30

    try:
        tickers_dict = get_all_taiwan_tickers()
        if not tickers_dict: return "⚠️ 無法取得台股代號列表，FinMind API 連線異常。"
            
        # 🔥 拔除 [:500] 封印！現在會像本機一樣掃描全台股 1700+ 檔
        tickers = list(tickers_dict.keys())
        
        # 配合全台股掃描，將資料期間從 3mo 改為 4mo，對齊本機 V5.6，確保季線準確
        data = yf.download(tickers, period="4mo", group_by="ticker", progress=False, threads=True)
        
        msg_bb, msg_macd, msg_rsi = "", "", ""
        
        for ticker in tickers:
            try:
                if ticker not in data or data[ticker].empty: continue
                df = data[ticker].dropna().copy()
                if len(df) < 65: continue
                
                df = calculate_screener_indicators(df)
                latest, prev1 = df.iloc[-1], df.iloc[-2]
                close, vol, open_px = latest['Close'].item(), latest['Volume'].item(), latest['Open'].item()
                
                # 修正成交額判斷：yfinance的vol是股數，所以 close * vol 即為真實成交金額(元)
                turnover = close * vol
                if turnover < vol_amount_min: continue 
                
                name = tickers_dict[ticker]['name']
                clean_code = ticker.replace('.TW', '').replace('.TWO', '')
                is_uptrend = latest['SMA_60'].item() > prev1['SMA_60'].item()
                
                match_strat = ""
                
                # 策略 1: 布林壓縮突破 (動能)
                bb_cond1 = prev1['BB_Width'].item() < bb_width_max
                bb_cond2 = close > latest['BB_Upper'].item()
                bb_cond3 = vol > (latest['Vol_SMA_20'].item() * vol_multiple)
                
                if bb_cond1 and bb_cond2 and bb_cond3 and is_uptrend and close > open_px:
                    match_strat = "BB"
                
                # 策略 2: MACD 順勢 (趨勢)
                macd_cross = (prev1['MACD'].item() < prev1['Signal_Line'].item()) and (latest['MACD'].item() > latest['Signal_Line'].item())
                macd_imminent = (latest['MACD'].item() < latest['Signal_Line'].item()) and (latest['MACD_Hist'].item() > prev1['MACD_Hist'].item())
                
                if is_uptrend and close > latest['SMA_60'].item():
                    if mode == "嚴格" and macd_cross: match_strat = "MACD"
                    elif mode in ["放寬", "極限"] and (macd_cross or macd_imminent): match_strat = "MACD"

                # 策略 3: RSI 乖離翻揚 (逆勢)
                rsi_rebound = prev1['RSI_14'].item() < rsi_bottom and latest['RSI_14'].item() >= rsi_bottom
                if close < (latest['SMA_20'].item() * 0.95) and rsi_rebound:
                    match_strat = "RSI"

                if match_strat:
                    chips = get_finmind_chip_5d(clean_code)
                    t_buy, f_buy = chips['投信'], chips['外資']
                    chip_txt = "🔥土洋連買" if t_buy > 200 and f_buy > 500 else "🔥投信進駐" if t_buy > 150 else ""
                    stock_info = f"📌 **{clean_code} {name}** | 收盤 `{round(close, 2)}` | 外資 `{f_buy}` 投信 `{t_buy}` {chip_txt}\n"
                    
                    if match_strat == "BB": msg_bb += stock_info
                    elif match_strat == "MACD": msg_macd += stock_info
                    elif match_strat == "RSI": msg_rsi += stock_info
            except Exception: 
                continue

        final_msg = f"🎯 **【盤後選股雷達推薦】** (模式: `{mode}`)\n=========================\n"
        if msg_bb: final_msg += "💥 **布林突破 (動能)**\n" + msg_bb + "\n"
        if msg_macd: final_msg += "🏄‍♂️ **MACD 翻揚 (波段)**\n" + msg_macd + "\n"
        if msg_rsi: final_msg += "🎣 **RSI 反彈 (逆勢)**\n" + msg_rsi + "\n"
        
        if not (msg_bb or msg_macd or msg_rsi):
            final_msg += f"今天大盤在 `{mode}` 模式下依然沒有符合條件的獵物 😴\n(建議可嘗試輸入 `!選股 放寬` 或 `!選股 極限`)"
            
        return final_msg
        
    except Exception as e:
        if "No objects to concatenate" in str(e):
