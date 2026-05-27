import discord
from discord.ext import commands, tasks
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web
from datetime import time, timezone, timedelta

TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "my_portfolio.json"

# ========= 🚨 請在這裡貼上你的 Discord 頻道 ID =========
PUSH_CHANNEL_ID = 1509058179458404495  # <--- 換成你剛剛複製的數字
# ========================================================

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
    """計算技術指標與成交量防呆"""
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    # 防呆機制：5日均量
    df['Vol_5MA'] = df['Volume'].rolling(window=5).mean()
    
    std = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['SMA_20'] + (2 * std)
    df['BB_Lower'] = df['SMA_20'] - (2 * std)
    
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    
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
    
    msg = "📊 **【雲端精準監控戰報】**\n"
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
        
        if df is None or len(df) <= 30:
            msg += f"❌ **{code}**: 抓取失敗\n\n"
            continue
            
        df = calculate_indicators(df)
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        close = latest['Close'].item()
        cost = info.get('buy_price', 0)
        strat = info.get('strategy', '無')
        profit = round(((close - cost) / cost) * 100, 2) if cost > 0 else 0
        
        # 指標與防呆判定
        ma20 = latest['SMA_20'].item()
        bb_upper = latest['BB_Upper'].item()
        bb_lower = latest['BB_Lower'].item()
        macd_val = latest['MACD'].item()
        sig_val = latest['Signal'].item()
        rsi_val = latest['RSI'].item()
        
        latest_vol = latest['Volume'].item()
        vol_5ma = latest['Vol_5MA'].item()
        is_high_vol = latest_vol > vol_5ma # 是否爆量
        
        macd_status = "✅ 多頭" if macd_val > sig_val else "⚠️ 空頭"
        
        custom_panel = ""
        alert_msg = ""
        
        # 依據策略與成交量判斷
        if "1" in strat or "布林" in strat:
            custom_panel = f"布林: 下軌 `{round(bb_lower, 2)}` | 上軌 `{round(bb_upper, 2)}`"
            if close < bb_lower:
                alert_msg = "🚨 [快出場] 爆量跌破下軌！(主力逃命)" if is_high_vol else "⚠️ [注意] 無量跌破下軌 (小心假跌破)"
            elif close > bb_upper:
                alert_msg = "🔥 [動能強] 帶量突破上軌！" if is_high_vol else "🤔 [觀察] 無量上漲 (可能後繼無力)"
                
        elif "2" in strat or "MACD" in strat:
            custom_panel = f"MACD: `{macd_status}` | 月線: `{round(ma20, 2)}`"
            if prev['MACD'].item() > prev['Signal'].item() and macd_val < sig_val:
                alert_msg = "📉 [警告] MACD 爆量死叉！" if is_high_vol else "📉 [警告] MACD 無量死叉 (動能衰退)"
            elif close < ma20:
                alert_msg = "🚨 [危險] 爆量跌破月線！" if is_high_vol else "⚠️ [注意] 縮量跌破月線 (先別急著砍)"
                
        elif "3" in strat or "RSI" in strat:
            custom_panel = f"RSI: `{round(rsi_val, 2)}` | 月線: `{round(ma20, 2)}`"
            if rsi_val < 30:
                alert_msg = "🟢 [超賣] RSI 低於 30，留意反彈"
            elif rsi_val > 70:
                alert_msg = "🔴 [超買] RSI 高於 70，留意過熱"
                
        else:
            custom_panel = f"月線: `{round(ma20, 2)}`"
            
        if close < ma20 and not alert_msg:
            alert_msg = "🚨 爆量跌破月線！" if is_high_vol else "⚠️ 縮量跌破月線 (觀察中)"
            
        if not alert_msg:
            alert_msg = "👌 狀態穩定"
            
        msg += f"📌 **{code}**\n"
        msg += f"   市價: `{round(close, 2)}` | 成本: `{cost}` | 報酬: `{profit}%`\n"
        msg += f"   策略: `{strat}`\n"
        msg += f"   指標: {custom_panel}\n"
        msg += f"   👉 {alert_msg}\n"
        msg += "-------------------------\n"
        
    return msg

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 測試用推播設定 (每 5 分鐘一次) ---
from datetime import datetime, timezone, timedelta

@tasks.loop(minutes=5)
async def auto_report():
    channel = bot.get_channel(PUSH_CHANNEL_ID)
    if channel:
        result = await asyncio.to_thread(run_health_check)
        await channel.send(f"🔔 **【系統測試】5分鐘定時推播**\n{result}")

@bot.event
async def on_ready():
    print(f"Bot 登入成功: {bot.user}")
    if not auto_report.is_running():
        auto_report.start()
        print("5分鐘推播測試已啟動！")
# ---------------------------------------------

@bot.event
async def on_ready():
    print(f"Bot 登入成功: {bot.user}")
    if not daily_report.is_running():
        daily_report.start()
        print("每日推播排程已啟動！")
# ---------------------------------------------

@bot.command()
async def 健檢(ctx):
    msg = await ctx.send("⏳ 正在分析量價關係與技術指標...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_health_check), timeout=30.0)
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 運算逾時，請稍後再試。")

@bot.command()
async def 新增(ctx, code: str, price: float, strat_num: str):
    full_strat = STRAT_MAP.get(strat_num, strat_num) 
    data = load_data()
    data[code] = {"buy_price": price, "strategy": full_strat}
    save_data(data)
    await ctx.send(f"✅ 已新增 **{code}**\n成本: `{price}`\n策略: `{full_strat}`")

@bot.command()
async def 刪除(ctx, code: str):
    data = load_data()
    if code in data:
        del data[code]
        save_data(data)
        await ctx.send(f"🗑️ 已從監控列表移除 **{code}**。")
    else:
        await ctx.send(f"⚠️ 找不到代號 {code}")

@bot.command()
async def 策略(ctx, code: str, strat_num: str):
    full_strat = STRAT_MAP.get(strat_num, strat_num)
    data = load_data()
    if code in data:
        data[code]['strategy'] = full_strat
        save_data(data)
        await ctx.send(f"✅ **{code}** 策略已更新為: `{full_strat}`")
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
