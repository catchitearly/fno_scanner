import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fyers_apiv3 import fyersModel
import pandas as pd
import yaml
from datetime import datetime
from scripts.utils import get_fno_symbols
import json

def load_config():
    with open('config.yaml', 'r') as file:
        return yaml.safe_load(file)

def setup_fyers_session():
    """Initialize Fyers API session"""
    config = load_config()
    app_id = config['fyers']['app_id']
    access_token = config['fyers']['access_token']
    
    fyers = fyersModel.FyersModel(client_id=app_id, is_async=False, token=access_token)
    return fyers

def get_option_chain(fyers, symbol, strike_count=10):
    """
    Fetch option chain with strike increments
    
    Args:
        fyers: Fyers API object
        symbol: Underlying symbol (e.g., "NSE:RELIANCE-EQ")
        strike_count: Number of strikes on each side of ATM
    """
    data = {
        "symbol": symbol,
        "strikecount": strike_count
    }
    
    try:
        response = fyers.option_chain(data=data)
        
        if response['s'] == 'ok':
            return response
        else:
            print(f"Error fetching option chain: {response}")
            return None
    except Exception as e:
        print(f"Exception fetching option chain: {e}")
        return None

def calculate_strike_increments(option_chain_data):
    """Calculate and display strike price increments"""
    if not option_chain_data or 'optionsChain' not in option_chain_data:
        return None
    
    strikes = []
    for option in option_chain_data['optionsChain']:
        strikes.append(option['strike'])
    
    strikes = sorted(set(strikes))
    
    if len(strikes) > 1:
        increments = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
        avg_increment = sum(increments) / len(increments)
        
        return {
            'strikes': strikes,
            'increments': increments,
            'avg_increment': avg_increment,
            'min_strike': min(strikes),
            'max_strike': max(strikes),
            'total_strikes': len(strikes)
        }
    
    return None

def main():
    stock_name = os.getenv('STOCK_SYMBOL', "RELIANCE")
    strike_count = 10  # Number of strikes on each side
    
    print(f"🔍 Fetching option chain for {stock_name}")
    
    # Get underlying symbol
    df_fno = get_fno_symbols()
    underlying_symbols = df_fno[
        (df_fno['symbol'].str.contains(stock_name, case=False)) & 
        (df_fno['segment'] == 'NSE-EQ')
    ]
    
    if underlying_symbols.empty:
        print(f"❌ Underlying symbol not found for {stock_name}")
        sys.exit(1)
    
    underlying_symbol = underlying_symbols.iloc[0]['symbol']
    print(f"📊 Underlying symbol: {underlying_symbol}")
    
    # Setup Fyers session
    fyers = setup_fyers_session()
    
    # Fetch option chain
    option_chain = get_option_chain(fyers, underlying_symbol, strike_count)
    
    if option_chain:
        # Calculate strike increments
        strike_info = calculate_strike_increments(option_chain)
        
        if strike_info:
            print(f"\n✅ Strike Price Analysis for {stock_name}:")
            print(f"Total strikes: {strike_info['total_strikes']}")
            print(f"Strike range: {strike_info['min_strike']} to {strike_info['max_strike']}")
            print(f"Average increment: ₹{strike_info['avg_increment']:.2f}")
            print(f"Strike increments: {strike_info['increments']}")
            
            # Save option chain data
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            os.makedirs("data", exist_ok=True)
            filename = f"data/{stock_name}_optionchain_{timestamp}.json"
            
            with open(filename, 'w') as f:
                json.dump(option_chain, f, indent=2)
            print(f"💾 Option chain saved to {filename}")
        else:
            print("⚠️ Unable to calculate strike increments")
    else:
        print("❌ Failed to fetch option chain")
        sys.exit(1)

if __name__ == "__main__":
    main()
