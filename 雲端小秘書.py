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

# 安全設定：Token 改為讀取環境變數 (請在代管平台或本地端設定環境變數)
TOKEN = os.environ.get('DISCORD_TOKEN')
# 若無設定環境變數，這裡填入原本的 Token 作為備案 (但強烈建議用環境變數)
FINMIND_TOKEN = os.environ.get('FINMIND_TOKEN', "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiam9lbDA2MjYiLCJlbWFpbCI6ImpvZWwwNjI2QG1zbi5jb20iLCJ0b2tlbl92ZXJzaW9uIjowfQ.j1KeK6JfXNUX2WlEKYmdMctQV_9_xfwpzVlANplYafs")

PORTFOLIO_FILE = "my_portfolio.json"

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

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: 
        return json.load(f)

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: 
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
# 🛡️ 核心：指標計算與個股評估
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
    """ 檢查大盤 (台灣加權指數) 是否站上月線，作為多空環境濾網 """
    try:
        twii = yf.download('^TWII', period="2mo", progress=False)
        if twii.empty: return True # 若抓不到預設允許交易
        if isinstance(twii.columns, pd.MultiIndex):
            twii.columns = twii.columns.get_level_values(0)
        close = twii['Close'].iloc[-1].item()
        ma20 = twii['Close'].rolling(window=20).mean().iloc[-1].item()
        return close > ma20
    except:
        return True

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
        # 1. 確認大盤環境
        market_uptrend = check_market_trend()
        
        # 2. 抓取個股資料
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
        
        # ==== 策略條件審查 (進場寬鬆化) ====
        # 布林：壓縮放寬至 0.10，量能 > 5日均量 1.5 倍
        bb_pass = (bb_width < 0.10) and (close > latest['BB_Upper'].item()) and (vol > (vol_5ma * 1.5)) and is_uptrend and (close > open_px)
        # MACD：站上季線且 MACD > 訊號線，且紅柱/指標上升中
        macd_pass = is_uptrend and (close > ma60) and (macd > sig) and (macd > prev_macd)
        # RSI：網子撒大，跌破 35 後反彈向上
        rsi_pass = (close < ma20) and (prev_rsi < 35) and (rsi >= 30) and (rsi > prev_rsi)
        # 縮量回踩：最低價來到月線上 3% 內就算，且收盤守住月線
        pullback_pass = is_uptrend and (close > ma60) and (low <= ma20 * 1.03) and (close >= ma20) and (vol < vol_5ma)
        # 強勢創高：大於 5 日均量的 1.2 倍即可
        breakout_pass = is_uptrend and (ma20 > ma60) and (close >= max20_prev) and (vol > vol_5ma * 1.2)

        # 🚨 大盤濾網介入：若大盤跌破月線，關閉突破與創高策略，避免假突破
        market_warning = ""
        if not market_uptrend:
            bb_pass = False
            breakout_pass = False
            market_warning = "⚠️ **[大盤警示]** 台灣加權指數目前跌破月線，系統已強制關閉「布林突破」與「強勢創高」策略，防禦假突破風險！\n"

        # ==== 產生評估報告 ====
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
            
            # ==== 資金控管建議 (Position Sizing) ====
            risk_pct = round(((close - ma20) / close) * 100, 2)
            if risk_pct > 0:
                suggested_alloc = round((2.0 / risk_pct) * 100, 1) # 單筆最大虧損 2% 換算
                if suggested_alloc > 100: suggested_alloc = 100
                risk_advice = f"⚖️ **資金控管建議**：目前距離月線停損約 `-{risk_pct}%`。若嚴守單筆虧損不超過總資金2%之紀律，本檔建議最多投入總資金的 **`{suggested_alloc}%`**。\n"
            else:
                risk_advice = f"⚖️ **資金控管建議**：目前股價已在月線之下，若進場屬於左側摸底，請極度縮小部位。\n"
            
            msg += f"💡 **【AI 教練結論】: 建議買進！**\n🔥 該股目前符合 **{', '.join(matched_strats)}** 的發動訊號。\n{risk_advice}若決定進場，請用 `!新增 {code} {close} {matched_strats[0][-1]}` 加入小秘書監控！"
        else:
            msg += f"💡 **【AI 教練結論】: 建議觀望 👀**\n這檔股票目前技術面**並未觸發**任何高勝率或動能攻擊條件。不要急著把資金卡在沒有表態的股票上，請多看少做！"
            
        return msg
    except Exception as e:
        return f"❌ 評估過程發生錯誤: `{e}`"

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空，請用 `!新增` 指令建立股票。不知道怎麼用請輸入 `!指令`"
    
    tickers_dict = get_all_taiwan_tickers() 
    msg = "📊 **【雲端精準監控戰報】**\n=========================\n"
    
    for code, info in portfolio.items():
        exact_ticker = f"{code}.TW"
        if f"{code}.TW" in tickers_dict: exact_ticker = f"{code}.TW"
        elif f"{code}.TWO" in tickers_dict: exact_ticker = f"{code}.TWO"
        
        try:
            df = yf.download(exact_ticker, period="3mo", progress=False)
            # 自動補齊名稱
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
            
            close = latest['Close'].item()
            cost = info.get('buy_price', 0)
            strat = info.get('strategy', '無')
            profit = round(((close - cost) / cost) * 100, 2) if cost > 0 else 0
            
            tp_pct, sl_pct = info.get('tp_pct', None), info.get('sl_pct', None)
            ma5, ma10, ma20 = latest['SMA_5'].item(), latest['SMA_10'].item(), latest['SMA_20'].item()
            bb_upper = latest['BB_Upper'].item()
            macd_val, sig_val = latest['MACD'].item(), latest['Signal'].item()
            hist_val, prev_hist = latest['MACD_Hist'].item(), prev['MACD_Hist'].item()
            rsi_val, prev_rsi = latest['RSI'].item(), prev['RSI'].item()
            
            macd_status = "✅ 多頭" if macd_val > sig_val else "⚠️ 空頭"
            
            custom_panel, alert_msg = "", ""
            
            # 第一層防線：自訂停損停利點
            if tp_pct and profit >= float(tp_pct): alert_msg = f"💰 [獲利出場] 報酬率 {profit}% 已達停利點 (+{tp_pct}%)！"
            elif sl_pct and profit <= -float(sl_pct): alert_msg = f"🛑 [落跑停損] 報酬率 {profit}% 已達停損點 (-{sl_pct}%)！"
                
            if not alert_msg:
                # 第二層防線：策略專屬紀律 (出場嚴格化)
                if "1" in strat or "布林" in strat:
                    custom_panel = f"上軌 `{round(bb_upper, 2)}` | 5日線 `{round(ma5, 2)}`"
                    if close < ma5: alert_msg = "🚨 [動能熄火] 跌破 5 日強勢線，建議立刻獲利了結或停損！"
                    elif close < bb_upper and prev['Close'].item() > prev['BB_Upper'].item(): alert_msg = "⚠️ [注意] 跌回布林通道內，動能減弱。"
                
                elif "2" in strat or "MACD" in strat:
                    custom_panel = f"MACD: `{macd_status}` | 10日線 `{round(ma10, 2)}`"
                    if macd_val < sig_val: alert_msg = "🚨 [趨勢反轉] MACD 死叉成形，波段結束！"
                    elif hist_val < prev_hist < df.iloc[-3]['MACD_Hist'].item(): alert_msg = "⚠️ [警訊] MACD 紅柱連續兩天縮減，隨時準備落跑。"
                    elif close < ma10: alert_msg = "📉 [轉弱] 跌破 10 日線，建議減碼。"
                
                elif "3" in strat or "RSI" in strat:
                    custom_panel = f"RSI: `{round(rsi_val, 2)}` | 5日線 `{round(ma5, 2)}`"
                    if rsi_val < prev_rsi and prev_rsi > 70: alert_msg = "🚨 [快跑] RSI 自高檔反轉向下！"
                    elif rsi_val > 75: alert_msg = "🔴 [極度超買] RSI 突破 75。"
                    elif rsi_val < 25: alert_msg = "🟢 [極度超賣] RSI 跌破 25，留意反彈。"
                
                elif "4" in strat or "回踩" in strat:
                    custom_panel = f"月線 `{round(ma20, 2)}`"
                    if close < ma20: alert_msg = "🚨 [防守失敗] 收盤跌破月線底線，毫無懸念立刻停損！"
                
                elif "5" in strat or "創高" in strat:
                    custom_panel = f"5日線 `{round(ma5, 2)}`"
                    if close < ma5: alert_msg = "🚨 [假突破/轉弱] 飆股跌破 5 日線，強勢慣性改變，砍倉！"
                
                # 通用底線
                else:
                    custom_panel = f"10日線 `{round(ma10, 2)}` | 月線 `{round(ma20, 2)}`"
                    if close < ma20: alert_msg = "🚨 跌破月線底線！"
                    
            if not alert_msg: alert_msg = "👌 狀態穩定"
                
            tp_sl_info = f" | 停利: `+{tp_pct}%` 停損: `-{sl_pct}%`" if (tp_pct or sl_pct) else " | 風控: `未設定`"
            
            msg += f"📌 **{display_title}**\n   市價: `{round(close, 2)}` | 成本: `{cost}` | 報酬: `{profit}%`{tp_sl_info}\n   策略: `{strat}`\n   指標: {custom_panel}\n   👉 {alert_msg}\n-------------------------\n"
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

# 播報時間維持不變
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
    embed_cmd.add_field(name="🔍 `!健檢`", value="極速掃描目前庫存所有持股的狀態。", inline=False)
    embed_cmd.add_field(name="🔬 `!評估 [代號]`", value="個股 X 光機！幫你鑑定這檔股票是否符合買進策略。\n*範例: `!評估 2330`*", inline=False)
    embed_cmd.add_field(name="📥 `!新增 [代號] [成本] [策略] [名稱] [停利%] [停損%]`", value="將股票交給小秘書監控 (名稱留空會自動抓取)。\n*範例: `!新增 2330 800 1`*", inline=False)
    embed_cmd.add_field(name="🛡️ `!風控 [代號] [停利%] [停損%]`", value="隨時更新股票的停損停利點。\n*範例: `!風控 2330 20 10`*", inline=False)
    embed_cmd.add_field(name="⚙️ `!策略 [代號] [策略代號]`", value="修改持股的防護策略。\n*範例: `!策略 2330 4`*", inline=False)
    embed_cmd.add_field(name="🗑️ `!刪除 [代號]`", value="將股票從監控清單中移除。\n*範例: `!刪除 2330`*", inline=False)
    
    embed_strat = discord.Embed(
        title="📖 【五大量化策略】操盤心法與進出場邏輯",
        description="系統會根據您選擇的策略，自動執行對應的防守底線：",
        color=0x3498DB
    )
    
    embed_strat.add_field(
        name="1️⃣ 布林壓縮突破 (抓動能飆股)",
        value="🟢 **為何買：** 股票盤整很久，突然帶量衝破上軌。代表主力準備發車！\n🔴 **為何賣(極度嚴格)：** 飆車最怕熄火。只要跌破「5日線」，小秘書會立刻叫你落袋為安。",
        inline=False
    )
    embed_strat.add_field(
        name="2️⃣ 雙均線 + MACD (抓波段趨勢)",
        value="🟢 **為何買：** 長線趨勢向上，且MACD翻揚。代表短線整理結束，新一波漲勢啟動。\n🔴 **為何賣：** 當 MACD 紅柱連續縮減，或是死叉跌破「10日線」，代表這波漲完準備撤退。",
        inline=False
    )
    embed_strat.add_field(
        name="3️⃣ RSI 超賣反彈 (抓危機入市)",
        value="🟢 **為何買：** 股價跌太深，散戶極度恐慌。當RSI重新勾起，代表殺盤竭盡，進場搶反彈。\n🔴 **為何賣：** 搶反彈有賺就跑。RSI自高檔跌落或衝上極度超買區，隨時停利。",
        inline=False
    )
    embed_strat.add_field(
        name="4️⃣ 多頭縮量回踩 (高勝率買跌)",
        value="🟢 **為何買：** 好股票跌到月線附近，但成交量極度萎縮。代表散戶被洗掉但主力沒跑。\n🔴 **為何賣(極度嚴格)：** 買在支撐就不能破支撐！只要「收盤跌破月線」，代表防守失敗立刻無情停損。",
        inline=False
    )
    embed_strat.add_field(
        name="5️⃣ 強勢創高確認 (高勝率追強)",
        value="🟢 **為何買：** 股價帶量突破過去一個月最高點。上面沒有套牢賣壓，容易一飛衝天。\n🔴 **為何賣(極度嚴格)：** 突破股必須強者恆強！只要跌破「5日線」判定為強勢慣性改變，砍倉。",
        inline=False
    )
    
    await ctx.send(embed=embed_cmd)
    await ctx.send(embed=embed_strat)

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
    msg = await ctx.send("⏳ 正在極速分析庫存短線敏銳技術指標...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_health_check), timeout=120.0)
        if len(result) > 1900: result = result[:1900] + "\n\n⚠️ ...(庫存過多，字數達 Discord 上限)"
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 運算逾時，Yahoo 財經連線不穩，請稍後再試。")

# 修正名稱自動抓取機制
@bot.command()
async def 新增(ctx, code: str, price: float, strat_num: str, name: str = "", tp: float = None, sl: float = None):
    if not name:
        tickers_dict = get_all_taiwan_tickers()
        exact_ticker = f"{code}.TW" if f"{code}.TW" in tickers_dict else f"{code}.TWO"
        if exact_ticker in tickers_dict:
            name = tickers_dict[exact_ticker]['name']
        else:
            name = "未知名稱"
            
    full_strat = STRAT_MAP.get(strat_num, strat_num) 
    data = load_data()
    data[code] = {"buy_price": price, "strategy": full_strat, "name": name, "tp_pct": tp, "sl_pct": sl}
    save_data(data)
    
    display_title = f"{code} {name}".strip()
    风控文 = f" | 停利: +{tp}% 停損: -{sl}%" if (tp or sl) else " | 未設定風控"
    await ctx.send(f"✅ 已新增 **{display_title}**\n成本: `{price}`\n策略: `{full_strat}`{风控文}")

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
