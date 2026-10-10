"""
Turnaround-delen: använder din turnaround_screener (compute_turnaround) oförändrad,
men behåller bara fasen "Tidig" och sorterar på Score (0-100), inte på vändningsstyrka (0-10).
"""
import numpy as np
import pandas as pd

import turnaround_screener as ts

SPARK_DAYS = 130


def _r(v, d=2):
    try:
        v = float(v)
        return round(v, d) if np.isfinite(v) else None
    except Exception:
        return None


def _spark(tc, datum):
    i = int(np.searchsorted(tc.dates, np.datetime64(datum, "ns"), side="right")) - 1
    a = max(0, i - SPARK_DAYS + 1)
    ser = lambda arr: [_r(x, 2 if x < 100 else 1) for x in arr[a:i + 1]]
    return {
        "c": ser(tc.close_arr), "e9": ser(tc.ema9), "e21": ser(tc.ema21), "e50": ser(tc.ema50),
        "from": str(pd.Timestamp(tc.dates[a]))[:10], "to": str(pd.Timestamp(tc.dates[i]))[:10],
    }


def run_turnaround(ticker_cache, datum, min_vol=None, min_weakness=None):
    """Returnerar (lista med tidiga turnaround-bolag sorterade på Score, antal kandidater i alla faser)."""
    min_vol = ts.MIN_VOL_SEK if min_vol is None else min_vol
    min_weakness = ts.MIN_WEAKNESS_P12M if min_weakness is None else min_weakness
    alla = ts.screen_turnaround(ticker_cache, datum, min_vol, min_weakness)
    tidig = [r for r in alla if "Tidig" in r["Fas"]]
    tidig.sort(key=lambda r: (r["Score"], r["VandStyrka"]), reverse=True)
    for r in tidig:
        r["spark"] = _spark(ticker_cache[r["Ticker"]], datum)
        r["signaler"] = r.pop("_signaler", [])
        r["varningar"] = r.pop("_varningar", [])
        r["Sektor"] = "" if r["Sektor"] in ("okand", "?") else r["Sektor"]
    return tidig, len(alla)
