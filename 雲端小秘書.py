import discord
from discord.ext import commands
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web

# 1. 強制讀取 Token 與設定檔
TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "my_portfolio.json" # 已修正為你的檔名
STRAT_MAP = {"1": "1. 布林壓縮突破", "2": "2. 雙均線+MACD", "3": "3. RSI超賣反彈"}

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

# 2. V5.6 完整指標計算邏輯
def calculate_indicators(df):
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    df['MACD'] = df['Close'].ewm(span=12, adjust=False).mean() - df['Close'].ewm(span=26, adjust=False).mean()
    df['Signal_Line'] = df['MACD'].ewm(span=9, adjust=False).mean()
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

# 3. 完整健檢邏輯
def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空，請用 !新增 指令。"
    
    msg = "📊 **【雲端持股健檢報表】**\n--------------------\n"
    for code, info in portfolio.items():
        # 嘗試抓取資料
        ticker = f"{code}.TW" if int(code) > 2000 else f"{code}.TWO"
        df = yf.download(ticker, period="6mo", progress=False)
        if df.empty: continue
        
        df = calculate_indicators(df)
        latest = df.iloc[-1]
        close = latest['Close'].item()
        buy_price = info.get('buy_price', 0)
        
        # 簡單策略判斷 (V5.6 風格)
        profit = round(((close - buy_price) / buy_price) * 100, 2)
        macd_status = "✅ 多頭" if latest['MACD'] > latest['Signal_Line'] else "⚠️ 空頭"
        
        msg += f"**{code}** | 現價: {round(close, 2)} | 報酬: {profit}%\n"
        msg += f"└ 策略: {info.get('strategy', '無')} | MACD: {macd_status}\n\n"
    return msg

# 4. 機器人啟動
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
