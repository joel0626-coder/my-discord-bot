import discord
from discord.ext import commands
import yfinance as yf
import pandas as pd
import json
import os
import asyncio
from aiohttp import web

# 環境變數設定
TOKEN = os.environ['DISCORD_TOKEN']
PORTFOLIO_FILE = "my_portfolio.json"

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

def calculate_indicators(df):
    """計算技術指標：MACD, 布林通道"""
    # SMA 20 (月線)
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    
    # 布林通道
    std = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['SMA_20'] + (2 * std)
    df['BB_Lower'] = df['SMA_20'] - (2 * std)
    
    # MACD
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    return df

def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空，請用 !新增 指令建立股票。"
    
    msg = "📊 **【雲端精準監控戰報】**\n"
    msg += "------------------------------\n"
    for code, info in portfolio.items():
        tickers = [f"{code}.TW", f"{code}.TWO", f"{code}"]
        df = None
        for t in tickers:
            try:
                # 這次抓 2 個月資料來算均線
                d = yf.Ticker(t).history(period="2mo")
                if len(d) > 20: # 確保資料夠算月線
                    df = d
                    break
            except:
                continue
        
        if df is None or len(df) <= 20:
            msg += f"❌ **{code}**: 抓取失敗或資料不足\n\n"
            continue
            
        df = calculate_indicators(df)
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        close = latest['Close'].item()
        cost = info.get('buy_price', 0)
        strat = info.get('strategy', '無')
        profit = round(((close - cost) / cost) * 100, 2) if cost > 0 else 0
        
        # --- 策略與警示判斷 ---
        alert_msg = ""
        
        # MACD 判斷
        macd_val = latest['MACD'].item()
        sig_val = latest['Signal'].item()
        prev_macd = prev['MACD'].item()
        prev_sig = prev['Signal'].item()
        
        macd_status = "✅ 多頭" if macd_val > sig_val else "⚠️ 空頭"
        if prev_macd > prev_sig and macd_val < sig_val:
            alert_msg += "📉 [警告] MACD 死叉成形！\n"
        elif prev_macd < prev_sig and macd_val > sig_val:
            alert_msg += "🚀 [訊號] MACD 金叉！\n"

        # 布林通道判斷
        bb_upper = latest['BB_Upper'].item()
        bb_lower = latest['BB_Lower'].item()
        ma20 = latest['SMA_20'].item()
        
        if close < bb_lower:
            alert_msg += "🚨 [快出場] 跌破布林下軌防守線！\n"
        elif close < ma20:
            alert_msg += "⚠️ [注意] 跌破月線 (20MA)！\n"
        
        if not alert_msg:
            alert_msg = "👌 狀態穩定\n"
            
        # 顯示排版
        msg += f"✅ **{code}**\n"
        msg += f"   市價: `{round(close, 2)}` | 成本: `{cost}` | 報酬: `{profit}%`\n"
        msg += f"   策略: `{strat}`\n"
        msg += f"   MACD: `{macd_status}` | 月線: `{round(ma20, 2)}`\n"
        msg += f"   👉 {alert_msg}\n"
        
    return msg

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.command()
async def 健檢(ctx):
    msg = await ctx.send("⏳ 正在撈取最新報價與計算技術指標...")
    try:
        result = await asyncio.wait_for(asyncio.to_thread(run_health_check), timeout=30.0)
        await msg.edit(content=result)
    except asyncio.TimeoutError:
        await msg.edit(content="⚠️ 運算逾時，請稍後再試。")

@bot.command()
async def 新增(ctx, code: str, price: float, strat_num: str):
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
        await ctx.send(f"⚠️ 找不到代號 {code}。")

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
