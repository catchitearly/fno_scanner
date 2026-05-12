import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fyers_api import fyersModel
from fyers_api import accessToken
import pandas as pd
import yaml
from datetime import datetime, timedelta
from scripts.utils import get_fno_symbols, find_future_symbol
import json

def load_config():
    with open('config.yaml', 'r') as file:
        return yaml.safe_load(file)

def setup_fyers_session():
    """Initialize Fyers API session"""
    config = load_config()
    
    app_id = config['fyers']['app_id']
    secret_key = config['fyers']['secret_key']
    redirect_uri = config['fyers']['redirect_uri']
    
    session = accessToken.SessionModel(
        client_id=app_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code"
    )
    
    # For GitHub Actions, use stored access token
    if 'access_token' in config['fyers']:
        session.set_token(config['fyers']['access_token'])
        fyers = fyersModel.FyersModel(client_id=app_id, token=config['fyers']['access_token'])
        return fyers
    
    # Manual authentication for local testing
    auth_code = config['fyers']['auth_code']
    session.generate_token(auth_code)
    access_token = session.access_token
    fyers = fyersModel.FyersModel(client_id=app_id, token=access_token)
    return fyers

def fetch_historical_data(fyers, symbol, resolution, start_date, end_date):
    """Fetch historical data for a symbol"""
    data = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": start_date,
        "range_to": end_date,
        "cont_flag": "1"
    }
    
    response = fyers.history(data=data)
    
    if response['s'] == 'ok':
        df = pd.DataFrame(response['candles'], 
                         columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
        return df
    else:
        print(f"Error fetching data for {symbol}: {response}")
        return None

def main():
    # Configuration
    stock_name = "RELIANCE"  # Example stock - make this configurable
    resolution = "5"  # 5 minutes
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    
    print(f"Fetching data for {stock_name} from {start_date} to {end_date}")
    
    # Get F&O symbols
    df_fno = get_fno_symbols()
    
    # Find future symbol
    future_symbol = find_future_symbol(stock_name, df_fno)
    
    if not future_symbol:
        print(f"Future symbol not found for {stock_name}")
        sys.exit(1)
    
    print(f"Found future symbol: {future_symbol}")
    
    # Setup Fyers session
    fyers = setup_fyers_session()
    
    # Fetch historical data
    df_historical = fetch_historical_data(fyers, future_symbol, resolution, start_date, end_date)
    
    if df_historical is not None and not df_historical.empty:
        # Save data
        filename = f"data/{stock_name}_FUT_{resolution}min_{start_date}_to_{end_date}.csv"
        os.makedirs("data", exist_ok=True)
        df_historical.to_csv(filename, index=False)
        print(f"Data saved to {filename}")
        
        # Print summary
        print(f"\nSummary for {stock_name} Future:")
        print(f"Total periods: {len(df_historical)}")
        print(f"Date range: {df_historical['timestamp'].min()} to {df_historical['timestamp'].max()}")
        print("\nLast 5 candles:")
        print(df_historical.tail(5))
    else:
        print("No data retrieved")

if __name__ == "__main__":
    main()
