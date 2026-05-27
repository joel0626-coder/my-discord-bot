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
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 目前 JSON 檔案為空，請使用 !新增 指令。"
    
    msg = "📊 **【雲端即時戰報】**\n"
    for code, info in portfolio.items():
        # 自動偵測上市/上櫃後綴
        ticker = f"{code}.TW" if int(code) > 2000 else f"{code}.TWO"
        # 使用詳細下載與異常處理
        df = yf.download(ticker, period="1mo", progress=False)
        
        if df.empty:
            msg += f"❌ **{code}**: 抓取失敗\n"
            continue
            
        close = df['Close'].iloc[-1].item()
        buy_price = info.get('buy_price', 0)
        profit = round(((close - buy_price) / buy_price) * 100, 2)
        msg += f"✅ **{code}** | 現價: `{round(close, 2)}` | 報酬: `{profit}%` | 策略: `{info.get('strategy')}`\n"
    return msg

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.command()
async def 健檢(ctx):
    result = await asyncio.to_thread(run_health_check)
    await ctx.send(result)

@bot.command()
async def 新增(ctx, code: str, price: float, strat: str):
    data = load_data()
    data[code] = {"buy_price": price, "strategy": strat}
    save_data(data)
    await ctx.send(f"✅ 已新增 {code}。")

@bot.command()
async def 策略(ctx, code: str, new_strat: str):
    data = load_data()
    if code in data:
        data[code]['strategy'] = new_strat
        save_data(data)
        await ctx.send(f"✅ {code} 策略已更新為: {new_strat}")
    else:
        await ctx.send("⚠️ 找不到該股票代號。")

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
