import pandas as pd
import requests
from io import StringIO

def get_fno_symbols():
    """Fetch the latest F&O symbol master from Fyers"""
    NSE_FO_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
    
    try:
        response = requests.get(NSE_FO_URL, timeout=30)
        response.raise_for_status()
        
        df = pd.read_csv(StringIO(response.text))
        print(f"✅ Loaded {len(df)} F&O symbols")
        return df
    except Exception as e:
        print(f"❌ Failed to fetch F&O symbols: {e}")
        # Return empty DataFrame as fallback
        return pd.DataFrame()

def find_future_symbol(symbol_name, df_fno):
    """Find the future symbol for a given stock"""
    if df_fno.empty:
        return None
    
    future_symbols = df_fno[
        (df_fno['symbol'].str.contains(symbol_name, case=False, na=False)) & 
        (df_fno['segment'] == 'NSE-FUT')
    ]
    
    if not future_symbols.empty:
        symbol = future_symbols.iloc[0]['symbol']
        return symbol if symbol else None
    return None

def get_option_chain_symbols(symbol_name, df_fno):
    """Get all option symbols for a given stock"""
    if df_fno.empty:
        return []
    
    option_symbols = df_fno[
        (df_fno['symbol'].str.contains(symbol_name, case=False, na=False)) & 
        (df_fno['segment'].str.contains('OPT', case=False, na=False))
    ]
    
    return option_symbols['symbol'].tolist()
