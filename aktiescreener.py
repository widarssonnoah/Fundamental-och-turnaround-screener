#!/usr/bin/env python3
"""
Aktiescreener – poängbaserad (fundamental + lite teknisk analys)
================================================================
Hämtar data från Yahoo Finance (via paketet `yfinance`), ger varje bolag
0–100 poäng och skriver en interaktiv HTML-rapport.

Ingen hård filtrering: varje kriterium ger poäng mellan 0 och 100 med linjär
interpolation. Är kravet helt uppfyllt får bolaget 100 p, halvvägs till kravet
50 p osv. Ett bolag försvinner aldrig bara för att ett krav missas.

Körning:
    pip install yfinance
    python aktiescreener.py                      # använder avanzaEuUs.py om den ligger bredvid
    python aktiescreener.py --countries SE US    # bara Sverige och USA ur listan
    python aktiescreener.py --caps mid large mega
    python aktiescreener.py --list annan_lista.py
    python aktiescreener.py --universe usa       # inbyggd USA-lista
    python aktiescreener.py --tickers VOLV-B.ST AAPL
    python aktiescreener.py --file mina_tickers.txt   # en ticker per rad
    python aktiescreener.py --demo               # fejkdata, för att förhandsgranska

Resultat: Fundaktiscreen_Resultat/fundaktiscreen_<datum>.html (+ .csv). Öppnas automatiskt i webbläsaren.
Inte finansiell rådgivning – verktyget sorterar data, det ger inga köprekommendationer.
"""
import argparse
import csv
import datetime as dt
import json
import math
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# ───────────────────────────── INSTÄLLNINGAR ──────────────────────────────
# Viktning mellan kategorierna (justeras även live i HTML-rapporten).
# Fokus: lågt värderade bolag som just börjat vända uppåt, inte stabila "alltid-stigare".
CATEGORIES = {
    "Värdering": 35,
    "Vändning": 30,
    "Katalysatorer": 15,
    "Kvalitet": 10,
    "Finansiell styrka": 5,
    "Tillväxt": 5,
}
# Värderingsmått som delvis bedöms mot bolagets sektor (så banker jämförs med banker).
REL_KEYS = ("pe", "fpe", "pb", "ev", "peg", "fcf")
REL_SHARE = 0.5        # andel av värderingspoängen som kommer från placering mot sektorn
REL_MIN_PEERS = 8      # minsta antal bolag i sektorn för att jämförelsen ska användas
MIN_COVERAGE_FACTOR = 0.75  # bolag med dålig datatäckning får max 75–100 % av poängen

# Alla resultat och all cachad data läggs i en egen mapp bredvid skriptet,
# oavsett varifrån du kör det, så att inget hamnar bland dina andra filer.
BASE_DIR = Path(__file__).resolve().parent / "Fundaktiscreen_Resultat"

OMX = """VOLV-B.ST ERIC-B.ST HM-B.ST ATCO-A.ST ATCO-B.ST INVE-B.ST SEB-A.ST SHB-A.ST SWED-A.ST
ABB.ST ALFA.ST ASSA-B.ST AZN.ST BOL.ST ELUX-B.ST EVO.ST EQT.ST ESSITY-B.ST GETI-B.ST HEXA-B.ST
HUSQ-B.ST KINV-B.ST NIBE-B.ST SAND.ST SBB-B.ST SCA-B.ST SKA-B.ST SKF-B.ST SINCH.ST SWEC-B.ST
TEL2-B.ST TELIA.ST TREL-B.ST LIFCO-B.ST LATO-B.ST LUND-B.ST SAAB-B.ST SECU-B.ST SSAB-B.ST
HOLM-B.ST INDT.ST BALD-B.ST CAST.ST FABG.ST WALL-B.ST WIHL.ST AXFO.ST BILL.ST BETS-B.ST
CLAS-B.ST DOM.ST ELAN-B.ST EPRO-B.ST HPOL-B.ST LOOMIS.ST MTG-B.ST NCC-B.ST PEAB-B.ST SAGA-B.ST
THULE.ST AAK.ST ADDT-B.ST AFRY.ST ALIV-SDB.ST AMBEA.ST ARJO-B.ST BEIJ-B.ST BURE.ST CATE.ST
DIOS.ST FING-B.ST HEBA-B.ST HMS.ST HUFV-A.ST INSTAL.ST INTRUM.ST LAGR-B.ST MIPS.ST MYCR.ST
NOLA-B.ST NYF.ST OEM-B.ST PNDX-B.ST RATO-B.ST SAVE.ST SECT-B.ST SOBI.ST STE-R.ST SYSR.ST
TIETOS.ST TROAX.ST VITR.ST VNV.ST""".split()

USA = """AAPL MSFT GOOGL AMZN META NVDA INTC CSCO IBM ORCL JPM BAC WFC C GS MS PFE MRK JNJ BMY ABBV
CVS UNH KO PEP PG WMT TGT COST HD LOW NKE XOM CVX COP F GM T VZ CMCSA DIS MMM CAT DE BA GE
HON UPS FDX MO PM KHC GIS CLX KMB DOW LYB NUE MPC VLO""".split()

SEKTOR_SV = {
    "Technology": "Teknik", "Industrials": "Industri", "Financial Services": "Finans",
    "Healthcare": "Hälsovård", "Consumer Cyclical": "Sällanköpsvaror",
    "Consumer Defensive": "Dagligvaror", "Basic Materials": "Material", "Energy": "Energi",
    "Real Estate": "Fastigheter", "Communication Services": "Kommunikation",
    "Utilities": "Kraftförsörjning",
}

INFO_KEYS = [
    "longName", "shortName", "sector", "industry", "country", "currency", "financialCurrency",
    "currentPrice", "marketCap", "longBusinessSummary", "website", "trailingPE", "forwardPE",
    "trailingEps", "forwardEps", "priceToBook", "enterpriseToEbitda", "ebitda", "trailingPegRatio",
    "pegRatio", "freeCashflow", "targetMeanPrice", "numberOfAnalystOpinions", "recommendationMean",
    "returnOnEquity", "operatingMargins", "profitMargins", "debtToEquity", "currentRatio",
    "totalDebt", "totalCash", "revenueGrowth", "earningsGrowth", "dividendRate",
]

BANK = ("Financial Services",)
BANK_RE = ("Financial Services", "Real Estate")
LOSS = 999.0  # markör för "negativt/otillgängligt p.g.a. förlust"


# ───────────────────────────── HJÄLPFUNKTIONER ────────────────────────────
def num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def g(info, key):
    return num(info.get(key))


def sv(x, d=1):
    return f"{x:.{d}f}".replace(".", ",")


def pct(x, d=0):
    return sv(x * 100, d) + " %"


def lin(zero, full, fmt):
    """Linjär poäng: `full` ger 100 %, `zero` ger 0 %, däremellan proportionellt."""
    def s(v):
        return max(0.0, min(1.0, (v - zero) / (full - zero)))
    s.rule = f"100 p vid {fmt(full)} eller bättre, 0 p vid {fmt(zero)} eller sämre, däremellan proportionellt"
    return s


def band(z1, f1, f2, z2, fmt):
    """Trapets: full poäng i intervallet f1–f2, noll utanför z1–z2."""
    def s(v):
        if v < f1:
            return max(0.0, min(1.0, (v - z1) / (f1 - z1)))
        if v > f2:
            return max(0.0, min(1.0, (z2 - v) / (z2 - f2)))
        return 1.0
    s.rule = f"100 p mellan {fmt(f1)} och {fmt(f2)}, 0 p under {fmt(z1)} eller över {fmt(z2)}, däremellan proportionellt"
    return s


# ───────────────────────────── TEKNISK ANALYS ─────────────────────────────
def sma_at(x, n, i):
    return sum(x[i - n + 1:i + 1]) / n if i >= n - 1 else None


def ema(x, n):
    k, out = 2 / (n + 1), [x[0]]
    for v in x[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(x, n=14):
    if len(x) < n + 1:
        return None
    d = [x[i] - x[i - 1] for i in range(1, len(x))]
    gains, losses = [max(v, 0) for v in d], [max(-v, 0) for v in d]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(d)):
        ag, al = (ag * (n - 1) + gains[i]) / n, (al * (n - 1) + losses[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def macd_hist(x):
    m = [a - b for a, b in zip(ema(x, 12), ema(x, 26))]
    return [a - b for a, b in zip(m, ema(m, 9))]


def tech(close, vol):
    if len(close) < 60:
        return {}
    p, yr = close[-1], close[-252:]
    hi, lo = max(yr), min(yr)
    s50, s200 = sma_at(close, 50, len(close) - 1), sma_at(close, 200, len(close) - 1)
    v20 = sum(vol[-20:]) / 20 if len(vol) >= 20 else None
    v90 = sum(vol[-90:]) / 90 if len(vol) >= 90 else None
    h = macd_hist(close)
    e9, e21 = ema(close, 9), ema(close, 21)
    d = [a - b for a, b in zip(e9, e21)]
    k = 0
    while k < len(d) and d[-1 - k] > 0:
        k += 1
    s50_10 = sma_at(close, 50, len(close) - 11)
    ups = sum(v for i, v in enumerate(vol[-30:]) if i and close[-30:][i] > close[-30:][i - 1])
    downs = sum(v for i, v in enumerate(vol[-30:]) if i and close[-30:][i] < close[-30:][i - 1])
    low90 = min(close[-90:])
    return {
        "ema": (d[-1] / e21[-1], k if d[-1] > 0 and k < len(d) else None),
        "ret_12m": close[-1] / close[-253] - 1 if len(close) >= 253 else None,
        "rebound": p / low90 - 1,
        "sma50_slope": s50 / s50_10 - 1 if s50 and s50_10 else None,
        "updown": ups / downs if downs > 0 else None,
        "price": p, "high": hi, "low": lo, "rsi": rsi(close),
        "dist_high": 1 - p / hi if hi else None,
        "sma50_gap": p / s50 - 1 if s50 else None,
        "sma200_gap": p / s200 - 1 if s200 else None,
        "vol_ratio": v20 / v90 if v20 is not None and v90 else None,
        "macd": (h[-1], h[-6]) if len(h) > 6 else None,
    }


def spark(close, dates):
    n = min(len(close), 252)
    idx = list(range(len(close) - n, len(close), 3))
    if idx[-1] != len(close) - 1:
        idx.append(len(close) - 1)
    r = lambda v: None if v is None else round(v, 2 if v < 100 else 1)
    return {
        "c": [r(close[i]) for i in idx],
        "s50": [r(sma_at(close, 50, i)) for i in idx],
        "s200": [r(sma_at(close, 200, i)) for i in idx],
        "from": dates[idx[0]] if dates else "", "to": dates[-1] if dates else "",
    }


# ───────────────────────────── KRITERIER ──────────────────────────────────
@dataclass
class Crit:
    cat: str
    key: str
    name: str
    weight: float
    get: Callable
    scorer: Callable
    fmt: Callable
    why: str
    skip: tuple = ()


def mult_fmt(v):
    return "förlust" if v >= LOSS else sv(v)


def _pe(key, eps_key):
    def f(x):
        i = x["i"]
        eps = g(i, eps_key)
        if eps is not None and eps <= 0:
            return LOSS
        v = g(i, key)
        return v if v and v > 0 else None
    return f


def _pb(x):
    v = g(x["i"], "priceToBook")
    return None if v is None else (v if v > 0 else LOSS)


def _ev(x):
    i = x["i"]
    v = g(i, "enterpriseToEbitda")
    if v is not None:
        return v if v > 0 else LOSS
    e = g(i, "ebitda")
    return LOSS if e is not None and e <= 0 else None


def _peg(x):
    v = g(x["i"], "trailingPegRatio")
    v = v if v is not None else g(x["i"], "pegRatio")
    return None if v is None else (v if v > 0 else LOSS)


PENCE = ("GBp", "GBX")


def _cur(c):
    return "GBP" if c in PENCE else c


def _fcf(x):
    i = x["i"]
    if i.get("financialCurrency") and i.get("currency") and _cur(i["financialCurrency"]) != _cur(i["currency"]):
        return None  # valutor skiljer sig, kvoten vore missvisande
    f, m = g(i, "freeCashflow"), g(i, "marketCap")
    return f / m if f is not None and m else None


def _upside(x):
    tp, p, n = g(x["i"], "targetMeanPrice"), x["p"], g(x["i"], "numberOfAnalystOpinions")
    return tp / p - 1 if tp and p and (n is None or n >= 2) else None


def _fwd_growth(x):
    te, fe = g(x["i"], "trailingEps"), g(x["i"], "forwardEps")
    return fe / te - 1 if te and te > 0 and fe is not None else None


def _netdebt(x):
    i = x["i"]
    debt, cash, e = g(i, "totalDebt"), g(i, "totalCash") or 0.0, g(i, "ebitda")
    if debt is None or e is None:
        return None
    nd = debt - cash
    if e <= 0:
        return 99.0 if nd > 0 else 0.0
    return nd / e


def _divy(x):
    r, p = g(x["i"], "dividendRate"), x["p"]
    if x["i"].get("currency") in PENCE:
        return None  # pence/pund-förväxling gör kvoten opålitlig
    return (r / p if r and p else 0.0) if p else None


def _macd_score(v):
    h, h5 = v
    if h > 0:
        return 1.0 if h >= h5 else 0.6
    return 0.5 if h > h5 else 0.0


def _ema_score(v):
    gap, days = v
    if gap > 0:
        if days is not None and days <= 20:
            return 1.0           # färsk uppåtkorsning
        return 0.6 if gap < 0.08 else 0.3
    return 0.4 if gap > -0.02 else 0.0   # nära korsning underifrån


def _ema_fmt(v):
    gap, days = v
    if gap > 0:
        return f"färsk uppåtkorsning ({days} d)" if days is not None and days <= 20 else "över EMA 21"
    return "nära korsning" if gap > -0.02 else "under EMA 21"


_ema_score.rule = ("Färsk uppåtkorsning (≤ 20 dagar) = 100 p, etablerad över EMA 21 = 60 p, "
                   "nära korsning underifrån = 40 p, långt under = 0 p")

_macd_score.rule = ("Positiv och stigande = 100 p, positiv men avtagande = 60 p, "
                    "negativ men vändande uppåt = 50 p, negativ och fallande = 0 p")

T = lambda k: (lambda x: x["t"].get(k))
x1 = lambda v: sv(v)

CRITERIA = [
    # ── Värdering
    Crit("Värdering", "pe", "P/E (trailing)", 8, _pe("trailingPE", "trailingEps"),
         lin(30, 12, x1), mult_fmt, "Lågt P/E = du betalar lite per krona vinst."),
    Crit("Värdering", "fpe", "P/E (förväntad)", 6, _pe("forwardPE", "forwardEps"),
         lin(25, 10, x1), mult_fmt, "Värdering mot analytikernas vinstprognos för nästa år."),
    Crit("Värdering", "pb", "P/B", 5, _pb, lin(4, 1, x1), mult_fmt,
         "Pris mot bokfört eget kapital. Nära eller under 1 kan vara en rabatt på tillgångarna."),
    Crit("Värdering", "ev", "EV/EBITDA", 6, _ev, lin(18, 7, x1), mult_fmt,
         "Värdering som tar hänsyn till skulder. Svårjämförd för banker.", BANK),
    Crit("Värdering", "peg", "PEG-tal", 3, _peg, lin(2.5, 1, x1), mult_fmt,
         "P/E satt i relation till vinsttillväxt. Under 1 betyder låg prislapp för tillväxten."),
    Crit("Värdering", "fcf", "Fritt kassaflöde / börsvärde", 7, _fcf, lin(0, 0.08, pct), lambda v: pct(v, 1),
         "Hur mycket kassa bolaget genererar i förhållande till vad det kostar. Svårt att fejka.", BANK),
    # ── Kvalitet
    Crit("Kvalitet", "roe", "Avkastning på eget kapital", 6, lambda x: g(x["i"], "returnOnEquity"),
         lin(0, 0.15, pct), lambda v: pct(v, 1), "Hur effektivt bolaget förräntar ägarnas pengar."),
    Crit("Kvalitet", "opm", "Rörelsemarginal", 5, lambda x: g(x["i"], "operatingMargins"),
         lin(0, 0.15, pct), lambda v: pct(v, 1), "Hög marginal tyder på prissättningsstyrka eller effektiv drift.", BANK),
    Crit("Kvalitet", "pm", "Nettomarginal", 4, lambda x: g(x["i"], "profitMargins"),
         lin(0, 0.10, pct), lambda v: pct(v, 1), "Hur mycket av försäljningen som blir vinst."),
    Crit("Kvalitet", "div", "Direktavkastning", 2, _divy, lin(0, 0.04, pct), lambda v: pct(v, 1),
         "Utdelning är ett kvitto på att vinsterna är verkliga. Litet viktat så tillväxtbolag inte straffas."),
    # ── Finansiell styrka
    Crit("Finansiell styrka", "de", "Skuldsättning (skuld/eget kapital)", 4, lambda x: g(x["i"], "debtToEquity"),
         lin(200, 50, lambda v: sv(v, 0) + " %"), lambda v: sv(v, 0) + " %",
         "Lägre skuldsättning ger större tålighet i en svacka.", BANK_RE),
    Crit("Finansiell styrka", "nd", "Nettoskuld / EBITDA", 4, _netdebt, lin(4, 1, x1),
         lambda v: "neg. EBITDA" if v >= 99 else sv(v), "Hur många års vinst före avskrivningar som krävs för att betala nettoskulden.", BANK_RE),
    Crit("Finansiell styrka", "cr", "Balanslikviditet (current ratio)", 3, lambda x: g(x["i"], "currentRatio"),
         lin(0.8, 1.5, x1), x1, "Omsättningstillgångar mot kortfristiga skulder. Över 1,5 är tryggt.", BANK_RE),
    # ── Tillväxt
    Crit("Tillväxt", "rg", "Omsättningstillväxt", 5, lambda x: g(x["i"], "revenueGrowth"),
         lin(-0.05, 0.10, pct), lambda v: pct(v, 1), "Växande försäljning är grunden för växande vinst."),
    Crit("Tillväxt", "eg", "Vinsttillväxt", 5, lambda x: g(x["i"], "earningsGrowth"),
         lin(-0.10, 0.15, pct), lambda v: pct(v, 1), "Stigande vinst är det som i längden driver aktiekursen."),
    # ── Katalysatorer
    Crit("Katalysatorer", "up", "Uppsida mot analytikernas riktkurs", 6, _upside,
         lin(0, 0.30, pct), lambda v: pct(v, 0), "Hur mycket högre analytikerna i snitt tror att kursen borde vara."),
    Crit("Katalysatorer", "rec", "Analytikerrekommendation (1 = köp, 5 = sälj)", 4,
         lambda x: g(x["i"], "recommendationMean"), lin(3.5, 2.0, x1), x1,
         "Konsensus bland analytiker. Låga tal är positivt."),
    Crit("Katalysatorer", "fg", "Förväntad vinstökning (nästa år)", 5, _fwd_growth,
         lin(0, 0.20, pct), lambda v: pct(v, 0), "Prognosen för vinst per aktie jämfört med senaste årets utfall."),
    # ── Vändning (har fallit, börjar vända)
    Crit("Vändning", "dh", "Rabatt mot 52-veckorshögsta", 4, T("dist_high"), band(0, 0.15, 0.45, 0.75, pct),
         lambda v: pct(v, 0), "Bolaget har fallit och har något att hämta in, men är inte i fritt fall."),
    Crit("Vändning", "r12", "Kursutveckling 12 månader", 3, T("ret_12m"), band(-0.70, -0.40, 0.10, 0.60, pct),
         lambda v: ("+" if v >= 0 else "") + pct(v, 0),
         "Missgynnar bolag som redan rusat (och stabila stigare), men även de som kollapsat."),
    Crit("Vändning", "reb", "Studs från 3-månaderslägsta", 4, T("rebound"), band(0.0, 0.08, 0.30, 0.60, pct),
         lambda v: "+" + pct(v, 0), "En första uppgång från botten, utan att loppet redan är kört."),
    Crit("Vändning", "ema", "EMA 9 mot EMA 21", 4, T("ema"), _ema_score, _ema_fmt,
         "Färsk uppåtkorsning (senaste 20 dagarna) är ett klassiskt vändningstecken."),
    Crit("Vändning", "s50", "Kurs mot 50-dagars medelvärde", 3, T("sma50_gap"), band(-0.10, 0.0, 0.08, 0.25, pct),
         lambda v: ("+" if v >= 0 else "") + pct(v, 1), "Kurs över SMA 50 men inte överstretchad antyder att trenden vänt."),
    Crit("Vändning", "slope", "SMA 50 vänder uppåt", 3, T("sma50_slope"), lin(-0.03, 0.02, pct),
         lambda v: ("+" if v >= 0 else "") + pct(v, 1), "Förändring i 50-dagarssnittet de senaste 10 dagarna. Platt eller stigande = nedtrenden har brutits."),
    Crit("Vändning", "macd", "MACD-histogram", 3, T("macd"), _macd_score,
         lambda v: ("positiv" if v[0] > 0 else "negativ") + (", stigande" if v[0] >= v[1] else ", avtagande"),
         "Momentumsignal. Vändning uppåt är ett tidigt tecken."),
    Crit("Vändning", "ud", "Ackumulering (volym uppdagar / nerdagar)", 3, T("updown"), lin(0.7, 1.25, x1),
         lambda v: sv(v, 2) + "×", "Mer volym på uppdagar än nerdagar senaste 30 dagarna tyder på att någon köper."),
    Crit("Vändning", "rsi", "RSI (14 dagar)", 2, T("rsi"), band(20, 35, 60, 80, lambda v: sv(v, 0)),
         lambda v: sv(v, 0), "Belönar bolag som lämnat översåld zon, men inte är överköpta."),
]


# ───────────────────────────── POÄNGBERÄKNING ─────────────────────────────
def blend(cats, weights):
    """Viktad snittpoäng över kategorier, med avdrag för saknad data."""
    num_, den, cn, cd = 0.0, 0.0, 0.0, 0.0
    for k, w in weights.items():
        c = cats.get(k)
        if not c or c["na"] or w <= 0:
            continue
        cd += w
        cn += w * c["c"]
        if c["s"] is not None:
            num_ += w * c["s"]
            den += w
    if not den:
        return 0.0
    cov = cn / cd if cd else 0
    return round(num_ / den * (MIN_COVERAGE_FACTOR + (1 - MIN_COVERAGE_FACTOR) * cov), 1)


def money(v):
    if v is None:
        return "–"
    if v >= 1e9:
        return f"{sv(v / 1e9, 1)} mdr"
    return f"{sv(v / 1e6, 0)} mn"


def evaluate(raw):
    """Steg 1: räknar kriterievärden. Poäng slutförs i finalize() efter sektorjämförelsen."""
    i, t = raw["info"], tech(raw["close"], raw["volume"])
    price = t.get("price") or g(i, "currentPrice")
    lst = raw.get("list", {})
    ctx, sector, rows = {"i": i, "t": t, "p": price}, i.get("sector") or "", []
    for c in CRITERIA:
        row = dict(key=c.key, name=c.name, cat=c.cat, w=c.weight, val="–", ratio=None, rel=None,
                   raw=None, na=sector in c.skip)
        if not row["na"]:
            try:
                v = c.get(ctx)
            except Exception:
                v = None
            if v is not None:
                row["raw"], row["ratio"], row["val"] = v, round(c.scorer(v), 3), c.fmt(v)
        rows.append(row)
    return {
        "ticker": raw["ticker"], "name": i.get("longName") or i.get("shortName") or lst.get("name") or raw["ticker"],
        "sector": SEKTOR_SV.get(sector) or sector or "Okänd", "ysector": sector,
        "land": lst.get("land") or (i.get("country") or "")[:2].upper(), "cap": lst.get("cap") or "",
        "industry": i.get("industry") or "", "country": i.get("country") or "", "currency": i.get("currency") or "",
        "price": round(price, 2) if price else None, "mcap": g(i, "marketCap"), "mcap_txt": money(g(i, "marketCap")),
        "summary": (i.get("longBusinessSummary") or "")[:360], "website": i.get("website") or "",
        "spark": spark(raw["close"], raw.get("dates", [])), "_rows": rows,
    }


def apply_sector_relative(companies):
    """Värderingsmåtten blandas med bolagets placering mot sin sektor: billigast i sektorn = 100 %."""
    groups = {}
    for c in companies:
        if c["ysector"]:
            groups.setdefault(c["ysector"], []).append(c)
    for members in groups.values():
        if len(members) < REL_MIN_PEERS:
            continue
        for k in REL_KEYS:
            higher_better = k == "fcf"
            vals = []
            for c in members:
                r = next(r for r in c["_rows"] if r["key"] == k)
                if not r["na"] and r["raw"] is not None:
                    vals.append((r["raw"], r))
            if len(vals) < REL_MIN_PEERS:
                continue
            n = len(vals)
            for v, r in vals:
                worse = sum(1 for w, _ in vals if (w < v if higher_better else w > v))
                same = sum(1 for w, _ in vals if w == v) - 1
                rel = (worse + same / 2) / (n - 1)
                r["rel"] = round(rel, 2)
                r["ratio"] = round((1 - REL_SHARE) * r["ratio"] + REL_SHARE * rel, 3)


def finalize(c):
    rows = c.pop("_rows")
    cats = {}
    for name in CATEGORIES:
        rs = [r for r in rows if r["cat"] == name and not r["na"]]
        if not rs:
            cats[name] = {"s": None, "c": 0, "na": True}
            continue
        av = [r for r in rs if r["ratio"] is not None]
        wa, wt = sum(r["w"] for r in av), sum(r["w"] for r in rs)
        cats[name] = {"s": round(sum(r["w"] * r["ratio"] for r in av) / wa * 100, 1) if av else None,
                      "c": round(wa / wt, 3), "na": False}
    c["cats"] = cats
    c["cr"] = [[r["ratio"], r["val"], int(r["na"]), r["rel"]] for r in rows]
    c["total"] = blend(cats, CATEGORIES)
    c.pop("ysector", None)
    return c


# ───────────────────────────── TICKERLISTA ────────────────────────────────
def load_list(path):
    """Läser en lista av typen UNIVERSE = [(ticker, namn, land, sektor, typ, cap), ...]."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("tickerlista", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = []
    for e in mod.UNIVERSE:
        e = [str(v).strip() for v in e]
        if len(e) < 6 or e[4].lower() != "aktie":
            continue
        out.append({"ticker": e[0], "name": e[1], "land": e[2], "sector": e[3], "cap": e[5]})
    return out


# ───────────────────────────── DATAHÄMTNING ───────────────────────────────
def _safe(t):
    return t.replace("/", "_").replace("\\", "_")


def _fresh(path, ttl_h, refresh):
    return path.exists() and not refresh and (time.time() - path.stat().st_mtime) < ttl_h * 3600


def _extract(df, t):
    """Plockar ut en tickers kurshistorik ur en batch-nedladdning."""
    import pandas as pd
    try:
        sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
        sub = sub.dropna(subset=["Close"])
        if len(sub) < 60:
            return None
        return {"close": [round(float(v), 4) for v in sub["Close"]],
                "volume": [0.0 if v != v else float(v) for v in sub["Volume"]],
                "dates": [d.strftime("%Y-%m-%d") for d in sub.index]}
    except Exception:
        return None


def fetch_history(tickers, cache, ttl_h, refresh, batch, retry_miss=False):
    """Steg 1: kurshistorik för många tickers per anrop (yf.download), vilket är mycket snabbare än ett anrop per aktie."""
    import yfinance as yf
    out, todo, missing, skipped = {}, [], [], 0
    for t in tickers:
        p = cache / (_safe(t) + ".hist.json")
        if _fresh(p, ttl_h, refresh):
            out[t] = json.loads(p.read_text())
        elif not retry_miss and not refresh and _fresh(p.with_suffix(".miss"), 12, False):
            skipped += 1
        else:
            todo.append(t)
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    print(f"Kurshistorik: {len(out)} färska i cache (hämtas inte igen på {ttl_h:g} h), "
          f"{skipped} hoppas över efter nyligt misslyckande (använd --retry-failed för att prova igen), "
          f"{len(todo)} att hämta i {len(chunks)} batchar om {batch}")
    for n, ch in enumerate(chunks, 1):
        df = None
        for attempt in range(3):
            try:
                df = yf.download(ch, period="2y", auto_adjust=True, group_by="ticker",
                                 threads=True, progress=False)
                break
            except Exception:
                time.sleep(3 * (attempt + 1))
        got = 0
        if df is not None and not df.empty:
            for t in ch:
                rec = _extract(df, t)
                if rec:
                    out[t] = rec
                    (cache / (_safe(t) + ".hist.json")).write_text(json.dumps(rec))
                    got += 1
                else:
                    missing.append(t)
        print(f"  batch {n}/{len(chunks)}: {got}/{len(ch)} med kursdata")
        time.sleep(1)
    if missing:
        # Batchar kan tappa enstaka tickers vid rate limit. Prova dem en och en innan vi ger upp.
        print(f"Andra försök för {len(missing)} tickers som saknades i batcharna…")
        for t in missing:
            rec = None
            for attempt in range(2):
                try:
                    rec = _extract(yf.Ticker(t).history(period="2y", auto_adjust=True), t)
                    if rec:
                        break
                except Exception:
                    pass
                time.sleep(1.5)
            p = cache / (_safe(t) + ".hist.json")
            if rec:
                out[t] = rec
                p.write_text(json.dumps(rec))
            else:
                p.with_suffix(".miss").write_text("x")  # saknas troligen hos Yahoo, hoppa över i 12 h
            time.sleep(0.3)
        print(f"  räddade {sum(1 for t in missing if t in out)} av {len(missing)}")
    return out


def fetch_info(t, cache, ttl_h, refresh):
    """Steg 2: nyckeltal. Yahoo erbjuder inget batch-anrop för detta, så det görs per aktie (parallellt)."""
    p = cache / (_safe(t) + ".info.json")
    if _fresh(p, ttl_h, refresh):
        return json.loads(p.read_text())
    import yfinance as yf
    for attempt in range(3):
        try:
            time.sleep(random.uniform(0.1, 0.4))  # skonsam mot Yahoos rate limit
            info = yf.Ticker(t).info or {}
            if len(info) < 5:
                return {}
            d = {k: info.get(k) for k in INFO_KEYS}
            p.write_text(json.dumps(d))
            return d
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return {}  # cachas inte, så nästa körning försöker igen


def demo_data(n=40):
    rnd = random.Random(7)
    sectors = list(SEKTOR_SV)
    out = []
    for k in range(n):
        sec = rnd.choice(sectors)
        price = rnd.uniform(40, 600)
        close, drift = [price], rnd.uniform(-0.0005, 0.0007)
        for _ in range(520):
            close.append(max(1, close[-1] * (1 + rnd.gauss(drift, 0.017))))
        pick = lambda a, b, miss=0.08: None if rnd.random() < miss else rnd.uniform(a, b)
        eps = rnd.uniform(-2, 25)
        info = {
            "longName": f"Demobolaget {k + 1} AB", "sector": sec, "industry": "Demo", "country": "Sweden",
            "currency": "SEK", "financialCurrency": "SEK", "currentPrice": close[-1],
            "marketCap": rnd.uniform(2e9, 3e11), "trailingEps": eps, "forwardEps": eps * rnd.uniform(0.8, 1.6),
            "trailingPE": pick(6, 45), "forwardPE": pick(6, 35), "priceToBook": pick(0.5, 7),
            "enterpriseToEbitda": pick(4, 25), "ebitda": rnd.uniform(-1e8, 5e10), "trailingPegRatio": pick(0.4, 3.5, 0.3),
            "freeCashflow": rnd.uniform(-2e9, 2.5e10), "targetMeanPrice": close[-1] * rnd.uniform(0.85, 1.5),
            "numberOfAnalystOpinions": rnd.randint(1, 15), "recommendationMean": pick(1.5, 3.8),
            "returnOnEquity": pick(-0.05, 0.3), "operatingMargins": pick(-0.05, 0.3), "profitMargins": pick(-0.05, 0.22),
            "debtToEquity": pick(10, 300), "currentRatio": pick(0.6, 2.8), "totalDebt": rnd.uniform(1e8, 4e10),
            "totalCash": rnd.uniform(1e7, 1e10), "revenueGrowth": pick(-0.1, 0.25), "earningsGrowth": pick(-0.3, 0.4),
            "dividendRate": rnd.choice([0, 0, price * rnd.uniform(0.01, 0.06)]),
            "longBusinessSummary": "Fiktivt bolag skapat för att förhandsgranska rapporten. Ingen riktig data.",
        }
        out.append({"ticker": f"DEMO{k + 1}.ST", "info": info, "close": close,
                    "volume": [rnd.uniform(5e5, 3e6) * (1 + (j > 480) * rnd.uniform(0, 0.6)) for j in range(len(close))],
                    "dates": [(dt.date.today() - dt.timedelta(days=len(close) - j)).isoformat() for j in range(len(close))]})
    return out


# ───────────────────────────── HTML-RAPPORT ───────────────────────────────
HTML = r"""<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aktiescreener</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#EDF0F3;--card:#fff;--ink:#14212E;--mute:#5E6E7E;--line:#D6DDE4;--accent:#0F766E;--near:#A16207;--part:#C2570C;--no:#B42318;--miss:#8A97A4;--hover:#F5F8FA}
@media(prefers-color-scheme:dark){:root{--bg:#0E1620;--card:#16212D;--ink:#E5ECF2;--mute:#94A4B4;--line:#27374A;--accent:#2DD4BF;--near:#E4B13C;--part:#F08A4B;--no:#F2766B;--miss:#7C8B99;--hover:#1B2836}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
main{max-width:1240px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:30px;line-height:1.15;margin:0 0 6px;font-weight:700;letter-spacing:-.01em}
.sub{color:var(--mute);margin:0 0 24px;max-width:70ch}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.controls{display:flex;flex-wrap:wrap;gap:14px;align-items:end}
.controls label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--mute)}
input[type=search],select{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:7px 10px;min-width:180px}
input[type=range]{accent-color:var(--accent)}
details{margin-top:12px}summary{cursor:pointer;font-weight:600;font-size:14px}
details p,details li{color:var(--mute);font-size:14px;max-width:80ch}
.sliders{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px 22px;margin-top:12px}
.sliders label{font-size:13px;color:var(--mute)}.sliders b{color:var(--ink);float:right}
button.link{font:inherit;background:none;border:0;color:var(--accent);cursor:pointer;padding:0;text-decoration:underline}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:960px}
th{font-weight:600;font-size:13px;color:var(--mute);text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;user-select:none}
th.n,td.n{text-align:right}th[aria-sort]{color:var(--ink)}
tr.row{cursor:pointer}tr.row:hover td{background:var(--hover)}
td{padding:8px;border-bottom:1px solid var(--line);vertical-align:middle}
td.rk{color:var(--mute);width:40px;padding-left:14px}
.nm{background:none;border:0;padding:0;font:inherit;font-weight:600;color:var(--ink);text-align:left;cursor:pointer}
.nm:focus-visible,th:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tk{display:block;font-size:12px;color:var(--mute);font-weight:400}
.sc{width:130px}.sc b{font-size:18px}.bar{height:5px;border-radius:3px;background:var(--line);margin-top:3px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
.heat{text-align:center;min-width:64px;font-size:14px;border-radius:0}
.heat.na{color:var(--miss)}
tr.det>td{background:var(--bg);padding:0;border-bottom:1px solid var(--line)}
.dg{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.7fr);gap:28px;padding:20px 18px 24px}
@media(max-width:900px){.dg{grid-template-columns:1fr}}
.dg h3{margin:0 0 6px;font-size:16px}.dg h4{margin:18px 0 6px;font-size:14px}
.dg p{margin:0 0 10px;color:var(--mute);font-size:14px}
.facts{display:grid;grid-template-columns:auto 1fr;gap:3px 14px;font-size:14px;margin:12px 0}.facts dt{color:var(--mute)}.facts dd{margin:0}
ul.pl{margin:0;padding-left:18px;font-size:14px}ul.pl li{margin:2px 0}
ul.pl.s li::marker{color:var(--accent)}ul.pl.w li::marker{color:var(--no)}
svg.ch{width:100%;height:auto;display:block;margin-top:8px}
.lg{font-size:12px;color:var(--mute);display:flex;gap:14px;flex-wrap:wrap}.lg i{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:5px}
.ct{width:100%;border-collapse:collapse;min-width:0;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.ct th{cursor:default;padding:7px 10px;background:var(--card)}
.ct td{padding:8px 10px;font-size:14px}
.ct tr.ch td{background:var(--hover);font-weight:600;padding:7px 10px}
.rl{display:block;font-size:12px;color:var(--mute);font-weight:400;margin-top:1px;max-width:52ch}
.chip{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}
.ok{color:var(--accent)}.near{color:var(--near)}.part{color:var(--part)}.no{color:var(--no)}.miss,.na{color:var(--miss)}
.pt{width:96px}.pt .bar{width:70px}
a{color:var(--accent)}
.empty{padding:28px;text-align:center;color:var(--mute)}
footer{margin-top:22px;color:var(--mute);font-size:13px;max-width:80ch}
</style>
</head>
<body>
<main>
<h1>Aktiescreener</h1>
<p class="sub" id="sub"></p>

<div class="panel">
  <div class="controls">
    <label>Sök bolag eller ticker<input type="search" id="q" placeholder="t.ex. Volvo"></label>
    <label>Sektor<select id="sec"><option value="">Alla sektorer</option></select></label>
    <label>Land<select id="land"><option value="">Alla länder</option></select></label>
    <label>Börsvärde<select id="cap"><option value="">Alla klasser</option></select></label>
    <label>Lägsta totalpoäng: <span id="minv">0</span><input type="range" id="min" min="0" max="90" value="0"></label>
  </div>
  <details>
    <summary>Justera vikter mellan kategorierna</summary>
    <div class="sliders" id="sl"></div>
    <p style="margin-top:10px"><button class="link" id="reset">Återställ standardvikter</button></p>
  </details>
  <details>
    <summary>Så fungerar poängen</summary>
    <p>Varje bolag bedöms mot ett 25-tal kriterier. Ett fullt uppfyllt kriterium ger 100 poäng, ett bolag som är halvvägs till kravet får 50 poäng, och ett som ligger helt fel får 0 poäng. Inget bolag sorteras bort för att ett krav missas.</p>
    <p>Fokus är lågt värderade bolag som just börjat vända uppåt. Kategorin Vändning belönar bolag som fallit men nu visar tidiga tecken på bottning, som färsk EMA-korsning, studs från botten och ackumulering, och den missgynnar bolag som redan rusat. Värderingsmåtten bedöms till hälften mot bolagets sektor, så en bank jämförs med andra banker.</p>
    <p>Kriterierna är grupperade i kategorier. Kategoripoängen är ett viktat snitt, och totalpoängen är ett viktat snitt av kategorierna. Saknas data för ett kriterium räknas det inte med, men bolag med många luckor får ett avdrag på högst 25 %. Banker och fastighetsbolag hoppar över mått som inte passar dem, som skuldsättning och EV/EBITDA.</p>
    <p>Klicka på ett bolag för att se vilka krav som uppfylls, nästan uppfylls eller inte uppfylls.</p>
  </details>
</div>

<div class="tw"><table id="t"><thead></thead><tbody></tbody></table><div class="empty" id="empty" hidden>Inga bolag matchar filtren.</div><div class="empty" id="mt"><button class="link" id="more" hidden></button></div></div>
<footer id="foot"></footer>
</main>

<script>
const D = __DATA__;
const C = D.companies, CATS = D.categories, M = D.meta;
const S0 = {q: '', sec: '', land: '', cap: '', min: 0, sort: 'total', dir: -1, open: null, limit: 100};
const DEF = Object.fromEntries(CATS.map(c => [c.name, c.weight]));
let W = {...DEF};
const S = {...S0};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmt = (v, d = 0) => v == null ? '–' : v.toLocaleString('sv-SE', {minimumFractionDigits: d, maximumFractionDigits: d});

function calc(c) {
  let n = 0, den = 0, cn = 0, cd = 0;
  for (const k in W) {
    const x = c.cats[k], w = W[k];
    if (!x || x.na || w <= 0) continue;
    cd += w; cn += w * x.c;
    if (x.s != null) { n += w * x.s; den += w; }
  }
  c.total = den ? Math.round(n / den * (D.minFactor + (1 - D.minFactor) * (cd ? cn / cd : 0)) * 10) / 10 : 0;
}
function status(r) {
  if (r.na) return ['na', 'Ej relevant'];
  if (r.ratio == null) return ['miss', 'Data saknas'];
  if (r.ratio >= .9) return ['ok', 'Uppfyllt'];
  if (r.ratio >= .5) return ['near', 'Nästan'];
  if (r.ratio > 0) return ['part', 'Delvis'];
  return ['no', 'Ej uppfyllt'];
}
const heat = s => s == null ? '<td class="heat na">–</td>'
  : `<td class="heat" style="background:color-mix(in srgb,var(--accent) ${Math.round(s * .62)}%,transparent)">${fmt(s)}</td>`;

function chart(c) {
  const s = c.spark; if (!s || s.c.length < 3) return '';
  const Wd = 560, H = 170, P = 6;
  const all = [...s.c, ...s.s50.filter(v => v != null), ...s.s200.filter(v => v != null)];
  const mn = Math.min(...all), mx = Math.max(...all), n = s.c.length;
  const X = i => P + i * (Wd - 2 * P) / (n - 1), Y = v => H - P - (v - mn) / ((mx - mn) || 1) * (H - 2 * P);
  const path = a => { let d = '', pen = false; a.forEach((v, i) => { if (v == null) { pen = false; return; } d += (pen ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(v).toFixed(1); pen = true; }); return d; };
  return `<svg class="ch" viewBox="0 0 ${Wd} ${H}" role="img" aria-label="Kursutveckling senaste året">
    <path d="${path(s.s200)}" fill="none" stroke="var(--near)" stroke-width="1.5" stroke-dasharray="4 3"/>
    <path d="${path(s.s50)}" fill="none" stroke="var(--accent)" stroke-width="1.5"/>
    <path d="${path(s.c)}" fill="none" stroke="var(--ink)" stroke-width="1.8"/></svg>
    <div class="lg"><span><i style="background:var(--ink)"></i>Kurs</span><span><i style="background:var(--accent)"></i>SMA 50</span><span><i style="background:var(--near)"></i>SMA 200</span><span>${esc(s.from)} till ${esc(s.to)}</span></div>`;
}

function detail(c) {
  const rows = M.map((m, i) => ({...m, ratio: c.cr[i][0], val: c.cr[i][1], na: !!c.cr[i][2], rel: c.cr[i][3]}));
  const strong = rows.filter(r => r.ratio != null && r.ratio >= .9).sort((a, b) => b.w - a.w).slice(0, 4);
  const weak = rows.filter(r => r.ratio != null && r.ratio <= .3).sort((a, b) => b.w - a.w).slice(0, 4);
  const li = r => `<li>${esc(r.name)}: ${esc(r.val)}</li>`;
  const live = Object.keys(W).filter(k => W[k] > 0 && c.cats[k] && !c.cats[k].na);
  const cov = live.length ? live.reduce((a, k) => a + W[k] * c.cats[k].c, 0) / live.reduce((a, k) => a + W[k], 0) : 0;
  let tbl = '';
  for (const cat of CATS.map(x => x.name)) {
    const rs = rows.filter(r => r.cat === cat); if (!rs.length) continue;
    const cc = c.cats[cat];
    tbl += `<tr class="ch"><td colspan="4">${esc(cat)}${cc.na ? ' (ej relevant för sektorn)' : ` &ndash; ${cc.s == null ? 'data saknas' : fmt(cc.s) + ' av 100 p'}`} <span class="na" style="font-weight:400">vikt ${W[cat]}</span></td></tr>`;
    for (const r of rs) {
      const [cl, lb] = status(r);
      tbl += `<tr><td>${esc(r.name)}<span class="rl">${esc(r.why)}</span><span class="rl">${esc(r.rule)}</span></td>
        <td>${esc(r.val)}${r.rel != null ? `<span class="rl">billigare än ${fmt(r.rel * 100)} % av sektorn</span>` : ''}</td>
        <td class="pt">${r.ratio == null ? '<span class="na">–</span>' : `<b>${fmt(r.ratio * 100)}</b> p<div class="bar"><i style="width:${r.ratio * 100}%"></i></div>`}</td>
        <td><span class="chip ${cl}">${lb}</span></td></tr>`;
    }
  }
  const link = c.website ? `<a href="${esc(c.website)}" target="_blank" rel="noopener">Bolagets webbplats</a> &nbsp; ` : '';
  return `<div class="dg"><div>
    <h3>${esc(c.name)}</h3>
    <p>${esc(c.summary) || 'Ingen bolagsbeskrivning tillgänglig.'}${c.summary.length >= 360 ? '…' : ''}</p>
    <dl class="facts"><dt>Sektor</dt><dd>${esc(c.sector)}${c.industry ? ', ' + esc(c.industry) : ''}</dd>
      <dt>Land</dt><dd>${esc(c.country) || '–'}</dd><dt>Kurs</dt><dd>${fmt(c.price, 2)} ${esc(c.currency)}</dd>
      <dt>Börsvärde</dt><dd>${esc(c.mcap_txt)} ${esc(c.currency)}</dd><dt>Datatäckning</dt><dd>${fmt(cov * 100)} %</dd></dl>
    <p>${link}<a href="https://finance.yahoo.com/quote/${encodeURIComponent(c.ticker)}" target="_blank" rel="noopener">Öppna på Yahoo Finance</a></p>
    <h4>Kursutveckling, 1 år</h4>${chart(c)}
    <h4>Starkaste kraven</h4>${strong.length ? `<ul class="pl s">${strong.map(li).join('')}</ul>` : '<p>Inga krav är helt uppfyllda.</p>'}
    <h4>Svagaste kraven</h4>${weak.length ? `<ul class="pl w">${weak.map(li).join('')}</ul>` : '<p>Inga uppenbara svagheter.</p>'}
  </div><div>
    <table class="ct"><thead><tr><th>Kriterium</th><th>Värde</th><th>Poäng</th><th>Status</th></tr></thead><tbody>${tbl}</tbody></table>
  </div></div>`;
}

function render() {
  C.forEach(calc);
  [...C].sort((a, b) => b.total - a.total).forEach((c, i) => c.rank = i + 1);
  const q = S.q.toLowerCase();
  let L = C.filter(c => (!q || (c.name + ' ' + c.ticker).toLowerCase().includes(q)) && (!S.sec || c.sector === S.sec) && (!S.land || c.land === S.land) && (!S.cap || c.cap === S.cap) && c.total >= S.min);
  const val = c => S.sort === 'total' ? c.total : S.sort === 'name' ? c.name.toLowerCase() : S.sort === 'mcap' ? (c.mcap ?? -1)
    : (c.cats[S.sort] && c.cats[S.sort].s != null ? c.cats[S.sort].s : -1);
  L.sort((a, b) => { const x = val(a), y = val(b); return (x < y ? -1 : x > y ? 1 : 0) * S.dir; });
  const th = (k, t, cls = '') => `<th class="${cls}" data-k="${k}" tabindex="0" ${S.sort === k ? `aria-sort="${S.dir < 0 ? 'descending' : 'ascending'}"` : ''}>${t}${S.sort === k ? (S.dir < 0 ? ' ▾' : ' ▴') : ''}</th>`;
  $('t').tHead.innerHTML = '<tr><th>#</th>' + th('name', 'Bolag') + th('total', 'Totalpoäng') + CATS.map(c => th(c.name, c.name, 'n')).join('') + th('mcap', 'Börsvärde', 'n') + '</tr>';
  let h = '';
  const total = L.length;
  L = L.slice(0, S.limit);
  for (const c of L) {
    const open = S.open === c.ticker;
    h += `<tr class="row" data-t="${esc(c.ticker)}"><td class="rk">${c.rank}</td>
      <td><button class="nm" aria-expanded="${open}">${esc(c.name)}</button><span class="tk">${esc(c.ticker)}, ${esc(c.land)}${c.cap ? ' ' + esc(c.cap) : ''}, ${esc(c.sector)}</span></td>
      <td class="sc"><b>${fmt(c.total, 1)}</b><div class="bar"><i style="width:${c.total}%"></i></div></td>
      ${CATS.map(k => heat(c.cats[k.name].na ? null : c.cats[k.name].s)).join('')}
      <td class="n">${esc(c.mcap_txt)}</td></tr>`;
    if (open) h += `<tr class="det"><td colspan="${CATS.length + 4}">${detail(c)}</td></tr>`;
  }
  $('t').tBodies[0].innerHTML = h;
  $('empty').hidden = total > 0;
  $('more').hidden = total <= S.limit;
  $('more').textContent = `Visar ${L.length} av ${total}. Visa 100 till`;
}

// Kontroller
$('sub').textContent = `${C.length} bolag ur ${D.universe} har poängsatts med data från Yahoo Finance (hämtad ${D.date}). Klicka på ett bolag för detaljer.`;
$('foot').textContent = 'Verktyget sorterar data och är inte finansiell rådgivning. Yahoo Finance-data kan vara ofullständig eller fördröjd, så kontrollera alltid siffrorna mot bolagets rapporter. ' + (D.failed.length ? `Kunde inte hämta: ${D.failed.join(', ')}.` : '');
[...new Set(C.map(c => c.sector))].sort().forEach(s => $('sec').insertAdjacentHTML('beforeend', `<option>${esc(s)}</option>`));
$('sl').innerHTML = CATS.map(c => `<label>${esc(c.name)} <b id="v_${esc(c.name)}">${c.weight}</b><input type="range" min="0" max="50" value="${c.weight}" data-c="${esc(c.name)}" style="width:100%"></label>`).join('');
$('sl').addEventListener('input', e => { const k = e.target.dataset.c; if (!k) return; W[k] = +e.target.value; document.getElementById('v_' + k).textContent = W[k]; render(); });
$('reset').onclick = () => { W = {...DEF}; $('sl').querySelectorAll('input').forEach(i => { i.value = W[i.dataset.c]; document.getElementById('v_' + i.dataset.c).textContent = W[i.dataset.c]; }); render(); };
$('q').oninput = e => { S.q = e.target.value; S.limit = 100; render(); };
[...new Set(C.map(c => c.land).filter(Boolean))].sort().forEach(s => $('land').insertAdjacentHTML('beforeend', `<option>${esc(s)}</option>`));
['mega', 'large', 'mid', 'small', 'micro'].filter(k => C.some(c => c.cap === k)).forEach(s => $('cap').insertAdjacentHTML('beforeend', `<option>${s}</option>`));
$('land').onchange = e => { S.land = e.target.value; S.limit = 100; render(); };
$('cap').onchange = e => { S.cap = e.target.value; S.limit = 100; render(); };
$('more').onclick = () => { S.limit += 100; render(); };
$('sec').onchange = e => { S.sec = e.target.value; render(); };
$('min').oninput = e => { S.min = +e.target.value; $('minv').textContent = S.min; render(); };
const sortBy = th => { const k = th && th.dataset.k; if (!k) return; S.dir = S.sort === k ? -S.dir : (k === 'name' ? 1 : -1); S.sort = k; render(); };
$('t').addEventListener('click', e => {
  const th = e.target.closest('th'); if (th) return sortBy(th);
  const tr = e.target.closest('tr.row'); if (!tr) return;
  S.open = S.open === tr.dataset.t ? null : tr.dataset.t; render();
  const el = document.querySelector(`tr.row[data-t="${CSS.escape(tr.dataset.t)}"] .nm`); if (el) el.focus({preventScroll: true});
});
$('t').addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.tagName === 'TH') sortBy(e.target); });
render();
</script>
</body>
</html>
"""


def build_html(companies, universe, failed):
    meta = [dict(key=c.key, name=c.name, cat=c.cat, w=c.weight, why=c.why,
                 rule=c.scorer.rule + (" Hälften av poängen avgörs av hur billigt bolaget är mot sin sektor." if c.key in REL_KEYS else ""))
            for c in CRITERIA]
    payload = {"meta": meta,
        "companies": companies, "universe": universe, "failed": failed,
        "date": dt.date.today().isoformat(), "minFactor": MIN_COVERAGE_FACTOR,
        "categories": [{"name": k, "weight": v} for k, v in CATEGORIES.items()],
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return HTML.replace("__DATA__", data)


# ───────────────────────────── HUVUDPROGRAM ───────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Poängbaserad aktiescreener")
    ap.add_argument("--universe", choices=["omx", "usa", "all"], default="omx")
    ap.add_argument("--tickers", nargs="+", help="egna tickers (Yahoo-format, t.ex. VOLV-B.ST)")
    ap.add_argument("--file", help="textfil med en ticker per rad")
    ap.add_argument("--list", help="tickerlista (.py med UNIVERSE), standard: avanzaEuUs.py bredvid skriptet")
    ap.add_argument("--countries", nargs="+", help="begränsa till länder ur listan, t.ex. SE US NO")
    ap.add_argument("--caps", nargs="+", help="begränsa till börsvärdesklasser: micro small mid large mega")
    ap.add_argument("--include-micro", action="store_true", help="ta med micro caps (ofta dålig data hos Yahoo)")
    ap.add_argument("--out", help="egen sökväg till HTML-filen (standard: Fundaktiscreen_Resultat/fundaktiscreen_ÅÅÅÅMMDD_TTMM.html)")
    ap.add_argument("--retry-failed", action="store_true", help="försök hämta tickers som nyligen misslyckades igen")
    ap.add_argument("--no-open", action="store_true", help="öppna inte rapporten i webbläsaren automatiskt")
    ap.add_argument("--workers", type=int, default=6, help="parallella trådar för nyckeltal")
    ap.add_argument("--batch", type=int, default=50, help="antal tickers per kurshistorik-anrop")
    ap.add_argument("--min-turnover", type=float, default=0,
                    help="valfritt: hoppa över aktier med lägre snittomsättning per dag (lokal valuta) före nyckeltalshämtning")
    ap.add_argument("--cache-hours", type=float, default=12, help="hur länge hämtad data återanvänds")
    ap.add_argument("--refresh", action="store_true", help="ignorera cache")
    ap.add_argument("--demo", action="store_true", help="använd fejkdata (kräver inte yfinance/internet)")
    a = ap.parse_args()

    raws, failed, lst = [], [], {}
    default_list = Path(__file__).with_name("avanzaEuUs.py")
    if a.demo:
        raws, universe = demo_data(), "demodata"
    else:
        if a.tickers:
            tickers, universe = a.tickers, "egna tickers"
        elif a.list or (default_list.exists() and not a.file and a.universe == "omx"):
            path = a.list or str(default_list)
            entries = load_list(path)
            n0 = len(entries)
            if a.countries:
                cs = {c.upper() for c in a.countries}
                entries = [e for e in entries if e["land"].upper() in cs]
            if a.caps:
                cp = {c.lower() for c in a.caps}
                entries = [e for e in entries if e["cap"].lower() in cp]
            elif not a.include_micro:
                entries = [e for e in entries if e["cap"].lower() != "micro"]
            lst = {e["ticker"]: e for e in entries}
            tickers, universe = list(lst), f"{Path(path).name}"
            print(f"{len(tickers)} av {n0} aktier valda ur {Path(path).name}"
                  + ("" if a.caps or a.include_micro else " (micro caps utelämnade, använd --include-micro för att ta med dem)"))
        elif a.file:
            tickers = [l.strip() for l in Path(a.file).read_text().splitlines() if l.strip() and not l.startswith("#")]
            universe = a.file
        else:
            tickers = {"omx": OMX, "usa": USA, "all": OMX + USA}[a.universe]
            universe = {"omx": "Stockholmsbörsen", "usa": "USA", "all": "Stockholm och USA"}[a.universe]
        tickers = list(dict.fromkeys(tickers))
        try:
            import yfinance  # noqa: F401
        except ImportError:
            sys.exit("Installera först: pip install yfinance")
        cache = BASE_DIR / "_cache"
        cache.mkdir(parents=True, exist_ok=True)
        hist = fetch_history(tickers, cache, a.cache_hours, a.refresh, a.batch, a.retry_failed)
        failed = [t for t in tickers if t not in hist]
        if a.min_turnover:
            def turnover(h):
                c, v = h["close"][-20:], h["volume"][-20:]
                return sum(x * y for x, y in zip(c, v)) / max(len(c), 1)
            thin = [t for t, h in hist.items() if turnover(h) < a.min_turnover]
            print(f"{len(thin)} aktier med omsättning under {a.min_turnover:,.0f} per dag hoppas över")
            for t in thin:
                hist.pop(t)
        print(f"Nyckeltal: hämtar för {len(hist)} aktier ({a.workers} parallella trådar)")
        infos, done = {}, 0
        with ThreadPoolExecutor(a.workers) as ex:
            futs = {ex.submit(fetch_info, t, cache, a.cache_hours, a.refresh): t for t in hist}
            for f in as_completed(futs):
                done += 1
                infos[futs[f]] = f.result()
                if done % 25 == 0 or done == len(futs):
                    print(f"  nyckeltal {done}/{len(futs)}")
        for t, h in hist.items():
            raws.append({"ticker": t, "info": infos.get(t) or {}, "list": lst.get(t, {}), **h})
        nofund = sum(1 for t in hist if not infos.get(t))
        if nofund:
            print(f"{nofund} aktier saknar nyckeltal hos Yahoo och poängsätts bara på teknik (kör igen senare om det beror på rate limit)")

    companies = [evaluate(r) for r in raws if len(r["close"]) >= 60]
    apply_sector_relative(companies)
    companies = [finalize(c) for c in companies]
    companies.sort(key=lambda c: -c["total"])
    if not companies:
        sys.exit("Ingen data kunde hämtas. Kontrollera internetanslutning och tickers.")

    if a.out:
        out = Path(a.out)
    else:
        out = BASE_DIR / f"fundaktiscreen_{dt.datetime.now():%Y%m%d_%H%M}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out = out.resolve()
    out.write_text(build_html(companies, universe, sorted(failed)), encoding="utf-8")
    csv_path = out.with_suffix(".csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Ticker", "Bolag", "Sektor", "Totalpoäng"] + list(CATEGORIES))
        for c in companies:
            w.writerow([c["ticker"], c["name"], c["sector"], sv(c["total"])] +
                       [("" if c["cats"][k]["s"] is None else sv(c["cats"][k]["s"])) for k in CATEGORIES])

    if failed:
        lp = BASE_DIR / "misslyckade_tickers.txt"
        with open(lp, "w", encoding="utf-8") as fh:
            fh.write("# Tickers som Yahoo Finance inte gav kursdata för. Kontrollera stavning, suffix och om bolaget är avnoterat/bytt namn.\n")
            for t in sorted(failed):
                fh.write(f"{t}\t{lst.get(t, {}).get('name', '')}\n")
        print(f"\n{len(failed)} tickers saknade data, se {lp}")
    print(f"\nTopp 10 av {len(companies)} bolag:")
    for n, c in enumerate(companies[:10], 1):
        print(f"{n:>2}. {c['total']:5.1f}  {c['name'][:38]:<38} {c['ticker']}")
    link = out.as_uri()
    if sys.platform == "win32":
        import os
        os.system("")  # aktiverar ANSI-koder i Windows-konsolen
    clickable = f"\033]8;;{link}\033\\{out.name}\033]8;;\033\\"  # OSC 8-hyperlänk
    print(f"\n✔ Klar! Öppna rapporten: {clickable}")
    print(f"  Länk (Ctrl+klick i Windows Terminal): {link}")
    print(f"  Sökväg: {out}\n  Tabell: {csv_path}")
    if not a.no_open:
        import webbrowser
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
