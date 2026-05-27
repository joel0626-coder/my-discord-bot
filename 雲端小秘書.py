import discord
from discord.ext import commands
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web

# 1. 安全設定：強制讀取環境變數 (需在 Render 設定 DISCORD_TOKEN)
TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "my_portfolio.json"

# 載入與儲存資料庫
def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: 
        return json.load(f)

# 2. 核心健檢邏輯 (含自動偵測代號機制)
def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空，請使用 !新增 指令寫入股票。"
    
    msg = "📊 **【雲端即時戰報】**\n--------------------\n"
    for code, info in portfolio.items():
        # 自動嘗試上市 (.TW) 或上櫃 (.TWO) 代號
        ticker_tw = f"{code}.TW"
        ticker_two = f"{code}.TWO"
        
        # 抓取資料
        df = yf.download(ticker_tw, period="1mo", progress=False)
        if df.empty:
            df = yf.download(ticker_two, period="1mo", progress=False)
            
        if df.empty:
            msg += f"⚠️ **{code}** | 抓取報價失敗\n"
            continue
            
        # 計算簡易指標
        close = df['Close'].iloc[-1].item()
        buy_price = info.get('buy_price', 0)
        strat = info.get('strategy', '未知')
        profit = round(((close - buy_price) / buy_price) * 100, 2)
        
        msg += f"✅ **{code}** | 現價: `{round(close, 2)}` | 報酬: `{profit}%`\n"
        msg += f"   └ 策略: {strat}\n\n"
    return msg

# 3. 機器人互動指令設定
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
async def 新增(ctx, code: str, price: float, strat_num: str):
    data = load_data()
    strat_map = {"1": "1. 布林壓縮突破", "2": "2. 雙均線+MACD", "3": "3. RSI超賣反彈"}
    data[code] = {"buy_price": price, "strategy": strat_map.get(strat_num, "未知")}
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)
    await ctx.send(f"✅ 已將 **{code}** 寫入雲端追蹤庫！")

# 4. Render 必備：虛擬網頁伺服器防止被當機
async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is alive!"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()

async def main():
    # 同時啟動網頁監聽與機器人
    await asyncio.gather(start_web_server(), bot.start(TOKEN))

if __name__ == "__main__":
    asyncio.run(main())
