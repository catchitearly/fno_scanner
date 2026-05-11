# FNO Options Scanner — Straddle VWAP Dashboard

Scans NSE F&O stocks, resolves exact option symbols from Fyers' **live symbol
master** (NSE_FO.csv), fetches 5-min historical data, computes straddle VWAP,
and generates an interactive HTML dashboard.

---

## Files

| File | Purpose |
|------|---------|
| `fno_scanner.py` | Main scanner script |
| `fno_stocks.csv` | Input stock list (tab-separated) |
| `requirements.txt` | Python dependencies |
| `fno_dashboard.html` | Generated output (open in browser) |

---

## Quick Start

### 1. Install dependencies
```bash
pip install requests pandas
```

### 2. Get a Fyers access token

```bash
pip install fyers-apiv3
```

```python
from fyers_apiv3 import fyersModel

session = fyersModel.SessionModel(
    client_id     = "YOUR_APP_ID-100",
    secret_key    = "YOUR_SECRET_KEY",
    redirect_uri  = "https://trade.fyers.in/api-login/redirect-uri/index.html",
    response_type = "code",
    grant_type    = "authorization_code"
)
print(session.generate_authcode())   # Open this URL in browser and log in
# After redirect, copy the auth_code= value from the URL bar

session.set_token("AUTH_CODE_FROM_URL")
resp = session.generate_token()
print(resp["access_token"])          # Paste this into fno_scanner.py
```

Access tokens expire daily at midnight. Re-run the above each trading day.

### 3. Configure fno_scanner.py

Edit the two lines at the very top of the script:

```python
FYERS_CLIENT_ID    = "AB1234XY-100"
FYERS_ACCESS_TOKEN = "eyJ0eXAiOiJKV1..."
```

### 4. Run

```bash
python fno_scanner.py
```

Open `fno_dashboard.html` in any browser. No server required.

---

## How Symbol Resolution Works

The old approach of constructing symbols by hand was error-prone. This script
instead downloads Fyers' own master file at runtime:

    https://public.fyers.in/sym_details/NSE_FO.csv

This file is updated daily by Fyers before market open. It contains every
valid option symbol with its exact expiry timestamp, strike, and type (CE/PE).

The script:
1. Downloads NSE_FO.csv once at startup (no auth needed, ~1 second)
2. Filters by the `underlying` name from your CSV
3. Picks the nearest upcoming expiry automatically
4. Looks up the exact symbol for each of the 9 strikes (e.g. `NSE:BHARATFORG26MAY200CE`)
5. Uses those symbols for all API calls — no guessing, no hardcoded dates

---

## CSV Format

Tab-separated, with a header row:

```
symbol             name          underlying   lot_size  increment  exchange
NSE:BHARATFORG-EQ  Bharat Forge  BHARATFORG   200       5          NSE
NSE:POLYCAB-EQ     Polycab       POLYCAB       125       5          NSE
```

The `underlying` column must exactly match the name used in NSE_FO.csv.
To verify: look at any option in your Fyers terminal, e.g.
`NSE:BHARATFORG26MAY200CE` -> underlying is `BHARATFORG`.

---

## API Details

| Item | Value |
|------|-------|
| Historical data endpoint | `https://api.fyers.in/data/history` |
| Auth header format | `Authorization: client_id:access_token` |
| date_format param | `0` (unix timestamps) |
| resolution param | `5` (5-minute candles) |

---

## VWAP Calculation

Running cumulative VWAP (resets at the start of the 12h window):

```
typical_price = (high + low + close) / 3
VWAP[i]       = sum(typical * volume)[0..i] / sum(volume)[0..i]
```

Straddle candles are built by inner-joining CE and PE candles on timestamp,
summing all OHLCV fields. VWAP is then calculated on the combined series.

---

## Liquidity Handling

A strike is illiquid if total volume across the 12h window < MIN_VOLUME_THRESHOLD (default 10).

Illiquid strikes show an ILLIQUID badge, -- for all prices, a No Data
placeholder in charts, and are excluded from the VWAP counts in the summary.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| 0/9 symbols resolved | Wrong `underlying` in CSV — check NSE_FO.csv |
| No price data for equity | Wrong `symbol` format — must be `NSE:SYMBOL-EQ` |
| All strikes illiquid | Run during market hours |
| 401 auth error | Regenerate access token (expires daily) |

---

## Scan Time

19 API calls per stock (1 equity + 9 CE + 9 PE) x 0.35s sleep = ~7s per stock.
8 stocks takes roughly 60 seconds.
