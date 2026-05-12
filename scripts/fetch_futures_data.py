import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import yaml
from datetime import datetime, timedelta
from fyers_apiv3 import fyersModel
from scripts.utils import get_fno_symbols, find_future_symbol

def load_config():
    with open('config.yaml', 'r') as file:
        return yaml.safe_load(file)

def setup_fyers_session():
    """Initialize Fyers API session"""
    config = load_config()
    
    app_id = config['fyers']['app_id']
    access_token = config['fyers']['access_token']
    
    # Initialize Fyers Model with your app_id and access_token
    fyers = fyersModel.FyersModel(client_id=app_id, is_async=False, token=access_token)
    return fyers

def fetch_historical_data(fyers, symbol, resolution, start_date, end_date):
    """Fetch historical data for a symbol"""
    # Convert date format for Fyers API
    from_datetime = datetime.strptime(start_date, "%Y-%m-%d")
    to_datetime = datetime.strptime(end_date, "%Y-%m-%d")
    
    data = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": start_date,
        "range_to": end_date,
        "cont_flag": "1"
    }
    
    try:
        response = fyers.history(data=data)
        
        if response['s'] == 'ok':
            df = pd.DataFrame(response['candles'], 
                             columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
            return df
        else:
            print(f"Error fetching data for {symbol}: {response}")
            return None
    except Exception as e:
        print(f"Exception while fetching data: {e}")
        return None

def main():
    # Configuration
    stock_name = os.getenv('STOCK_SYMBOL', "RELIANCE")
    resolution = "5"  # 5 minutes
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    
    print(f"Fetching data for {stock_name} from {start_date} to {end_date}")
    
    # Get F&O symbols from public URL
    print("Downloading F&O symbol master...")
    df_fno = get_fno_symbols()
    print(f"Found {len(df_fno)} F&O symbols")
    
    # Find future symbol
    future_symbol = find_future_symbol(stock_name, df_fno)
    
    if not future_symbol:
        print(f"Future symbol not found for {stock_name}")
        print("Available symbols for", stock_name)
        matching = df_fno[df_fno['symbol'].str.contains(stock_name, case=False)]
        print(matching[['symbol', 'segment']].head())
        sys.exit(1)
    
    print(f"Found future symbol: {future_symbol}")
    
    # Setup Fyers session
    fyers = setup_fyers_session()
    
    # Verify connection
    profile = fyers.get_profile()
    if profile['s'] == 'ok':
        print(f"Connected as: {profile.get('name', 'Unknown')}")
    else:
        print(f"Connection failed: {profile}")
        sys.exit(1)
    
    # Fetch historical data
    df_historical = fetch_historical_data(fyers, future_symbol, resolution, start_date, end_date)
    
    if df_historical is not None and not df_historical.empty:
        # Save data
        os.makedirs("data", exist_ok=True)
        filename = f"data/{stock_name}_FUT_{resolution}min_{start_date}_to_{end_date}.csv"
        df_historical.to_csv(filename, index=False)
        print(f"✅ Data saved to {filename}")
        
        # Print summary
        print(f"\n📊 Summary for {stock_name} Future:")
        print(f"Total periods: {len(df_historical)}")
        print(f"Date range: {df_historical['timestamp'].min()} to {df_historical['timestamp'].max()}")
        print(f"Price range: {df_historical['low'].min():.2f} - {df_historical['high'].max():.2f}")
        print("\n📈 Last 5 candles:")
        print(df_historical.tail(5).to_string())
    else:
        print("❌ No data retrieved")
        sys.exit(1)

if __name__ == "__main__":
    main()
