def run_health_check():
    portfolio = load_data()
    if not portfolio: return "⚠️ 資料庫為空。"
    
    msg = "📊 **【雲端即時戰報】**\n--------------------\n"
    for code, info in portfolio.items():
        # 強制測試兩套規則
        tickers = [f"{code}.TW", f"{code}.TWO", f"{code}"]
        df = pd.DataFrame()
        
        for t in tickers:
            df = yf.download(t, period="1mo", progress=False)
            if not df.empty: 
                break
            
        if df.empty:
            msg += f"❌ **{code}**: 抓取失敗 (Yahoo 查無此號)\n"
            continue
            
        latest_price = df['Close'].iloc[-1].item()
        buy_price = info.get('buy_price', 0)
        profit = round(((latest_price - buy_price) / buy_price) * 100, 2)
        
        msg += f"✅ **{code}** | 市價: `{round(latest_price, 2)}` | 報酬: `{profit}%`\n"
        msg += f"   └ 策略: {info.get('strategy', '無')}\n\n"
    return msg
