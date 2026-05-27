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
    if not portfolio: return "⚠️ 資料庫為空。"
    
    # 這裡加入換行符號 \n 確保版面整齊
    msg = "📊 **【雲端即時戰報】**\n"
    msg += "------------------------------\n"
    for code, info in portfolio.items():
        tickers = [f"{code}.TW", f"{code}.TWO", f"{code}"]
        df = None
        for t in tickers:
            d = yf.Ticker(t).history(period="1d")
            if not d.empty:
                df = d
                break
        
        if df is None or df.empty:
            msg += f"❌ **{code}**: 抓取失敗\n"
            continue
            
        latest_price = df['Close'].iloc[-1].item()
        cost = info.get('buy_price', 0)
        strat = info.get('strategy', '無')
        profit = round(((latest_price - cost) / cost) * 100, 2)
        
        # 這裡明確顯示所有欄位
        msg += f"✅ **{code}**\n"
        msg += f"   市價: `{round(latest_price, 2)}` | 成本: `{cost}`\n"
        msg += f"   報酬: `{profit}%` | 策略: `{strat}`\n\n"
    return msg

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.command()
async def 健檢(ctx):
    msg = await ctx.send("⏳ 正在撈取最新報價...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_health_check), timeout=20.0)
        await msg.edit(content=result)
    except:
        await msg.edit(content="⚠️ 撈取逾時。")

@bot.command()
async def 新增(ctx, code: str, price: float, strat: str):
    data = load_data()
    data[code] = {"buy_price": price, "strategy": strat}
    save_data(data)
    await ctx.send(f"✅ 已新增 {code} (成本:{price}, 策略:{strat})")

@bot.command()
async def 刪除(ctx, code: str):
    data = load_data()
    # 確保字串比對正確
    if code in data:
        del data[code]
        save_data(data)
        await ctx.send(f"🗑️ 已從監控列表移除 {code}。")
    else:
        await ctx.send(f"⚠️ 找不到代號 {code} (JSON內代號為: {list(data.keys())})")

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
