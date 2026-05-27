import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web

# 環境設定
TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "my_portfolio.json"
bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: return json.load(f)

# V5.6 完整指標計算
def calculate_indicators(df):
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    std = df['Close'].rolling(window=20).std()
    df['BB_Lower'] = df['SMA_20'] - (2 * std)
    df['MACD'] = df['Close'].ewm(span=12, adjust=False).mean() - df['Close'].ewm(span=26, adjust=False).mean()
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    return df

# 核心判斷邏輯
async def auto_check_logic():
    portfolio = load_data()
    channel = bot.get_channel(你的群組頻道ID) # ⚠️ 請填入你的頻道ID
    if not channel: return

    msg = "⏰ **【盤中自動巡航報表】**\n"
    for code, info in portfolio.items():
        ticker = f"{code}.TW" if int(code) > 2000 else f"{code}.TWO"
        df = yf.download(ticker, period="3mo", progress=False)
        if df.empty: continue
        
        df = calculate_indicators(df)
        curr = df.iloc[-1]
        close = curr['Close'].item()
        
        # 策略判斷
        defense = info.get('highest_price', info['buy_price']) * 0.9
        alert = ""
        if close <= defense: alert = "🚨 跌破防守線，快出場！"
        elif curr['MACD'] < curr['Signal']: alert = "📉 MACD 出現死叉"
        
        if alert:
            msg += f"⚠️ **{code}**: {alert}\n"
    
    if len(msg) > 30: await channel.send(msg)

# 機器人指令
@bot.event
async def on_ready():
    scheduler = AsyncIOScheduler()
    # 設定 09:30-13:00 每半小時執行一次
    scheduler.add_job(auto_check_logic, 'cron', hour='9-13', minute='*/30')
    scheduler.start()
    print("✅ 雲端小秘書已上線並啟動定時巡航！")

@bot.command()
async def 健檢(ctx):
    result = await asyncio.to_thread(run_health_check_sync)
    await ctx.send(result)

# 為了配合 async 運作的同步函數
def run_health_check_sync():
    # 此處放置與 auto_check_logic 相同的資料抓取與回傳邏輯
    return "執行中..."

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()

if __name__ == "__main__":
    asyncio.run(main())
