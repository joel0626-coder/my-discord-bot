import discord
from discord.ext import commands
import yfinance as yf
import json
import os
import asyncio
from aiohttp import web

TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "my_portfolio.json"

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空。"
    
    msg = "📊 **【雲端即時戰報】**\n--------------------\n"
    for code, info in portfolio.items():
        # 強化的自動搜尋邏輯：遍歷所有可能的後綴
        possible_tickers = [f"{code}.TW", f"{code}.TWO"]
        df = pd.DataFrame()
        
        for ticker in possible_tickers:
            df = yf.download(ticker, period="1d", progress=False)
            if not df.empty: break
            
        if df.empty:
            msg += f"❌ **{code}**: 抓取失敗 (請檢查是否為下市或代號有誤)\n"
            continue
            
        latest_price = df['Close'].iloc[-1].item()
        buy_price = info.get('buy_price', 0)
        profit = round(((latest_price - buy_price) / buy_price) * 100, 2)
        
        msg += f"✅ **{code}** | 市價: `{round(latest_price, 2)}` | 報酬: `{profit}%` | 策略: `{info.get('strategy')}`\n"
    return msg

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.command()
async def 健檢(ctx):
    # 增加一個「正在抓取」的反饋，防止以為當機
    msg = await ctx.send("⏳ 正在連線 Yahoo 財經撈取最新報價...")
    result = await asyncio.to_thread(run_health_check)
    await msg.edit(content=result)

@bot.command()
async def 新增(ctx, code: str, price: float, strat: str):
    data = load_data()
    data[code] = {"buy_price": price, "strategy": strat}
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)
    await ctx.send(f"✅ 已新增 {code} 到雲端監控。")

# Render 防止關機
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
