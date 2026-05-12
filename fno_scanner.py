"""
FNO Options Scanner — Fyers API v3
====================================
Scans FNO stocks from a CSV, resolves exact option symbols from
Fyers' live NSE_FO.csv master, fetches 5-min historical data,
computes straddle VWAP, and generates an interactive HTML dashboard.

Symbol resolution is done entirely from the official master file:
  https://public.fyers.in/sym_details/NSE_FO.csv
No symbols are constructed by hand — this guarantees correctness.

Usage:
  pip install requests pandas
  python fno_scanner.py
"""

import io
import csv
import json
import time
import datetime
import requests
import pandas as pd
from typing import Optional
from fyers_apiv3 import fyersModel

import os

# Config from environment variables (set as GitHub Secrets or local exports)
# .strip() guards against accidental newlines/spaces when pasting into GitHub Secrets
FYERS_CLIENT_ID      = os.environ.get("FYERS_CLIENT_ID",    "YOUR_APP_ID-100").strip()
_raw_token           = os.environ.get("FYERS_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN").strip()
# Normalise token: some users store "APPID-100:eyJ..." (the combined string);
# others store just the raw JWT "eyJ...".  Strip the "APPID:" prefix if present
# so the header is always built as  client_id + ":" + raw_jwt.
if ":" in _raw_token and not _raw_token.startswith("eyJ"):
    FYERS_ACCESS_TOKEN = _raw_token.split(":", 1)[1].strip()
else:
    FYERS_ACCESS_TOKEN = _raw_token
CSV_FILE             = os.environ.get("CSV_FILE",           "fno_stocks.csv").strip()
TIMEFRAME            = int(os.environ.get("TIMEFRAME",      "5").strip())
LOOKBACK_HOURS       = int(os.environ.get("LOOKBACK_HOURS", "48").strip())
MIN_VOLUME_THRESHOLD = int(os.environ.get("MIN_VOLUME",     "10").strip())

# Fyers public symbol master (updated daily by Fyers, no auth needed)
NSE_FO_URL  = "https://public.fyers.in/sym_details/NSE_FO.csv"

# SDK instance — initialised in main() after credential validation
_fyers: fyersModel.FyersModel = None  # type: ignore
_debug_call_count: int = 0  # how many history() calls have been made


def init_sdk() -> fyersModel.FyersModel:
    """Initialise the Fyers SDK with the access token."""
    return fyersModel.FyersModel(
        client_id  = FYERS_CLIENT_ID,
        token      = FYERS_ACCESS_TOKEN,
        is_async   = False,
        log_path   = "",          # suppress SDK log files
    )

# NSE_FO.csv column indices (0-based, confirmed from live file May 2026)
COL_SYMBOL    = 9   # Full Fyers symbol  e.g. NSE:BHARATFORG26MAY200CE
COL_EXPIRY_TS = 8   # Expiry unix timestamp
COL_UNDERLY   = 13  # Underlying name    e.g. BHARATFORG
COL_STRIKE    = 15  # Strike price       e.g. 200.0
COL_OPT_TYPE  = 16  # CE or PE
COL_LOT_SIZE  = 2   # Lot size


# (auth header no longer needed — SDK handles auth internally)


# ── Symbol master ─────────────────────────────────────────────────────────────

def load_fo_master() -> pd.DataFrame:
    """
    Download NSE_FO.csv from Fyers' public URL.
    Returns a DataFrame. Fyers updates this file daily before market open.
    """
    print("  Downloading NSE_FO.csv symbol master ...", end=" ", flush=True)
    try:
        r = requests.get(NSE_FO_URL, timeout=20)
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Could not download NSE_FO.csv: {e}")
    df = pd.read_csv(io.StringIO(r.text), header=None)
    print(f"OK  ({len(df):,} rows)")
    return df


def get_nearest_expiry(fo_df: pd.DataFrame, underlying: str) -> Optional[int]:
    """
    Return the nearest upcoming expiry timestamp for the given underlying.
    For stock options this is always the current monthly expiry.
    """
    now_ts = int(time.time())
    sub = fo_df[
        (fo_df[COL_UNDERLY]  == underlying) &
        (fo_df[COL_OPT_TYPE].isin(["CE", "PE"]))
    ].copy()
    sub[COL_EXPIRY_TS] = pd.to_numeric(sub[COL_EXPIRY_TS], errors="coerce")
    future = sub[sub[COL_EXPIRY_TS] >= now_ts]
    if future.empty:
        return None
    return int(future[COL_EXPIRY_TS].min())


def get_strike_increment_from_master(fo_df: pd.DataFrame,
                                     underlying: str,
                                     expiry_ts: int) -> Optional[float]:
    """
    Infer the strike increment by looking at sorted unique strikes
    in the master for this underlying + expiry.
    """
    sub = fo_df[
        (fo_df[COL_UNDERLY]   == underlying) &
        (fo_df[COL_EXPIRY_TS] == expiry_ts)
    ].copy()
    sub[COL_STRIKE] = pd.to_numeric(sub[COL_STRIKE], errors="coerce")
    strikes = sorted(sub[COL_STRIKE].dropna().unique())
    if len(strikes) < 2:
        return None
    diffs = [round(strikes[i+1] - strikes[i], 2) for i in range(len(strikes)-1)]
    return max(set(diffs), key=diffs.count)


def get_option_symbols(fo_df: pd.DataFrame,
                       underlying: str,
                       strikes: list[float],
                       expiry_ts: int) -> dict[tuple, str]:
    """
    Return a dict:  (strike, 'CE') -> 'NSE:BHARATFORG26MAY200CE'
    Matched on underlying + expiry timestamp + strike + option type.
    """
    sub = fo_df[
        (fo_df[COL_UNDERLY]   == underlying) &
        (fo_df[COL_EXPIRY_TS] == expiry_ts)
    ].copy()
    sub[COL_STRIKE] = pd.to_numeric(sub[COL_STRIKE], errors="coerce")

    result = {}
    for strike in strikes:
        for opt in ("CE", "PE"):
            row = sub[
                (sub[COL_STRIKE]   == float(strike)) &
                (sub[COL_OPT_TYPE] == opt)
            ]
            if not row.empty:
                result[(strike, opt)] = row.iloc[0][COL_SYMBOL]
    return result


# ── Fyers v3 historical data ──────────────────────────────────────────────────

def get_historical(symbol: str, from_ts: int, to_ts: int,
                   resolution: int = 5) -> list[dict]:
    """
    Fetch OHLCV candles using the official fyers-apiv3 SDK.
    The SDK handles the correct endpoint, method and auth header internally.
    Returns list of {ts, open, high, low, close, volume} or [].
    """
    global _fyers
    data = {
        "symbol":      symbol,
        "resolution":  str(resolution),
        "date_format": "0",       # unix timestamps in response
        "range_from":  str(from_ts),
        "range_to":    str(to_ts),
        "cont_flag":   "1",
    }
    try:
        resp = _fyers.history(data=data)
        # Always print full response for the first call to aid debugging;
        # after DEBUG_CALLS calls, only print on failure.
        global _debug_call_count
        _debug_call_count += 1
        if _debug_call_count <= 2 or resp.get("s") != "ok":
            # Truncate candles array to keep logs readable
            dbg = {k: (v[:2] if k == "candles" and isinstance(v, list) else v)
                   for k, v in resp.items()}
            print(f"    [DBG] {symbol}: {dbg}")
        if resp.get("s") == "ok" and resp.get("candles"):
            return [
                {"ts": c[0], "open": c[1], "high": c[2],
                 "low": c[3], "close": c[4], "volume": c[5]}
                for c in resp["candles"]
            ]
    except Exception as e:
        print(f"    [ERR] {symbol}: {e}")
    return []


def rate_sleep(sec: float = 0.35):
    time.sleep(sec)


# ── VWAP & liquidity ──────────────────────────────────────────────────────────

def running_vwap(candles: list[dict]) -> list[float]:
    """Cumulative running VWAP aligned 1:1 with candles list."""
    cum_pv = cum_vol = 0.0
    out = []
    for c in candles:
        tp       = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv  += tp * c["volume"]
        cum_vol += c["volume"]
        out.append(cum_pv / cum_vol if cum_vol else c["close"])
    return out


def scalar_vwap(candles: list[dict]) -> float:
    """Final VWAP value for the whole period."""
    v = running_vwap(candles)
    return v[-1] if v else 0.0


def is_liquid(candles: list[dict]) -> bool:
    return sum(c["volume"] for c in candles) >= MIN_VOLUME_THRESHOLD


# ── CSV loader ────────────────────────────────────────────────────────────────

def load_stocks(filepath: str) -> list[dict]:
    stocks = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            stocks.append({k: v.strip() for k, v in row.items()})
    return stocks


# ── Straddle helpers ──────────────────────────────────────────────────────────

def merge_straddle(ce: list[dict], pe: list[dict]) -> list[dict]:
    """
    Inner-join CE and PE candles on timestamp, summing OHLCV.
    Falls back to whichever leg has data if the other is empty.
    """
    if not ce and not pe:
        return []
    if not ce:
        return pe
    if not pe:
        return ce
    pe_map = {c["ts"]: c for c in pe}
    merged = [
        {
            "ts":     c["ts"],
            "open":   c["open"]   + pe_map[c["ts"]]["open"],
            "high":   c["high"]   + pe_map[c["ts"]]["high"],
            "low":    c["low"]    + pe_map[c["ts"]]["low"],
            "close":  c["close"]  + pe_map[c["ts"]]["close"],
            "volume": c["volume"] + pe_map[c["ts"]]["volume"],
        }
        for c in ce if c["ts"] in pe_map
    ]
    return merged if merged else ce  # fallback: CE-only if no timestamp match


# ── Per-stock scan ────────────────────────────────────────────────────────────

def scan_stock(stock: dict,
               fo_df: pd.DataFrame,
               from_ts: int, to_ts: int) -> Optional[dict]:

    sym        = stock["symbol"]       # NSE:BHARATFORG-EQ
    underlying = stock["underlying"]   # BHARATFORG
    name       = stock["name"]
    csv_lot    = int(stock["lot_size"])
    csv_inc    = float(stock["increment"])

    print(f"\n{'─'*62}")
    print(f"  {name}  ({underlying})")

    # 1. Underlying price candles
    candles = get_historical(sym, from_ts, to_ts, TIMEFRAME)
    rate_sleep()
    if not candles:
        print(f"  [SKIP] No price data for {sym}")
        return None

    ltp = candles[-1]["close"]

    # 2. Nearest expiry from master
    expiry_ts = get_nearest_expiry(fo_df, underlying)
    if expiry_ts is None:
        print(f"  [SKIP] '{underlying}' not found in NSE_FO.csv")
        return None
    expiry_dt = datetime.datetime.fromtimestamp(expiry_ts).strftime("%d %b %Y")

    # 3. Strike increment: prefer master-derived, fall back to CSV value
    master_inc = get_strike_increment_from_master(fo_df, underlying, expiry_ts)
    increment  = master_inc if master_inc else csv_inc

    # 4. ATM and ±4 strikes
    atm     = round(round(ltp / increment) * increment, 2)
    strikes = [round(atm + i * increment, 2) for i in range(-4, 5)]

    print(f"  LTP={ltp:.2f}  ATM={atm:.0f}  Inc={increment}  Expiry={expiry_dt}")

    # 5. Resolve exact Fyers symbols from master (no guessing)
    sym_map = get_option_symbols(fo_df, underlying, strikes, expiry_ts)
    found   = sum(1 for s in strikes if (s, "CE") in sym_map)
    print(f"  Symbols resolved: {found}/9 strikes in master")

    # 6. Fetch option candles for each strike
    strike_data = {}
    for strike in strikes:
        ce_sym = sym_map.get((strike, "CE"))
        pe_sym = sym_map.get((strike, "PE"))

        ce_candles = get_historical(ce_sym, from_ts, to_ts, TIMEFRAME) if ce_sym else []
        rate_sleep()
        pe_candles = get_historical(pe_sym, from_ts, to_ts, TIMEFRAME) if pe_sym else []
        rate_sleep()

        ce_liq = is_liquid(ce_candles)
        pe_liq = is_liquid(pe_candles)

        ce_ltp = ce_candles[-1]["close"] if ce_candles else 0.0
        pe_ltp = pe_candles[-1]["close"] if pe_candles else 0.0

        straddle_candles = merge_straddle(ce_candles, pe_candles)
        straddle_liq     = is_liquid(straddle_candles)

        straddle_ltp   = (ce_ltp + pe_ltp) if (ce_liq or pe_liq) else 0.0
        straddle_vwap  = scalar_vwap(straddle_candles) if straddle_liq else 0.0
        straddle_rvwap = running_vwap(straddle_candles) if straddle_liq else []

        above_vwap: Optional[bool] = None
        if straddle_liq and straddle_vwap:
            above_vwap = straddle_ltp > straddle_vwap

        pos    = "ATM" if strike == atm else ("Below" if strike < atm else "Above")
        status = "liquid" if straddle_liq else "illiquid"
        print(f"    {strike:>9.2f} {pos:<6}  CE={ce_ltp:>7.2f}  PE={pe_ltp:>7.2f}"
              f"  Straddle={straddle_ltp:>8.2f}  VWAP={straddle_vwap:>8.2f}  [{status}]")

        strike_data[str(strike)] = {
            "strike":           strike,
            "is_atm":           strike == atm,
            "pos":              pos,
            "ce_symbol":        ce_sym or "N/A",
            "pe_symbol":        pe_sym or "N/A",
            "ce_ltp":           ce_ltp,
            "pe_ltp":           pe_ltp,
            "straddle_price":   straddle_ltp,
            "straddle_vwap":    straddle_vwap,
            "ce_liquid":        ce_liq,
            "pe_liquid":        pe_liq,
            "straddle_liquid":  straddle_liq,
            "above_vwap":       above_vwap,
            "straddle_candles": straddle_candles,
            "straddle_rvwap":   straddle_rvwap,
        }

    # 7. Summary counts (ATM excluded from above/below buckets)
    def _cnt(side_fn, vwap_fn):
        return sum(
            1 for s in strikes
            if s != atm
            and strike_data[str(s)]["straddle_liquid"]
            and strike_data[str(s)]["above_vwap"] is not None
            and side_fn(s)
            and vwap_fn(strike_data[str(s)]["above_vwap"])
        )

    summary = {
        "below_above_vwap": _cnt(lambda s: s < atm, lambda v:  v),
        "below_below_vwap": _cnt(lambda s: s < atm, lambda v: not v),
        "above_above_vwap": _cnt(lambda s: s > atm, lambda v:  v),
        "above_below_vwap": _cnt(lambda s: s > atm, lambda v: not v),
    }

    return {
        "symbol":             sym,
        "name":               name,
        "underlying":         underlying,
        "lot_size":           csv_lot,
        "increment":          increment,
        "ltp":                ltp,
        "atm":                str(atm),
        "expiry_date":        expiry_dt,
        "strikes":            [str(s) for s in strikes],
        "strike_data":        strike_data,
        "underlying_candles": candles,
        "summary":            summary,
    }


# ── HTML dashboard ────────────────────────────────────────────────────────────

def generate_dashboard(results: list[dict], out: str = "fno_dashboard.html"):
    js_data = json.dumps(results)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FNO Straddle VWAP Scanner</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Inter:wght@300;400;500;600&display=swap');
:root{{
  --bg:#080d14;--bg2:#0d1520;--bg3:#121e2e;--bg4:#1a2840;
  --border:#1e3150;--border2:#243a5e;
  --accent:#00c8ff;--accent2:#ff6b35;
  --green:#00e676;--red:#ff4545;--yellow:#ffd740;--purple:#b388ff;
  --text:#ddeeff;--muted:#5a7a99;--mono:'JetBrains Mono',monospace;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
html{{scrollbar-width:thin;scrollbar-color:var(--border) transparent}}
body{{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;min-height:100vh}}
.hdr{{position:sticky;top:0;z-index:200;background:rgba(8,13,20,.93);backdrop-filter:blur(16px);
  border-bottom:1px solid var(--border);display:flex;align-items:center;
  justify-content:space-between;padding:14px 28px;}}
.hdr-logo{{font-family:var(--mono);font-size:.95rem;font-weight:700;color:var(--accent);letter-spacing:3px;text-transform:uppercase}}
.hdr-sub{{font-family:var(--mono);font-size:.62rem;color:var(--muted);letter-spacing:1.5px;margin-top:2px}}
.hdr-time{{font-family:var(--mono);font-size:.65rem;color:var(--yellow)}}
.tabs{{background:var(--bg2);border-bottom:1px solid var(--border);display:flex;
  overflow-x:auto;padding:0 16px;scrollbar-width:thin;scrollbar-color:var(--border) transparent}}
.tab{{padding:11px 20px;background:none;border:none;border-bottom:2px solid transparent;
  color:var(--muted);font-family:var(--mono);font-size:.7rem;font-weight:600;cursor:pointer;
  white-space:nowrap;letter-spacing:1px;text-transform:uppercase;transition:all .15s}}
.tab:hover{{color:var(--text)}}.tab.on{{color:var(--accent);border-bottom-color:var(--accent);background:rgba(0,200,255,.04)}}
.panel{{display:none;padding:24px 28px;animation:fadeIn .2s ease}}.panel.on{{display:block}}
@keyframes fadeIn{{from{{opacity:0;transform:translateY(4px)}}to{{opacity:1}}}}
.stats{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}}
.stat{{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px 18px;min-width:130px}}
.stat-l{{font-size:.6rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}}
.stat-v{{font-family:var(--mono);font-size:1.4rem;font-weight:700;color:var(--accent)}}
.sec{{font-family:var(--mono);font-size:.7rem;color:var(--accent);letter-spacing:2px;
  text-transform:uppercase;padding-bottom:8px;border-bottom:1px solid var(--border);margin-bottom:16px}}
.cards{{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));margin-bottom:28px}}
.card{{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:18px;
  cursor:pointer;transition:all .18s;position:relative;overflow:hidden}}
.card::after{{content:'';position:absolute;inset:0;border-radius:10px;
  box-shadow:inset 0 0 0 1px var(--accent);opacity:0;transition:opacity .18s}}
.card:hover{{transform:translateY(-2px);background:var(--bg3)}}.card:hover::after{{opacity:1}}
.card-top{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px}}
.card-name{{font-weight:600;font-size:.92rem}}.card-sym{{font-family:var(--mono);font-size:.6rem;color:var(--muted);margin-top:2px}}
.card-ltp{{font-family:var(--mono);font-size:1rem;font-weight:700;color:var(--yellow);text-align:right}}
.card-atm{{font-family:var(--mono);font-size:.65rem;color:var(--purple);margin-top:2px;text-align:right}}
.card-exp{{font-family:var(--mono);font-size:.6rem;color:var(--muted);text-align:right;margin-top:2px}}
.quad{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.qbox{{background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:9px 11px}}
.ql{{font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}}
.qv{{font-family:var(--mono);font-size:1.15rem;font-weight:700}}.qv.g{{color:var(--green)}}.qv.r{{color:var(--red)}}
.detail-hdr{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px;align-items:center}}
.pill{{padding:4px 12px;border-radius:20px;font-family:var(--mono);font-size:.65rem;
  border:1px solid var(--border);background:var(--bg3);color:var(--muted)}}
.pill.atm{{border-color:var(--purple);color:var(--purple);background:rgba(179,136,255,.1)}}
.pill.exp{{border-color:var(--yellow);color:var(--yellow);background:rgba(255,215,64,.07)}}
.chart-row{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}}
@media(max-width:860px){{.chart-row{{grid-template-columns:1fr}}}}
.cbox{{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:18px}}
.cbox-title{{font-family:var(--mono);font-size:.65rem;color:var(--muted);
  text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px}}
.tbl{{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:.78rem;margin-bottom:28px}}
.tbl th{{padding:8px 12px;background:var(--bg3);color:var(--muted);font-size:.62rem;
  letter-spacing:.8px;text-transform:uppercase;border-bottom:1px solid var(--border);text-align:right}}
.tbl th:first-child{{text-align:left}}
.tbl td{{padding:9px 12px;border-bottom:1px solid rgba(30,50,80,.5);text-align:right}}
.tbl td:first-child{{text-align:left}}
.tbl tr:hover td{{background:rgba(0,200,255,.03)}}
.tbl tr.atm-row td{{background:rgba(179,136,255,.07);color:var(--purple);font-weight:600}}
.tbl tr.atm-row td:first-child::before{{content:'\\25C6 ';color:var(--purple)}}
.badge{{padding:2px 8px;border-radius:4px;font-size:.62rem;font-weight:700;letter-spacing:.5px}}
.badge.above{{background:rgba(0,230,118,.12);color:var(--green)}}
.badge.below{{background:rgba(255,69,69,.12);color:var(--red)}}
.badge.illiq{{background:rgba(90,122,153,.12);color:var(--muted)}}
.sgrid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:14px;margin-bottom:28px}}
@media(max-width:500px){{.sgrid{{grid-template-columns:1fr}}}}
.no-data{{text-align:center;padding:32px;color:var(--muted);font-size:.75rem}}
.sym-small{{font-size:.58rem;color:var(--muted);display:block;margin-top:2px;word-break:break-all}}
</style>
</head>
<body>
<div class="hdr">
  <div>
    <div class="hdr-logo">&#x2B21; FNO Straddle Scanner</div>
    <div class="hdr-sub">VWAP Analysis &middot; 5 Min &middot; Last 12 Hours &middot; NSE Stock Options</div>
  </div>
  <div class="hdr-time" id="scanTime"></div>
</div>
<div class="tabs" id="tabBar">
  <button class="tab on" id="tab-summary" onclick="show('summary')">&#x25C8; Summary</button>
</div>
<div id="panels">
  <div class="panel on" id="panel-summary">
    <div class="stats" id="ovStats"></div>
    <div class="sec">All Stocks &mdash; click a card to open detail view</div>
    <div class="cards" id="cardGrid"></div>
  </div>
</div>
<script>
const DATA = {js_data};
const rendered = {{}};

document.getElementById('scanTime').textContent =
  'Scanned ' + new Date().toLocaleString('en-IN',{{timeZone:'Asia/Kolkata',
  hour:'2-digit',minute:'2-digit',day:'2-digit',month:'short'}});

function show(id){{
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('on'));
  document.getElementById('panel-'+id)?.classList.add('on');
  document.getElementById('tab-'+id)?.classList.add('on');
  if(id.startsWith('s')&&id!=='summary'){{
    const idx=parseInt(id.slice(1));
    if(!rendered[idx]){{rendered[idx]=true;drawStock(idx);}}
  }}
}}

const f=(n,d=2)=>(n||n===0)?Number(n).toFixed(d):'--';
const fmtTS=ts=>new Date(ts*1000).toLocaleTimeString('en-IN',{{hour:'2-digit',minute:'2-digit'}});

// Overview stats
(()=>{{
  let liq=0,illiq=0;
  DATA.forEach(r=>r.strikes.forEach(s=>{{
    r.strike_data[s].straddle_liquid?liq++:illiq++;
  }}));
  document.getElementById('ovStats').innerHTML=`
    <div class="stat"><div class="stat-l">Stocks</div><div class="stat-v">${{DATA.length}}</div></div>
    <div class="stat"><div class="stat-l">Total Strikes</div><div class="stat-v">${{liq+illiq}}</div></div>
    <div class="stat"><div class="stat-l">Liquid</div><div class="stat-v" style="color:var(--green)">${{liq}}</div></div>
    <div class="stat"><div class="stat-l">Illiquid</div><div class="stat-v" style="color:var(--red)">${{illiq}}</div></div>`;
}})();

// Build cards + tab shells
DATA.forEach((r,i)=>{{
  const s=r.summary;
  const card=document.createElement('div');
  card.className='card'; card.onclick=()=>show('s'+i);
  card.innerHTML=`
    <div class="card-top">
      <div><div class="card-name">${{r.name}}</div><div class="card-sym">${{r.symbol}}</div></div>
      <div>
        <div class="card-ltp">&#x20B9;${{f(r.ltp)}}</div>
        <div class="card-atm">ATM ${{r.atm}}</div>
        <div class="card-exp">${{r.expiry_date}}</div>
      </div>
    </div>
    <div class="quad">
      <div class="qbox"><div class="ql">Below ATM &uarr; VWAP</div><div class="qv g">${{s.below_above_vwap}}</div></div>
      <div class="qbox"><div class="ql">Below ATM &darr; VWAP</div><div class="qv r">${{s.below_below_vwap}}</div></div>
      <div class="qbox"><div class="ql">Above ATM &uarr; VWAP</div><div class="qv g">${{s.above_above_vwap}}</div></div>
      <div class="qbox"><div class="ql">Above ATM &darr; VWAP</div><div class="qv r">${{s.above_below_vwap}}</div></div>
    </div>`;
  document.getElementById('cardGrid').appendChild(card);

  const btn=document.createElement('button');
  btn.className='tab'; btn.id='tab-s'+i;
  btn.textContent=r.underlying; btn.onclick=()=>show('s'+i);
  document.getElementById('tabBar').appendChild(btn);

  const panel=document.createElement('div');
  panel.className='panel'; panel.id='panel-s'+i;
  panel.innerHTML=buildShell(r,i);
  document.getElementById('panels').appendChild(panel);
}});

function buildShell(r,i){{
  const rows=r.strikes.map(sk=>{{
    const sd=r.strike_data[sk];
    const badge=!sd.straddle_liquid
      ?'<span class="badge illiq">ILLIQUID</span>'
      :sd.above_vwap
        ?'<span class="badge above">&#x25B2; ABOVE</span>'
        :'<span class="badge below">&#x25BC; BELOW</span>';
    const pos=sd.is_atm?'ATM':(parseFloat(sk)<parseFloat(r.atm)?'&darr; Below':'&uarr; Above');
    const ceSymHtml=sd.ce_symbol&&sd.ce_symbol!='N/A'?`<span class="sym-small">${{sd.ce_symbol}}</span>`:'';
    return `<tr class="${{sd.is_atm?'atm-row':''}}">
      <td>${{f(sk,0)}} <span style="font-size:.6rem;color:var(--muted)">${{pos}}</span></td>
      <td>${{sd.ce_liquid?f(sd.ce_ltp):'<span style="color:var(--muted)">--</span>'}}</td>
      <td>${{sd.pe_liquid?f(sd.pe_ltp):'<span style="color:var(--muted)">--</span>'}}</td>
      <td>${{sd.straddle_liquid?f(sd.straddle_price):'<span style="color:var(--muted)">--</span>'}}</td>
      <td>${{sd.straddle_liquid?f(sd.straddle_vwap):'<span style="color:var(--muted)">--</span>'}}</td>
      <td>${{badge}}</td>
      <td style="font-size:.6rem;color:var(--muted);text-align:left">
        ${{sd.ce_symbol!='N/A'?sd.ce_symbol:''}}
        ${{sd.pe_symbol!='N/A'?' / '+sd.pe_symbol:''}}
      </td></tr>`;
  }}).join('');
  return `
  <div class="detail-hdr">
    <span class="pill">${{r.name}}</span>
    <span class="pill">LTP &#x20B9;${{f(r.ltp)}}</span>
    <span class="pill atm">ATM ${{r.atm}}</span>
    <span class="pill exp">Expiry ${{r.expiry_date}}</span>
    <span class="pill">Inc ${{r.increment}}</span>
    <span class="pill">Lot ${{r.lot_size}}</span>
  </div>
  <div class="chart-row">
    <div class="cbox"><div class="cbox-title">Underlying Price &mdash; 5 Min</div><canvas id="pc${{i}}" height="200"></canvas></div>
    <div class="cbox"><div class="cbox-title">ATM Straddle vs VWAP</div><canvas id="ac${{i}}" height="200"></canvas></div>
  </div>
  <div class="sec">Strike Table &mdash; sorted ascending</div>
  <table class="tbl">
    <thead><tr>
      <th>Strike</th><th>CE LTP</th><th>PE LTP</th>
      <th>Straddle</th><th>VWAP</th><th>vs VWAP</th><th style="text-align:left">Symbols Used</th>
    </tr></thead>
    <tbody>${{rows}}</tbody>
  </table>
  <div class="sec">Straddle Charts vs VWAP &mdash; all strikes ascending</div>
  <div class="sgrid" id="sg${{i}}"></div>`;
}}

const COPTS={{
  responsive:true,
  plugins:{{
    legend:{{labels:{{color:'#5a7a99',font:{{family:'JetBrains Mono',size:10}},boxWidth:12}}}},
    tooltip:{{mode:'index',intersect:false,
      backgroundColor:'rgba(13,21,32,.95)',titleColor:'#ddeeff',
      bodyColor:'#8aaabb',borderColor:'#1e3150',borderWidth:1}}
  }},
  scales:{{
    x:{{ticks:{{color:'#5a7a99',maxTicksLimit:8,font:{{family:'JetBrains Mono',size:9}}}},
        grid:{{color:'rgba(30,50,80,.4)'}}}},
    y:{{ticks:{{color:'#5a7a99',font:{{family:'JetBrains Mono',size:9}}}},
        grid:{{color:'rgba(30,50,80,.4)'}}}}
  }}
}};
function mkChart(ctx,labels,datasets){{
  return new Chart(ctx,{{type:'line',data:{{labels,datasets}},
    options:JSON.parse(JSON.stringify(COPTS))}});
}}

function drawStock(i){{
  const r=DATA[i];
  const uc=r.underlying_candles;
  if(uc?.length){{
    mkChart(document.getElementById('pc'+i),uc.map(c=>fmtTS(c.ts)),[
      {{label:r.underlying,data:uc.map(c=>c.close),
        borderColor:'#00c8ff',backgroundColor:'rgba(0,200,255,.06)',
        borderWidth:1.5,pointRadius:0,fill:true,tension:.3}}]);
  }}
  const atmSD=r.strike_data[r.atm];
  if(atmSD?.straddle_candles?.length){{
    mkChart(document.getElementById('ac'+i),atmSD.straddle_candles.map(c=>fmtTS(c.ts)),[
      {{label:'Straddle',data:atmSD.straddle_candles.map(c=>c.close),
        borderColor:'#b388ff',backgroundColor:'rgba(179,136,255,.07)',
        borderWidth:1.5,pointRadius:0,fill:true,tension:.3}},
      {{label:'VWAP',data:atmSD.straddle_rvwap,
        borderColor:'#ffd740',borderWidth:1.5,borderDash:[5,3],
        pointRadius:0,fill:false,tension:0}}]);
  }}
  const grid=document.getElementById('sg'+i);
  const sorted=[...r.strikes].sort((a,b)=>parseFloat(a)-parseFloat(b));
  sorted.forEach((sk,si)=>{{
    const sd=r.strike_data[sk];
    const box=document.createElement('div'); box.className='cbox';
    const atmMark=sd.is_atm?' <span style="color:var(--purple)">&#x25C6; ATM</span>':'';
    const cid='sc_'+i+'_'+si;
    box.innerHTML=`<div class="cbox-title">Strike ${{f(sk,0)}}${{atmMark}}</div>`+
      (sd.straddle_liquid&&sd.straddle_candles?.length
        ?`<canvas id="${{cid}}" height="160"></canvas>`
        :`<div class="no-data">Illiquid / No Data</div>`);
    grid.appendChild(box);
    if(sd.straddle_liquid&&sd.straddle_candles?.length){{
      requestAnimationFrame(()=>{{
        mkChart(document.getElementById(cid),
          sd.straddle_candles.map(c=>fmtTS(c.ts)),[
          {{label:'Straddle',data:sd.straddle_candles.map(c=>c.close),
            borderColor:sd.is_atm?'#b388ff':'#00c8ff',
            backgroundColor:sd.is_atm?'rgba(179,136,255,.07)':'rgba(0,200,255,.05)',
            borderWidth:1.5,pointRadius:0,fill:true,tension:.3}},
          {{label:'VWAP',data:sd.straddle_rvwap,
            borderColor:'#ffd740',borderWidth:1.5,borderDash:[5,3],
            pointRadius:0,fill:false,tension:0}}]);
      }});
    }}
  }});
}}
</script>
</body>
</html>"""

    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n  Dashboard saved: {out}")
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now     = datetime.datetime.now()
    to_ts   = int(time.time())
    from_ts = to_ts - LOOKBACK_HOURS * 3600

    print(f"\n{'='*62}")
    print(f"  FNO Straddle VWAP Scanner")
    print(f"  Time      : {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Lookback  : {LOOKBACK_HOURS}h  ({TIMEFRAME}-min candles)")
    print(f"  Liquidity : min volume >= {MIN_VOLUME_THRESHOLD}")
    print(f"{'='*62}")

    # Validate credentials with clear diagnostics
    client_id_ok = FYERS_CLIENT_ID and "YOUR_" not in FYERS_CLIENT_ID
    token_ok     = FYERS_ACCESS_TOKEN and "YOUR_" not in FYERS_ACCESS_TOKEN

    print(f"  Client ID : {'SET (' + FYERS_CLIENT_ID[:8] + '...)' if client_id_ok else 'MISSING — set FYERS_CLIENT_ID secret'}")
    print(f"  Token     : {'SET (length=' + str(len(FYERS_ACCESS_TOKEN)) + ')' if token_ok else 'MISSING — set FYERS_ACCESS_TOKEN secret'}")
    print(f"  Token type: {'JWT (eyJ...)' if FYERS_ACCESS_TOKEN.startswith('eyJ') else 'UNEXPECTED FORMAT — should start with eyJ'}")
    print(f"  Auth hdr  : {FYERS_CLIENT_ID}:<token>  (requests library will send this)")
    print()

    if not client_id_ok or not token_ok:
        print("  [ERROR] One or more credentials are missing or placeholder.")
        print("  Set FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN as GitHub Secrets.")
        raise SystemExit(1)

    # Initialise Fyers SDK (after credential check)
    global _fyers
    _fyers = init_sdk()
    print("  SDK       : fyers-apiv3 initialised")
    print()

    # Download symbol master once (shared across all stocks)
    fo_df = load_fo_master()

    # Load stock list
    stocks = load_stocks(CSV_FILE)
    print(f"  Stocks    : {len(stocks)} loaded from {CSV_FILE}\n")

    results = []
    for stock in stocks:
        result = scan_stock(stock, fo_df, from_ts, to_ts)
        if result:
            results.append(result)

    if not results:
        print("\n  [ERROR] No results obtained. "
              "Check credentials, CSV underlyings, and that the market has traded today.")
        return

    generate_dashboard(results, "fno_dashboard.html")

    print(f"\n{'='*62}")
    print(f"  Scanned   : {len(results)}/{len(stocks)} stocks successfully")
    print(f"  Output    : fno_dashboard.html  (open in any browser)")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    main()
