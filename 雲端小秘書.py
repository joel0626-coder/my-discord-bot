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
# 🛡️ 核心：指標計算、環境濾網與個股評估
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
        if twii.empty: return True 
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
        
        # ==== 策略條件審查 (進場寬鬆化) ====
        bb_pass = (bb_width < 0.10) and (close > latest['BB_Upper'].item()) and (vol > (vol_5ma * 1.5)) and is_uptrend and (close > open_px)
        macd_pass = is_uptrend and (close > ma60) and (macd > sig) and (macd > prev_macd)
        rsi_pass = (close < ma20) and (prev_rsi < 35) and (rsi >= 30) and (rsi > prev_rsi)
        pullback_pass = is_uptrend and (close > ma60) and (low <= ma20 * 1.03) and (close >= ma20) and (vol < vol_5ma)
        breakout_pass = is_uptrend and (ma20 > ma60) and (close >= max20_prev) and (vol > vol_5ma * 1.2)

        # 🚨 大盤濾網介入
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
            
            # 資金控管建議
            risk_pct = round(((close - ma20) / close) * 100, 2)
            if risk_pct > 0:
                suggested_alloc = round((2.0 / risk_pct) * 100, 1) 
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
                # ==========================================
                # 🔥 第二層防線：【AI 教練綜合建議】(雙重視角)
                # ==========================================
                
                # 策略 1 (布林) & 策略 5 (創高) - 動能突破系
                if "1" in strat or "布林" in strat or "5" in strat or "創高" in strat:
                    custom_panel = f"5日線 `{round(ma5, 2)}` | 10日線 `{round(ma10, 2)}` | 月線 `{round(ma20, 2)}`"
                    if close < ma20: 
                        alert_msg = "🚨 [防守貫破] 已跌破月線，強勢慣性已被徹底破壞，無論長短線皆建議清倉！"
                    elif close < ma10: 
                        alert_msg = "⚠️ [轉弱/洗盤] 跌破 10 日線。\n      👉 **短線客**：強烈建議出場。\n      👉 **波段客**：建議先減碼一半保本，剩餘用月線防守。"
                    elif close < ma5: 
                        alert_msg = "🤔 [動能衰退] 跌破 5 日線。\n      👉 **短線客**：建議獲利了結，有賺就跑。\n      👉 **波段客**：視為正常洗盤，可續抱觀察。"
                    elif "1" in strat and bb_upper and close < bb_upper and prev['Close'].item() > prev['BB_Upper'].item(): 
                        alert_msg = "💡 [漲多休息] 跌回布林通道內，進入高檔震盪。"
                
                # 策略 2 (MACD趨勢) - 波段趨勢系
                elif "2" in strat or "MACD" in strat:
                    custom_panel = f"MACD: `{macd_status}` | 10日線 `{round(ma10, 2)}` | 月線 `{round(ma20, 2)}`"
                    if close < ma20:
                        alert_msg = "🚨 [趨勢破壞] 跌破月線，波段防守底線遭貫破，建議清倉！"
                    elif macd_val < sig_val: 
                        alert_msg = "⚠️ [MACD死叉] 波段轉弱。\n      👉 **短線客**：立刻出場。\n      👉 **波段客**：可考慮減碼，剩餘部位觀察月線支撐。"
                    elif hist_val < prev_hist < df.iloc[-3]['MACD_Hist'].item():
                        alert_msg = "🤔 [動能衰退] MACD 紅柱連續縮減。\n      👉 **短線客**：準備落跑。\n      👉 **波段客**：無須理會小波動，續抱。"
                
                # 策略 3 (RSI反彈) - 逆勢抄底系
                elif "3" in strat or "RSI" in strat:
                    custom_panel = f"RSI: `{round(rsi_val, 2)}` | 月線 `{round(ma20, 2)}`"
                    if close < ma20 * 0.97: 
                        alert_msg = "🚨 [破底] 跌破月線超過 3%，反彈徹底失敗，立刻停損！"
                    elif rsi_val < prev_rsi and prev_rsi > 70: 
                        alert_msg = "💰 [高檔反轉] RSI 自高檔反轉向下。\n      👉 **短線客**：全數停利。\n      👉 **波段客**：分批減碼。"
                
                # 策略 4 (縮量回踩) - 支撐防守系
                elif "4" in strat or "回踩" in strat:
                    custom_panel = f"月線 `{round(ma20, 2)}` | 緩衝區 `{round(ma20 * 0.97, 2)}`"
                    if close < ma20 * 0.97: 
                        alert_msg = "🚨 [防守貫破] 跌破月線超過 3% 緩衝區，主力確認棄守，毫無懸念立刻停損！"
                    elif close < ma20: 
                        alert_msg = "⚠️ [支撐測試] 跌破月線。\n      👉 **短線客**：嚴格停損出場。\n      👉 **波段客**：進入 3% 洗盤緩衝區，觀察三天內能否站回。"
                
                # 通用底線
                else:
                    custom_panel = f"10日線 `{round(ma10, 2)}` | 月線 `{round(ma20, 2)}`"
                    if close < ma20 * 0.97: alert_msg = "🚨 跌破月線底線 3%！"
                    
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
        title="📖 【五大量化策略】AI 雙重視角操盤邏輯",
        description="系統會根據您的策略，同時給予短線客與波段客的操作建議：",
        color=0x3498DB
    )
    
    embed_strat.add_field(
        name="1️⃣ 布林壓縮突破 & 5️⃣ 強勢創高確認",
        value="🟢 **進場：** 帶量突破壓力區，主力準備發車。\n🔴 **防守：**\n短線客👉 跌破 5 日線即獲利了結。\n波段客👉 破 10 日線減碼，破月線清倉。",
        inline=False
    )
    embed_strat.add_field(
        name="2️⃣ 雙均線 + MACD (抓波段趨勢)",
        value="🟢 **進場：** 站上季線且 MACD 翻揚。\n🔴 **防守：**\n短線客👉 MACD 死叉或紅柱縮減即出場。\n波段客👉 不理會小波動，以月線做最後防線。",
        inline=False
    )
    embed_strat.add_field(
        name="3️⃣ RSI 超賣反彈 (抓危機入市)",
        value="🟢 **進場：** RSI 跌破超賣區後反轉向上。\n🔴 **防守：** 反彈高點 RSI 衝破 70 後勾下時停利；若跌破月線 3% 則停損。",
        inline=False
    )
    embed_strat.add_field(
        name="4️⃣ 多頭縮量回踩 (高勝率買跌)",
        value="🟢 **進場：** 好股票跌到月線附近，且成交量極度萎縮。\n🔴 **防守：** \n短線客👉 跌破月線嚴格停損。\n波段客👉 觀察 3% 洗盤緩衝區，若遭貫破即刻停損。",
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
