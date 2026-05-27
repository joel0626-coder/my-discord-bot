import discord
from discord.ext import commands
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
import logging

logging.getLogger('yfinance').setLevel(logging.CRITICAL)

# ==========================================
# 📱 雲端系統設定 (存檔會跟著程式放在雲端資料夾)
# ==========================================
PORTFOLIO_FILE = "cloud_portfolio.json"

STRAT_MAP = {
    "1": "1. 布林壓縮突破 (動能)",
    "2": "2. 雙均線+MACD (趨勢)",
    "3": "3. RSI超賣反彈 (逆勢)"
}

# 確保雲端存檔存在
if not os.path.exists(PORTFOLIO_FILE):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
        json.dump({}, f)

def load_data():
    with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

# ==========================================
# 📊 指標與邏輯計算區
# ==========================================
def calculate_indicators(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    df['EMA_10'] = df['Close'].ewm(span=10, adjust=False).mean()
    df['EMA_20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['MACD'] = df['Close'].ewm(span=12, adjust=False).mean() - df['Close'].ewm(span=26, adjust=False).mean()
    df['Signal_Line'] = df['MACD'].ewm(span=9, adjust=False).mean()
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    rs = gain.ewm(alpha=1/14, adjust=False).mean() / loss.ewm(alpha=1/14, adjust=False).mean()
    df['RSI_14'] = 100 - (100 / (1 + rs))
    return df

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 老闆，目前雲端資料庫沒有追蹤任何持股喔！請用 `!新增` 指令加入。"

    msg = "📊 **【老闆，這是您目前的持股戰情即時健檢】**\n========================================\n"
    portfolio_updated = False
    
    for stock_code, info in portfolio.items():
        try:
            ticker = f"{stock_code}.TW"
            df = yf.download(ticker, period="6mo", progress=False)
            if df.empty: df = yf.download(f"{stock_code}.TWO", period="6mo", progress=False)
            if df.empty: continue

            df = calculate_indicators(df)
            latest, prev1 = df.iloc[-1], df.iloc[-2]
            close = latest['Close'].item()
            
            buy_price, strategy = info['buy_price'], info['strategy']
            highest_price = info.get('highest_price', buy_price)
            
            if close > highest_price:
                highest_price = close
                portfolio[stock_code]['highest_price'] = highest_price
                portfolio_updated = True

            profit_pct = round(((close - buy_price) / buy_price) * 100, 2)
            defense_price = round(highest_price * 0.9, 2)
            
            status_icon, warning = "✅", ""
            if strategy in [STRAT_MAP["1"], STRAT_MAP["2"]] and close <= defense_price:
                status_icon, warning = "🚨", " (觸發10%移動停利，請出場！)"
            elif strategy == STRAT_MAP["1"] and close < latest['SMA_20'].item():
                status_icon, warning = "📉", " (跌破月線，動能破壞)"
            elif strategy == STRAT_MAP["2"] and ((prev1['MACD'].item() > prev1['Signal_Line'].item()) and (latest['MACD'].item() < latest['Signal_Line'].item())):
                status_icon, warning = "📉", " (MACD死叉)"
            
            strat_short = strategy.split(" ")[1]
            msg += f"{status_icon} **{stock_code}** | 現價 `{round(close,2)}` | 報酬 **{profit_pct}%**\n"
            msg += f"   └ 防守: {defense_price} | 策略: {strat_short} {warning}\n"
        except: continue

    msg += "========================================"
    if portfolio_updated: save_data(portfolio)
    return msg

# ==========================================
# 🤖 Discord 機器人指令區
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'✅ 雲端小秘書 {bot.user} 已上線！')

@bot.command()
async def 健檢(ctx):
    await ctx.send("⏳ 收到！正在為老闆抓取最新報價與計算防守線...")
    result_msg = await asyncio.to_thread(run_health_check)
    await ctx.send(result_msg)

@bot.command()
async def 新增(ctx, code: str, price: float, strat_num: str):
    """用法: !新增 2330 800 2 (1:布林 2:MACD 3:RSI)"""
    if strat_num not in STRAT_MAP:
        await ctx.send("❌ 策略代號錯誤！請輸入 1(布林), 2(MACD), 或 3(RSI)。\n範例：`!新增 2330 800 2`")
        return
    
    portfolio = load_data()
    portfolio[code] = {
        "buy_price": price,
        "strategy": STRAT_MAP[strat_num],
        "highest_price": price
    }
    save_data(portfolio)
    await ctx.send(f"✅ 報告老闆！已將 **{code}** (買價 {price}, 策略 {STRAT_MAP[strat_num].split(' ')[1]}) 寫入雲端追蹤庫！")

@bot.command()
async def 刪除(ctx, code: str):
    """用法: !刪除 2330"""
    portfolio = load_data()
    if code in portfolio:
        del portfolio[code]
        save_data(portfolio)
        await ctx.send(f"🗑️ 已從雲端資料庫停止追蹤 **{code}**。")
    else:
        await ctx.send(f"⚠️ 雲端資料庫中找不到 **{code}**。")

# 🔑 在這裡貼上你的 Bot Token
bot.run('MTUwOTA1OTAyMTc3MDg1MDM2NQ.GXXDe2.QvvTD9NgktsAphkP4YkVlLQT5MEZ7aVncsQxCY')