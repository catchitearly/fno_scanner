import pandas as pd
import requests
from io import StringIO
from datetime import datetime, timedelta

def get_fno_symbols():
    """Fetch the latest F&O symbol master from Fyers"""
    NSE_FO_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
    
    response = requests.get(NSE_FO_URL)
    response.raise_for_status()
    
    df = pd.read_csv(StringIO(response.text))
    return df

def find_future_symbol(symbol_name, df_fno):
    """Find the future symbol for a given stock"""
    future_symbols = df_fno[
        (df_fno['symbol'].str.contains(symbol_name, case=False)) & 
        (df_fno['segment'] == 'NSE-FUT')
    ]
    
    if not future_symbols.empty:
        return future_symbols.iloc[0]['symbol']
    return None

def get_option_chain_symbols(symbol_name, df_fno):
    """Get all option symbols for a given stock"""
    option_symbols = df_fno[
        (df_fno['symbol'].str.contains(symbol_name, case=False)) & 
        (df_fno['segment'].str.contains('OPT', case=False))
    ]
    
    return option_symbols['symbol'].tolist()
