import discord
from discord.ext import commands
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web

# 設定
TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "my_portfolio.json"

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空。"
    
    msg = "📊 **【雲端監控戰報】**\n--------------------\n"
    for code, info in portfolio.items():
        ticker = f"{code}.TW" if int(code) > 2000 else f"{code}.TWO"
        df = yf.download(ticker, period="6mo", progress=False)
        if df.empty: continue
        
        close = df['Close'].iloc[-1].item()
        buy_price = info.get('buy_price', 0)
        profit = round(((close - buy_price) / buy_price) * 100, 2)
        msg += f"✅ **{code}** | 現價: `{round(close, 2)}` | 報酬: `{profit}%` | 策略: `{info.get('strategy')}`\n"
    return msg

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 指令區 ---

@bot.command()
async def 健檢(ctx):
    result = await asyncio.to_thread(run_health_check)
    await ctx.send(result)

@bot.command()
async def 新增(ctx, code: str, price: float, strat: str):
    data = load_data()
    data[code] = {"buy_price": price, "strategy": strat}
    save_data(data)
    await ctx.send(f"✅ {code} 已加入監控。")

@bot.command()
async def 刪除(ctx, code: str):
    data = load_data()
    if code in data:
        del data[code]
        save_data(data)
        await ctx.send(f"🗑️ 已從監控列表移除 {code}。")
    else:
        await ctx.send("⚠️ 找不到該股票代號。")

# Render Web Server
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
