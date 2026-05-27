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

# 關閉 yfinance 煩人的紅字報錯
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
# 🔍 核心 1：大數據智能選股雷達 (移植自 V5.6)
# =====================================================================
_TICKER_CACHE = {}
def get_all_taiwan_tickers():
    global _TICKER_CACHE
    if _TICKER_CACHE: return _TICKER_CACHE
    tickers_dict = {}
    urls = {".TW": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", ".TWO": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"}
    for suffix, url in urls.items():
        try:
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
            df = pd.read_html(StringIO(res.text))[0]
            for _, row in df.iterrows():
                val_str = str(row[0]).strip().replace(' ', ' ')
                match = re.match(r'^(\d{4})\s+(.+)$', val_str)
                if match: tickers_dict[f"{match.group(1)}{suffix}"] = {"name": match.group(2).strip(), "sector": str(row[4]).strip()}
        except: pass
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
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    df['SMA_60'] = df['Close'].rolling(window=60).mean()
    df['Vol_SMA_20'] = df['Volume'].rolling(window=20).mean()
    df['BB_Upper'] = df['SMA_20'] + (2 * df['Close'].rolling(window=20).std())
    df['BB_Lower'] = df['SMA_20'] - (2 * df['Close'].rolling(window=20).std())
    df['BB_Width'] = (df['BB_Upper'] - df['BB_Lower']) / df['SMA_20']
    
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['Signal_Line'] = df['MACD'].ewm(span=9, adjust=False).mean()
    
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    rs = gain.ewm(alpha=1/14, adjust=False).mean() / loss.ewm(alpha=1/14, adjust=False).mean()
    df['RSI_14'] = 100 - (100 / (1 + rs))
    return df

def run_screener_for_discord():
    tickers_dict = get_all_taiwan_tickers()
    # 為了避免雲端伺服器運算逾時，隨機挑選或縮小範圍。若要全掃可能需要分批。
    # 這裡我們先轉換成 list
    tickers = list(tickers_dict.keys())
    
    # Render 免費版記憶體有限，建議先限制掃描數量，例如先掃前 500 檔測試
    # 如果確定跑得動，可以把這行拿掉
    tickers = tickers[:500] 
    
    data = yf.download(tickers, period="3mo", group_by="ticker", progress=False, threads=True)
    
    msg_bb = ""
    msg_macd = ""
    msg_rsi = ""
    
    for ticker in tickers:
        try:
            df = data[ticker].dropna().copy()
            if len(df) < 65: continue
            df = calculate_screener_indicators(df)
            
            latest, prev1 = df.iloc[-1], df.iloc[-2]
            close, vol, open_px = latest['Close'].item(), latest['Volume'].item(), latest['Open'].item()
            
            # 成交量濾網：> 5000萬
            if close * vol * 1000 < 50000000: continue 
            
            name = tickers_dict[ticker]['name']
            clean_code = ticker.replace('.TW', '').replace('.TWO', '')
            is_uptrend = latest['SMA_60'].item() > prev1['SMA_60'].item()
            
            match_strat = ""
            
            # 策略 1: 布林壓縮突破
            if prev1['BB_Width'].item() < 0.08 and close > latest['BB_Upper'].item() and vol > (latest['Vol_SMA_20'].item() * 2) and is_uptrend and close > open_px:
                match_strat = "BB"
            # 策略 2: MACD 金叉
            elif is_uptrend and close > latest['SMA_60'].item() and (prev1['MACD'].item() < prev1['Signal_Line'].item()) and (latest['MACD'].item() > latest['Signal_Line'].item()):
                match_strat = "MACD"
            # 策略 3: RSI 翻揚
            elif close < (latest['SMA_20'].item() * 0.90) and prev1['RSI_14'].item() < 30 and latest['RSI_14'].item() >= 30:
                match_strat = "RSI"

            if match_strat:
                chips = get_finmind_chip_5d(clean_code)
                t_buy, f_buy = chips['投信'], chips['外資']
                chip_txt = "🔥土洋連買" if t_buy > 200 and f_buy > 500 else "🔥投信進駐" if t_buy > 150 else ""
                
                stock_info = f"📌 **{clean_code} {name}** | 收盤 `{round(close, 2)}` | 外資 `{f_buy}` 投信 `{t_buy}` {chip_txt}\n"
                
                if match_strat == "BB": msg_bb += stock_info
                elif match_strat == "MACD": msg_macd += stock_info
                elif match_strat == "RSI": msg_rsi += stock_info
                
        except: continue

    final_msg = "🎯 **【盤後主力選股雷達推薦】**\n=========================\n"
    if msg_bb: final_msg += "💥 **布林壓縮突破 (飆股動能)**\n" + msg_bb + "\n"
    if msg_macd: final_msg += "🏄‍♂️ **均線 MACD 金叉 (順勢波段)**\n" + msg_macd + "\n"
    if msg_rsi: final_msg += "🎣 **RSI 乖離翻揚 (危機入市)**\n" + msg_rsi + "\n"
    
    if not (msg_bb or msg_macd or msg_rsi):
        final_msg += "今天大盤太無聊，沒有符合強勢條件的獵物 😴\n"
        
    return final_msg

# =====================================================================
# 🛡️ 核心 2：個人持股盯盤 (原本的小秘書)
# =====================================================================
def calculate_indicators(df):
    df['SMA_5'] = df['Close'].rolling(window=5).mean()
    df['SMA_10'] = df['Close'].rolling(window=10).mean()
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    df['Vol_5MA'] = df['Volume'].rolling(window=5).mean()
    
    std = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['SMA_20'] + (2 * std)
    df['BB_Lower'] = df['SMA_20'] - (2 * std)
    
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

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空，請用 !新增 指令建立股票。"
    
    msg = "📊 **【雲端精準監控戰報】** (極限防護版)\n"
    msg += "=========================\n"
    
    for code, info in portfolio.items():
        tickers = [f"{code}.TW", f"{code}.TWO", f"{code}"]
        df = None
        for t in tickers:
            try:
                d = yf.Ticker(t).history(period="3mo")
                if len(d) > 30: 
                    df = d
                    break
            except: continue
        
        stock_name = info.get('name', '')
        display_title = f"{code} {stock_name}".strip()
        
        if df is None or len(df) <= 30:
            msg += f"❌ **{display_title}**: 抓取失敗\n\n"
            continue
            
        df = calculate_indicators(df)
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        close = latest['Close'].item()
        cost = info.get('buy_price', 0)
        strat = info.get('strategy', '無')
        profit = round(((close - cost) / cost) * 100, 2) if cost > 0 else 0
        
        tp_pct = info.get('tp_pct', None)
        sl_pct = info.get('sl_pct', None)
        
        ma5, ma10, ma20 = latest['SMA_5'].item(), latest['SMA_10'].item(), latest['SMA_20'].item()
        bb_upper, bb_lower = latest['BB_Upper'].item(), latest['BB_Lower'].item()
        
        macd_val, sig_val = latest['MACD'].item(), latest['Signal'].item()
        hist_val, prev_hist = latest['MACD_Hist'].item(), prev['MACD_Hist'].item()
        
        rsi_val, prev_rsi = latest['RSI'].item(), prev['RSI'].item()
        
        latest_vol, vol_5ma = latest['Volume'].item(), latest['Vol_5MA'].item()
        is_high_vol = latest_vol > vol_5ma
        
        macd_status = "✅ 多頭" if macd_val > sig_val else "⚠️ 空頭"
        
        custom_panel, alert_msg = "", ""
        
        # 第一層：%數限制
        if tp_pct and profit >= float(tp_pct):
            alert_msg = f"💰 [獲利出場] 報酬率 {profit}% 已達停利點 (+{tp_pct}%)！"
        elif sl_pct and profit <= -float(sl_pct):
            alert_msg = f"🛑 [落跑停損] 報酬率 {profit}% 已達停損點 (-{sl_pct}%)！"
            
        # 第二層：敏銳技術指標
        if not alert_msg:
            if "1" in strat or "布林" in strat:
                custom_panel = f"上軌 `{round(bb_upper, 2)}` | 5日線 `{round(ma5, 2)}` | 10日線 `{round(ma10, 2)}`"
                if close < ma10: alert_msg = "🚨 [快出場] 跌破 10 日線，動能消散！" if is_high_vol else "⚠️ [注意] 破 10 日線。"
                elif close < ma5: alert_msg = "⚠️ [警訊] 跌破 5 日線，短線可能熄火。"
                elif close > bb_upper: alert_msg = "🔥 [動能強] 帶量突破上軌！" if is_high_vol else "🤔 [觀察] 無量上漲。"
            elif "2" in strat or "MACD" in strat:
                custom_panel = f"MACD: `{macd_status}` | 10日線 `{round(ma10, 2)}` | 月線 `{round(ma20, 2)}`"
                if prev['MACD'].item() > prev['Signal'].item() and macd_val < sig_val: alert_msg = "🚨 [逃命] MACD 死叉成形！"
                elif hist_val > 0 and hist_val < prev_hist: alert_msg = "⚠️ [警訊] MACD 紅柱縮減，動能衰退。"
                elif close < ma10: alert_msg = "📉 [轉弱] 跌破 10 日線，提防下探。"
            elif "3" in strat or "RSI" in strat:
                custom_panel = f"RSI: `{round(rsi_val, 2)}` | 5日線 `{round(ma5, 2)}`"
                if rsi_val < prev_rsi and prev_rsi > 70: alert_msg = "🚨 [快跑] RSI 自高檔反轉向下！"
                elif rsi_val > 75: alert_msg = "🔴 [極度超買] RSI 突破 75。"
                elif rsi_val < 25: alert_msg = "🟢 [極度超賣] RSI 跌破 25，留意反彈。"
            else:
                custom_panel = f"10日線 `{round(ma10, 2)}` | 月線 `{round(ma20, 2)}`"
                
            if close < ma20 and not alert_msg: alert_msg = "🚨 爆量跌破月線！" if is_high_vol else "⚠️ 縮量跌破月線"
                
        if not alert_msg: alert_msg = "👌 狀態穩定"
            
        tp_sl_info = f" | 停利: `+{tp_pct}%` 停損: `-{sl_pct}%`" if (tp_pct or sl_pct) else " | 風控: `未設定`"
        
        msg += f"📌 **{display_title}**\n"
        msg += f"   市價: `{round(close, 2)}` | 成本: `{cost}` | 報酬: `{profit}%`{tp_sl_info}\n"
        msg += f"   策略: `{strat}`\n"
        msg += f"   指標: {custom_panel}\n"
        msg += f"   👉 {alert_msg}\n"
        msg += "-------------------------\n"
        
    return msg

# =====================================================================
# 🤖 Discord 機器人主程式
# =====================================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 🕒 盤中每半小時盯盤 (09:30~14:00)
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

# 🕒 盤後每日選股推播 (下午 14:30)
@tasks.loop(time=time(hour=14, minute=30, tzinfo=timezone(timedelta(hours=8))))
async def daily_screener_report():
    tw_tz = timezone(timedelta(hours=8))
    now = datetime.now(tw_tz)
    if now.weekday() > 4: return # 假日不選股
    
    channel = bot.get_channel(PUSH_CHANNEL_ID)
    if channel:
        await channel.send("⏳ 雲端投顧老師正在幫您掃瞄全台股，這需要幾分鐘，請稍候...")
        # 由於掃全台股很耗時，必須用 to_thread 避免卡死機器人
        result = await asyncio.to_thread(run_screener_for_discord)
        await channel.send(result)

@bot.event
async def on_ready():
    print(f"✅ Bot 登入成功: {bot.user}")
    if not auto_report.is_running(): auto_report.start()
    if not daily_screener_report.is_running(): daily_screener_report.start()
    print("🚀 盤中盯盤 & 盤後選股雙排程已啟動！")

# --- 手動指令區 ---
@bot.command()
async def 健檢(ctx):
    msg = await ctx.send("⏳ 正在分析短線敏銳技術指標...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_health_check), timeout=30.0)
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 運算逾時，請稍後再試。")

@bot.command()
async def 選股(ctx):
    msg = await ctx.send("⏳ 雲端投顧老師正在為您海選強勢股，請耐心等候幾分鐘...")
    try:
        # 將超時時間拉長到 5 分鐘
        result = await asyncio.wait_for(asyncio.to_thread(run_screener_for_discord), timeout=300.0)
        
        # 🛡️ 防呆：Discord 單則訊息不能超過 2000 字
        if len(result) > 1900:
            result = result[:1900] + "\n\n⚠️ ...(名單太多，字數達 Discord 上限，已省略後續清單)"
            
        await msg.edit(content=result)
        
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 掃瞄逾時！向 Yahoo 財經索取 500 檔資料太久了，請稍後再試。")
    except Exception as e:
        # 🚨 如果發生任何程式崩潰，直接印出錯誤碼
        error_msg = f"❌ 系統崩潰！錯誤代碼：`{str(e)}`\n請截圖給工程師（我）看！"
        await msg.edit(content=error_msg)
        print(f"選股功能嚴重錯誤: {e}")

@bot.command()
async def 新增(ctx, code: str, price: float, strat_num: str, name: str = "", tp: float = None, sl: float = None):
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

# =====================================================================
# Render 雲端保活伺服器
# =====================================================================
async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()

async def main():
    await asyncio.gather(start_web_server(), bot.start(TOKEN))

if __name__ == "__main__":
    asyncio.run(main())

