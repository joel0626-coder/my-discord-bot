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
# 📚 FinMind 股票代號快取 (解決上市/上櫃瞎猜問題，極速提升健檢效能)
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
# 🛡️ 核心：個人持股極速健檢盯盤
# =====================================================================
def calculate_indicators(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
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
    if not portfolio: return "⚠️ 資料庫為空，請用 `!新增` 指令建立股票。不知道怎麼用請輸入 `!指令`"
    
    tickers_dict = get_all_taiwan_tickers() 
    msg = "📊 **【雲端精準監控戰報】**\n=========================\n"
    
    for code, info in portfolio.items():
        # ⚡ 直接從快取鎖定正確後綴，不浪費時間嘗試錯誤
        exact_ticker = f"{code}.TW"
        if f"{code}.TW" in tickers_dict: exact_ticker = f"{code}.TW"
        elif f"{code}.TWO" in tickers_dict: exact_ticker = f"{code}.TWO"
        
        try:
            df = yf.download(exact_ticker, period="3mo", progress=False)
            
            stock_name = info.get('name', '')
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
            latest_vol, vol_5ma = latest['Volume'].item(), latest['Vol_5MA'].item()
            
            is_high_vol = latest_vol > vol_5ma
            macd_status = "✅ 多頭" if macd_val > sig_val else "⚠️ 空頭"
            
            custom_panel, alert_msg = "", ""
            
            # 第一層防護：%數硬停損利
            if tp_pct and profit >= float(tp_pct): alert_msg = f"💰 [獲利出場] 報酬率 {profit}% 已達停利點 (+{tp_pct}%)！"
            elif sl_pct and profit <= -float(sl_pct): alert_msg = f"🛑 [落跑停損] 報酬率 {profit}% 已達停損點 (-{sl_pct}%)！"
                
            # 第二層防護：策略技術面快篩
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

# 移除 Discord 預設的陽春 Help 指令，我們自己寫一個更帥的
bot.remove_command('help')

# 🕒 平日 09:30~14:00 每半小時自動推播庫存狀況
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
    print("🚀 盤中持股監控排程已啟動！")

# =====================================================================
# 💬 使用者手動控制指令區
# =====================================================================
@bot.command(aliases=['help', '幫助'])
async def 指令(ctx):
    """視覺化指令導覽選單"""
    embed = discord.Embed(
        title="🤖 雲端小秘書 - 指令大全",
        description="老闆，以下是您可以對我下達的指令清單，格式內的 `[ ]` 代表需要填寫的參數（請空一格）：",
        color=0x2ECC71
    )
    
    embed.add_field(name="🔍 `!健檢`", value="極速掃描目前所有持股的技術面與風控狀態。", inline=False)
    embed.add_field(name="📥 `!新增 [代號] [成本] [策略] [名稱] [停利%] [停損%]`", value="加入新持股。\n*範例: `!新增 2330 800 1 台積電 15 5`*\n(名稱與風控％數若不設定可直接留空)", inline=False)
    embed.add_field(name="🛡️ `!風控 [代號] [停利%] [停損%]`", value="更新指定股票的停損停利點。\n*範例: `!風控 2330 20 10`*", inline=False)
    embed.add_field(name="⚙️ `!策略 [代號] [策略代號]`", value="修改持股的技術面防護策略。\n*範例: `!策略 2330 2`*", inline=False)
    embed.add_field(name="🏷️ `!命名 [代號] [名稱]`", value="幫股票加上中文名稱，方便推播辨識。\n*範例: `!命名 2330 護國神山`*", inline=False)
    embed.add_field(name="🗑️ `!刪除 [代號]`", value="將股票從監控清單中徹底移除。\n*範例: `!刪除 2330`*", inline=False)
    
    embed.add_field(name="💡 【策略代號對照表】", value="`1` : 布林壓縮突破 (動能：防守 5/10 日線)\n`2` : 雙均線+MACD (趨勢：防守 MACD 紅綠柱與死叉)\n`3` : RSI超賣反彈 (逆勢：防守高檔轉折)", inline=False)
    
    embed.set_footer(text="🌟 提示：平日開盤期間 (09:30~14:00)，小秘書會每半小時自動推播一次戰報！")
    
    await ctx.send(embed=embed)

@bot.command()
async def 健檢(ctx):
    """手動檢查目前庫存狀況"""
    msg = await ctx.send("⏳ 正在極速分析庫存短線敏銳技術指標...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_health_check), timeout=120.0)
        if len(result) > 1900: result = result[:1900] + "\n\n⚠️ ...(庫存過多，字數達 Discord 上限)"
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 運算逾時，Yahoo 財經連線不穩，請稍後再試。")

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
# 🌐 Render 專用不休眠 Web 伺服器
# =====================================================================
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
