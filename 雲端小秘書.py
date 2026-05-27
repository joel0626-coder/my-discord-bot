import discord
from discord.ext import commands, tasks
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web
from datetime import datetime, time, timezone, timedelta

TOKEN = os.environ.get('DISCORD_TOKEN')
PORTFOLIO_FILE = "my_portfolio.json"

# ========= 🚨 你的專屬 Discord 頻道 ID =========
PUSH_CHANNEL_ID = 1509058179458404495
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

def calculate_indicators(df):
    """計算更敏銳的短線技術指標"""
    # 新增 5日線 與 10日線 作為短線防護網
    df['SMA_5'] = df['Close'].rolling(window=5).mean()
    df['SMA_10'] = df['Close'].rolling(window=10).mean()
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    df['Vol_5MA'] = df['Volume'].rolling(window=5).mean()
    
    # 布林通道 (維持 20 日計算基礎)
    std = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['SMA_20'] + (2 * std)
    df['BB_Lower'] = df['SMA_20'] - (2 * std)
    
    # MACD 與 柱狀圖 (Histogram)
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist'] = df['MACD'] - df['Signal'] # 敏銳度關鍵：紅綠柱
    
    # RSI
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
        
        # 抓取各項指標最新與昨日數值
        ma5 = latest['SMA_5'].item()
        ma10 = latest['SMA_10'].item()
        ma20 = latest['SMA_20'].item()
        bb_upper = latest['BB_Upper'].item()
        bb_lower = latest['BB_Lower'].item()
        
        macd_val = latest['MACD'].item()
        sig_val = latest['Signal'].item()
        hist_val = latest['MACD_Hist'].item()
        prev_hist = prev['MACD_Hist'].item()
        
        rsi_val = latest['RSI'].item()
        prev_rsi = prev['RSI'].item()
        
        latest_vol = latest['Volume'].item()
        vol_5ma = latest['Vol_5MA'].item()
        is_high_vol = latest_vol > vol_5ma
        
        macd_status = "✅ 多頭" if macd_val > sig_val else "⚠️ 空頭"
        
        custom_panel = ""
        alert_msg = ""
        
        # ====== 第一層防護網：%數硬限制 ======
        if tp_pct and profit >= float(tp_pct):
            alert_msg = f"💰 [獲利出場] 報酬率 {profit}% 已達停利點 (+{tp_pct}%)！"
        elif sl_pct and profit <= -float(sl_pct):
            alert_msg = f"🛑 [落跑停損] 報酬率 {profit}% 已達停損點 (-{sl_pct}%)！"
            
        # ====== 第二層防護網：敏銳版技術指標 ======
        if not alert_msg:
            # 策略 1：布林動能 (改看 5MA 與 10MA)
            if "1" in strat or "布林" in strat:
                custom_panel = f"上軌 `{round(bb_upper, 2)}` | 5日線 `{round(ma5, 2)}` | 10日線 `{round(ma10, 2)}`"
                if close < ma10:
                    alert_msg = "🚨 [快出場] 跌破 10 日線，飆車動能完全消散！" if is_high_vol else "⚠️ [注意] 破 10 日線，趨勢轉弱。"
                elif close < ma5:
                    alert_msg = "⚠️ [警訊] 跌破 5 日線，短線可能熄火。"
                elif close > bb_upper:
                    alert_msg = "🔥 [動能強] 帶量突破上軌！" if is_high_vol else "🤔 [觀察] 無量上漲，提防騙線。"
                    
            # 策略 2：MACD 趨勢 (提早看紅柱縮減與 10MA)
            elif "2" in strat or "MACD" in strat:
                custom_panel = f"MACD: `{macd_status}` | 10日線 `{round(ma10, 2)}` | 月線 `{round(ma20, 2)}`"
                
                # 敏銳判斷：紅柱縮短代表動能衰退，死叉則是最後通牒
                if prev['MACD'].item() > prev['Signal'].item() and macd_val < sig_val:
                    alert_msg = "🚨 [逃命] MACD 死叉成形，波段結束！"
                elif hist_val > 0 and hist_val < prev_hist:
                    alert_msg = "⚠️ [警訊] MACD 紅柱縮減，上漲動能開始衰退。"
                elif close < ma10:
                    alert_msg = "📉 [轉弱] 跌破 10 日線，提防下探月線。"
                    
            # 策略 3：RSI 逆勢 (看高檔反轉)
            elif "3" in strat or "RSI" in strat:
                custom_panel = f"RSI: `{round(rsi_val, 2)}` | 5日線 `{round(ma5, 2)}`"
                
                # 敏銳判斷：RSI 衝高後開始反轉向下
                if rsi_val < prev_rsi and prev_rsi > 70:
                    alert_msg = "🚨 [快跑] RSI 自高檔反轉向下，主力可能正在倒貨！"
                elif rsi_val > 75:
                    alert_msg = "🔴 [極度超買] RSI 突破 75，隨時面臨獲利了結賣壓。"
                elif rsi_val < 25:
                    alert_msg = "🟢 [極度超賣] RSI 跌破 25，散戶恐慌，留意報復性反彈。"
                    
            else:
                custom_panel = f"10日線 `{round(ma10, 2)}` | 月線 `{round(ma20, 2)}`"
                
            # 通用底線防護
            if close < ma20 and not alert_msg:
                alert_msg = "🚨 爆量跌破月線 (中線轉空)！" if is_high_vol else "⚠️ 縮量跌破月線"
                
        if not alert_msg:
            alert_msg = "👌 狀態穩定"
            
        tp_sl_info = f" | 停利: `+{tp_pct}%` 停損: `-{sl_pct}%`" if (tp_pct or sl_pct) else " | 風控: `未設定`"
        
        msg += f"📌 **{display_title}**\n"
        msg += f"   市價: `{round(close, 2)}` | 成本: `{cost}` | 報酬: `{profit}%`{tp_sl_info}\n"
        msg += f"   策略: `{strat}`\n"
        msg += f"   指標: {custom_panel}\n"
        msg += f"   👉 {alert_msg}\n"
        msg += "-------------------------\n"
        
    return msg

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 自動推播排程
@tasks.loop(minutes=30)
async def auto_report():
    tw_tz = timezone(timedelta(hours=8))
    now = datetime.now(tw_tz)
    if now.weekday() > 4: return 
    
    current_time = now.time()
    start_time = time(hour=9, minute=30)
    end_time = time(hour=14, minute=0)
    
    if start_time <= current_time <= end_time:
        channel = bot.get_channel(PUSH_CHANNEL_ID)
        if channel:
            result = await asyncio.to_thread(run_health_check)
            await channel.send(f"🔔 **【盤中即時監控】{now.strftime('%H:%M')} 戰報**\n{result}")

@bot.event
async def on_ready():
    print(f"✅ Bot 登入成功: {bot.user}")
    if not auto_report.is_running():
        auto_report.start()
        print("🚀 盤中半小時推播排程已啟動！")

@bot.command()
async def 健檢(ctx):
    msg = await ctx.send("⏳ 正在分析短線敏銳技術指標...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_health_check), timeout=30.0)
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 運算逾時，請稍後再試。")

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
    else:
        await ctx.send(f"⚠️ 找不到代號 {code}。")

@bot.command()
async def 命名(ctx, code: str, name: str):
    data = load_data()
    if code in data:
        data[code]['name'] = name
        save_data(data)
        await ctx.send(f"✅ 已將代號 **{code}** 命名為 **{name}**")
    else:
        await ctx.send(f"⚠️ 找不到代號 {code}。")

@bot.command()
async def 刪除(ctx, code: str):
    data = load_data()
    if code in data:
        name = data[code].get('name', '')
        del data[code]
        save_data(data)
        await ctx.send(f"🗑️ 已從監控列表移除 **{code} {name}**。")
    else:
        await ctx.send(f"⚠️ 找不到代號 {code}")

@bot.command()
async def 策略(ctx, code: str, strat_num: str):
    full_strat = STRAT_MAP.get(strat_num, strat_num)
    data = load_data()
    if code in data:
        data[code]['strategy'] = full_strat
        save_data(data)
        name = data[code].get('name', '')
        await ctx.send(f"✅ **{code} {name}** 策略已更新為: `{full_strat}`")
    else:
        await ctx.send(f"⚠️ 找不到代號 {code}。")

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
