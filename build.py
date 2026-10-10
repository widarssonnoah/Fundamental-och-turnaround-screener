#!/usr/bin/env python3
"""
Kör fundamental-screenern och turnaround-screenern (tidig fas) i ett svep
och bygger EN HTML-fil med två flikar: docs/index.html

    python build.py                 # riktig körning mot Yahoo Finance
    python build.py --demo          # påhittad data, utan internet (skriver till demo_output/)
    python build.py --max-tickers 80   # snabbtest
"""
import argparse
import datetime as dt
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def parse():
    ap = argparse.ArgumentParser(description="Fundamental och turnaround screen")
    ap.add_argument("--out", help="HTML-fil att skriva (standard: docs/index.html)")
    ap.add_argument("--cache-dir", default=str(ROOT / ".cache"), help="mapp för cachade nyckeltal")
    ap.add_argument("--info-ttl-hours", type=float, default=20, help="hur länge nyckeltal återanvänds")
    ap.add_argument("--workers", type=int, default=4, help="parallella trådar för nyckeltal")
    ap.add_argument("--max-tickers", type=int, help="begränsa antalet tickers (test)")
    ap.add_argument("--include-micro", action="store_true", help="ta med micro caps i fundamental-fliken")
    ap.add_argument("--demo", action="store_true", help="använd påhittad data")
    return ap.parse_args()


def fetch_prices(ms, tickers, datum_str, retry_rounds=2):
    """Kurshistorik i batchar (din fetch_all_history). Saknade tickers prövas om i nya omgångar."""
    all_hist, bench = ms.fetch_all_history(tickers, datum_str, datum_str)
    for r in range(retry_rounds):
        missing = [t for t in tickers if t not in all_hist]
        if not missing:
            break
        print(f"\n[INFO] Omgång {r + 2}: provar {len(missing)} saknade tickers igen")
        time.sleep(15)
        more, b2 = ms.fetch_all_history(missing, datum_str, datum_str)
        all_hist.update(more)
        bench = bench if bench is not None else b2
        if not more:
            break
    return all_hist, bench


def to_raw(ticker, h, info, entry):
    c = h["Close"].squeeze().astype(float)
    v = h["Volume"].squeeze().astype(float).fillna(0.0)
    ok = c.notna()
    c, v = c[ok], v[ok]
    return {"ticker": ticker, "info": info or {}, "list": entry,
            "close": [round(float(x), 4) for x in c], "volume": [float(x) for x in v],
            "dates": [d.strftime("%Y-%m-%d") for d in c.index]}


def run_fundamental(fa, all_hist, entries, a):
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    lst = {e["ticker"]: e for e in entries if a.include_micro or e["cap"].lower() != "micro"}
    have = [t for t in lst if t in all_hist]
    failed = [t for t in lst if t not in all_hist]
    print(f"\n[INFO] Nyckeltal: hämtar för {len(have)} aktier ({a.workers} trådar, cache {a.info_ttl_hours:g} h)")
    infos, done = {}, 0
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(fa.fetch_info, t, cache, a.info_ttl_hours, False): t for t in have}
        for f in as_completed(futs):
            done += 1
            infos[futs[f]] = f.result()
            if done % 100 == 0 or done == len(futs):
                print(f"   nyckeltal {done}/{len(futs)}")
    companies = [fa.evaluate(to_raw(t, all_hist[t], infos.get(t), lst[t])) for t in have]
    fa.apply_sector_relative(companies)
    companies = [fa.finalize(c) for c in companies]
    companies.sort(key=lambda c: -c["total"])
    no_fund = sum(1 for t in have if not infos.get(t))
    if no_fund:
        print(f"[VARNING] {no_fund} aktier saknar nyckeltal och poängsätts bara på teknik")
    return companies, failed, no_fund


def main():
    a = parse()
    if a.demo:
        import demo
        demo.install()
    import pandas as pd
    import master_screener as ms
    import aktiescreener as fa
    from turnaround import run_turnaround
    from report import build_report, write_csvs

    t0 = time.time()
    entries = [dict(ticker=u[0], name=u[1], land=u[2], sector=u[3], cap=u[5])
               for u in ms.UNIVERSE if len(u) >= 6 and str(u[4]).lower() == "aktie"]
    if a.max_tickers:
        entries = entries[:a.max_tickers]
    tickers = [e["ticker"] for e in entries]
    datum = pd.Timestamp.today().normalize()
    datum_str = datum.strftime("%Y-%m-%d")
    print(f"[INFO] {len(tickers)} aktier i listan, datum {datum_str}")

    all_hist, bench = fetch_prices(ms, tickers, datum_str)
    if len(all_hist) < max(5, 0.3 * len(tickers)):
        sys.exit(f"[FEL] Bara {len(all_hist)} av {len(tickers)} tickers gav kursdata. Avbryter så att en tom rapport inte publiceras.")

    # Turnaround (tidig fas), sorterad på Score 0-100
    print("\n[INFO] Beräknar indikatorer och turnaround...")
    cache = ms.precompute_all(all_hist, bench)
    turn, n_screened = run_turnaround(cache, datum)
    print(f"[INFO] Turnaround: {n_screened} kandidater i alla faser, {len(turn)} i tidig fas")

    # Fundamental
    fund, failed, no_fund = run_fundamental(fa, all_hist, entries, a)

    now = dt.datetime.now(ZoneInfo("Europe/Stockholm"))
    payload = {
        "generated": now.strftime("%Y-%m-%d %H:%M"), "demo": a.demo,
        "fund": {"companies": fund, "meta": [dict(key=c.key, name=c.name, cat=c.cat, w=c.weight, why=c.why,
                 rule=c.scorer.rule + (" Hälften av poängen avgörs av hur billigt bolaget är mot sin sektor." if c.key in fa.REL_KEYS else ""))
                 for c in fa.CRITERIA],
                 "categories": [{"name": k, "weight": v} for k, v in fa.CATEGORIES.items()],
                 "minFactor": fa.MIN_COVERAGE_FACTOR, "failed": sorted(failed), "noFund": no_fund},
        "turn": {"companies": turn, "screened": n_screened},
    }
    out = Path(a.out) if a.out else ROOT / ("demo_output" if a.demo else "docs") / "index.html"
    build_report(payload, out)
    write_csvs(out.parent, fund, turn, fa.CATEGORIES)
    print(f"\n[KLAR] {out}  ({len(fund)} fundamental, {len(turn)} turnaround, {time.time() - t0:.0f} s)")
    for n, r in enumerate(turn[:5], 1):
        print(f"  {n}. {r['Score']:>3}/100  {r['Namn'][:32]:<32} {r['Ticker']}")


if __name__ == "__main__":
    main()
