import discord
from discord.ext import commands, tasks
import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import asyncio
from aiohttp import web
from datetime import datetime, time, timezone, timedelta
import requests
import logging
import copy

# 強制關閉 yfinance 煩人的紅字報錯
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

# =====================================================================
# 🔑 系統與金鑰設定
# =====================================================================
TOKEN = os.environ.get('DISCORD_TOKEN')
FINMIND_TOKEN = os.environ.get('FINMIND_TOKEN', "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiam9lbDA2MjYiLCJlbWFpbCI6ImpvZWwwNjI2QG1zbi5jb20iLCJ0b2tlbl92ZXJzaW9uIjowfQ.j1KeK6JfXNUX2WlEKYmdMctQV_9_xfwpzVlANplYafs")

PORTFOLIO_FILE = "my_portfolio.json"
TRADE_HISTORY_FILE = "trade_history.json"
CONFIG_FILE = "config.json"

# ========= 🚨 你的專屬設定 =========
PUSH_CHANNEL_ID = 1509058179458404495
# ============================================

STRAT_MAP = {
    "1": "1. 布林壓縮突破 (動能攻擊)",
    "2": "2. 雙均線+MACD (趨勢波段)",
    "3": "3. RSI超賣反彈 (極端逆勢)",
    "4": "4. 多頭縮量回踩 (窒息量防守)",
    "5": "5. 強勢創高確認 (爆量攻擊)"
}

# =====================================================================
# 🗂️ 資料庫存取模組
# =====================================================================
def load_config():
    if not os.path.exists(CONFIG_FILE): return {"total_capital": 0}
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f: 
        return json.load(f)

def save_config(data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f: 
        json.dump(data, f, indent=4, ensure_ascii=False)

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: 
        return json.load(f)

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: 
        json.dump(data, f, indent=4, ensure_ascii=False)

def load_history():
    if not os.path.exists(TRADE_HISTORY_FILE): 
        return {"total_pnl": 0.0, "trades": []}
    with open(TRADE_HISTORY_FILE, 'r', encoding='utf-8') as f: 
        return json.load(f)

def save_history(data):
    with open(TRADE_HISTORY_FILE, 'w', encoding='utf-8') as f: 
        json.dump(data, f, indent=4, ensure_ascii=False)

# =====================================================================
# 📚 FinMind 股票代號快取
# =====================================================================
_TICKER_CACHE = {}
def get_all_taiwan_tickers():
    global _TICKER_CACHE
    if _TICKER_CACHE: return _TICKER_CACHE
    
    tickers_dict = {}
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {"dataset": "TaiwanStockInfo", "token": FINMIND_TOKEN}
    
    try:
        res = requests.get(url, params=params, timeout=10)
        data = res.json()
        if data.get("status") == 200:
            for item in data.get("data", []):
                stock_id = str(item.get("stock_id", ""))
                if len(stock_id) == 4 and stock_id.isdigit():
                    stock_type = item.get("type", "")
                    suffix = ".TWO" if stock_type == "tpex" else ".TW"
                    tickers_dict[f"{stock_id}{suffix}"] = {
                        "name": item.get("stock_name", "")
                    }
    except Exception as e:
        print(f"FinMind 股票清單下載失敗: {e}")
        
    _TICKER_CACHE = tickers_dict
    return _TICKER_CACHE

# =====================================================================
# 🛡️ 核心：指標計算與環境濾網
# =====================================================================
def calculate_indicators(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    
    df['SMA_5'] = df['Close'].rolling(window=5).mean()
    df['SMA_10'] = df['Close'].rolling(window=10).mean()
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    df['SMA_60'] = df['Close'].rolling(window=60).mean()
    
    df['Vol_5MA'] = df['Volume'].rolling(window=5).mean()
    df['Vol_20MA'] = df['Volume'].rolling(window=20).mean()
    
    df['Max_20'] = df['Close'].rolling(window=20).max()
    
    std = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['SMA_20'] + (2 * std)
    df['BB_Lower'] = df['SMA_20'] - (2 * std)
    df['BB_Width'] = (df['BB_Upper'] - df['BB_Lower']) / df['SMA_20']
    
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist'] = df['MACD'] - df['Signal']
    
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    df['ATR_14'] = true_range.rolling(14).mean()
    
    df['Bull_Aligned'] = (df['SMA_5'] > df['SMA_10']) & (df['SMA_10'] > df['SMA_20']) & (df['SMA_20'] > df['SMA_60'])
    
    return df

def check_market_trend():
    try:
        twii = yf.download('^TWII', period="2mo", progress=False)
        if twii.empty: return True 
        if isinstance(twii.columns, pd.MultiIndex):
            twii.columns = twii.columns.get_level_values(0)
        close = twii['Close'].iloc[-1].item()
        ma20 = twii['Close'].rolling(window=20).mean().iloc[-1].item()
        return close > ma20
    except:
        return True

def run_loss_analysis(code, name, buy_price, sell_price, strat):
    tickers_dict = get_all_taiwan_tickers()
    exact_ticker = f"{code}.TW" if f"{code}.TW" in tickers_dict else f"{code}.TWO"
    
    try:
        df = yf.download(exact_ticker, period="3mo", progress=False)
        if df.empty: return "無法取得報價，無法進行深入分析。"
        
        df = calculate_indicators(df)
        latest = df.iloc[-1]
        
        ma10 = latest['SMA_10'].item()
        ma20 = latest['SMA_20'].item()
        macd_val = latest['MACD'].item()
        sig_val = latest['Signal'].item()
        rsi = latest['RSI'].item()
        vol = latest['Volume'].item()
        vol_5ma = latest['Vol_5MA'].item()
        
        vol_surge = vol > vol_5ma * 1.5
        vol_shrink = vol < vol_5ma * 0.7
        market_uptrend = check_market_trend()
        reasons = []
        
        if not market_uptrend:
            reasons.append("📉 **大盤拖累**：加權指數跌破月線，覆巢之下無完卵，此筆屬於正確的紀律防守。")
            
        if sell_price < ma20 and vol_surge:
            reasons.append("☠️ **爆量破底**：股價帶大散戶逃命量跌破月線，主力徹底撤退，這筆停損完全正確，避開了大坑！")
        elif sell_price < ma20 and vol_shrink:
            reasons.append("⚠️ **無量跌破**：雖破月線但量能極度萎縮，此筆有機會是主力惡意洗盤。但嚴守紀律停損依然是好習慣。")
        elif sell_price < ma20:
            reasons.append("⚠️ **破底停損**：股價跌破月線(波段生命線)，技術面翻空。")
            
        elif sell_price < ma10 and ("1" in strat or "5" in strat):
            reasons.append("⚠️ **動能消散**：動能策略跌破 10 日線，強勢慣性改變，出場保本。")
            
        if macd_val < sig_val:
            reasons.append("📉 **趨勢轉空**：MACD 出現死叉，買盤力道衰退。")
            
        if rsi < 40:
            reasons.append("🧊 **人氣退潮**：RSI 轉弱，陷入無量陰跌。")
            
        if buy_price > ma10 and sell_price < ma10 and not market_uptrend:
            reasons.append("💡 **AI 教練碎碎念**：大盤不佳時追高強勢股容易被雙巴，下次請耐心等待「縮量回踩」再試單。")
            
        if not reasons:
            reasons.append("💡 **AI 教練碎碎念**：技術面無明顯嚴重破壞。此筆虧損可能為震盪洗盤，或老闆個人資金調度，無需過度氣餒。")
            
        return "\n".join(reasons)
    except Exception as e:
        return f"分析失敗 ({e})"

# =====================================================================
# 📚 深度健檢 (分析模組)
# =====================================================================
def run_deep_analysis(code):
    tickers_dict = get_all_taiwan_tickers() 
    exact_ticker = f"{code}.TW"
    stock_name = "未知名稱"
    
    if f"{code}.TW" in tickers_dict: 
        exact_ticker = f"{code}.TW"
        stock_name = tickers_dict[exact_ticker]['name']
    elif f"{code}.TWO" in tickers_dict: 
        exact_ticker = f"{code}.TWO"
        stock_name = tickers_dict[exact_ticker]['name']
        
    try:
        ticker_obj = yf.Ticker(exact_ticker)
        info = ticker_obj.info
        sector = info.get('sector', info.get('industry', '未分類產業'))
        pe_ratio = info.get('trailingPE', 'N/A')
        if isinstance(pe_ratio, float): pe_ratio = round(pe_ratio, 2)
        
        df = yf.download(exact_ticker, period="4mo", progress=False)
        if df.empty or len(df) < 65:
            return f"⚠️ 找不到代號 {code} 的報價，或上市時間太短不足以計算季線與技術指標。"
            
        df = calculate_indicators(df)
        latest = df.iloc[-1]
        
        close = round(latest['Close'].item(), 2)
        ma10 = round(latest['SMA_10'].item(), 2)
        ma20 = round(latest['SMA_20'].item(), 2)
        ma60 = round(latest['SMA_60'].item(), 2)
        bb_upper = round(latest['BB_Upper'].item(), 2)
        rsi = round(latest['RSI'].item(), 2)
        macd = round(latest['MACD'].item(), 2)
        sig = round(latest['Signal'].item(), 2)
        atr = latest['ATR_14'].item()
        
        vol = latest['Volume'].item()
        vol_5ma = latest['Vol_5MA'].item()
        vol_lots = int(vol / 1000)
        
        val_status = ""
        if pe_ratio != 'N/A':
            if pe_ratio > 40: val_status = "屬於高估值動能股，對資金行情敏感，嚴禁向下攤平。"
            elif pe_ratio < 15: val_status = "屬於低本益比價值股，下檔具備基本面保護傘。"
            else: val_status = "估值處於合理區間。"
        
        foreign_net_lots, trust_net_lots = 0, 0
        chip_status = ""
        try:
            start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            url = "https://api.finmindtrade.com/api/v4/data"
            params = {"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": code, "start_date": start_date, "token": FINMIND_TOKEN}
            res = requests.get(url, params=params, timeout=8).json()
            
            if res.get('status') == 200 and res.get('data'):
                data_list = res.get('data', [])
                dates = sorted(list(set(d['date'] for d in data_list)), reverse=True)[:5] 
                foreign_net, trust_net = 0, 0
                for d in data_list:
                    if d['date'] in dates:
                        name = d.get('name', '')
                        net = d.get('buy', 0) - d.get('sell', 0)
                        if '外資' in name or 'Foreign' in name: foreign_net += net
                        elif '投信' in name or 'Investment' in name: trust_net += net
                
                foreign_net_lots = int(foreign_net / 1000)
                trust_net_lots = int(trust_net / 1000)
                
                f_str = f"買超 {foreign_net_lots}" if foreign_net_lots >= 0 else f"賣超 {abs(foreign_net_lots)}"
                t_str = f"買超 {trust_net_lots}" if trust_net_lots >= 0 else f"賣超 {abs(trust_net_lots)}"
                chip_status = f"近 5 日外資合計: `{f_str}` 張\n近 5 日投信合計: `{t_str}` 張\n"
                
                if abs(foreign_net_lots) < 200 and abs(trust_net_lots) < 200:
                    chip_status += "    結論：法人進出量極小，視為散單或被動型基金微調，無明顯方向。"
                elif foreign_net_lots > 0 and trust_net_lots > 0: chip_status += "    結論：法人同步看好，籌碼集中度極佳。"
                elif trust_net_lots > 200: chip_status += "    結論：投信具備規模進駐，內資主導籌碼。"
                elif foreign_net_lots < -500 and trust_net_lots < -200: chip_status += "    結論：土洋法人同步倒貨，籌碼結構明顯轉弱。"
                else: chip_status += "    結論：外資投信分歧，籌碼目前處於換手震盪狀態。"
            else: chip_status = "⚠️ 無法取得近期籌碼資料或伺服器無資料。"
        except: chip_status = "⚠️ 籌碼資料伺服器連線異常或超時。"

        bull_aligned = latest['Bull_Aligned'].item()
        if bull_aligned and close > ma60: trend_msg = f"均線呈現標準「多頭排列」，長中短天期趨勢一致向上。"
        elif close > ma20 and close > ma60: trend_msg = f"目前股價 (`{close}`) 站穩月季線，屬偏多震盪格局。"
        elif close < ma20 and close < ma60: trend_msg = f"股價跌破生命線，處於空頭弱勢格局，易跌難漲。"
        else: trend_msg = f"目前股價處於月線與季線之間，屬於震盪整理、方向未明階段。"
        
        if vol > vol_5ma * 2.0: vol_msg = f"當日 `{vol_lots:,}` 張，達 5 日均量 2 倍以上，屬「**爆量/攻擊量**」格局。"
        elif vol > vol_5ma * 1.5: vol_msg = f"當日 `{vol_lots:,}` 張，達 5 日均量 1.5 倍，屬「**攻擊推升量**」。"
        elif vol < vol_5ma * 0.7: vol_msg = f"當日 `{vol_lots:,}` 張，低於 5 日均量 70%，屬「**極度量縮/窒息量**」冷卻狀態。"
        else: vol_msg = f"當日 `{vol_lots:,}` 張，與近期均量相近，動能維持常態。"

        def1_low, def1_high = round(ma10 - (atr * 0.5), 1), round(ma10 + (atr * 0.5), 1)
        def2_low, def2_high = round(ma20 - (atr * 0.5), 1), round(ma20 + (atr * 0.5), 1)

        now_str = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
        
        msg = f"🔍 **【法人級 個股深度健檢】**\n"
        msg += f"📌 **{code} {stock_name}** | 檢測時間：`{now_str}` | 收盤價：`{close}`\n"
        msg += "----------------------------------------\n"
        msg += "**一、產業定錨與估值 (Fundamentals & Valuation)**\n"
        msg += f"• **產業定位**：【{sector}】\n"
        msg += f"• **估值評級**：目前本益比 `{pe_ratio}` 倍。{val_status}\n\n"
        msg += "**二、技術與量價動能 (Momentum & Volume)**\n"
        msg += f"• **量能結構**：{vol_msg}\n"
        msg += f"• **趨勢狀態**：{trend_msg}\n"
        msg += f"• **RSI (14日)**: `{rsi}` (動能狀態)\n"
        msg += f"• **MACD 狀態**: `{macd}` vs `{sig}` (快慢線)\n\n"
        msg += "**三、ATR 波動防禦點位 (Risk & Entry)**\n"
        msg += f"• **日均波動(ATR)**: `{round(atr, 2)}` 元 (設定停損的參考級距)\n"
        msg += f"• **近期短壓 (布林上軌)**: 約 `{bb_upper}` 元附近\n"
        msg += f"• **第一防線 (10日動能支撐)**: `{def1_low} ~ {def1_high}` 元\n"
        msg += f"• **第二防線 (月線波段大本營)**: `{def2_low} ~ {def2_high}` 元\n"
        msg += "    *操作紀律：買黑不買紅，切勿在乖離過大時追高，建議於防線「量縮」時試單。*\n\n"
        msg += "**四、法人籌碼結構 (Chip Analysis)**\n"
        msg += f"{chip_status}\n\n"
        msg += "**五、總結與教練對策 (Manager's Plan)**\n"
        if close > ma20:
            msg += f"• **行動綱領**：趨勢偏多。若決定進場，請將總防守停損線設於 **`{round(ma20 - atr, 1)}`** (月線扣除一倍ATR)，若實體黑K跌破且三日內不站回，無條件停損出場。"
        else:
            msg += "• **行動綱領**：目前趨勢偏空。右側交易者應耐心等待股價「帶量站回月線」且「MACD黃金交叉」後，再行評估進場。"
            
        return msg
    except Exception as e:
        return f"❌ 分析過程發生錯誤: `{e}`"

# =====================================================================
# 🔬 評估模組 (彈性分級試單版)
# =====================================================================
def run_evaluation(code):
    tickers_dict = get_all_taiwan_tickers() 
    exact_ticker = f"{code}.TW"
    stock_name = "未知名稱"
    
    if f"{code}.TW" in tickers_dict: 
        exact_ticker = f"{code}.TW"
        stock_name = tickers_dict[exact_ticker]['name']
    elif f"{code}.TWO" in tickers_dict: 
        exact_ticker = f"{code}.TWO"
        stock_name = tickers_dict[exact_ticker]['name']
        
    try:
        market_uptrend = check_market_trend()
        df = yf.download(exact_ticker, period="4mo", progress=False)
        if df.empty or len(df) < 65: return f"⚠️ 找不到報價，或上市時間太短。"
            
        df = calculate_indicators(df)
        latest = df.iloc[-1]
        prev1 = df.iloc[-2]
        
        close = round(latest['Close'].item(), 2)
        low = round(latest['Low'].item(), 2)
        vol = latest['Volume'].item()
        vol_5ma = latest['Vol_5MA'].item()
        
        ma20 = round(latest['SMA_20'].item(), 2)
        ma60 = round(latest['SMA_60'].item(), 2)
        atr = latest['ATR_14'].item()
        
        bb_width = prev1['BB_Width'].item()
        macd, sig = latest['MACD'].item(), latest['Signal'].item()
        rsi, prev_rsi = latest['RSI'].item(), prev1['RSI'].item()
        max20_prev = prev1['Max_20'].item()
        
        vol_shrink_strict = vol < (vol_5ma * 0.7)
        vol_shrink_warn = vol < (vol_5ma * 0.95)
        vol_surge_strict = vol > (vol_5ma * 1.5)
        vol_surge_warn = vol > (vol_5ma * 1.2)
        
        bull_strict = latest['Bull_Aligned'].item()
        bull_warn = (close > ma20) and (ma20 > ma60)
        
        s1_base = (bb_width < 0.15) and (close > latest['BB_Upper'].item())
        s1_stat = 2 if (s1_base and vol_surge_strict and bull_strict) else (1 if (s1_base and vol_surge_warn and bull_warn) else 0)
        
        s2_base = (macd > sig) and (prev1['MACD'].item() <= prev1['Signal'].item())
        s2_stat = 2 if (s2_base and bull_strict) else (1 if (s2_base and bull_warn) else 0)
        
        s3_base = (close < ma20) and (rsi >= 30)
        s3_stat = 2 if (s3_base and prev_rsi < 30) else (1 if (s3_base and prev_rsi < 35) else 0)
        
        s4_base = (low <= ma20 + (atr*0.5)) and (close >= ma20 * 0.98)
        s4_stat = 2 if (s4_base and vol_shrink_strict and bull_strict) else (1 if (s4_base and vol_shrink_warn and bull_warn) else 0)
        
        s5_base = (close >= max20_prev)
        s5_stat = 2 if (s5_base and vol_surge_strict and bull_strict) else (1 if (s5_base and vol_surge_warn and bull_warn) else 0)

        market_warning = ""
        if not market_uptrend:
            s1_stat = s5_stat = 0
            market_warning = "⚠️ **[大盤警示]** 台灣加權指數跌破月線，系統強制關閉「動能突破」策略，嚴防假突破！\n"

        def get_stat_text(stat, msg_perfect, msg_warn):
            if stat == 2: return f"✅ **完美觸發** [{msg_perfect}]"
            elif stat == 1: return f"🟡 **溫和達標** [{msg_warn}]"
            else: return "❌ 未達標"

        msg = f"🔬 **【AI 量化打擊區 X 光機】(彈性試單版)**\n"
        msg += f"📌 **{code} {stock_name}** | 收盤價: `{close}` | 日均波動 ATR: `{round(atr, 2)}`\n"
        msg += f"📊 5日均量基準: `{int(vol_5ma/1000):,}` 張 | 當日成交: `{int(vol/1000):,}` 張\n"
        msg += "=========================\n"
        if market_warning: msg += market_warning + "=========================\n"
        
        msg += f"💥 **策略 1 (布林帶量突破)**: {get_stat_text(s1_stat, '量增>1.5倍/完美多頭', '量能溫和/趨勢盤堅')}\n"
        msg += f"🏄‍♂️ **策略 2 (MACD多方發動)**: {get_stat_text(s2_stat, '完美多頭', '站穩月線')}\n"
        msg += f"🎣 **策略 3 (極端超賣反彈)**: {get_stat_text(s3_stat, '極端超賣<30', '中度超賣<35')}\n"
        msg += f"🛡️ **策略 4 (多頭縮量回踩)**: {get_stat_text(s4_stat, '極限窒息量<70%', '量縮<95%')}\n"
        msg += f"🚀 **策略 5 (帶量創波段高)**: {get_stat_text(s5_stat, '量增>1.5倍/完美多頭', '量能溫和/趨勢盤堅')}\n"
        msg += "=========================\n"
        
        has_perfect = any(stat == 2 for stat in [s1_stat, s2_stat, s3_stat, s4_stat, s5_stat])
        has_warn = any(stat == 1 for stat in [s1_stat, s2_stat, s3_stat, s4_stat, s5_stat])
        
        if has_perfect or has_warn:
            matched_strats = []
            if s1_stat > 0: matched_strats.append(("1", s1_stat))
            if s2_stat > 0: matched_strats.append(("2", s2_stat))
            if s3_stat > 0: matched_strats.append(("3", s3_stat))
            if s4_stat > 0: matched_strats.append(("4", s4_stat))
            if s5_stat > 0: matched_strats.append(("5", s5_stat))
            
            strat_display = ", ".join([f"策略{s[0]}" for s in matched_strats])
            best_strat_num = matched_strats[0][0] 
            
            config = load_config()
            proxy_equity = config.get('total_capital', 0) + load_history().get('total_pnl', 0)
            
            stop_loss_price = round(close - (1.5 * atr), 2)
            risk_per_share = close - stop_loss_price
            
            if proxy_equity > 0 and risk_per_share > 0:
                risk_pct = 0.02 if has_perfect else 0.01
                max_loss_amt = int(proxy_equity * risk_pct)
                suggested_shares = int(max_loss_amt / risk_per_share)
                suggested_amt = int(suggested_shares * close)
                
                risk_advice = (
                    f"⚖️ **機構級資金控管 (風險平價模型)**\n"
                    f"   👉 建議防守線 (1.5xATR)：破 **`{stop_loss_price}`** 元停損\n"
                    f"   👉 單筆風險上限 ({int(risk_pct*100)}%)：約 `{max_loss_amt:,}` 元\n"
                    f"   👉 換算**建議買進股數**：最多 **`{suggested_shares:,}` 股** (總投入約 `{suggested_amt:,}` 元)\n"
                )
            else:
                risk_advice = f"⚖️ **防守建議**：將防守線設於 `{round(close - (1.5 * atr), 2)}`，嚴守紀律。\n"
            
            if has_perfect:
                msg += f"💡 **【AI 教練評估】: 發現高勝率機會！**\n🔥 該股符合 **{strat_display}** 完美觸發訊號，動能充沛。\n{risk_advice}\n進場請用 `!新增 {code} {close} [股數] {best_strat_num}` 建立監控！"
            else:
                msg += f"💡 **【AI 教練評估】: 具備潛力，建議小注試單。**\n🟡 該股符合 **{strat_display}** 溫和達標訊號，但動能或趨勢尚未達完美狀態。\n⚠️ **因僅為溫和達標，建議將部位減半試單。**\n{risk_advice}\n進場請用 `!新增 {code} {close} [股數] {best_strat_num}` 建立監控！"
        else:
            msg += f"💡 **【AI 教練評估】: 建議觀望 👀**\n目前未觸發任何量價攻擊或防守條件，資金切勿在此無效率區間消耗。"
            
        return msg
    except Exception as e:
        return f"❌ 評估過程發生錯誤: `{e}`"

# =====================================================================
# 🏥 庫存健康檢查模組
# =====================================================================
def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空，請用 `!新增` 指令建立股票。不知道怎麼用請輸入 `!指令`"
    
    market_uptrend = check_market_trend()
    tickers_dict = get_all_taiwan_tickers() 
    
    config = load_config()
    base_capital = config.get('total_capital', 0)
    history = load_history()
    history_pnl = history.get('total_pnl', 0)
    
    total_cost_of_holdings = 0
    total_market_value = 0
    stock_messages = []
    
    for code, info in portfolio.items():
        exact_ticker = f"{code}.TW"
        if f"{code}.TW" in tickers_dict: exact_ticker = f"{code}.TW"
        elif f"{code}.TWO" in tickers_dict: exact_ticker = f"{code}.TWO"
        
        try:
            df = yf.download(exact_ticker, period="3mo", progress=False)
            stock_name = info.get('name', '')
            if not stock_name or stock_name == "未知名稱":
                if exact_ticker in tickers_dict: stock_name = tickers_dict[exact_ticker]['name']
                
            display_title = f"{code} {stock_name}".strip()
            
            if df.empty or len(df) <= 30:
                stock_messages.append(f"❌ **{display_title}**: 報價抓取失敗\n\n")
                continue
                
            df = calculate_indicators(df)
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            
            close = round(latest['Close'].item(), 2)
            low = round(latest['Low'].item(), 2)
            open_px = latest['Open'].item()
            vol = latest['Volume'].item()
            
            cost = info.get('buy_price', 0)
            shares = info.get('shares', 1000)
            
            item_cost = int(round(cost * shares))
            item_value = int(round(close * shares))
            total_cost_of_holdings += item_cost
            total_market_value += item_value
            
            profit_amount = item_value - item_cost
            profit_pct = round(((close - cost) / cost) * 100, 2) if cost > 0 else 0
            
            strat = info.get('strategy', '無')
            tp_pct, sl_pct = info.get('tp_pct', None), info.get('sl_pct', None)
            ma5, ma10, ma20 = round(latest['SMA_5'].item(), 2), round(latest['SMA_10'].item(), 2), round(latest['SMA_20'].item(), 2)
            ma60 = round(latest['SMA_60'].item(), 2)
            vol_5ma = latest['Vol_5MA'].item()
            bb_upper = latest['BB_Upper'].item()
            bb_width = prev['BB_Width'].item()
            macd_val, sig_val = latest['MACD'].item(), latest['Signal'].item()
            hist_val, prev_hist = latest['MACD_Hist'].item(), prev['MACD_Hist'].item()
            prev_macd = prev['MACD'].item()
            rsi_val, prev_rsi = latest['RSI'].item(), prev['RSI'].item()
            max20_prev = prev['Max_20'].item()
            is_uptrend = latest['SMA_60'].item() > prev['SMA_60'].item()
            
            vol_surge_pass = vol > (vol_5ma * 1.5)
            vol_shrink_pass = vol < (vol_5ma * 0.7)
            is_bear_candle = close < open_px 
            
            bb_pass = (bb_width < 0.10) and (close > bb_upper) and vol_surge_pass and is_uptrend and (close > open_px)
            macd_pass = is_uptrend and (close > ma60) and (macd_val > sig_val) and (macd_val > prev_macd)
            rsi_pass = (close < ma20) and (prev_rsi < 35) and (rsi_val >= 30) and (rsi_val > prev_rsi)
            pullback_pass = is_uptrend and (close > ma60) and (low <= ma20 * 1.03) and (close >= ma20) and vol_shrink_pass
            breakout_pass = is_uptrend and (ma20 > ma60) and (close >= max20_prev) and vol_surge_pass
            
            if not market_uptrend:
                bb_pass = False
                breakout_pass = False
            
            alert_msg = ""
            
            if tp_pct and profit_pct >= float(tp_pct): alert_msg = f"💰 [已達停利點] 報酬率 {profit_pct}% (+{tp_pct}%)！"
            elif sl_pct and profit_pct <= -float(sl_pct): alert_msg = f"🛑 [已達停損點] 報酬率 {profit_pct}% (-{sl_pct}%)！"
            
            panel_str = ""
            if alert_msg:
                panel_str = f"   👉 {alert_msg}"
            else:
                is_s15 = "1" in strat or "布林" in strat or "5" in strat or "創高" in strat
                is_s2 = "2" in strat or "MACD" in strat
                is_s3 = "3" in strat or "RSI" in strat
                is_s4 = "4" in strat or "回踩" in strat
                
                msg_15 = ""
                if close < ma20 and vol_surge_pass:
                    msg_15 = "☠️ [爆量破月線] 法人倒貨，波段防守徹底崩潰，無條件清倉！"
                elif close < ma20:
                    msg_15 = "🚨 [破月線] 動能波段皆破壞，建議清倉。"
                elif close < ma10 and vol_surge_pass:
                    msg_15 = "🩸 [爆量破10日線] 高檔主力出貨，短線多單快逃！"
                elif close < ma10:
                    msg_15 = "⚠️ [破10日線] 跌破動能線，波段減碼一半。"
                elif close < ma5 and vol_surge_pass and is_bear_candle:
                    msg_15 = "⚠️ [高檔爆量黑K] 漲多爆量不漲/收黑，主力疑獲利了結，建議減碼！"
                elif close < ma5 and vol_shrink_pass:
                    msg_15 = "🤔 [量縮破5日] 跌破短線，但極度量縮，可視為主力洗盤續抱。"
                elif close < ma5:
                    msg_15 = "🤔 [破5日線] 短線強勢慣性改變，建議部分獲利了結。"
                elif bb_pass: msg_15 = "🎯 [突破發動] 帶量突破上軌，動能攻擊中！"
                elif breakout_pass: msg_15 = "🎯 [創高發動] 帶量突破近一月新高，強勢上攻！"
                else: msg_15 = "🚀 [強勢多頭] 延續上攻慣性，緊抱！"

                msg_2 = ""
                if close < ma20 and vol_surge_pass:
                    msg_2 = "☠️ [爆量破月線] 趨勢防守底遭重挫大出貨，清倉！"
                elif close < ma20: 
                    msg_2 = "🚨 [破月線] 跌破波段生命線，建議出場。"
                elif macd_val < sig_val: 
                    msg_2 = "⚠️ [MACD死叉] 短線出場；波段考慮減碼。"
                elif hist_val < prev_hist < df.iloc[-3]['MACD_Hist'].item(): 
                    msg_2 = "🤔 [紅柱連縮] 攻擊動能衰退，短線準備落跑。"
                elif macd_pass: msg_2 = "🎯 [趨勢發動] MACD多頭發散，新波段攻擊！"
                else: msg_2 = "🌊 [波段健康] 趨勢向上無虞。"

                msg_3 = ""
                if close < ma20 * 0.97 and vol_surge_pass: 
                    msg_3 = "☠️ [爆量破底] 反彈徹底失敗且遭遇追殺，無懸念停損！"
                elif close < ma20 * 0.97: 
                    msg_3 = "🚨 [破底] 跌破月線3%，反彈失敗，停損。"
                elif rsi_val < prev_rsi and prev_rsi > 70: 
                    msg_3 = "💰 [高檔反轉] 短線停利；波段分批減碼。"
                elif rsi_pass: msg_3 = "🎯 [抄底發動] 跌破超賣區後翻揚，反彈確立！"
                else: msg_3 = f"🎣 [RSI {round(rsi_val, 1)}] 處於反彈或震盪週期。"

                msg_4 = ""
                if close < ma20 * 0.97 and vol_surge_pass: 
                    msg_4 = "☠️ [爆量貫破] 防守區遭主力帶量倒貨擊穿，立即停損！"
                elif close < ma20 * 0.97: 
                    msg_4 = "🚨 [防守貫破] 跌穿3%主力洗盤區，停損出場。"
                elif close < ma20 and vol_shrink_pass: 
                    msg_4 = "🛡️ [量縮破月線] 進入洗盤區但極度量縮，防守有效，可多看一天。"
                elif close < ma20: 
                    msg_4 = "⚠️ [支撐測試] 跌破月線，進入3%洗盤區，密切觀察。"
                elif pullback_pass: msg_4 = "🎯 [回踩發動] 縮量回測月線有守，防守加碼點！"
                else: msg_4 = "🛡️ [月線有守] 支撐強勁，安全續抱。"

                s15_label = "🔥 動能/創高" + (" (★)" if is_s15 else "")
                s2_label  = "🌊 波段趨勢" + (" (★)" if is_s2 else "")
                s3_label  = "🎣 逆勢反彈" + (" (★)" if is_s3 else "")
                s4_label  = "🛡️ 支撐防守" + (" (★)" if is_s4 else "")

                panel_str = (
                    f"   [關鍵位階]: 5日 `{ma5}` | 10日 `{ma10}` | 月線 `{ma20}`\n"
                    f"   -------------------------\n"
                    f"   {s15_label}: {msg_15}\n"
                    f"   {s2_label}: {msg_2}\n"
                    f"   {s3_label}: {msg_3}\n"
                    f"   {s4_label}: {msg_4}"
                )

            tp_sl_info = f" | 停利: `+{tp_pct}%` 停損: `-{sl_pct}%`" if (tp_pct or sl_pct) else " | 風控: `未設定`"
            pnl_amt_str = f"+{profit_amount:,}" if profit_amount >= 0 else f"{profit_amount:,}"
            
            s_msg = f"📌 **{display_title}**\n"
            s_msg += f"   市價: `{close}` | 均價: `{cost}` | 股數: `{shares:,}` 股\n"
            s_msg += f"   總成本: `{item_cost:,}` | 總市值: `{item_value:,}`\n"
            s_msg += f"   帳面損益: **{pnl_amt_str}** (`{profit_pct}%`){tp_sl_info}\n"
            s_msg += f"   策略: `{strat}`\n{panel_str}\n=========================\n"
            stock_messages.append(s_msg)
            
        except Exception as e:
            stock_messages.append(f"❌ **{code} {info.get('name', '')}**: 運算錯誤 ({e})\n\n")

    header_msg = "📊 **【雲端精準監控戰報】(V13 彈性試單版)**\n"
    if not market_uptrend:
        header_msg += "⚠️ **[大盤警示]** 加權指數跌破月線，系統已自動關閉攻擊判定！\n"
    header_msg += "=========================\n"
    
    if base_capital > 0:
        current_cash = base_capital + history_pnl - total_cost_of_holdings
        total_equity = current_cash + total_market_value
        position_level = round((total_market_value / total_equity) * 100, 1) if total_equity > 0 else 0
        unrealized_pnl = total_market_value - total_cost_of_holdings
        unrealized_pct = round((unrealized_pnl / total_cost_of_holdings) * 100, 2) if total_cost_of_holdings > 0 else 0
        
        pnl_sign = "+" if unrealized_pnl >= 0 else ""
        header_msg += f"🏦 **帳戶總權益**: `{int(total_equity):,}` 元\n"
        header_msg += f"💵 **可用現金**: `{int(current_cash):,}` 元 | 📦 **股票市值**: `{int(total_market_value):,}` 元\n"
        header_msg += f"📈 **未實現損益**: `{pnl_sign}{int(unrealized_pnl):,}` 元 (`{pnl_sign}{unrealized_pct}%`)\n"
        header_msg += f"⚖️ **持股水位**: `{position_level}%`\n"
        header_msg += "=========================\n"
        
    full_msg = header_msg + "".join(stock_messages)
    return full_msg

# =====================================================================
# 🤖 Discord 機器人主程式
# =====================================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
bot.remove_command('help')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if '\n' in message.content:
        lines = message.content.split('\n')
        for line in lines:
            line = line.strip()
            if line.startswith(bot.command_prefix):
                msg_copy = copy.copy(message)
                msg_copy.content = line
                await bot.process_commands(msg_copy)
    else:
        await bot.process_commands(message)

@tasks.loop(minutes=30)
async def auto_report():
    tw_tz = timezone(timedelta(hours=8))
    now = datetime.now(tw_tz)
    if now.weekday() > 4: return 
    current_time = now.time()
    
    if time(hour=9, minute=30) <= current_time <= time(hour=14, minute=0):
        channel = bot.get_channel(PUSH_CHANNEL_ID)
        if channel:
            result = await asyncio.to_thread(run_health_check)
            full_msg = f"🔔 **【盤中即時監控】{now.strftime('%H:%M')} 戰報**\n{result}"
            
            chunks = []
            current_chunk = ""
            for line in full_msg.split('\n'):
                if len(current_chunk) + len(line) + 1 > 1900:
                    chunks.append(current_chunk)
                    current_chunk = line + "\n"
                else:
                    current_chunk += line + "\n"
            if current_chunk:
                chunks.append(current_chunk)
                
            for chunk in chunks:
                await channel.send(chunk)

@bot.event
async def on_ready():
    print(f"✅ 雲端小秘書登入成功: {bot.user}")
    if not auto_report.is_running(): auto_report.start()

# =====================================================================
# 💬 使用者手動控制指令區
# =====================================================================
@bot.command(aliases=['help', '幫助'])
async def 指令(ctx):
    embed_cmd = discord.Embed(
        title="🤖 雲端小秘書 - 指令大全",
        description="老闆，請隨時對我下達以下指令（格式內的 `[ ]` 請記得空一格）：",
        color=0x2ECC71
    )
    embed_cmd.add_field(name="🏦 `!本金 [金額]`", value="設定初始帳戶總資金，解鎖法人級儀表板。", inline=False)
    embed_cmd.add_field(name="📦 `!庫存`", value="秒速清點精簡版庫存與帳面賺賠明細。", inline=False)
    embed_cmd.add_field(name="🔍 `!健檢`", value="360度全方位掃描目前庫存狀態與操盤教練對策。", inline=False)
    embed_cmd.add_field(name="🔬 `!評估 [代號]`", value="個股攻擊雷達！套用彈性分級試單濾網。", inline=False)
    embed_cmd.add_field(name="📑 `!分析 [代號]`", value="法人級個股深度研究報告 (含外資投信籌碼)。", inline=False)
    embed_cmd.add_field(name="📥 `!新增 [代號] [均價] [股數] [策略] [停利%] [停損%]`", value="將股票交給小秘書監控 (名稱全自動抓取)。", inline=False)
    embed_cmd.add_field(name="✏️ `!部位 [代號] [新均價] [新總股數]`", value="手動校正持股的成本價與總股數。", inline=False)
    embed_cmd.add_field(name="🛡️ `!風控 [代號] [停利%] [停損%]`", value="隨時更新股票的停損停利點。", inline=False)
    embed_cmd.add_field(name="⚙️ `!策略 [代號] [策略代號]`", value="修改持股的防護策略。", inline=False)
    embed_cmd.add_field(name="🗑️ `!刪除 [代號]`", value="將股票從監控清單中移除。", inline=False)
    embed_cmd.add_field(name="💸 `!賣出 [代號] [賣出價] [賣出股數]`", value="結算並記錄損益。股數留空則全數賣出。", inline=False)
    embed_cmd.add_field(name="🏆 `!績效 [YYYY-MM]`", value="查看總績效與月度績效明細。", inline=False)
    
    # 🆕 策略代號對照表 (親民白話文版)
    strat_desc = (
        "**1** ➡️ `布林帶量突破`：盤整很久後，突然爆量衝破上軌，抓準備飆升的發動點。\n"
        "**2** ➡️ `MACD趨勢波段`：長短均線多頭排列且MACD轉強，適合穩健抱波段吃魚身。\n"
        "**3** ➡️ `極端超賣反彈`：跌到極致後指標翻揚，適合搶跌深反彈的左側摸底。\n"
        "**4** ➡️ `多頭縮量回踩`：強勢股拉回月線且量縮到極致(沒人賣)，高勝率的安全上車點。\n"
        "**5** ➡️ `帶量創波段高`：爆量突破近期高點，適合順勢追擊極強勢的飆股。"
    )
    embed_cmd.add_field(name="📋 【策略代號對照表 & 白話文說明】 (新增/修改策略時使用)", value=strat_desc, inline=False)
    
    await ctx.send(embed=embed_cmd)

@bot.command()
async def 本金(ctx, amount: float):
    config = load_config()
    config['total_capital'] = amount
    save_config(config)
    await ctx.send(f"✅ 初始本金已成功設定為: `{int(amount):,}` 元！\n系統將自動結合您的「歷史實現損益」來精準計算您的即時可用現金與帳戶總權益。")

@bot.command()
async def 庫存(ctx):
    msg = await ctx.send("⏳ 正在清點您的精簡版庫存與帳務明細...")
    try:
        portfolio = load_data()
        if not portfolio:
            await msg.edit(content="⚠️ 資料庫為空，目前沒有任何庫存紀錄。")
            return

        tickers_dict = get_all_taiwan_tickers() 
        config = load_config()
        base_capital = config.get('total_capital', 0)
        history = load_history()
        history_pnl = history.get('total_pnl', 0)
        
        total_cost_of_holdings = 0
        total_market_value = 0
        
        embed = discord.Embed(title="📦 【雲端小秘書 - 庫存總覽】", color=0x3498DB)
        
        for code, info in portfolio.items():
            exact_ticker = f"{code}.TW"
            if f"{code}.TW" in tickers_dict: exact_ticker = f"{code}.TW"
            elif f"{code}.TWO" in tickers_dict: exact_ticker = f"{code}.TWO"
            
            try:
                df = yf.download(exact_ticker, period="5d", progress=False)
                if df.empty:
                    close = info.get('buy_price', 0)
                else:
                    close = round(df['Close'].iloc[-1].item(), 2)
            except:
                close = info.get('buy_price', 0)

            cost = info.get('buy_price', 0)
            shares = info.get('shares', 1000)
            stock_name = info.get('name', '未知名稱')
            if not stock_name or stock_name == "未知名稱":
                if exact_ticker in tickers_dict: stock_name = tickers_dict[exact_ticker]['name']
                
            item_cost = int(round(cost * shares))
            item_value = int(round(close * shares))
            total_cost_of_holdings += item_cost
            total_market_value += item_value
            
            profit_amount = item_value - item_cost
            profit_pct = round(((close - cost) / cost) * 100, 2) if cost > 0 else 0
            
            pnl_sign = "+" if profit_amount >= 0 else ""
            
            field_value = f"市價: `{close}` | 均價: `{cost}` | 股數: `{shares:,}`\n帳面損益: **{pnl_sign}{profit_amount:,}** ({pnl_sign}{profit_pct}%)"
            embed.add_field(name=f"📌 {code} {stock_name}", value=field_value, inline=False)

        if base_capital > 0:
            current_cash = base_capital + history_pnl - total_cost_of_holdings
            total_equity = current_cash + total_market_value
            unrealized_pnl = total_market_value - total_cost_of_holdings
            unrealized_pct = round((unrealized_pnl / total_cost_of_holdings) * 100, 2) if total_cost_of_holdings > 0 else 0
            
            pnl_sign = "+" if unrealized_pnl >= 0 else ""
            
            desc = (
                f"🏦 **帳戶總權益**: `{int(total_equity):,}` 元\n"
                f"💵 **可用現金**: `{int(current_cash):,}` 元 | 📦 **股票市值**: `{int(total_market_value):,}` 元\n"
                f"📈 **總未實現損益**: **{pnl_sign}{int(unrealized_pnl):,}** ({pnl_sign}{unrealized_pct}%)\n"
                f"========================="
            )
            embed.description = desc
            
        await msg.edit(content=None, embed=embed)
        
    except Exception as e:
        await msg.edit(content=f"❌ 查詢過程發生系統錯誤: `{str(e)}`")

@bot.command()
async def 分析(ctx, code: str):
    msg = await ctx.send(f"⏳ 正在調閱 `{code}` 的技術線圖與三大法人籌碼，撰寫 AI 深度健檢報告...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_deep_analysis, code), timeout=30.0)
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 運算逾時，籌碼伺服器連線不穩，請稍後再試。")
    except Exception as e:
        await msg.edit(content=f"❌ 分析過程發生系統錯誤: `{str(e)}`")

@bot.command()
async def 評估(ctx, code: str):
    msg = await ctx.send(f"⏳ 正在調閱 `{code}` 的技術線圖，啟動量化打擊區與資金模型分析...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_evaluation, code), timeout=30.0)
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 運算逾時，Yahoo 財經連線不穩，請稍後再試。")
    except Exception as e:
        await msg.edit(content=f"❌ 評估過程發生系統錯誤: `{str(e)}`")

@bot.command()
async def 健檢(ctx):
    msg = await ctx.send("⏳ 正在啟動 360 度全方位庫存診斷與資金部位結算...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_health_check), timeout=120.0)
        
        if len(result) <= 1900:
            await msg.edit(content=result)
        else:
            await msg.edit(content="⏳ 報告長度達到上限，小秘書正在為您分頁傳輸...")
            chunks = []
            current_chunk = ""
            for line in result.split('\n'):
                if len(current_chunk) + len(line) + 1 > 1900:
                    chunks.append(current_chunk)
                    current_chunk = line + "\n"
                else:
                    current_chunk += line + "\n"
            if current_chunk:
                chunks.append(current_chunk)
                
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await msg.edit(content=chunk)
                else:
                    await ctx.send(chunk)
                    
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 運算逾時，Yahoo 財經連線不穩，請稍後再試。")

@bot.command()
async def 新增(ctx, code: str, price: float, shares: int, strat_num: str, tp: float = None, sl: float = None):
    tickers_dict = get_all_taiwan_tickers()
    exact_ticker = f"{code}.TW" if f"{code}.TW" in tickers_dict else f"{code}.TWO"
    if exact_ticker in tickers_dict: 
        name = tickers_dict[exact_ticker]['name']
    else: 
        name = "未知名稱"
            
    full_strat = STRAT_MAP.get(strat_num, strat_num) 
    data = load_data()
    
    is_addon = False
    old_shares = 0
    old_price = 0
    
    if code in data:
        is_addon = True
        old_shares = data[code].get('shares', 1000)
        old_price = data[code].get('buy_price', 0)
        
        total_cost = (old_price * old_shares) + (price * shares)
        new_total_shares = old_shares + shares
        new_avg_price = round(total_cost / new_total_shares, 2)
        
        price = new_avg_price
        shares = new_total_shares
        
        if tp is None: tp = data[code].get('tp_pct')
        if sl is None: sl = data[code].get('sl_pct')

    data[code] = {"buy_price": price, "shares": shares, "strategy": full_strat, "name": name, "tp_pct": tp, "sl_pct": sl}
    save_data(data)
    
    display_title = f"{code} {name}".strip()
    风控文 = f" | 停利: +{tp}% 停損: -{sl}%" if (tp or sl) else " | 未設定風控"
    
    if is_addon:
        await ctx.send(f"🔄 已自動為 **{display_title}** 執行【加碼/攤平】計算！\n加碼前: 均價 `{old_price}` ({old_shares:,} 股)\n新均價: `{price}` (總計 `{shares:,}` 股)\n當前策略: `{full_strat}`{风控文}")
    else:
        await ctx.send(f"✅ 已新增 **{display_title}**\n總投入: `{int(price*shares):,}` (均價 {price} x {shares:,} 股)\n策略: `{full_strat}`{风控文}")

@bot.command()
async def 部位(ctx, code: str, new_price: float, new_shares: int):
    data = load_data()
    if code in data:
        old_price = data[code].get('buy_price', 0)
        old_shares = data[code].get('shares', 1000)
        data[code]['buy_price'] = new_price
        data[code]['shares'] = new_shares
        save_data(data)
        name = data[code].get('name', '')
        await ctx.send(f"✅ **{code} {name}** 庫存已更新！\n成本價: `{old_price}` ➡️ `{new_price}`\n總股數: `{old_shares:,}` ➡️ `{new_shares:,}`")
    else:
        await ctx.send(f"⚠️ 老闆，你的監控清單裡找不到代號 **{code}** 喔！")

@bot.command()
async def 風控(ctx, code: str, tp: float, sl: float):
    data = load_data()
    if code in data:
        data[code]['tp_pct'] = tp
        data[code]['sl_pct'] = sl
        save_data(data)
        name = data[code].get('name', '')
        await ctx.send(f"✅ **{code} {name}** 風控設定成功！\n🎯 停利點: `+{tp}%`\n🛑 停損點: `-{sl}%`")
    else: await ctx.send(f"⚠️ 找不到代號 {code}。")

@bot.command()
async def 命名(ctx, code: str, name: str):
    data = load_data()
    if code in data:
        data[code]['name'] = name
        save_data(data)
        await ctx.send(f"✅ 已將代號 **{code}** 命名為 **{name}**")
    else: await ctx.send(f"⚠️ 找不到代號 {code}。")

@bot.command()
async def 刪除(ctx, code: str):
    data = load_data()
    if code in data:
        name = data[code].get('name', '')
        del data[code]
        save_data(data)
        await ctx.send(f"🗑️ 已從監控列表移除 **{code} {name}**。")
    else: await ctx.send(f"⚠️ 找不到代號 {code}")

@bot.command()
async def 策略(ctx, code: str, strat_num: str):
    full_strat = STRAT_MAP.get(strat_num, strat_num)
    data = load_data()
    if code in data:
        data[code]['strategy'] = full_strat
        save_data(data)
        name = data[code].get('name', '')
        await ctx.send(f"✅ **{code} {name}** 策略已更新為: `{full_strat}`")
    else: await ctx.send(f"⚠️ 找不到代號 {code}。")

@bot.command()
async def 賣出(ctx, code: str, sell_price: float, sell_shares: int = None):
    data = load_data()
    if code not in data:
        await ctx.send(f"⚠️ 老闆，你的監控清單裡找不到代號 **{code}** 的庫存記錄喔！")
        return
    
    info = data[code]
    buy_price = info.get('buy_price', 0)
    held_shares = info.get('shares', 1000) 
    name = info.get('name', '')
    strat = info.get('strategy', '無')
    
    if sell_shares is None or sell_shares <= 0:
        sell_shares = held_shares 
        
    if sell_shares > held_shares:
        await ctx.send(f"⚠️ 庫存不足！你手上只有 `{held_shares}` 股 **{code} {name}**，無法賣出 `{sell_shares}` 股。")
        return
    
    if not name or name == "未知名稱":
        tickers_dict = get_all_taiwan_tickers()
        exact_ticker = f"{code}.TW" if f"{code}.TW" in tickers_dict else f"{code}.TWO"
        if exact_ticker in tickers_dict: name = tickers_dict[exact_ticker]['name']
        else: name = "未知名稱"
    
    pnl_amount = (sell_price - buy_price) * sell_shares
    pnl_pct = round(((sell_price - buy_price) / buy_price) * 100, 2) if buy_price > 0 else 0
    
    msg = await ctx.send(f"⏳ 正在結算 **{code} {name}** 交易，計算損益中...")
    
    history = load_history()
    history['total_pnl'] += pnl_amount
    
    loss_reason = ""
    if pnl_amount < 0:
        await msg.edit(content=f"⏳ 正在結算 **{code} {name}**... 偵測到虧損，小秘書正在啟動 🩸AI戰敗檢討系統...")
        loss_reason = await asyncio.to_thread(run_loss_analysis, code, name, buy_price, sell_price, strat)
        
    record = {
        "date": datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M'),
        "code": code,
        "name": name,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "shares": sell_shares,
        "pnl": pnl_amount,
        "pnl_pct": pnl_pct,
        "strategy": strat,
        "loss_reason": loss_reason
    }
    history['trades'].append(record)
    save_history(history)
    
    is_partial_sell = False
    if sell_shares == held_shares:
        del data[code]
    else:
        is_partial_sell = True
        data[code]['shares'] -= sell_shares
    save_data(data)
    
    title = f"🤝 交易結算單：{code} {name}"
    if is_partial_sell: title += " (分批停利/損)"
        
    embed = discord.Embed(
        title=title,
        color=0xE74C3C if pnl_amount < 0 else 0x2ECC71 
    )
    embed.add_field(name="買入均價", value=f"`{buy_price}`", inline=True)
    embed.add_field(name="賣出價格", value=f"`{sell_price}`", inline=True)
    embed.add_field(name="交易股數", value=f"`{sell_shares:,}` 股", inline=True)
    
    pnl_str = f"+{int(pnl_amount):,}" if pnl_amount >= 0 else f"{int(pnl_amount):,}"
    pnl_pct_str = f"+{pnl_pct}%" if pnl_pct >= 0 else f"{pnl_pct}%"
    embed.add_field(name="💵 單筆結算損益", value=f"**{pnl_str} 元** ({pnl_pct_str})", inline=False)
    
    if is_partial_sell:
        embed.add_field(name="📦 剩餘庫存", value=f"尚有 `{data[code]['shares']:,}` 股持續監控中", inline=False)
    
    total_pnl = history['total_pnl']
    total_str = f"+{int(total_pnl):,}" if total_pnl >= 0 else f"{int(total_pnl):,}"
    embed.add_field(name="🏦 歷史累積總損益", value=f"**{total_str} 元**", inline=False)
    
    if loss_reason: embed.add_field(name="🩸 AI 戰敗檢討報告", value=loss_reason, inline=False)
    await msg.edit(content=None, embed=embed)

@bot.command()
async def 績效(ctx, target_month: str = None):
    history = load_history()
    trades = history.get('trades', [])
    
    if not trades:
        await ctx.send("📊 目前還沒有任何已結算的交易紀錄喔！")
        return
        
    total_all_time = history.get('total_pnl', 0)
    
    monthly_data = {}
    for t in trades:
        month_key = t['date'][:7] 
        if month_key not in monthly_data:
            monthly_data[month_key] = {'pnl': 0, 'wins': 0, 'losses': 0, 'details': []}
        monthly_data[month_key]['pnl'] += t['pnl']
        if t['pnl'] >= 0: monthly_data[month_key]['wins'] += 1
        else: monthly_data[month_key]['losses'] += 1
        monthly_data[month_key]['details'].append(t)
        
    if not target_month:
        target_month = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m')
        
    if target_month not in monthly_data and monthly_data:
        target_month = sorted(monthly_data.keys())[-1]

    embed = discord.Embed(title="🏆 雲端小秘書 - 月度績效戰報", color=0xF1C40F)
    total_str = f"+{int(total_all_time):,}" if total_all_time >= 0 else f"{int(total_all_time):,}"
    embed.add_field(name="🌍 歷史累積總損益 (All-Time)", value=f"**{total_str} 元**", inline=False)
    
    recent_months = sorted(monthly_data.keys(), reverse=True)[:3]
    month_summary_str = ""
    for m in recent_months:
        m_pnl = monthly_data[m]['pnl']
        m_str = f"+{int(m_pnl):,}" if m_pnl >= 0 else f"{int(m_pnl):,}"
        total_trades = monthly_data[m]['wins'] + monthly_data[m]['losses']
        m_win_rate = round(monthly_data[m]['wins'] / total_trades * 100, 1) if total_trades > 0 else 0
        month_summary_str += f"**{m}**: `{m_str}元` (勝率 {m_win_rate}%)\n"
        
    embed.add_field(name="📅 近期月份總結", value=month_summary_str, inline=False)
    
    if target_month in monthly_data:
        t_data = monthly_data[target_month]
        t_pnl = t_data['pnl']
        t_str = f"+{int(t_pnl):,}" if t_pnl >= 0 else f"{int(t_pnl):,}"
        t_total = t_data['wins'] + t_data['losses']
        t_win_rate = round(t_data['wins'] / t_total * 100, 1) if t_total > 0 else 0
        
        detail_str = ""
        for t in t_data['details']:
            p_str = f"+{int(t['pnl']):,}" if t['pnl'] >= 0 else f"{int(t['pnl']):,}"
            detail_str += f"• **{t['code']} {t['name']}**: `{p_str}元` ({t['pnl_pct']}%)\n"
            
        if len(detail_str) > 900: detail_str = detail_str[:900] + "...\n(為節省版面，隱藏部分明細)"
        embed.add_field(name=f"🔍 {target_month} 當月明細 (總計: {t_str}元 | 勝率: {t_win_rate}%)", value=detail_str, inline=False)
    else:
        embed.add_field(name=f"🔍 {target_month} 當月明細", value="該月份尚無結算的交易紀錄。", inline=False)
        
    embed.set_footer(text="💡 提示：輸入 `!績效 2026-04` 可調閱特定月份明細。")
    await ctx.send(embed=embed)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Cloud Secretary Guardian is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()

async def main():
    await asyncio.gather(start_web_server(), bot.start(TOKEN))

if __name__ == "__main__":
    asyncio.run(main())
