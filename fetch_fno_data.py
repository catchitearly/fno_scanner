"""
Fyers API v3 — FnO Data Fetcher
════════════════════════════════
Fetches for each configured stock:
  1. 5-min OHLCV history  — near-month futures (last 2 trading days)
  2. Option chain snapshot — ATM ± STRIKE_COUNT strikes, all expiries
  3. Straddle history      — 5-min OHLCV for CE + PE of every ATM ± 5
                             strike from the nearest expiry (last 2 days)

Required GitHub Secrets (env vars):
  FYERS_CLIENT_ID      e.g. XYZ123-100
  FYERS_ACCESS_TOKEN   daily token (update manually each trading day)

Usage:
  python fetch_fno_data.py
  python fetch_fno_data.py --stocks RELIANCE INFY TCS
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from fyers_apiv3 import fyersModel

# ─────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / f"fetch_{datetime.now().strftime('%Y%m%d')}.log"),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────
DATA_DIR     = Path(__file__).parent.parent / "data"
FUTURES_DIR  = DATA_DIR / "futures"
OPTCHAIN_DIR = DATA_DIR / "option_chain"
STRADDLE_DIR = DATA_DIR / "straddles"

for _d in (FUTURES_DIR, OPTCHAIN_DIR, STRADDLE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DEFAULT_STOCKS         = ["RELIANCE"]   # override via --stocks CLI arg
RESOLUTION             = "5"            # 5-minute candles
CONT_FLAG              = "1"            # continuous / front-month roll
STRIKE_COUNT           = 5             # ATM ± 5 → 11 strikes total
LOOKBACK_CALENDAR_DAYS = 5             # wide window to capture 2 trading days
API_CALL_DELAY_SEC     = 0.35          # polite gap between history() calls


# ─────────────────────────────────────────────────────────────────
# Symbol helpers
# ─────────────────────────────────────────────────────────────────

def _last_thursday(year: int, month: int):
    """Date of the last Thursday in the given month."""
    if month == 12:
        last_day = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = datetime(year, month + 1, 1) - timedelta(days=1)
    days_behind = (last_day.weekday() - 3) % 7   # weekday 3 = Thursday
    return (last_day - timedelta(days=days_behind)).date()


def _front_month() -> tuple:
    """(yy_str, MMM_str) for the live front-month NSE futures contract."""
    now = datetime.now()
    year, month = now.year, now.month
    if now.date() > _last_thursday(year, month):
        month = month + 1 if month < 12 else 1
        year  = year if month > 1 else year + 1
    ref = datetime(year, month, 1)
    return ref.strftime("%y"), ref.strftime("%b").upper()


def futures_symbol(stock: str) -> str:
    """e.g.  NSE:RELIANCE25MAYFUT"""
    yy, mmm = _front_month()
    return f"NSE:{stock.upper()}{yy}{mmm}FUT"


def eq_symbol(stock: str) -> str:
    """e.g.  NSE:RELIANCE-EQ  (underlying for option chain API)"""
    return f"NSE:{stock.upper()}-EQ"


def option_symbol(stock: str, expiry_str: str, strike, opt_type: str) -> str:
    """
    Build Fyers option symbol from option-chain fields.
    Format: NSE:<STOCK><DDMMMYY><STRIKE><CE|PE>
    e.g.    NSE:RELIANCE29MAY251400CE

    expiry_str is whatever the option chain API returns, e.g. "29-May-2025".
    """
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            dt  = datetime.strptime(expiry_str.strip(), fmt)
            tag = dt.strftime("%d%b%y").upper()   # e.g. 29MAY25
            break
        except ValueError:
            continue
    else:
        log.warning("Cannot parse expiry '%s' — skipping symbol build", expiry_str)
        return ""

    strike_val = int(float(strike)) if float(strike) == int(float(strike)) else strike
    return f"NSE:{stock.upper()}{tag}{strike_val}{opt_type.upper()}"


# ─────────────────────────────────────────────────────────────────
# Fyers client  (uses only CLIENT_ID + ACCESS_TOKEN)
# ─────────────────────────────────────────────────────────────────

def get_fyers_client() -> fyersModel.FyersModel:
    client_id    = os.environ.get("FYERS_CLIENT_ID", "").strip()
    access_token = os.environ.get("FYERS_ACCESS_TOKEN", "").strip()

    if not client_id or not access_token:
        log.error(
            "Missing env var(s). Set FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN "
            "as GitHub Actions secrets."
        )
        sys.exit(1)

    fyers = fyersModel.FyersModel(
        client_id=client_id,
        token=access_token,
        is_async=False,
        log_path=str(LOG_DIR),
    )
    log.info("Fyers client ready | client_id=%s", client_id)
    return fyers


# ─────────────────────────────────────────────────────────────────
# Generic 5-min history fetch
# ─────────────────────────────────────────────────────────────────

def _fetch_history_raw(fyers: fyersModel.FyersModel, symbol: str) -> pd.DataFrame:
    """
    Fetch 5-min OHLCV for `symbol` over the past LOOKBACK_CALENDAR_DAYS.
    Returns a DataFrame trimmed to the last 2 trading days.
    Returns empty DataFrame on any error.
    """
    today      = datetime.now().date()
    range_from = (today - timedelta(days=LOOKBACK_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    range_to   = today.strftime("%Y-%m-%d")

    payload = {
        "symbol":      symbol,
        "resolution":  RESOLUTION,
        "date_format": "1",
        "range_from":  range_from,
        "range_to":    range_to,
        "cont_flag":   CONT_FLAG,
    }

    resp = fyers.history(data=payload)

    if resp.get("s") != "ok":
        log.warning("    history() failed for %s | %s", symbol, resp.get("message", resp))
        return pd.DataFrame()

    candles = resp.get("candles", [])
    if not candles:
        log.warning("    No candles for %s", symbol)
        return pd.DataFrame()

    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["datetime"] = (
        pd.to_datetime(df["timestamp"], unit="s", utc=True)
        .dt.tz_convert("Asia/Kolkata")
    )
    df["date"]   = df["datetime"].dt.date
    df["symbol"] = symbol

    # Keep last 2 trading days only
    trading_days = sorted(df["date"].unique())[-2:]
    return df[df["date"].isin(trading_days)].reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────
# 1. Futures history
# ─────────────────────────────────────────────────────────────────

def fetch_futures_history(fyers: fyersModel.FyersModel, stock: str) -> pd.DataFrame:
    sym = futures_symbol(stock)
    log.info("  [Futures] %s", sym)
    df = _fetch_history_raw(fyers, sym)
    if not df.empty:
        df["stock"] = stock
    log.info("    → %d rows | days=%s", len(df),
             sorted(df["date"].unique()) if not df.empty else "—")
    return df


def save_futures(df: pd.DataFrame, stock: str):
    if df.empty:
        return
    tag  = datetime.now().strftime("%Y%m%d")
    path = FUTURES_DIR / f"{stock}_futures_5min_{tag}.csv"
    df.to_csv(path, index=False)
    df.to_csv(FUTURES_DIR / f"{stock}_futures_5min_latest.csv", index=False)
    log.info("    ✓ Futures → %s  (%d rows)", path.name, len(df))


# ─────────────────────────────────────────────────────────────────
# 2. Option chain snapshot
# ─────────────────────────────────────────────────────────────────

def fetch_option_chain(fyers: fyersModel.FyersModel, stock: str) -> dict:
    sym = eq_symbol(stock)
    log.info("  [OptionChain] %s | strikecount=±%d", sym, STRIKE_COUNT)
    resp = fyers.optionchain(data={
        "symbol":      sym,
        "strikecount": STRIKE_COUNT,
        "timestamp":   "",
    })
    if resp.get("s") != "ok":
        log.error("    optionchain() error: %s", resp)
        return {}
    return resp


def parse_option_chain(response: dict, stock: str) -> pd.DataFrame:
    """Flatten option chain JSON → tidy DataFrame."""
    if not response:
        return pd.DataFrame()

    rows        = []
    option_data = response.get("data", {})

    for expiry_block in option_data.get("expiryData", []):
        expiry = expiry_block.get("expiry", "")
        for opt in expiry_block.get("optionsChain", []):
            strike = opt.get("strikePrice")
            for side in ("CE", "PE"):
                s = opt.get(side) or {}
                rows.append({
                    "stock":        stock,
                    "expiry":       expiry,
                    "strike":       strike,
                    "option_type":  side,
                    "ltp":          s.get("ltp"),
                    "open":         s.get("open_price"),
                    "high":         s.get("high_price"),
                    "low":          s.get("low_price"),
                    "close":        s.get("close_price"),
                    "volume":       s.get("volume"),
                    "oi":           s.get("oi"),
                    "oi_change":    s.get("oiChange"),
                    "bid":          s.get("bid"),
                    "ask":          s.get("ask"),
                    "iv":           s.get("iv"),
                    "delta":        s.get("delta"),
                    "gamma":        s.get("gamma"),
                    "theta":        s.get("theta"),
                    "vega":         s.get("vega"),
                    "fyers_symbol": s.get("symbol", ""),
                    "fetched_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })

    df = pd.DataFrame(rows)
    log.info("    → %d option rows", len(df))
    return df


def save_option_chain(df: pd.DataFrame, raw: dict, stock: str):
    if df.empty:
        return
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    df.to_csv(OPTCHAIN_DIR / f"{stock}_optchain_{tag}.csv", index=False)
    df.to_csv(OPTCHAIN_DIR / f"{stock}_optchain_latest.csv", index=False)
    (OPTCHAIN_DIR / f"{stock}_optchain_{tag}.json").write_text(json.dumps(raw, indent=2))
    (OPTCHAIN_DIR / f"{stock}_optchain_latest.json").write_text(json.dumps(raw, indent=2))
    log.info("    ✓ Option chain → %s_optchain_%s.csv", stock, tag)


# ─────────────────────────────────────────────────────────────────
# 3. Straddle history  (ATM ± 5, nearest expiry, 5-min, last 2 days)
# ─────────────────────────────────────────────────────────────────

def _atm_strikes_from_chain(chain_df: pd.DataFrame) -> tuple:
    """
    Returns (nearest_expiry_str, sorted_list_of_selected_strikes).

    ATM is identified as the strike where |CE_ltp - PE_ltp| is minimum
    in the nearest expiry.  Then we select ATM ± STRIKE_COUNT strikes.
    """
    if chain_df.empty:
        return "", []

    nearest_expiry = sorted(chain_df["expiry"].unique())[0]
    exp_df = chain_df[chain_df["expiry"] == nearest_expiry]

    all_strikes = sorted(exp_df["strike"].unique())

    # Find ATM
    ce = exp_df[exp_df["option_type"] == "CE"][["strike", "ltp"]].rename(columns={"ltp": "ce_ltp"})
    pe = exp_df[exp_df["option_type"] == "PE"][["strike", "ltp"]].rename(columns={"ltp": "pe_ltp"})
    merged = ce.merge(pe, on="strike").dropna()

    if merged.empty:
        atm_idx = len(all_strikes) // 2
    else:
        merged["diff"] = (merged["ce_ltp"] - merged["pe_ltp"]).abs()
        atm_strike     = float(merged.loc[merged["diff"].idxmin(), "strike"])
        # snap to nearest available strike
        atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - atm_strike))

    lo = max(0, atm_idx - STRIKE_COUNT)
    hi = min(len(all_strikes) - 1, atm_idx + STRIKE_COUNT)
    selected = [int(s) for s in all_strikes[lo : hi + 1]]

    log.info(
        "    ATM=%s | expiry=%s | %d strikes selected: %d … %d",
        all_strikes[atm_idx], nearest_expiry, len(selected), selected[0], selected[-1],
    )
    return nearest_expiry, selected


def fetch_straddle_history(
    fyers:    fyersModel.FyersModel,
    stock:    str,
    chain_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Fetch 5-min OHLCV for CE + PE of every ATM ± 5 strike (nearest expiry).
    Appends a `straddle_premium` column = CE_close + PE_close per candle.
    """
    expiry, strikes = _atm_strikes_from_chain(chain_df)
    if not strikes:
        log.warning("  [Straddle] No strikes to fetch.")
        return pd.DataFrame()

    total_calls = len(strikes) * 2
    log.info("  [Straddle] %d strikes × 2 legs = %d API calls", len(strikes), total_calls)

    frames = []
    for strike in strikes:
        for opt_type in ("CE", "PE"):
            sym = option_symbol(stock, expiry, strike, opt_type)
            if not sym:
                continue

            time.sleep(API_CALL_DELAY_SEC)
            df = _fetch_history_raw(fyers, sym)

            if df.empty:
                log.warning("    No data: %s", sym)
                continue

            df["stock"]       = stock
            df["expiry"]      = expiry
            df["strike"]      = strike
            df["option_type"] = opt_type
            frames.append(df)
            log.info("    ✓ %s → %d rows", sym, len(df))

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)

    # Compute straddle_premium (CE_close + PE_close) per (strike, datetime)
    pivot = (
        combined
        .pivot_table(
            index=["stock", "expiry", "strike", "datetime"],
            columns="option_type",
            values="close",
            aggfunc="first",
        )
        .reset_index()
    )
    pivot.columns.name = None
    pivot["straddle_premium"] = (
        pivot.get("CE", pd.Series(dtype=float)).fillna(0)
        + pivot.get("PE", pd.Series(dtype=float)).fillna(0)
    )

    combined = combined.merge(
        pivot[["stock", "expiry", "strike", "datetime", "straddle_premium"]],
        on=["stock", "expiry", "strike", "datetime"],
        how="left",
    )

    log.info("  [Straddle] Total rows: %d", len(combined))
    return combined


def save_straddle(df: pd.DataFrame, stock: str):
    if df.empty:
        return
    tag  = datetime.now().strftime("%Y%m%d")
    path = STRADDLE_DIR / f"{stock}_straddle_5min_{tag}.csv"
    df.to_csv(path, index=False)
    df.to_csv(STRADDLE_DIR / f"{stock}_straddle_5min_latest.csv", index=False)
    log.info("    ✓ Straddle → %s  (%d rows)", path.name, len(df))


# ─────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────

def print_summary(stock, fut_df, chain_df, straddle_df):
    sep = "═" * 62
    print(f"\n{sep}\n  SUMMARY — {stock}\n{sep}")

    if not fut_df.empty:
        r = fut_df.iloc[-1]
        print(f"\n  📈 Futures 5-min | {len(fut_df)} rows")
        print(f"     Symbol : {fut_df['symbol'].iloc[0]}")
        print(f"     Days   : {sorted(fut_df['date'].unique())}")
        print(f"     Last   : {r['datetime']}  close={r['close']}")
    else:
        print("\n  ⚠  Futures — no data")

    if not chain_df.empty:
        print(f"\n  🔗 Option Chain | {len(chain_df)} rows")
        print(f"     Expiries: {sorted(chain_df['expiry'].unique())}")
        print(f"     CE/PE   : {len(chain_df[chain_df['option_type']=='CE'])} / "
              f"{len(chain_df[chain_df['option_type']=='PE'])}")
    else:
        print("\n  ⚠  Option chain — no data")

    if not straddle_df.empty:
        strikes = sorted(straddle_df["strike"].unique())
        print(f"\n  📊 Straddle History 5-min | {len(straddle_df)} rows")
        print(f"     Expiry  : {straddle_df['expiry'].iloc[0]}")
        print(f"     Days    : {sorted(straddle_df['date'].unique())}")
        print(f"     Strikes : {strikes[0]} … {strikes[-1]}  ({len(strikes)} total)")
        sample = straddle_df[straddle_df["straddle_premium"].notna()].tail(1)
        if not sample.empty:
            r = sample.iloc[0]
            print(f"     Last premium: {r['datetime']}  strike={int(r['strike'])}  "
                  f"premium={r['straddle_premium']:.2f}")
    else:
        print("\n  ⚠  Straddle — no data")

    print()


# ─────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fyers FnO fetcher — futures, option chain, straddle history"
    )
    parser.add_argument(
        "--stocks", nargs="+", default=DEFAULT_STOCKS,
        help="NSE tickers without exchange prefix, e.g. --stocks RELIANCE INFY TCS",
    )
    args   = parser.parse_args()
    stocks = [s.upper() for s in args.stocks]

    log.info("═" * 60)
    log.info("FnO fetch started | stocks=%s", stocks)
    log.info("═" * 60)

    fyers = get_fyers_client()

    for stock in stocks:
        log.info("\n▶ %s", stock)

        # 1. Futures history (5-min, last 2 trading days)
        fut_df = fetch_futures_history(fyers, stock)
        save_futures(fut_df, stock)
        time.sleep(API_CALL_DELAY_SEC)

        # 2. Option chain snapshot
        raw_chain = fetch_option_chain(fyers, stock)
        chain_df  = parse_option_chain(raw_chain, stock)
        save_option_chain(chain_df, raw_chain, stock)
        time.sleep(API_CALL_DELAY_SEC)

        # 3. Straddle historical data (CE + PE per ATM ± 5 strike, 5-min, last 2 days)
        straddle_df = fetch_straddle_history(fyers, stock, chain_df)
        save_straddle(straddle_df, stock)

        print_summary(stock, fut_df, chain_df, straddle_df)

    log.info("All done. Data saved under → %s", DATA_DIR)


if __name__ == "__main__":
    main()
