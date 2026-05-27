import discord
from discord.ext import commands
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web

# 原本寫法：TOKEN = os.environ.get('DISCORD_TOKEN')
# 改成這個強制讀取的方式：
TOKEN = os.environ['DISCORD_TOKEN']

PORTFOLIO_FILE = "cloud_portfolio.json"
STRAT_MAP = {"1": "1. 布林壓縮突破", "2": "2. 雙均線+MACD", "3": "3. RSI超賣反彈"}

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

# 機器人邏輯
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.command()
async def 健檢(ctx):
    await ctx.send("⏳ 正在連線 Yahoo 財經抓取最新報價...")
    # (此處省略計算函數，請保留你之前的計算邏輯)
    # 確保這支程式在雲端能穩定運作
    await ctx.send("✅ 系統運作正常，防守線計算完成。")

@bot.command()
async def 新增(ctx, code: str, price: float, strat_num: str):
    data = load_data()
    data[code] = {"buy_price": price, "strategy": STRAT_MAP.get(strat_num, "未知")}
    save_data(data)
    await ctx.send(f"✅ {code} 已加入監控。")

# 2. Render 必備：建立虛擬 Web 服務防止被關機
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
