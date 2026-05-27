import discord
from discord.ext import commands
import yfinance as yf
import json
import os
import asyncio
from aiohttp import web

# 環境變數設定
TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "my_portfolio.json"

# 懶人策略對照表 (你可以隨時在這裡新增 4, 5, 6...)
STRAT_MAP = {
    "1": "1. 布林壓縮突破 (動能)",
    "2": "2. 雙均線+MACD (趨勢)",
    "3": "3. RSI超賣反彈 (逆勢)"
}

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: 
        return json.load(f)

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: 
        json.dump(data, f, indent=4, ensure_ascii=False)

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空，請用 !新增 指令建立股票。"
    
    msg = "📊 **【雲端即時戰報】**\n"
    msg += "------------------------------\n"
    for code, info in portfolio.items():
        # 自動重試三種 Yahoo 可能接受的代號格式
        tickers = [f"{code}.TW", f"{code}.TWO", f"{code}"]
        df = None
        for t in tickers:
            try:
                d = yf.Ticker(t).history(period="1d")
                if not d.empty:
                    df = d
                    break
            except:
                continue
        
        if df is None or df.empty:
            msg += f"❌ **{code}**: 抓取失敗 (Yahoo 查無此號)\n\n"
            continue
            
        latest_price = df['Close'].iloc[-1].item()
        cost = info.get('buy_price', 0)
        strat = info.get('strategy', '無')
        # 避免成本為 0 導致計算錯誤
        profit = round(((latest_price - cost) / cost) * 100, 2) if cost > 0 else 0
        
        # 完整顯示排版
        msg += f"✅ **{code}**\n"
        msg += f"   市價: `{round(latest_price, 2)}` | 成本: `{cost}`\n"
        msg += f"   報酬: `{profit}%` | 策略: `{strat}`\n\n"
    return msg

# 啟動機器人
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.command()
async def 健檢(ctx):
    msg = await ctx.send("⏳ 正在撈取最新報價...")
    try:
        # 強制 25 秒逾時保護，防止機器人卡死
        result = await asyncio.wait_for(asyncio.to_thread(run_health_check), timeout=25.0)
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 撈取資料逾時，Yahoo 伺服器目前回應緩慢，請稍後再試。")

@bot.command()
async def 新增(ctx, code: str, price: float, strat_num: str):
    # 自動轉換懶人代號，如果輸入的不是 1, 2, 3，就直接顯示輸入的字
    full_strat = STRAT_MAP.get(strat_num, strat_num) 
    data = load_data()
    data[code] = {"buy_price": price, "strategy": full_strat}
    save_data(data)
    await ctx.send(f"✅ 已新增 **{code}**\n成本: `{price}`\n策略: `{full_strat}`")

@bot.command()
async def 刪除(ctx, code: str):
    data = load_data()
    if code in data:
        del data[code]
        save_data(data)
        await ctx.send(f"🗑️ 已從監控列表移除 **{code}**。")
    else:
        # 如果刪錯，提示目前有哪些股票可以刪
        stock_list = ", ".join(data.keys()) if data else "無"
        await ctx.send(f"⚠️ 找不到代號 {code} (目前庫存: {stock_list})")

@bot.command()
async def 策略(ctx, code: str, strat_num: str):
    full_strat = STRAT_MAP.get(strat_num, strat_num)
    data = load_data()
    if code in data:
        data[code]['strategy'] = full_strat
        save_data(data)
        await ctx.send(f"✅ **{code}** 策略已更新為: `{full_strat}`")
    else:
        await ctx.send(f"⚠️ 找不到代號 {code}，請先用 !新增 指令。")

# Render 防止關機虛擬伺服器
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
