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
import logging

# 強制關閉 yfinance 煩人的紅字報錯
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

# 系統與金鑰設定
TOKEN = os.environ.get('DISCORD_TOKEN')
FINMIND_TOKEN = os.environ.get('FINMIND_TOKEN', "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiam9lbDA2MjYiLCJlbWFpbCI6ImpvZWwwNjI2QG1zbi5jb20iLCJ0b2tlbl92ZXJzaW9uIjowfQ.j1KeK6JfXNUX2WlEKYmdMctQV_9_xfwpzVlANplYafs")

PORTFOLIO_FILE = "my_portfolio.json"
TRADE_HISTORY_FILE = "trade_history.json"

# ========= 🚨 你的專屬設定 =========
PUSH_CHANNEL_ID = 1509058179458404495
# ============================================

STRAT_MAP = {
    "1": "1. 布林壓縮突破 (動能)",
    "2": "2. 雙均線+MACD (趨勢)",
    "3": "3. RSI超賣反彈 (逆勢)",
    "4": "4. 多頭縮量回踩 (高勝率防守)",
    "5": "5. 強勢創高確認 (高勝率攻擊)"
}

# =====================================================================
# 🗂️ 資料庫存取模組
# =====================================================================
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
# 🛡️ 核心：指標計算、環境濾網與戰敗分析
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
        
        market_uptrend = check_market_trend()
        reasons = []
        
        if not market_uptrend:
            reasons.append("📉 **大盤拖累**：加權指數跌破月線，覆巢之下無完卵，此筆屬於正確防守。")
        if sell_price < ma20:
            reasons.append("⚠️ **破底停損**：股價跌破月線(波段生命線)，技術面翻空。")
        elif sell_price < ma10 and ("1" in strat or "5" in strat):
            reasons.append("⚠️ **動能消散**：動能策略跌破 10 日線，強勢慣性改變。")
        if macd_val < sig_val:
            reasons.append("📉 **趨勢轉空**：MACD 出現死叉，買盤力道衰退。")
        if rsi < 40:
            reasons.append("🧊 **人氣退潮**：RSI 轉弱，陷入無量陰跌。")
        if buy_price > ma10 and sell_price < ma10 and not market_uptrend:
            reasons.append("💡 **AI 教練碎碎念**：大盤不佳時追高強勢股容易被洗，下次請等「縮量回踩」。")
            
        if not reasons:
            reasons.append("💡 **AI 教練碎碎念**：技術面無明顯嚴重破壞。此筆可能為洗盤或個人資金調度。")
            
        return "\n".join(reasons)
    except Exception as e:
        return f"分析失敗 ({e})"

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
        if df.empty or len(df) < 65:
            return f"⚠️ 找不到代號 {code} 的報價，或上市時間太短不足以計算季線與技術指標。"
            
        df = calculate_indicators(df)
        latest = df.iloc[-1]
        prev1 = df.iloc[-2]
        
        close = round(latest['Close'].item(), 2)
        low = round(latest['Low'].item(), 2)
        vol = latest['Volume'].item()
        open_px = latest['Open'].item()
        ma20 = round(latest['SMA_20'].item(), 2)
        ma60 = round(latest['SMA_60'].item(), 2)
        vol_5ma = latest['Vol_5MA'].item()
        bb_width = prev1['BB_Width'].item()
        macd, sig = latest['MACD'].item(), latest['Signal'].item()
        prev_macd = prev1['MACD'].item()
        rsi = latest['RSI'].item()
        prev_rsi = prev1['RSI'].item()
        max20_prev = prev1['Max_20'].item()
        is_uptrend = latest['SMA_60'].item() > prev1['SMA_60'].item()
        
        bb_pass = (bb_width < 0.10) and (close > latest['BB_Upper'].item()) and (vol > (vol_5ma * 1.5)) and is_uptrend and (close > open_px)
        macd_pass = is_uptrend and (close > ma60) and (macd > sig) and (macd > prev_macd)
        rsi_pass = (close < ma20) and (prev_rsi < 35) and (rsi >= 30) and (rsi > prev_rsi)
        pullback_pass = is_uptrend and (close > ma60) and (low <= ma20 * 1.03) and (close >= ma20) and (vol < vol_5ma)
        breakout_pass = is_uptrend and (ma20 > ma60) and (close >= max20_prev) and (vol > vol_5ma * 1.2)

        market_warning = ""
        if not market_uptrend:
            bb_pass = False
            breakout_pass = False
            market_warning = "⚠️ **[大盤警示]** 台灣加權指數目前跌破月線，系統已強制關閉「布林突破」與「強勢創高」策略，防禦假突破風險！\n"

        msg = f"🔬 **【個股 X 光機評估報告】**\n"
        msg += f"📌 **{code} {stock_name}** | 最新收盤價: `{close}`\n"
        msg += f"📊 基準: 月線 `{ma20}` | 季線 `{ma60}`\n"
        msg += "=========================\n"
        if market_warning: msg += market_warning + "=========================\n"
        
        if bb_pass: msg += f"💥 **策略 1 (布林動能)**: ✅ 帶量突破上軌\n"
        else: msg += f"💥 **策略 1 (布林動能)**: ❌ 未達標\n"
        if macd_pass: msg += f"🏄‍♂️ **策略 2 (MACD趨勢)**: ✅ MACD多頭發散中\n"
        else: msg += f"🏄‍♂️ **策略 2 (MACD趨勢)**: ❌ 未達標\n"
        if rsi_pass: msg += f"🎣 **策略 3 (RSI逆勢)**: ✅ 跌破超賣區後翻揚\n"
        else: msg += f"🎣 **策略 3 (RSI逆勢)**: ❌ 未達標\n"
        if pullback_pass: msg += f"🛡️ **策略 4 (縮量回踩)**: ✅ **[高勝率]** 縮量回測月線有守\n"
        else: msg += f"🛡️ **策略 4 (縮量回踩)**: ❌ 未達標\n"
        if breakout_pass: msg += f"🚀 **策略 5 (強勢創高)**: ✅ **[高勝率]** 帶量突破近一月新高\n"
        else: msg += f"🚀 **策略 5 (強勢創高)**: ❌ 未達標\n"
            
        msg += "=========================\n"
        
        if bb_pass or macd_pass or rsi_pass or pullback_pass or breakout_pass:
            matched_strats = []
            if bb_pass: matched_strats.append("策略1")
            if macd_pass: matched_strats.append("策略2")
            if rsi_pass: matched_strats.append("策略3")
            if pullback_pass: matched_strats.append("策略4")
            if breakout_pass: matched_strats.append("策略5")
            
            risk_pct = round(((close - ma20) / close) * 100, 2)
            if risk_pct > 0:
                suggested_alloc = round((2.0 / risk_pct) * 100, 1) 
                if suggested_alloc > 100: suggested_alloc = 100
                risk_advice = f"⚖️ **資金控管建議**：目前距離月線停損約 `-{risk_pct}%`。若嚴守單筆虧損不超過總資金2%之紀律，本檔建議最多投入總資金的 **`{suggested_alloc}%`**。\n"
            else:
                risk_advice = f"⚖️ **資金控管建議**：目前股價已在月線之下，若進場屬於左側摸底，請極度縮小部位。\n"
            
            msg += f"💡 **【AI 教練結論】: 建議買進！**\n🔥 該股目前符合 **{', '.join(matched_strats)}** 的發動訊號。\n{risk_advice}若決定進場，請用 `!新增 {code} {close} {matched_strats[0][-1]}` 加入小秘書監控！"
        else:
            msg += f"💡 **【AI 教練結論】: 建議觀望 👀**\n這檔股票目前技術面**並未觸發**任何高勝率或動能攻擊條件。請多看少做！"
            
        return msg
    except Exception as e:
        return f"❌ 評估過程發生錯誤: `{e}`"

# 🔥 終極進化：攻守一體的 360 度健檢面板
def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空，請用 `!新增` 指令建立股票。不知道怎麼用請輸入 `!指令`"
    
    # 掃描前先統一確認大盤趨勢，避免重複請求
    market_uptrend = check_market_trend()
    tickers_dict = get_all_taiwan_tickers() 
    
    msg = "📊 **【雲端精準監控戰報】(360度攻守一體版)**\n"
    if not market_uptrend:
        msg += "⚠️ **[大盤警示]** 加權指數跌破月線，系統已自動關閉「動能與突破」攻擊判定！\n"
    msg += "=========================\n"
    
    for code, info in portfolio.items():
        exact_ticker = f"{code}.TW"
        if f"{code}.TW" in tickers_dict: exact_ticker = f"{code}.TW"
        elif f"{code}.TWO" in tickers_dict: exact_ticker = f"{code}.TWO"
        
        try:
            df = yf.download(exact_ticker, period="3mo", progress=False)
            stock_name = info.get('name', '')
            if not stock_name and exact_ticker in tickers_dict:
                stock_name = tickers_dict[exact_ticker]['name']
                
            display_title = f"{code} {stock_name}".strip()
            
            if df.empty or len(df) <= 30:
                msg += f"❌ **{display_title}**: 報價抓取失敗\n\n"
                continue
                
            df = calculate_indicators(df)
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            
            close = round(latest['Close'].item(), 2)
            low = round(latest['Low'].item(), 2)
            open_px = latest['Open'].item()
            vol = latest['Volume'].item()
            cost = info.get('buy_price', 0)
            strat = info.get('strategy', '無')
            profit = round(((close - cost) / cost) * 100, 2) if cost > 0 else 0
            
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
            
            # 💡 計算攻擊訊號 (與評估邏輯同步)
            bb_pass = (bb_width < 0.10) and (close > bb_upper) and (vol > (vol_5ma * 1.5)) and is_uptrend and (close > open_px)
            macd_pass = is_uptrend and (close > ma60) and (macd_val > sig_val) and (macd_val > prev_macd)
            rsi_pass = (close < ma20) and (prev_rsi < 35) and (rsi_val >= 30) and (rsi_val > prev_rsi)
            pullback_pass = is_uptrend and (close > ma60) and (low <= ma20 * 1.03) and (close >= ma20) and (vol < vol_5ma)
            breakout_pass = is_uptrend and (ma20 > ma60) and (close >= max20_prev) and (vol > vol_5ma * 1.2)
            
            if not market_uptrend:
                bb_pass = False
                breakout_pass = False
            
            alert_msg = ""
            
            # 優先判定：自訂停損停利點 (最嚴格防線)
            if tp_pct and profit >= float(tp_pct): alert_msg = f"💰 [已達停利點] 報酬率 {profit}% (+{tp_pct}%)！"
            elif sl_pct and profit <= -float(sl_pct): alert_msg = f"🛑 [已達停損點] 報酬率 {profit}% (-{sl_pct}%)！"
            
            panel_str = ""
            if alert_msg:
                panel_str = f"   👉 {alert_msg}"
            else:
                # ==========================================
                # 🔥 AI 360 度攻守一體診斷系統
                # ==========================================
                is_s15 = "1" in strat or "布林" in strat or "5" in strat or "創高" in strat
                is_s2 = "2" in strat or "MACD" in strat
                is_s3 = "3" in strat or "RSI" in strat
                is_s4 = "4" in strat or "回踩" in strat
                
                # 1. 動能/創高視角 (S1/S5)
                msg_15 = ""
                if close < ma20: msg_15 = "🚨 [破月線] 動能波段皆破壞，建議清倉。"
                elif close < ma10: msg_15 = "⚠️ [破10日線] 短線強烈建議出場；波段減碼一半。"
                elif close < ma5: msg_15 = "🤔 [破5日線] 短線建議獲利了結；波段視為洗盤續抱。"
                elif bb_pass: msg_15 = "🎯 [突破發動] 帶量突破上軌，動能攻擊中！"
                elif breakout_pass: msg_15 = "🎯 [創高發動] 帶量突破近一月新高，強勢上攻！"
                elif "1" in strat and bb_upper and close < bb_upper and prev['Close'].item() > prev['BB_Upper'].item():
                    msg_15 = "💡 [回落通道] 漲多休息，進入高檔震盪。"
                else: msg_15 = "🚀 [強勢多頭] 延續上攻慣性，緊抱！"

                # 2. 波段趨勢視角 (S2)
                msg_2 = ""
                if close < ma20: msg_2 = "🚨 [破月線] 趨勢防守底線遭貫破，建議清倉！"
                elif macd_val < sig_val: msg_2 = "⚠️ [MACD死叉] 短線出場；波段考慮減碼。"
                elif hist_val < prev_hist < df.iloc[-3]['MACD_Hist'].item(): msg_2 = "🤔 [紅柱連縮] 短線準備落跑；波段續抱。"
                elif macd_pass: msg_2 = "🎯 [趨勢發動] MACD多頭發散，新波段攻擊中！"
                else: msg_2 = "🌊 [波段健康] 趨勢向上無虞。"

                # 3. 逆勢反彈視角 (S3)
                msg_3 = ""
                if close < ma20 * 0.97: msg_3 = "🚨 [破底] 跌破月線3%，反彈徹底失敗，停損！"
                elif rsi_val < prev_rsi and prev_rsi > 70: msg_3 = "💰 [高檔反轉] 短線全數停利；波段分批減碼。"
                elif rsi_pass: msg_3 = "🎯 [抄底發動] 跌破超賣區後翻揚，反彈確立！"
                else: msg_3 = f"🎣 [RSI {round(rsi_val, 1)}] 處於反彈或震盪週期。"

                # 4. 支撐防守視角 (S4)
                msg_4 = ""
                if close < ma20 * 0.97: msg_4 = "🚨 [防守貫破] 跌穿3%主力洗盤區，毫無懸念停損！"
                elif close < ma20: msg_4 = "⚠️ [支撐測試] 跌破月線，進入3%洗盤緩衝區，密切觀察。"
                elif pullback_pass: msg_4 = "🎯 [回踩發動] 縮量回測月線有守，絕佳防守加碼點！"
                else: msg_4 = "🛡️ [月線有守] 支撐強勁，安全續抱。"

                # 標記使用者當前選擇的策略
                s15_label = "🔥 動能/創高" + (" (★)" if is_s15 else "")
                s2_label  = "🌊 波段趨勢" + (" (★)" if is_s2 else "")
                s3_label  = "🎣 逆勢反彈" + (" (★)" if is_s3 else "")
                s4_label  = "🛡️ 支撐防守" + (" (★)" if is_s4 else "")

                # 組合優雅排版
                panel_str = (
                    f"   [關鍵位階]: 5日 `{ma5}` | 10日 `{ma10}` | 月線 `{ma20}`\n"
                    f"   -------------------------\n"
                    f"   {s15_label}: {msg_15}\n"
                    f"   {s2_label}: {msg_2}\n"
                    f"   {s3_label}: {msg_3}\n"
                    f"   {s4_label}: {msg_4}"
                )

            tp_sl_info = f" | 停利: `+{tp_pct}%` 停損: `-{sl_pct}%`" if (tp_pct or sl_pct) else " | 風控: `未設定`"
            
            msg += f"📌 **{display_title}**\n   市價: `{close}` | 成本: `{cost}` | 報酬: `{profit}%`{tp_sl_info}\n   策略: `{strat}`\n{panel_str}\n=========================\n"
        except Exception as e:
            msg += f"❌ **{code} {info.get('name', '')}**: 運算錯誤 ({e})\n\n"
            
    return msg

# =====================================================================
# 🤖 Discord 機器人主程式
# =====================================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
bot.remove_command('help')

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
            await channel.send(f"🔔 **【盤中即時監控】{now.strftime('%H:%M')} 戰報**\n{result}")

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
    embed_cmd.add_field(name="🔍 `!健檢`", value="360度全方位掃描目前庫存的狀態與攻擊訊號。", inline=False)
    embed_cmd.add_field(name="🔬 `!評估 [代號]`", value="個股 X 光機！幫你鑑定這檔股票是否符合買進策略。", inline=False)
    embed_cmd.add_field(name="📥 `!新增 [代號] [成本] [策略] [名稱] [停利%] [停損%]`", value="將股票交給小秘書監控 (名稱留空會自動抓取)。", inline=False)
    embed_cmd.add_field(name="✏️ `!成本 [代號] [新成本]`", value="修改持股的買入平均成本價。\n*範例: `!成本 2330 820`*", inline=False)
    embed_cmd.add_field(name="🛡️ `!風控 [代號] [停利%] [停損%]`", value="隨時更新股票的停損停利點。", inline=False)
    embed_cmd.add_field(name="⚙️ `!策略 [代號] [策略代號]`", value="修改持股的防護策略。", inline=False)
    embed_cmd.add_field(name="🗑️ `!刪除 [代號]`", value="將股票從監控清單中移除。", inline=False)
    embed_cmd.add_field(name="💸 `!賣出 [代號] [賣出價] [股數]`", value="結算交易並記錄損益(股數預設1000)。\n*範例: `!賣出 2330 850 2000`*", inline=False)
    embed_cmd.add_field(name="🏆 `!績效 [YYYY-MM]`", value="查看總績效與月度績效明細。\n*範例: `!績效` (看當月) 或 `!績效 2026-04`*", inline=False)
    
    await ctx.send(embed=embed_cmd)

@bot.command()
async def 評估(ctx, code: str):
    msg = await ctx.send(f"⏳ 正在調閱 `{code}` 的技術線圖，啟動量化打擊區與大盤濾網分析...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_evaluation, code), timeout=30.0)
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 運算逾時，Yahoo 財經連線不穩，請稍後再試。")
    except Exception as e:
        await msg.edit(content=f"❌ 評估過程發生系統錯誤: `{str(e)}`")

@bot.command()
async def 健檢(ctx):
    msg = await ctx.send("⏳ 正在啟動 360 度全方位庫存診斷系統 (含攻擊訊號掃描)...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_health_check), timeout=120.0)
        if len(result) > 1900: result = result[:1900] + "\n\n⚠️ ...(庫存過多，字數達 Discord 上限)"
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 運算逾時，Yahoo 財經連線不穩，請稍後再試。")

@bot.command()
async def 新增(ctx, code: str, price: float, strat_num: str, name: str = "", tp: float = None, sl: float = None):
    if not name:
        tickers_dict = get_all_taiwan_tickers()
        exact_ticker = f"{code}.TW" if f"{code}.TW" in tickers_dict else f"{code}.TWO"
        if exact_ticker in tickers_dict: name = tickers_dict[exact_ticker]['name']
        else: name = "未知名稱"
            
    full_strat = STRAT_MAP.get(strat_num, strat_num) 
    data = load_data()
    data[code] = {"buy_price": price, "strategy": full_strat, "name": name, "tp_pct": tp, "sl_pct": sl}
    save_data(data)
    
    display_title = f"{code} {name}".strip()
    风控文 = f" | 停利: +{tp}% 停損: -{sl}%" if (tp or sl) else " | 未設定風控"
    await ctx.send(f"✅ 已新增 **{display_title}**\n成本: `{price}`\n策略: `{full_strat}`{风控文}")

@bot.command()
async def 成本(ctx, code: str, new_price: float):
    data = load_data()
    if code in data:
        old_price = data[code].get('buy_price', 0)
        data[code]['buy_price'] = new_price
        save_data(data)
        name = data[code].get('name', '')
        await ctx.send(f"✅ **{code} {name}** 成本價已成功從 `{old_price}` 修改為 `{new_price}`！")
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
async def 賣出(ctx, code: str, sell_price: float, shares: int = 1000):
    data = load_data()
    if code not in data:
        await ctx.send(f"⚠️ 老闆，你的監控清單裡找不到代號 **{code}** 的庫存記錄喔！")
        return
    
    info = data[code]
    buy_price = info.get('buy_price', 0)
    name = info.get('name', '')
    strat = info.get('strategy', '無')
    
    if not name or name == "未知名稱":
        tickers_dict = get_all_taiwan_tickers()
        exact_ticker = f"{code}.TW" if f"{code}.TW" in tickers_dict else f"{code}.TWO"
        if exact_ticker in tickers_dict: name = tickers_dict[exact_ticker]['name']
        else: name = "未知名稱"
    
    pnl_amount = (sell_price - buy_price) * shares
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
        "shares": shares,
        "pnl": pnl_amount,
        "pnl_pct": pnl_pct,
        "strategy": strat,
        "loss_reason": loss_reason
    }
    history['trades'].append(record)
    save_history(history)
    
    del data[code]
    save_data(data)
    
    embed = discord.Embed(
        title=f"🤝 交易結算單：{code} {name}",
        color=0xE74C3C if pnl_amount < 0 else 0x2ECC71 
    )
    embed.add_field(name="買入成本", value=f"`{buy_price}`", inline=True)
    embed.add_field(name="賣出價格", value=f"`{sell_price}`", inline=True)
    embed.add_field(name="交易股數", value=f"`{shares}` 股", inline=True)
    
    pnl_str = f"+{int(pnl_amount)}" if pnl_amount >= 0 else f"{int(pnl_amount)}"
    pnl_pct_str = f"+{pnl_pct}%" if pnl_pct >= 0 else f"{pnl_pct}%"
    embed.add_field(name="💵 單筆損益", value=f"**{pnl_str} 元** ({pnl_pct_str})", inline=False)
    
    total_pnl = history['total_pnl']
    total_str = f"+{int(total_pnl)}" if total_pnl >= 0 else f"{int(total_pnl)}"
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
    total_str = f"+{int(total_all_time)}" if total_all_time >= 0 else f"{int(total_all_time)}"
    embed.add_field(name="🌍 歷史累積總損益 (All-Time)", value=f"**{total_str} 元**", inline=False)
    
    recent_months = sorted(monthly_data.keys(), reverse=True)[:3]
    month_summary_str = ""
    for m in recent_months:
        m_pnl = monthly_data[m]['pnl']
        m_str = f"+{int(m_pnl)}" if m_pnl >= 0 else f"{int(m_pnl)}"
        total_trades = monthly_data[m]['wins'] + monthly_data[m]['losses']
        m_win_rate = round(monthly_data[m]['wins'] / total_trades * 100, 1) if total_trades > 0 else 0
        month_summary_str += f"**{m}**: `{m_str}元` (勝率 {m_win_rate}%)\n"
        
    embed.add_field(name="📅 近期月份總結", value=month_summary_str, inline=False)
    
    if target_month in monthly_data:
        t_data = monthly_data[target_month]
        t_pnl = t_data['pnl']
        t_str = f"+{int(t_pnl)}" if t_pnl >= 0 else f"{int(t_pnl)}"
        t_total = t_data['wins'] + t_data['losses']
        t_win_rate = round(t_data['wins'] / t_total * 100, 1) if t_total > 0 else 0
        
        detail_str = ""
        for t in t_data['details']:
            p_str = f"+{int(t['pnl'])}" if t['pnl'] >= 0 else f"{int(t['pnl'])}"
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
