import discord
from discord.ext import commands
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
import twstock  # 🌟 自動查中文股名用
from aiohttp import web

# 強制讀取環境變數
TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "my_portfolio.json"

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

# 自動獲取中文股名函數
def get_stock_name(code):
    stock = twstock.stock(code)
    # 若抓不到則顯示代號
    return stock.name if stock else code

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空。"
    
    msg = "📊 **【雲端即時戰報】**\n--------------------\n"
    for code, info in portfolio.items():
        # 自動偵測 TW/TWO 格式
        ticker = f"{code}.TW" if int(code) > 2000 else f"{code}.TWO"
        df = yf.download(ticker, period="1mo", progress=False)
        
        if df.empty:
            msg += f"⚠️ **{code}** | 抓不到報價\n"
            continue
            
        close = df['Close'].iloc[-1].item()
        name = get_stock_name(code) # 🌟 自動查名
        buy_price = info.get('buy_price', 0)
        strat = info.get('strategy', '未設定')
        profit = round(((close - buy_price) / buy_price) * 100, 2)
        
        msg += f"✅ **{name} ({code})**\n"
        msg += f"   現價: `{round(close, 2)}` | 報酬: `{profit}%` | 策略: {strat}\n\n"
    return msg

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

@bot.command()
async def 新增(ctx, code: str, price: float, strategy: str):
    data = load_data()
    data[code] = {"buy_price": price, "strategy": strategy}
    save_data(data)
    await ctx.send(f"✅ {get_stock_name(code)} ({code}) 已加入監控。")

# Render 必須
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
