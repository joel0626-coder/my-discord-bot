import discord
from discord.ext import commands
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web

# 強制讀取 Token
TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "cloud_portfolio.json"
STRAT_MAP = {"1": "1. 布林壓縮突破", "2": "2. 雙均線+MACD", "3": "3. RSI超賣反彈"}

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

def calculate_indicators(df):
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    df['MACD'] = df['Close'].ewm(span=12, adjust=False).mean() - df['Close'].ewm(span=26, adjust=False).mean()
    df['Signal_Line'] = df['MACD'].ewm(span=9, adjust=False).mean()
    return df

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 目前沒有持股資料。"
    msg = "📊 **持股健檢報表**\n--------------------\n"
    for code, info in portfolio.items():
        df = yf.download(f"{code}.TW", period="6mo", progress=False)
        if df.empty: df = yf.download(f"{code}.TWO", period="6mo", progress=False)
        df = calculate_indicators(df)
        close = df['Close'].iloc[-1].item()
        msg += f"✅ **{code}** | 現價: {round(close, 2)}\n"
    return msg

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.command()
async def 健檢(ctx):
    await ctx.message.add_reaction("⏳")
    result_msg = await asyncio.to_thread(run_health_check)
    await ctx.send(result_msg)
    await ctx.message.remove_reaction("⏳", bot.user)
    await ctx.message.add_reaction("✅")

@bot.command()
async def 新增(ctx, code: str, price: float, strat_num: str):
    data = load_data()
    data[code] = {"buy_price": price, "strategy": STRAT_MAP.get(strat_num, "未知")}
    save_data(data)
    await ctx.send(f"✅ {code} 已加入監控。")

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
