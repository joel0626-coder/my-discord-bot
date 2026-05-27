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
    # 增加一個偽裝參數，避免被 Yahoo 擋住
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'}
    
    for code, info in portfolio.items():
        # 自動嘗試上市 (.TW) 或上櫃 (.TWO)
        tickers = [f"{code}.TW", f"{code}.TWO"]
        found_price = None
        
        for t in tickers:
            try:
                # 限制下載時間，確保不當機
                data = yf.Ticker(t).history(period="1d")
                if not data.empty:
                    found_price = data['Close'].iloc[-1].item()
                    break
            except: continue
            
        if found_price:
            buy_price = info.get('buy_price', 0)
            profit = round(((found_price - buy_price) / buy_price) * 100, 2)
            msg += f"✅ **{code}** | 市價: `{round(found_price, 2)}` | 報酬: `{profit}%`\n"
        else:
            msg += f"❌ **{code}**: 抓取逾時/失敗\n"
    return msg

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.command()
async def 健檢(ctx):
    # 改為發送訊息後，再進行編輯，這樣比較不會出現「已讀不回」的錯覺
    msg = await ctx.send("⏳ 正在撈取最新報價 (如超過 10 秒代表 Yahoo 反應慢)...")
    try:
        # 給予 15 秒執行上限
        result = await asyncio.wait_for(asyncio.to_thread(run_health_check), timeout=15.0)
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 撈取資料逾時，請稍後再試。")

@bot.command()
async def 新增(ctx, code: str, price: float, strat: str):
    data = load_data()
    data[code] = {"buy_price": price, "strategy": strat}
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)
    await ctx.send(f"✅ 已新增 {code}。")

# Render 必須要有 Web Server
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
