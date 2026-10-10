"""
Demoläge: ersätter yfinance med påhittad data så att hela kedjan (hämtning, poäng, rapport)
kan testas utan internet. Siffrorna är fiktiva även om bolagsnamnen är riktiga.
"""
import sys
import types
import zlib
import random

import numpy as np
import pandas as pd

N_DAYS = 330


def _series(ticker):
    """Påhittad kursserie. Var femte aktie får en 'vändnings-form' (fall, botten, första studs)."""
    seed = zlib.crc32(ticker.encode())
    rnd = np.random.default_rng(seed)
    kind = seed % 5
    drift = rnd.normal(0.0002, 0.0004)
    r = rnd.normal(drift, 0.014, N_DAYS)
    if kind == 0:                       # fall senaste ~7 månaderna, botten för en vecka sedan, liten studs
        r[:60] += 0.0004
        r[60:N_DAYS - 7] -= 0.0042
        r[N_DAYS - 7:N_DAYS - 2] += 0.006
        r[N_DAYS - 2:] += 0.004
    elif kind == 1:                     # stadig stigare
        r += 0.0012
    price = 50 * np.exp(np.cumsum(r)) * (1 + (seed % 17))
    close = price
    high = close * (1 + np.abs(rnd.normal(0, 0.008, N_DAYS)))
    low = close * (1 - np.abs(rnd.normal(0, 0.008, N_DAYS)))
    open_ = np.r_[close[0], close[:-1]]
    vol = rnd.uniform(0.6, 1.4, N_DAYS) * (200_000 + (seed % 9) * 400_000)
    up = np.r_[False, np.diff(close) > 0]
    vol = np.where(up & (np.arange(N_DAYS) > N_DAYS - 25), vol * 1.7, vol)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol})


def _index():
    return pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=N_DAYS + 5)[-N_DAYS:]


def download(tickers, start=None, end=None, auto_adjust=True, progress=False,
             group_by="column", threads=True, timeout=30, **_):
    tl = tickers.split() if isinstance(tickers, str) else list(tickers)
    idx = _index()
    frames = {}
    for t in tl:
        if t.startswith("SAND") or t.startswith("ERIC"):   # låtsas att några tickers saknas
            continue
        f = _series(t)
        f.index = idx
        frames[t] = f
    if not frames:
        return pd.DataFrame()
    if len(tl) == 1:
        return frames[tl[0]]
    return pd.concat(frames, axis=1)   # MultiIndex (ticker, fält), som yfinance med group_by='ticker'


def fake_info(ticker):
    rnd = random.Random(zlib.crc32(ticker.encode()))
    pick = lambda a, b, miss=0.08: None if rnd.random() < miss else rnd.uniform(a, b)
    sectors = ["Technology", "Industrials", "Financial Services", "Healthcare", "Consumer Cyclical",
               "Consumer Defensive", "Basic Materials", "Energy", "Real Estate", "Utilities", "Communication Services"]
    eps = rnd.uniform(-2, 25)
    price = _series(ticker)["Close"].iloc[-1]
    if rnd.random() < 0.05:
        return {}
    return {
        "longName": ticker + " (demo)", "sector": rnd.choice(sectors), "industry": "Demo", "country": "Sweden",
        "currency": "SEK", "financialCurrency": "SEK", "currentPrice": price, "marketCap": rnd.uniform(2e9, 3e11),
        "trailingEps": eps, "forwardEps": eps * rnd.uniform(0.8, 1.6), "trailingPE": pick(6, 45), "forwardPE": pick(6, 35),
        "priceToBook": pick(0.5, 7), "enterpriseToEbitda": pick(4, 25), "ebitda": rnd.uniform(-1e8, 5e10),
        "trailingPegRatio": pick(0.4, 3.5, 0.3), "freeCashflow": rnd.uniform(-2e9, 2.5e10),
        "targetMeanPrice": price * rnd.uniform(0.85, 1.5), "numberOfAnalystOpinions": rnd.randint(1, 15),
        "recommendationMean": pick(1.5, 3.8), "returnOnEquity": pick(-0.05, 0.3), "operatingMargins": pick(-0.05, 0.3),
        "profitMargins": pick(-0.05, 0.22), "debtToEquity": pick(10, 300), "currentRatio": pick(0.6, 2.8),
        "totalDebt": rnd.uniform(1e8, 4e10), "totalCash": rnd.uniform(1e7, 1e10), "revenueGrowth": pick(-0.1, 0.25),
        "earningsGrowth": pick(-0.3, 0.4), "dividendRate": rnd.choice([0, 0, price * rnd.uniform(0.01, 0.06)]),
        "longBusinessSummary": "Fiktivt bolag i demoläge. Siffrorna är påhittade.",
    }


class _Ticker:
    def __init__(self, t):
        self.t = t

    @property
    def info(self):
        return fake_info(self.t)


def install():
    mod = types.ModuleType("yfinance")
    mod.download = download
    mod.Ticker = _Ticker
    sys.modules["yfinance"] = mod
