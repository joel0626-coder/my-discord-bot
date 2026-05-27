import discord
from discord.ext import commands
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web

# 強制讀取設定
TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "my_portfolio.json"

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: return json.load(f)

# V5.6 核心邏輯：完整指標計算
def calculate_indicators(df):
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    df['SMA_60'] = df['Close'].rolling(window=60).mean()
    df['MACD'] = df['Close'].ewm(span=12, adjust=False).mean() - df['Close'].ewm(span=26, adjust=False).mean()
    df['Signal_Line'] = df['MACD'].ewm(span=9, adjust=False).mean()
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    rs = gain.ewm(alpha=1/14, adjust=False).mean() / loss.ewm(alpha=1/14, adjust=False).mean()
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 目前沒有持股資料。"
    
    msg = "📊 **【V5.6 雲端戰情室】**\n--------------------\n"
    for code, info in portfolio.items():
        ticker = f"{code}.TW" if int(code) > 2000 else f"{code}.TWO"
        df = yf.download(ticker, period="6mo", progress=False)
        if df.empty: continue
        
        df = calculate_indicators(df)
        latest = df.iloc[-1]
        close = latest['Close'].item()
        
        # 顯示 V5.6 的報酬與策略
        profit = round(((close - info['buy_price']) / info['buy_price']) * 100, 2)
        macd = "✅ 多頭" if latest['MACD'] > latest['Signal_Line'] else "⚠️ 空頭"
        rsi = round(latest['RSI'].item(), 2)
        
        msg += f"✅ **{code}** | 現價: `{round(close, 2)}` | 報酬: `{profit}%`\n"
        msg += f"   └ 策略: {info['strategy']}\n"
        msg += f"   └ MACD: {macd} | RSI: {rsi}\n\n"
    return msg

# Discord 機器人設定
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.command()
async def 健檢(ctx):
    await ctx.message.add_reaction("⏳")
    result = await asyncio.to_thread(run_health_check)
    await ctx.send(result)
    await ctx.message.remove_reaction("⏳", bot.user)
    await ctx.message.add_reaction("✅")

# 雲端防當機機制
async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is alive!"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()

async def main():
    await asyncio.gather(start_web_server(), bot.start(TOKEN))

if __name__ == "__main__":
    asyncio.run(main())
