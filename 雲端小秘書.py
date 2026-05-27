import discord
from discord.ext import commands
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web

# 強制讀取環境變數 (確保 Render 設定了 DISCORD_TOKEN)
TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "my_portfolio.json"

def load_data():
    if not os.path.exists(PORTFOLIO_FILE): return {}
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: return json.load(f)

# V5.6 指標計算邏輯
def calculate_indicators(df):
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    # 布林通道
    std = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['SMA_20'] + (2 * std)
    # MACD
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    return df

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空。"
    
    msg = "📊 **【雲端精準監控戰報】**\n--------------------\n"
    for code, info in portfolio.items():
        ticker = f"{code}.TW" if int(code) > 2000 else f"{code}.TWO"
        df = yf.download(ticker, period="6mo", progress=False)
        if df.empty: continue
            
        df = calculate_indicators(df)
        latest = df.iloc[-1]
        close = latest['Close'].item()
        
        # 計算防守線 (以最高價的 90% 為例)
        highest = info.get('highest_price', info['buy_price'])
        defense_line = round(highest * 0.9, 2)
        
        # 策略判斷
        alert = ""
        if close <= defense_line:
            alert = "🚨 [快出場] 跌破防守線！"
        elif latest['MACD'] < latest['Signal']:
            alert = "📉 [MACD死叉] 轉弱警示"
            
        profit = round(((close - info['buy_price']) / info['buy_price']) * 100, 2)
        msg += f"✅ **{code}** | 現價: `{round(close, 2)}` | 報酬: `{profit}%`\n"
        msg += f"   └ {alert}\n" if alert else f"   └ 狀態: 正常運作\n"
    return msg

# Discord 機器人設定
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
async def 新增(ctx, code: str, price: float, strat: str):
    data = load_data()
    data[code] = {"buy_price": price, "strategy": strat, "highest_price": price}
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)
    await ctx.send(f"✅ {code} 已加入監控。")

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
