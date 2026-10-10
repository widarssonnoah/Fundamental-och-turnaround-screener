"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                              ║
║   TURNAROUND SCREENER  v1.0  —  Sverige + Europa                            ║
║                                                                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  FILOSOFI: Hitta aktier som gått ned länge och nu vänder.                   ║
║  Inte EMA9>21>50>200 (etablerad upptrend) — vi letar det OMVÄNDA:           ║
║  EMA9 börjar röra sig MOT EMA21 underifrån medan EMA50/200 fortfarande     ║
║  är ovan = tidig vändning från svaghet.                                     ║
║                                                                              ║
║  TVÅ OBLIGATORISKA FASER:                                                   ║
║  1. HISTORISK SVAGHET: p12m<-15% ELLER p6m<-20%, pris under EMA50          ║
║  2. VÄNDNINGSSIGNAL:   RSI återhämtar, RS_acc vänder, OBV divergens,       ║
║     ackumuleringsvolym, EMA9 böjer uppåt mot EMA21                          ║
║                                                                              ║
║  SKYDDAR MOT VALUE TRAPS:                                                   ║
║  - Utesluter p12m < -65% (fritt fall / konkursrisk)                         ║
║  - Kräver att nedtrenden FAKTISKT bromsar (ADX-fall, DI+ stiger)            ║
║  - Kräver ackumuleringsvolym (institutioner köper på svaghet)               ║
║                                                                              ║
║  KÖRKOMANDON:                                                                ║
║    python turnaround_screener.py                                             ║
║    python turnaround_screener.py --topn 20                                  ║
║    python turnaround_screener.py --land SE NO DK                            ║
║    python turnaround_screener.py --min-svaghet 20   # min 20% nedgång      ║
║    python turnaround_screener.py --min-score 50                              ║
║    python turnaround_screener.py --ingen-csv                                ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
import os, sys, time, math, warnings, logging, argparse
from datetime import datetime
from typing import Optional, List

import pandas as pd
import numpy as np

# ── Importera all infrastruktur från master_screener ─────────────────────────
try:
    from master_screener import (
        fetch_all_history, precompute_all,
        UNIVERSE, _NAMN, _LAND, _SEKTOR, _TYP, _CAP,
        BENCHMARK_TICKER, MIN_HISTORY, MIN_AVG_DAILY_VOL,
        compute_regime, RegimDetalj, Regime,
        precompute_regime_series,
        _g, TickerCache,
        _sparkline_svg, _calc_levels, _asset_signal,
    )
except ImportError as e:
    sys.exit("[FEL] Kan inte importera master_screener.py: {}\n"
             "Lägg turnaround_screener.py i samma mapp.".format(e))

logging.getLogger('yfinance').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)
warnings.filterwarnings('ignore')


# ════════════════════════════════════════════════════════════════════════════
#  KONFIGURATION
# ════════════════════════════════════════════════════════════════════════════

OUTPUT_FOLDER   = 'Turnaround_Output'
TOP_N_DEFAULT   = 25

# ── Hårdfilter (OBLIGATORISKA) ────────────────────────────────────────────────
# Fas 1: historisk svaghet (minst ett av dessa måste vara uppfyllt)
MIN_WEAKNESS_P12M  = -15.0   # max -15% på 12 månader
MIN_WEAKNESS_P6M   = -20.0   # ELLER max -20% på 6 månader
MAX_WEAKNESS_P12M  = -65.0   # uteslut fritt fall / konkursrisk

# Fas 2: vändningssignal (minst ett av dessa)
MIN_TURN_SIGNALS   = 2       # minst 2 vändningssignaler krävs

# Grundkrav
MIN_VOL_SEK        = MIN_AVG_DAILY_VOL   # 500k SEK dagsomsättning


# ════════════════════════════════════════════════════════════════════════════
#  KÄRNLOGIK — TURNAROUND SCORE
# ════════════════════════════════════════════════════════════════════════════

def compute_turnaround(ticker: str, tc: TickerCache, i: int,
                       min_vol: float, min_weakness_p12m: float) -> Optional[dict]:
    """
    Beräknar turnaround-score för en ticker vid index i.

    Returnerar None om:
    - För lite historik
    - För låg omsättning
    - Ingen historisk svaghet (är inte en turnaround-kandidat)
    - Fritt fall (p12m < -65%, konkursrisk)
    - Fortfarande i aktiv stark nedtrend utan bromsar

    Score 0-100 baserad på:
    - Fas 1: Historisk svaghet (inträdesbiljett, ger inga poäng i sig)
    - Fas 2: Vändningsstyrka (ger poäng — ju starkare signal, desto högre score)
    - Fas 3: Tidningsfaktor (hur tidigt är vi? Mer poäng för tidiga signaler)
    """
    if i < MIN_HISTORY - 1:
        return None

    kurs = _g(tc.close_arr, i)
    if np.isnan(kurs) or kurs <= 0:
        return None

    # Volymfilter
    vol_avg = _g(tc.vol_avg22, i)
    if np.isnan(vol_avg) or vol_avg <= 0:
        vol_avg = _g(tc.vol_avg22, i-1, 0.0)
    if np.isnan(vol_avg) or vol_avg <= 0:
        sl_v = tc.vol_arr[max(0, i-21):i+1]
        vol_avg = float(np.nanmean(sl_v)) if len(sl_v) >= 3 else 0.0
    avg_vol_sek = vol_avg * kurs
    if avg_vol_sek < min_vol and avg_vol_sek > 0:
        return None

    # ── Hämta alla indikatorer ────────────────────────────────────────────────
    def avk(d):
        j = max(0, i - d)
        prev = tc.close_arr[j]
        if np.isnan(prev) or prev <= 0: return 0.0
        return (kurs / prev - 1) * 100

    p1d  = avk(1);   p1w  = avk(5)
    p3m  = _g(tc.p3m, i, 0.0)
    p6m  = _g(tc.p6m, i, 0.0)
    p12m = _g(tc.p12m, i, 0.0)

    # EMAs
    e9   = _g(tc.ema9,   i, kurs)
    e21  = _g(tc.ema21,  i, kurs)
    e50  = _g(tc.ema50,  i, kurs)
    e200 = _g(tc.ema200, i, kurs)

    # Föregående EMA9 (för att detektera böj uppåt)
    e9_prev  = _g(tc.ema9, max(0, i-5), e9)
    e9_prev1 = _g(tc.ema9, max(0, i-10), e9_prev)
    e21_prev = _g(tc.ema21, max(0, i-5), e21)

    # SMAs
    sma50  = _g(tc.sma50,  i, kurs)
    sma200 = _g(tc.sma200, i, kurs)
    sma200_slope = _g(tc.sma200_slope, i, 0.0)

    # RSI
    rsi14 = max(0.0, min(100.0, _g(tc.rsi14, i, 50.0)))
    rsi_slope = _g(tc.rsi_slope5, i, 0.0)

    # RSI historik — var vi i oversold-territoriet nyligen?
    rsi_window_60 = tc.rsi14[max(0, i-59):i+1]
    rsi_min_60 = float(np.nanmin(rsi_window_60)) if len(rsi_window_60) > 0 else rsi14
    rsi_was_oversold = rsi_min_60 < 30.0   # RSI under 30 någon gång senaste 60 dagar

    # RSI bullish divergens: kurs gör lägre låg men RSI gör högre låg
    rsi_bull_div = False
    if i >= 30:
        c30  = tc.close_arr[i-29:i+1]
        r30  = tc.rsi14[i-29:i+1]
        c10  = c30[-10:];  r10 = r30[-10:]
        if (np.nanmin(c10) < np.nanmin(c30) and
                np.nanmin(r10) > np.nanmin(r30)):
            rsi_bull_div = True

    # StochRSI
    srsi_k      = _g(tc.stochrsi_k, i, 50.0)
    srsi_d      = _g(tc.stochrsi_d, i, 50.0)
    srsi_k_prev = _g(tc.stochrsi_k, i-1, srsi_k)
    srsi_d_prev = _g(tc.stochrsi_d, i-1, srsi_d)
    stochrsi_cross_up = (srsi_k_prev < srsi_d_prev and srsi_k >= srsi_d and srsi_k < 50)

    # Bollinger %B
    bb_pctB = _g(tc.bb_pctB, i, 0.5)
    bb_pctB_prev5 = _g(tc.bb_pctB, max(0, i-5), bb_pctB)
    bb_recovering = (bb_pctB_prev5 < 0.15 and bb_pctB > bb_pctB_prev5)  # bottnade och stiger

    # ADX — är nedtrenden på väg att MATTAS?
    adx_nu  = _g(tc.adx, i, 20.0)
    dip_nu  = _g(tc.dip, i, 25.0)
    dim_nu  = _g(tc.dim, i, 25.0)
    # ADX 10 dagar sedan
    adx_10d = _g(tc.adx, max(0, i-10), adx_nu)
    dip_10d = _g(tc.dip, max(0, i-10), dip_nu)
    dim_10d = _g(tc.dim, max(0, i-10), dim_nu)

    adx_falling     = adx_nu < adx_10d           # ADX faller = trend mattas
    dip_rising      = dip_nu > dip_10d           # DI+ ökar = köptryck ökar
    dim_falling     = dim_nu < dim_10d           # DI- minskar = säljtryck minskar
    di_cross_soon   = (dip_nu > dim_nu * 0.8 and dim_nu > dip_nu * 0.8)  # nära korsning
    di_crossed      = (dip_nu > dim_nu)           # DI+ > DI- = trendbyte bekräftat

    # OBV — divergens: kurs ned men OBV upp = ackumulation
    obv_now    = _g(tc.obv,       i, 0.0)
    obv_ema    = _g(tc.obv_ema20, i, 0.0)
    obv_slope  = _g(tc.obv_slope20, i, 0.0)
    obv_above_ema  = obv_now > obv_ema          # OBV ovanför sitt MA = bullish
    obv_slope_pos  = obv_slope > 0.05            # OBV lutar uppåt

    # OBV divergens: kurs ny 20d-botten men OBV INTE ny botten
    obv_divergens = False
    if i >= 20:
        c20  = tc.close_arr[i-19:i+1]
        ob20 = tc.obv[i-19:i+1]
        if (float(np.nanmin(c20[-5:])) <= float(np.nanmin(c20)) * 1.02 and   # kurs nära botten
                float(np.nanmin(ob20[-5:])) > float(np.nanmin(ob20)) * 1.05):  # OBV inte ny botten
            obv_divergens = True

    # Volym — ackumuleringsmönster
    vol_snitt    = _g(tc.vol_avg22, i, float(np.nanmean(tc.vol_arr[max(0,i-22):i])) + 1e-10)
    vol_5d       = float(np.nanmean(tc.vol_arr[max(0, i-4):i+1]))
    vol_ratio    = round(vol_5d / (vol_snitt + 1e-10), 2)

    # Up/Down volume ratio (institutionellt flöde)
    ud_vol = _g(tc.up_dn_vol, i, 1.0)
    akkumulering  = ud_vol > 1.3    # fler köpvolymdagar än säljvolymdagar
    stark_ackum   = ud_vol > 1.8    # tydlig institutionell ackumulation

    # CMF
    cmf_nu = _g(tc.cmf, i, 0.0)
    cmf_positiv = cmf_nu > 0.0
    cmf_stark   = cmf_nu > 0.10

    # Squeeze
    squeeze_on  = bool(_g(tc.sq_on,  i, 0.0) > 0.5)
    squeeze_mom = _g(tc.sq_mom, i, 0.0)

    # RS och RS-acceleration
    rs     = _g(tc.rs21, i, 0.0)
    rs_acc = _g(tc.rs_acc, i, 0.0)
    rs_acc_turning = rs_acc > -1.0 and rs_acc > _g(tc.rs_acc, max(0, i-10), rs_acc) # RS_acc förbättras

    # 52v position
    d52   = min(252, i+1)
    c_win = tc.close_arr[max(0, i-d52+1):i+1]
    high52 = float(np.nanmax(c_win))
    low52  = float(np.nanmin(c_win))
    pos52  = round((kurs - low52) / (high52 - low52 + 1e-10), 3)

    # Max drawdown 120d
    c120 = tc.close_arr[max(0, i-119):i+1]
    peak120 = np.maximum.accumulate(np.where(np.isnan(c120), 0, c120))
    dd_arr  = (c120 / (peak120 + 1e-10) - 1) * 100
    max_dd  = round(float(np.nanmin(dd_arr)), 1)

    # ATR
    atr14v = _g(tc.atr14, i, kurs * 0.02)
    atr50v = _g(tc.atr50, i, kurs * 0.02)
    atr_exp = atr14v / (atr50v + 1e-10)
    atr_pct = round(atr14v / (kurs + 1e-10) * 100, 1)

    # ═══════════════════════════════════════════════════════════════════════════
    #  FAS 1: HISTORISK SVAGHET (obligatorisk inträdesbiljett)
    # ═══════════════════════════════════════════════════════════════════════════

    svaghet_p12m = p12m <= min_weakness_p12m          # t.ex. -15% på 12m
    svaghet_p6m  = p6m  <= MIN_WEAKNESS_P6M            # t.ex. -20% på 6m
    under_ema50  = kurs < e50                           # pris under EMA50
    under_sma200 = kurs < sma200                        # pris under SMA200

    # Kräv MINST ett svaghetstecken
    har_historisk_svaghet = svaghet_p12m or svaghet_p6m

    if not har_historisk_svaghet:
        return None   # Inte en turnaround-kandidat — för stark redan

    # Uteslut fritt fall (konkursrisk / tech-nolla)
    if p12m < MAX_WEAKNESS_P12M:
        return None

    # Uteslut om fortfarande i AKTIV stark nedtrend utan bromsar alls
    # (ADX > 35 + DI- >> DI+ + RSI < 25 + ingen ackumulation)
    aktiv_nedtrend_blocker = (
        adx_nu > 40 and
        dim_nu > dip_nu * 1.5 and
        rsi14 < 25 and
        not akkumulering and
        not obv_divergens
    )
    if aktiv_nedtrend_blocker:
        return None

    # ═══════════════════════════════════════════════════════════════════════════
    #  FAS 2: VÄNDNINGSSIGNALER (scoring)
    #
    #  Kategori A: EMA-signaler (20 poäng max)
    #  Kategori B: Momentum / RSI (25 poäng max)
    #  Kategori C: Volym / Institutionellt flöde (20 poäng max)
    #  Kategori D: Trendbromsar / ADX (15 poäng max)
    #  Kategori E: Tidighet / Fas-bedömning (20 poäng max)
    # ═══════════════════════════════════════════════════════════════════════════

    score  = 0.0
    signaler   = []   # textbeskrivningar av aktiva signaler
    varningar  = []

    # ── KATEGORI A: EMA-signaler ─────────────────────────────────────────────
    # Turnaround-profil: EMA9 rör sig uppåt mot EMA21 medan EMA50/200 är ovan
    # Detta är MOTSATSEN till normala screener (9>21>50>200)

    ema9_bottnade  = e9 > e9_prev   and e9_prev < e9_prev1   # EMA9 bottnade och stiger
    ema9_nærmar_21 = abs(e9 - e21) < abs(e9_prev - e21_prev) and e9 < e21  # klyftan minskar
    ema9_korsade_21 = e9 > e21 and e9_prev < e21_prev        # färsk EMA9 > EMA21-korsning
    under_ema50_men_ovanfor_sma_lows = (kurs > low52 * 1.05) # inte på absolut botten

    if ema9_korsade_21 and not (kurs > e50):
        score += 20
        signaler.append("📊 EMA9 korsade EMA21 uppåt — tidig trendbyte, pris fortfarande under EMA50 (idealt turnaround-läge)")
    elif ema9_korsade_21 and kurs > e50:
        score += 12
        signaler.append("📊 EMA9 > EMA21 och pris > EMA50 — turnaround mer etablerad")
    elif ema9_bottnade and ema9_nærmar_21:
        score += 14
        signaler.append("📊 EMA9 bottnade och rör sig mot EMA21 — tidigt vändningsmönster")
    elif ema9_bottnade:
        score += 7
        signaler.append("📊 EMA9 bottnade och börjar stiga")

    # Extra om pris just brutit EMA50 (bekräftad vändning)
    if kurs > e50 and _g(tc.close_arr, max(0, i-5), 0.0) < e50:
        score += 10
        signaler.append("🎯 Pris bröt EMA50 uppåt de senaste 5 dagarna — bekräftad trendbyte")

    # ── KATEGORI B: Momentum / RSI ──────────────────────────────────────────

    # RSI-återhämtning från oversold
    if rsi_was_oversold and rsi14 > 35 and rsi_slope > 0:
        score += 18
        signaler.append("🔄 RSI återhämtar sig från oversold-zon (var under 30, nu {:.0f} och stigande) — typisk turnaround-signal".format(rsi14))
    elif rsi14 < 45 and rsi_slope > 3:
        score += 10
        signaler.append("📈 RSI {:.0f} med stark positiv slope — momentum byggs".format(rsi14))
    elif 35 <= rsi14 <= 55 and rsi_slope > 0:
        score += 7
        signaler.append("📈 RSI {:.0f} i återhämtnings-zon med positiv riktning".format(rsi14))

    # RSI bullish divergens (kraftfull vändningssignal)
    if rsi_bull_div:
        score += 15
        signaler.append("⚡ RSI-divergens: aktien gör lägre låg men RSI gör högre låg — klassisk reversal-signal")

    # StochRSI-korsning uppåt från låg nivå
    if stochrsi_cross_up:
        score += 8
        signaler.append("📊 StochRSI korsade uppåt under 50 — early momentum-skift")

    # Bollinger %B återhämtar
    if bb_recovering:
        score += 7
        signaler.append("📉→📈 Bollinger %B bottnade under 15% och stiger — priset lämnar undre bandet")

    # RS-acceleration vänder (relativt benchmark)
    if rs_acc_turning and rs_acc > 0:
        score += 10
        signaler.append("⚡ RS-acceleration positiv ({:+.1f}) — aktien börjar slå benchmarken".format(rs_acc))
    elif rs_acc_turning and rs_acc > -2:
        score += 5
        signaler.append("⚡ RS-acceleration förbättras ({:+.1f})".format(rs_acc))

    # Squeeze-release uppåt på svag aktie (extremt sällsynt och kraftfullt)
    if squeeze_on and squeeze_mom > 0 and not (kurs > e50):
        score += 12
        signaler.append("🗜️ Squeeze-release UPPÅT medan pris fortfarande under EMA50 — ovanlig och kraftfull signal")
    elif squeeze_on and squeeze_mom > 0:
        score += 6
        signaler.append("🗜️ Squeeze-release uppåt")

    # ── KATEGORI C: Volym / Institutionellt flöde ───────────────────────────

    # OBV-divergens (starkaste volymsignal för turnarounds)
    if obv_divergens:
        score += 18
        signaler.append("🐋 OBV-divergens: aktien på ny kursbotten men OBV INTE — institutioner ackumulerar i smyg")
    elif obv_above_ema and obv_slope_pos:
        score += 8
        signaler.append("📊 OBV ovanför sitt EMA20 med positiv slope — köptrycket ökar")

    # Institutionellt ackumuleringsmönster
    if stark_ackum:
        score += 12
        signaler.append("🐳 Stark institutionell ackumulation (up/down vol-ratio: {:.1f}) — smarta pengar köper".format(ud_vol))
    elif akkumulering:
        score += 7
        signaler.append("📈 Ackumuleringsvolym (up/down vol: {:.1f}) — fler köpare än säljare".format(ud_vol))

    # CMF positiv på svag aktie
    if cmf_stark and not (kurs > e50):
        score += 10
        signaler.append("💰 CMF positiv ({:.2f}) trots att aktie under EMA50 — pengar flödar in".format(cmf_nu))
    elif cmf_positiv:
        score += 4
        signaler.append("💰 CMF positiv ({:.2f})".format(cmf_nu))

    # Volymspike på upp-dag (intresse väcks)
    if vol_ratio >= 3.0 and p1d > 2.0:
        score += 8
        signaler.append("🚀 Volymspike ({:.0f}×) på upp-dag — marknaden uppmärksammar aktien".format(vol_ratio))

    # ── KATEGORI D: Trendbromsar / ADX ──────────────────────────────────────

    # ADX mattas — nedtrenden mister kraft
    if adx_falling and dim_falling and dip_rising:
        score += 15
        signaler.append("🛑 ADX faller ({:.0f}→{:.0f}) + DI- minskar + DI+ ökar — trendkraft mattas i alla tre dimensioner".format(
            adx_10d, adx_nu))
    elif adx_falling and (dim_falling or dip_rising):
        score += 9
        signaler.append("🛑 ADX faller + teckn på trendbyte i DI-linjer")
    elif adx_falling:
        score += 5
        signaler.append("🛑 ADX faller — nedtrendkraft mattas")

    # DI+ korsade DI- (starkt trendbyte-signal)
    if di_crossed:
        score += 12
        signaler.append("✅ DI+ ({:.0f}) > DI- ({:.0f}) — ADX bekräftar att köptrycket nu dominerar".format(dip_nu, dim_nu))
    elif di_cross_soon:
        score += 6
        signaler.append("⏳ DI+ och DI- nära korsning — trendbyte kan vara nära")

    # ── KATEGORI E: Tidighet / Fas-bedömning ────────────────────────────────
    # Belönar aktier som är i TIDIG fas av vändningen

    # Tidig fas: pris under EMA50 men EMA9 vänder — idealt entry
    if kurs < e50 and e9 < e21 and ema9_bottnade:
        score += 20
        signaler.append("🎯 TIDIG FAS: EMA9 bottnar under EMA21 och EMA50 — klassiskt tidigt turnaround-entry")

    # Mellanfas: EMA9 > EMA21 men pris under EMA50
    elif kurs < e50 and e9 > e21:
        score += 14
        signaler.append("🎯 MELLANFAS: EMA9 > EMA21 men pris under EMA50 — turnaround pågår")

    # Bekräftelsesfas: pris bröt EMA50 men under SMA200
    elif e50 <= kurs < sma200:
        score += 8
        signaler.append("🎯 BEKRÄFTELSEFAS: Pris ovan EMA50 men under SMA200 — stark uppgång möjlig")

    # Sen fas: pris nästan tillbaka vid SMA200 — mer risk/reward
    elif kurs >= sma200 * 0.97:
        score += 4
        varningar.append("⚠️ Sen fas — aktie nära SMA200, en stor del av turnaround-rörelsen kan redan ha skett")

    # Poängavdrag / varningar
    if rsi14 > 70:
        score = max(0, score - 15)
        varningar.append("⚠️ RSI {:.0f} — redan överköpt, sämre entry".format(rsi14))

    if p1w > 15:
        score = max(0, score - 10)
        varningar.append("⚠️ +{:.0f}% senaste veckan — risk för kortsiktig utmattning".format(p1w))

    if vol_ratio < 0.5:
        score = max(0, score - 5)
        varningar.append("⚠️ Låg volym ({:.1f}×) — begränsat intresse".format(vol_ratio))

    score = min(100, max(0, round(score)))

    # ═══════════════════════════════════════════════════════════════════════════
    #  MINSTA ANTAL VÄNDNINGSSIGNALER
    # ═══════════════════════════════════════════════════════════════════════════
    if len(signaler) < MIN_TURN_SIGNALS:
        return None

    # ── Bestäm fas-etikett ────────────────────────────────────────────────────
    if kurs < e50 and e9 < e21 and ema9_bottnade:
        fas = "🌱 Tidig"
        fas_color = "#ffd700"
    elif kurs < e50 and e9 > e21:
        fas = "🌿 Mellanfas"
        fas_color = "#40c4ff"
    elif kurs < sma200:
        fas = "🌳 Bekräftad"
        fas_color = "#00e676"
    else:
        fas = "🍂 Sen"
        fas_color = "#f97316"

    # ── Beräkna "vändningsstyrka" (0-10) ─────────────────────────────────────
    vand_styrka = min(10, round(
        (1 if ema9_korsade_21 or ema9_bottnade else 0) +
        (2 if rsi_bull_div else (1 if rsi_was_oversold else 0)) +
        (2 if obv_divergens else (1 if obv_above_ema else 0)) +
        (1 if stark_ackum else 0) +
        (1 if di_crossed else (0.5 if adx_falling else 0)) +
        (1 if stochrsi_cross_up or bb_recovering else 0) +
        (1 if rs_acc > 0 else 0) +
        (1 if cmf_stark else 0)
    ))

    return {
        'Ticker':   ticker,
        'Namn':     _NAMN.get(ticker, ticker),
        'Land':     _LAND.get(ticker, '?'),
        'Sektor':   _SEKTOR.get(ticker, '?'),
        'Typ':      _TYP.get(ticker, 'aktie'),
        'Cap':      _CAP.get(ticker, '?'),
        'Kurs':     round(kurs, 4),
        'Score':    score,
        'Fas':      fas,
        'FasColor': fas_color,
        'VandStyrka': vand_styrka,
        # Historisk svaghet
        'p12m%':    round(p12m, 1),
        'p6m%':     round(p6m,  1),
        'p3m%':     round(p3m,  1),
        'p1w%':     round(p1w,  1),
        'p1d%':     round(p1d,  1),
        # Vändningsindikatorer
        'RSI':      round(rsi14, 1),
        'RSISlope': round(rsi_slope, 1),
        'RSI_bull_div': rsi_bull_div,
        'RSI_was_os':   rsi_was_oversold,
        'StochRSI_cross': stochrsi_cross_up,
        'OBV_div':  obv_divergens,
        'OBV_bull': obv_above_ema and obv_slope_pos,
        'Ackum':    stark_ackum or akkumulering,
        'StarkAckum': stark_ackum,
        'CMF':      round(cmf_nu, 3),
        'CMF_positiv': cmf_positiv,
        'ADX':      round(adx_nu, 1),
        'ADX_fall': adx_falling,
        'DI_plus':  round(dip_nu, 1),
        'DI_minus': round(dim_nu, 1),
        'DI_cross': di_crossed,
        'Squeeze':  '✓' if squeeze_on else '',
        'SqzMom':   round(squeeze_mom, 3),
        'RS':       round(rs, 1),
        'RS_acc':   round(rs_acc, 1),
        'RS_acc_turn': rs_acc_turning,
        'EMA9_cross21': ema9_korsade_21,
        'EMA9_bottom':  ema9_bottnade,
        # Positionsinfo
        'EMA50%':   round((kurs/e50 - 1)*100, 1) if e50 > 0 else 0,
        'SMA200%':  round((kurs/sma200 - 1)*100, 1) if sma200 > 0 else 0,
        'Pos52v%':  round(pos52*100, 1),
        'MaxDD%':   max_dd,
        'ATR%':     atr_pct,
        'ATR_exp':  round(atr_exp, 2),
        'VolRatio': round(vol_ratio, 2),
        'UdVol':    round(ud_vol, 2),
        # Signaler för HTML
        '_signaler':  signaler,
        '_varningar': varningar,
        # Extra för HTML
        'SMA200':   '✓' if kurs >= sma200 else '✗',
        'EMA50':    '✓' if kurs >= e50   else '✗',
    }


# ════════════════════════════════════════════════════════════════════════════
#  BATCH-SCREENER
# ════════════════════════════════════════════════════════════════════════════

def screen_turnaround(ticker_cache: dict, datum, min_vol: float,
                      min_weakness: float) -> list:
    """Kör turnaround-screener på alla tickers."""
    date64  = np.datetime64(datum, 'ns')
    results = []
    n_none = n_blocked = n_err = 0

    for ticker, tc in ticker_cache.items():
        i = int(np.searchsorted(tc.dates, date64, side='right')) - 1
        if i < MIN_HISTORY - 1:
            continue
        try:
            r = compute_turnaround(ticker, tc, i, min_vol, min_weakness)
            if r is not None:
                results.append(r)
        except Exception as ex:
            n_err += 1
            if n_err <= 3:
                print("\n   [DEBUG] {}: {}".format(ticker, ex))

    results.sort(key=lambda x: (x['VandStyrka'], x['Score']), reverse=True)
    return results


# ════════════════════════════════════════════════════════════════════════════
#  HTML-RAPPORT  (samma design som Master Screener)
# ════════════════════════════════════════════════════════════════════════════

def _sparkline(vals, w=120, h=40):
    if not vals or len(vals) < 2: return ''
    mn, mx = min(vals), max(vals)
    rng = mx - mn or 1
    pts = []
    for idx, v in enumerate(vals):
        x = round(2 + idx / (len(vals)-1) * (w-4), 1)
        y = round(2 + (1-(v-mn)/rng) * (h-4), 1)
        pts.append('{},{}'.format(x,y))
    clr = '#00e676' if vals[-1] >= vals[0] else '#ef5350'
    lp = (vals[-1]/vals[0]-1)*100 if vals[0] else 0
    return ('<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            '<rect width="{w}" height="{h}" fill="#0d1117" rx="3"/>'
            '<polyline points="{p}" fill="none" stroke="{c}" stroke-width="1.5"/>'
            '<text x="{tx}" y="{ty}" text-anchor="end" fill="{c}" '
            'font-size="9" font-weight="bold">{s}{lp:.1f}%</text></svg>').format(
        w=w, h=h, p=' '.join(pts), c=clr,
        tx=w-2, ty=h-2, s='+' if lp>=0 else '', lp=lp)


def generate_html(results: list, regime_info: dict, datum_str: str,
                  out_folder: str) -> str:
    import json as _json

    os.makedirs(out_folder, exist_ok=True)
    ts    = datetime.now().strftime('%Y%m%d_%H%M')
    fname = 'turnaround_{}_{}.html'.format(datum_str.replace('-',''), ts)
    path  = os.path.join(out_folder, fname)

    reg_fas   = regime_info.get('fas',    'Osäker')
    reg_enkel = regime_info.get('enkel',  'Osäker')
    reg_avk1m = regime_info.get('avk_1m', 0.0)
    reg_avk3m = regime_info.get('avk_3m', 0.0)
    reg_color = ('#00e676' if 'Bull' in str(reg_enkel)
                 else '#ef5350' if 'Bear' in str(reg_enkel) else '#ffd700')
    risk_on   = 'Bull' in str(reg_enkel)

    # Statistik
    n_total  = len(results)
    n_tidig  = sum(1 for r in results if 'Tidig' in r.get('Fas',''))
    n_mellan = sum(1 for r in results if 'Mellanfas' in r.get('Fas',''))
    n_bekr   = sum(1 for r in results if 'Bekräftad' in r.get('Fas',''))
    best     = results[0]['Ticker'] if results else '–'

    # Sektorer
    sek_dist = {}
    for r in results:
        s = r.get('Sektor','?')
        sek_dist[s] = sek_dist.get(s,0)+1
    top_sek = sorted(sek_dist.items(), key=lambda x:-x[1])[:8]

    cap_dist = {}
    for r in results:
        cap_dist[r.get('Cap','?')] = cap_dist.get(r.get('Cap','?'),0)+1

    # Bygg assets_json
    cap_cols = {'micro':'#ff6b6b','small':'#ffd700','mid':'#40c4ff',
                'large':'#8b949e','mega':'#6e7681','?':'#555'}

    assets_json = []
    for r in results:
        sc = r['Score']
        sig = ('STARK VÄNDNING' if sc>=70 else
               'VÄNDNING'      if sc>=50 else
               'TIDIG SIGNAL'  if sc>=30 else 'BEVAKA')
        sig_clr = ('#00e676' if sc>=70 else
                   '#40c4ff' if sc>=50 else
                   '#ffd700' if sc>=30 else '#8b949e')

        icons = []
        if r.get('RSI_bull_div'):   icons.append('⚡')
        if r.get('OBV_div'):        icons.append('🐋')
        if r.get('StarkAckum'):     icons.append('🏦')
        if r.get('DI_cross'):       icons.append('✅')
        if r.get('EMA9_cross21'):   icons.append('📊')
        if r.get('Squeeze')=='✓':   icons.append('🗜️')
        if r.get('RS_acc',0) > 0:   icons.append('🚀')

        spark_vals = [
            r.get('p12m%',0)*0.1, r.get('p6m%',0)*0.2,
            r.get('p3m%',0)*0.5,  r.get('p1w%',0)*2.0,
            r.get('p1d%',0)*5.0,
        ]

        ema50_pct  = r.get('EMA50%', 0)
        sma200_pct = r.get('SMA200%', 0)

        fas_txt = r['Fas'].replace('🌱','').replace('🌿','').replace('🌳','').replace('🍂','').strip()
        ai_text = ('{} ({}) handlas till {:.2f}. Turnaround-score: {}/100. '
                   'Fas: {}. 12m ned {:.0f}%, 6m ned {:.0f}%. '
                   'EMA50: {:+.1f}%, SMA200: {:+.1f}%. '
                   'Vändningsstyrka: {}/10 signaler.').format(
            r['Ticker'], r.get('Namn',''), r['Kurs'], sc,
            fas_txt,
            abs(r.get('p12m%',0)), abs(r.get('p6m%',0)),
            ema50_pct, sma200_pct, r.get('VandStyrka',0))

        assets_json.append({
            'ticker':   r['Ticker'],
            'name':     r.get('Namn', r['Ticker'])[:28],
            'land':     r.get('Land','?'),
            'sektor':   r.get('Sektor','?')[:20],
            'typ':      r.get('Typ','aktie'),
            'cap':      r.get('Cap','?'),
            'price':    round(r['Kurs'],4),
            'score':    sc,
            'fas':      r.get('Fas','?'),
            'fas_color':r.get('FasColor','#8b949e'),
            'vand_styrka': r.get('VandStyrka',0),
            'signal':   sig,
            'signal_clr': sig_clr,
            'icons':    ' '.join(icons),
            'spark_svg':_sparkline(spark_vals),
            'ai_text':  ai_text,
            # Historik
            'p12m':  r.get('p12m%',0),
            'p6m':   r.get('p6m%',0),
            'p3m':   r.get('p3m%',0),
            'p1w':   r.get('p1w%',0),
            'p1d':   r.get('p1d%',0),
            # Vändningsindikatorer
            'rsi':   r.get('RSI',50),
            'rsi_slope': r.get('RSISlope',0),
            'rsi_div':   r.get('RSI_bull_div',False),
            'rsi_was_os':r.get('RSI_was_os',False),
            'srsi_cross':r.get('StochRSI_cross',False),
            'obv_div':   r.get('OBV_div',False),
            'obv_bull':  r.get('OBV_bull',False),
            'ackum':     r.get('Ackum',False),
            'stark_ackum':r.get('StarkAckum',False),
            'cmf':   r.get('CMF',0),
            'adx':   r.get('ADX',20),
            'adx_fall': r.get('ADX_fall',False),
            'di_plus':  r.get('DI_plus',25),
            'di_minus': r.get('DI_minus',25),
            'di_cross': r.get('DI_cross',False),
            'squeeze':  r.get('Squeeze','')=='✓',
            'rs':    r.get('RS',0),
            'rs_acc':r.get('RS_acc',0),
            'rs_acc_turn': r.get('RS_acc_turn',False),
            'ema9_cross': r.get('EMA9_cross21',False),
            'ema9_bot':   r.get('EMA9_bottom',False),
            # Positionering
            'ema50_pct':  r.get('EMA50%',0),
            'sma200_pct': r.get('SMA200%',0),
            'pos52w':     r.get('Pos52v%',50)/100,
            'max_dd':     r.get('MaxDD%',0),
            'atr_pct':    r.get('ATR%',2),
            'atr_exp':    r.get('ATR_exp',1),
            'vol_ratio':  r.get('VolRatio',1),
            'ud_vol':     r.get('UdVol',1),
            'above_sma200': r.get('SMA200','✗')=='✓',
            'above_ema50':  r.get('EMA50','✗')=='✓',
            # Signaler för modal
            'signaler':  r.get('_signaler',[]),
            'varningar': r.get('_varningar',[]),
        })

    # ── Bygg kort-HTML ────────────────────────────────────────────────────────
    def card_html(a):
        sc   = a['score']
        clr  = a['signal_clr']
        cc   = cap_cols.get(a['cap'],'#555')
        fc   = a['fas_color']
        p6_c = '#00e676' if a['p6m']>=0 else '#ef5350'
        p3_c = '#00e676' if a['p3m']>=0 else '#ef5350'
        return (
            '<div class="asset-card" data-ticker="{t}" data-fas="{fas_raw}" '
            'onclick="openModal(\'{t}\')" style="border-left:3px solid {clr}">'
            '<div style="background:{fc}18;border-bottom:1px solid {fc}33;'
            'margin:-12px -12px 8px -12px;padding:4px 10px;border-radius:10px 10px 0 0;'
            'display:flex;justify-content:space-between;align-items:center">'
            '<span style="color:{fc};font-size:10px;font-weight:bold">{fas}</span>'
            '<span style="color:{fc};font-size:11px;font-weight:bold">{vs}/10 🔔</span>'
            '</div>'
            '<div class="card-top"><div>'
            '<div class="card-ticker">{t}'
            '<span style="font-size:9px;padding:1px 5px;border-radius:3px;'
            'background:{cc}22;color:{cc};border:1px solid {cc}55;margin-left:4px">'
            '{cap}</span></div>'
            '<div class="card-name">{nm}</div>'
            '<div style="font-size:9px;color:var(--muted)">{land} · {sek}</div>'
            '</div>'
            '<div style="text-align:right">'
            '<div class="card-score" style="color:{clr}">{sc}</div>'
            '<div style="font-size:10px;color:{clr}">{sig}</div>'
            '</div></div>'
            '<div class="card-rets">'
            '<div><span style="color:var(--muted);font-size:9px">6m</span>'
            ' <span style="color:{p6_c};font-weight:bold">{p6m:+.0f}%</span></div>'
            '<div><span style="color:var(--muted);font-size:9px">3m</span>'
            ' <span style="color:{p3_c};font-weight:bold">{p3m:+.0f}%</span></div>'
            '<div><span style="color:var(--muted);font-size:9px">RSI</span>'
            ' <span style="color:white">{rsi:.0f}</span></div>'
            '<div><span style="color:var(--muted);font-size:9px">ADX</span>'
            ' <span style="color:white">{adx:.0f}</span></div>'
            '</div>'
            '<div class="signal-icons">{icons}</div>'
            '<div class="score-bar">'
            '<div style="background:{clr};width:{bar}%;height:3px;'
            'border-radius:2px;transition:width .3s"></div></div>'
            '</div>'
        ).format(
            t=a['ticker'], fas_raw=a['fas'], fas=a['fas'], fc=fc, vs=a['vand_styrka'],
            clr=clr, cc=cc, cap=(a['cap'] or '?').upper(),
            nm=a['name'][:22], land=a['land'], sek=a['sektor'][:14],
            sc=sc, sig=a['signal'],
            p6m=a['p6m'], p6_c=p6_c, p3m=a['p3m'], p3_c=p3_c,
            rsi=a['rsi'], adx=a['adx'],
            icons=a['icons'], bar=min(100,sc),
        )

    cards_html = ''.join(card_html(a) for a in assets_json)

    # Sektor-rader
    sector_rows = ''.join(
        '<div style="display:flex;justify-content:space-between;margin:3px 0">'
        '<span>{}</span><span style="background:#21262d;padding:1px 8px;'
        'border-radius:10px;color:var(--blue)">{}</span></div>'.format(s[:20],n)
        for s,n in top_sek)
    cap_labels = [('micro','#ff6b6b'),('small','#ffd700'),('mid','#40c4ff'),
                  ('large','#8b949e'),('mega','#6e7681')]
    cap_dist_html = ''.join(
        '<span class="cap-d" style="background:{c}22;color:{c}">{k}: {n}</span>'.format(
            c=c, k=k.upper(), n=cap_dist.get(k,0)) for k,c in cap_labels)

    m1c = '#00e676' if reg_avk1m>=0 else '#ef5350'
    m3c = '#00e676' if reg_avk3m>=0 else '#ef5350'

    # ── Sätt ihop HTML ────────────────────────────────────────────────────────
    parts = []
    parts.append('''<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Turnaround Screener v1.0 - {ts}</title>
<style>
:root{{--bg:#0d1117;--panel:#161b22;--border:#21262d;--text:#c9d1d9;
      --green:#00e676;--blue:#40c4ff;--gold:#ffd700;--orange:#f97316;
      --purple:#a78bfa;--red:#ef5350;--muted:#8b949e}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',Arial,sans-serif;
     font-size:13px;line-height:1.5}}
.top-bar{{background:#010409;border-bottom:1px solid var(--border);padding:10px 20px;
          display:flex;align-items:center;justify-content:space-between;
          position:sticky;top:0;z-index:100}}
.logo{{color:var(--gold);font-weight:bold;font-size:16px}}
.version{{color:var(--muted);font-size:11px}}
.main{{max-width:1600px;margin:0 auto;padding:16px 12px}}
.section{{background:var(--panel);border:1px solid var(--border);border-radius:12px;
          padding:16px;margin-bottom:16px}}
.section-title{{color:var(--gold);font-weight:bold;font-size:14px;margin-bottom:12px;
                display:flex;align-items:center;gap:8px}}
.macro-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.stat-row{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px}}
.stat{{background:#0d1117;border:1px solid var(--border);border-radius:8px;
       padding:8px 14px;text-align:center}}
.stat-val{{font-size:20px;font-weight:bold}}
.stat-lbl{{font-size:9px;color:var(--muted);margin-top:1px}}
.cap-dist{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}}
.cap-d{{font-size:11px;padding:3px 8px;border-radius:5px}}
.cards-filter{{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}}
.filter-btn{{background:#21262d;border:1px solid #30363d;border-radius:6px;
             padding:5px 12px;cursor:pointer;font-size:11px;color:var(--text);
             transition:all .15s}}
.filter-btn:hover,.filter-btn.active{{background:#388bfd22;border-color:#388bfd;color:#79c0ff}}
.cards-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}}
.asset-card{{background:#0d1117;border:1px solid var(--border);border-radius:10px;
             padding:12px;cursor:pointer;transition:all .2s}}
.asset-card:hover{{border-color:#30363d;transform:translateY(-1px);background:#161b22}}
.card-top{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px}}
.card-ticker{{font-weight:bold;font-size:13px;color:white}}
.card-name{{font-size:10px;color:var(--muted);margin-top:1px}}
.card-score{{font-size:24px;font-weight:bold}}
.card-rets{{display:flex;gap:10px;margin-bottom:6px}}
.signal-icons{{font-size:14px;letter-spacing:2px;min-height:18px;margin-bottom:4px}}
.score-bar{{background:#21262d;height:3px;border-radius:2px}}
.modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);
                z-index:1000;overflow-y:auto;padding:20px}}
.modal{{background:#161b22;border:1px solid #30363d;border-radius:14px;
        max-width:820px;margin:0 auto;overflow:hidden}}
.modal-header{{background:#010409;padding:14px 18px;display:flex;
               justify-content:space-between;align-items:center;
               border-bottom:1px solid #21262d}}
.modal-close{{background:none;border:none;color:#8b949e;font-size:20px;
              cursor:pointer;padding:4px 8px}}
.modal-close:hover{{color:white}}
.modal-body{{padding:16px}}
.modal-tabs{{display:flex;gap:0;border-bottom:1px solid #21262d;margin-bottom:14px}}
.mtab{{padding:8px 16px;cursor:pointer;font-size:12px;color:#8b949e;border:none;
       background:none;border-bottom:2px solid transparent;transition:all .15s}}
.mtab.active{{color:white;border-bottom-color:var(--gold)}}
.mpanel{{display:none}}.mpanel.active{{display:block}}
.ai-text{{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:14px;
          font-size:12px;line-height:1.7;color:#c9d1d9;white-space:pre-wrap}}
.ind-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:6px}}
.ind-item{{background:#0d1117;border-radius:6px;padding:6px 8px}}
.signal-list{{background:#0d1117;border-radius:8px;padding:12px;font-size:12px;line-height:2}}
.sl-tp-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}
.sl-box{{background:#0d1117;border-radius:8px;padding:12px}}
.sl-title{{font-size:11px;font-weight:bold;color:var(--muted);text-transform:uppercase;
           letter-spacing:.5px;margin-bottom:8px}}
.sl-row{{display:flex;justify-content:space-between;padding:4px 0;
          border-bottom:1px solid #21262d;font-size:12px}}
.sl-row:last-child{{border-bottom:none}}
@media(max-width:600px){{.cards-grid{{grid-template-columns:1fr 1fr}}
  .sl-tp-grid,.macro-grid{{grid-template-columns:1fr}}}}
footer{{text-align:center;padding:20px;color:#555;font-size:11px;
        border-top:1px solid #21262d}}
</style>
</head>
<body>
'''.format(ts=ts))

    # Top-bar
    parts.append('<div class="top-bar">')
    parts.append('<div><div class="logo">🔄 TURNAROUND SCREENER v1.0</div>')
    parts.append('<div class="version">Vändpunktssignaler · Sverige + Europa · {}</div></div>'.format(datum_str))
    parts.append('<div style="color:var(--muted);font-size:11px">Ej investeringsrådgivning</div>')
    parts.append('</div>\n<div class="main">\n')

    # Marknadsbild
    parts.append('<div class="section"><div class="section-title">🌍 MARKNADSBILD</div>')
    parts.append('<div class="macro-grid"><div>')
    parts.append('<div style="color:var(--muted);font-size:11px">Marknadsfas: <span style="color:{}">{}</span></div>'.format(reg_color, reg_fas))
    parts.append('<div style="color:var(--muted);font-size:11px">Regim: {}</div>'.format(reg_enkel))
    parts.append('<div style="color:var(--muted);font-size:11px">1m: <span style="color:{}">{:+.1f}%</span>  ·  3m: <span style="color:{}">{:+.1f}%</span></div>'.format(m1c,reg_avk1m,m3c,reg_avk3m))
    parts.append('<div style="color:var(--muted);font-size:11px">Risk-On: {}</div>'.format('✅' if risk_on else '❌'))
    parts.append('<div style="font-size:11px;color:#f97316;margin-top:6px">💡 I bear-regim: fokusera på Tidig-fas och kräv stark ackumulation</div>')
    parts.append('</div><div>')
    parts.append('<div style="font-size:11px;color:var(--muted);margin-bottom:6px">Topp sektorer (turnarounds):</div>')
    parts.append(sector_rows)
    parts.append('</div></div></div>\n')

    # Stats + kortgrid
    parts.append('<div class="section"><div class="section-title">🔄 {} TURNAROUND-KANDIDATER</div>'.format(n_total))
    parts.append('<div class="stat-row">')
    parts.append('<div class="stat"><div class="stat-val" style="color:#ffd700">{}</div><div class="stat-lbl">🌱 Tidig fas</div></div>'.format(n_tidig))
    parts.append('<div class="stat"><div class="stat-val" style="color:#40c4ff">{}</div><div class="stat-lbl">🌿 Mellanfas</div></div>'.format(n_mellan))
    parts.append('<div class="stat"><div class="stat-val" style="color:#00e676">{}</div><div class="stat-lbl">🌳 Bekräftad</div></div>'.format(n_bekr))
    parts.append('<div class="stat"><div class="stat-val" style="color:var(--gold)">{}</div><div class="stat-lbl">Bäst just nu</div></div>'.format(best))
    parts.append('</div>')
    parts.append('<div class="cap-dist"><span style="font-size:11px;color:var(--muted);align-self:center">Storlek:</span>')
    parts.append(cap_dist_html)
    parts.append('</div>')
    # Filterknappar
    parts.append('<div class="cards-filter">')
    for fid, flbl, active in [
        ('all',      'Alla',          True),
        ('Tidig',    '🌱 Tidig fas',  False),
        ('Mellan',   '🌿 Mellanfas',  False),
        ('Bekräftad','🌳 Bekräftad',  False),
        ('Sen',      '🍂 Sen fas',    False),
        ('obv_div',  '🐋 OBV-div',   False),
        ('rsi_div',  '⚡ RSI-div',    False),
        ('di_cross', '✅ DI-korsning',False),
        ('aktie',    'Aktier',        False),
    ]:
        parts.append('<button class="filter-btn{}" onclick="filterCards(\'{}\',this)">{}</button>'.format(
            ' active' if active else '', fid, flbl))
    parts.append('</div>')
    parts.append('<div class="cards-grid" id="cards-grid">')
    parts.append(cards_html)
    parts.append('</div></div>\n')

    # Modal
    parts.append('''
<div class="modal-overlay" id="modal-overlay" onclick="if(event.target===this)closeModal()">
<div class="modal">
  <div class="modal-header">
    <div>
      <span id="modal-ticker" style="font-weight:bold;font-size:16px;color:white"></span>
      <span id="modal-fas-badge" style="margin-left:8px"></span>
      <span id="modal-name" style="color:var(--muted);font-size:12px;margin-left:8px"></span>
    </div>
    <div style="display:flex;align-items:center;gap:12px">
      <span id="modal-price" style="font-size:14px;color:white;font-weight:bold"></span>
      <span id="modal-signal" style="font-size:12px;font-weight:bold"></span>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
  </div>
  <div class="modal-body">
    <div id="modal-rets" style="display:flex;gap:16px;margin-bottom:12px;flex-wrap:wrap"></div>
    <div id="modal-chart" style="margin-bottom:12px"></div>
    <div class="modal-tabs">
      <button class="mtab active" onclick="showMTab(\'mp-analys\',this)">📝 Turnaround-analys</button>
      <button class="mtab" onclick="showMTab(\'mp-signaler\',this)">⚡ Signaler</button>
      <button class="mtab" onclick="showMTab(\'mp-inds\',this)">🔬 Indikatorer</button>
      <button class="mtab" onclick="showMTab(\'mp-position\',this)">📐 Positionering</button>
    </div>
    <div id="mp-analys" class="mpanel active">
      <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:#8b949e;margin-bottom:8px">
        🤖 Automatisk turnaround-analys
      </div>
      <div class="ai-text" id="modal-ai-text"></div>
      <div id="modal-vand-styrka" style="margin-top:12px"></div>
    </div>
    <div id="mp-signaler" class="mpanel">
      <div class="signal-list" id="modal-signaler-list"></div>
      <div style="background:#ef535011;border:1px solid #ef535033;border-radius:8px;
                  padding:12px;margin-top:10px">
        <div style="color:var(--red);font-size:11px;margin-bottom:6px">⚠️ VARNINGAR</div>
        <div id="modal-varningar" style="font-size:12px;line-height:2;color:var(--muted)"></div>
      </div>
    </div>
    <div id="mp-inds" class="mpanel">
      <div class="ind-grid" id="modal-inds"></div>
    </div>
    <div id="mp-position" class="mpanel">
      <div id="modal-pos-content"></div>
    </div>
  </div>
</div>
</div>
''')

    parts.append('<footer>Turnaround Screener v1.0 · Genererad {} · Ej investeringsrådgivning</footer>'.format(
        datetime.now().strftime('%Y-%m-%d %H:%M')))
    parts.append('</div>\n')

    # JavaScript
    parts.append('<script>\nconst ALL_ASSETS=')
    parts.append(_json.dumps(assets_json, ensure_ascii=False,
                              default=lambda o: o.item() if hasattr(o, 'item') else str(o)))
    parts.append(';\n')

    parts.append('''
function openModal(ticker){
  const a=ALL_ASSETS.find(x=>x.ticker===ticker);
  if(!a)return;

  document.getElementById('modal-ticker').textContent=a.ticker;
  document.getElementById('modal-name').textContent=a.name;
  document.getElementById('modal-price').textContent=a.price.toFixed(2);
  document.getElementById('modal-signal').textContent=a.signal;
  document.getElementById('modal-signal').style.color=a.signal_clr;

  // Fas-badge
  document.getElementById('modal-fas-badge').innerHTML=
    `<span style="font-size:11px;padding:2px 8px;border-radius:10px;background:${a.fas_color}22;color:${a.fas_color};border:1px solid ${a.fas_color}55">${a.fas}</span>`;

  // Avkastningsrad (historisk svaghet)
  document.getElementById('modal-rets').innerHTML=
    [['1d',a.p1d],['1v',a.p1w],['3m',a.p3m],['6m',a.p6m],['12m',a.p12m]].map(([l,v])=>{
      const c=v>=0?'#00e676':'#ef5350';
      return `<span><span style="color:#8b949e;font-size:10px">${l} </span><span style="color:${c};font-weight:bold">${v>=0?'+':''}${v.toFixed(1)}%</span></span>`;
    }).join('');

  // Sparkline
  document.getElementById('modal-chart').innerHTML=a.spark_svg||'';

  // AI-text
  document.getElementById('modal-ai-text').textContent=a.ai_text;

  // Vändningsstyrka-bar
  const vsw=a.vand_styrka*10;
  const vsc=vsw>=70?'#00e676':vsw>=40?'#ffd700':'#f97316';
  document.getElementById('modal-vand-styrka').innerHTML=`
    <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
      <span style="font-size:11px;color:#8b949e;white-space:nowrap">Vändningsstyrka:</span>
      <div style="flex:1;background:#21262d;border-radius:4px;height:8px">
        <div style="background:${vsc};width:${vsw}%;height:100%;border-radius:4px;transition:width .5s"></div>
      </div>
      <span style="color:${vsc};font-weight:bold;font-size:14px">${a.vand_styrka}/10</span>
    </div>`;

  // Signaler
  const sEl=document.getElementById('modal-signaler-list');
  if(a.signaler&&a.signaler.length){
    sEl.innerHTML='<div style="color:#ffd700;font-size:11px;margin-bottom:8px">✅ AKTIVA VÄNDNINGSSIGNALER</div>'+
      a.signaler.map(s=>`<div style="color:#c9d1d9;margin-bottom:4px;font-size:12px">• ${s}</div>`).join('');
  } else {
    sEl.innerHTML='<div style="color:#8b949e">Inga starka signaler detekterade.</div>';
  }
  const vEl=document.getElementById('modal-varningar');
  vEl.innerHTML=a.varningar&&a.varningar.length
    ?a.varningar.map(v=>`<div>${v}</div>`).join('')
    :'<div style="color:#8b949e">Inga varningar.</div>';

  // Indikatorer
  const boolBadge=(v,yes,no)=>v
    ?`<span style="color:#00e676;font-size:10px">✓ ${yes}</span>`
    :`<span style="color:#ef5350;font-size:10px">✗ ${no}</span>`;
  const rsiC=a.rsi<30?'#ef5350':a.rsi<45?'#ffd700':a.rsi<=65?'#00e676':'#f97316';
  const adxC=a.adx<20?'#8b949e':a.adx<30?'#ffd700':'#00e676';
  const diC=a.di_plus>a.di_minus?'#00e676':'#ef5350';
  const cmfC=a.cmf>0.1?'#00e676':a.cmf>0?'#ffd700':'#ef5350';
  document.getElementById('modal-inds').innerHTML=[
    ['RSI 14',    a.rsi.toFixed(0),       rsiC],
    ['RSI Slope', (a.rsi_slope>=0?'+':'')+a.rsi_slope.toFixed(1), a.rsi_slope>0?'#00e676':'#ef5350'],
    ['ADX',       a.adx.toFixed(0),        adxC],
    ['DI+',       a.di_plus.toFixed(0),    a.di_plus>a.di_minus?'#00e676':'#8b949e'],
    ['DI-',       a.di_minus.toFixed(0),   a.di_minus>a.di_plus?'#ef5350':'#8b949e'],
    ['CMF',       (a.cmf>=0?'+':'')+a.cmf.toFixed(2), cmfC],
    ['Up/Dn Vol', a.ud_vol.toFixed(1)+'×', a.ud_vol>1.5?'#00e676':a.ud_vol>1?'#ffd700':'#ef5350'],
    ['Vol-ratio', a.vol_ratio.toFixed(1)+'×', a.vol_ratio>=2?'#00e676':'#8b949e'],
    ['RS bench',  (a.rs>=0?'+':'')+a.rs.toFixed(1),   a.rs>0?'#00e676':'#ef5350'],
    ['RS-acc',    (a.rs_acc>=0?'+':'')+a.rs_acc.toFixed(1), a.rs_acc>0?'#00e676':'white'],
    ['ATR%',      a.atr_pct.toFixed(1)+'%','white'],
    ['ATR-exp',   a.atr_exp.toFixed(2),    a.atr_exp>1.2?'#00e676':'white'],
    ['Squeeze',   a.squeeze?'🗜️ ON':'OFF',  a.squeeze?'#f97316':'#8b949e'],
    ['OBV-div',   a.obv_div?'✓ JA':'✗ Nej', a.obv_div?'#00e676':'#8b949e'],
    ['RSI-div',   a.rsi_div?'✓ JA':'✗ Nej', a.rsi_div?'#00e676':'#8b949e'],
    ['StochRSI ↑',a.srsi_cross?'✓ JA':'✗ Nej',a.srsi_cross?'#00e676':'#8b949e'],
  ].map(([l,v,c])=>`<div class="ind-item"><div style="color:#8b949e;font-size:9px">${l}</div>
    <div style="color:${c};font-weight:bold;font-size:12px">${v}</div></div>`).join('');

  // Positionering
  const e50c  = a.ema50_pct>=0?'#00e676':'#ef5350';
  const s200c = a.sma200_pct>=0?'#00e676':'#ef5350';
  const pos52w= Math.round(a.pos52w*100);
  document.getElementById('modal-pos-content').innerHTML=`
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px">
      <div class="stat"><div class="stat-val" style="color:${e50c}">${a.ema50_pct>=0?'+':''}${a.ema50_pct.toFixed(1)}%</div>
        <div class="stat-lbl">vs EMA50</div></div>
      <div class="stat"><div class="stat-val" style="color:${s200c}">${a.sma200_pct>=0?'+':''}${a.sma200_pct.toFixed(1)}%</div>
        <div class="stat-lbl">vs SMA200</div></div>
      <div class="stat"><div class="stat-val" style="color:white">${pos52w}%</div>
        <div class="stat-lbl">52v-position</div></div>
      <div class="stat"><div class="stat-val" style="color:${a.max_dd<-20?'#ef5350':'#ffd700'}">${a.max_dd.toFixed(0)}%</div>
        <div class="stat-lbl">Max drawdown 120d</div></div>
    </div>
    <div style="background:#0d1117;border-radius:8px;padding:12px;margin-bottom:10px">
      <div style="font-size:11px;color:#8b949e;margin-bottom:8px">📐 POSITIONSPROFIL</div>
      <div style="font-size:12px;color:#c9d1d9;line-height:2">
        <div>EMA50:  <b style="color:${a.above_ema50?'#00e676':'#ef5350'}">${a.above_ema50?'✓ Pris ovan EMA50':'✗ Pris under EMA50 (turnaround-läge)'}</b></div>
        <div>SMA200: <b style="color:${a.above_sma200?'#00e676':'#ef5350'}">${a.above_sma200?'✓ Pris ovan SMA200':'✗ Pris under SMA200 (recovery möjlig)'}</b></div>
        <div>DI+/DI-: <b style="color:${a.di_plus>a.di_minus?'#00e676':'#ef5350'}">${a.di_plus>a.di_minus?'✓ DI+ > DI- (köptryck dominerar)':'✗ DI- > DI+ (säljtryck dominerar)'}</b></div>
        <div>ADX-trend: <b style="color:${a.adx_fall?'#00e676':'#8b949e'}">${a.adx_fall?'✓ ADX faller (nedtrendkraft mattas)':'ADX stabilt/stigande'}</b></div>
      </div>
    </div>
    <div style="background:#0d1117;border-radius:8px;padding:12px;font-size:11px;color:#8b949e">
      <b style="color:#ffd700">⚠️ Stop-loss-princip för turnarounds:</b><br>
      Tidig fas: stopp ${(a.price*(1-a.atr_pct*2/100)).toFixed(2)} (−${(a.atr_pct*2).toFixed(1)}% / 2×ATR)<br>
      Om inga nya signaler inom 10 dagar → exit manuellt
    </div>`;

  document.getElementById('modal-overlay').style.display='block';
  document.body.style.overflow='hidden';
  document.querySelectorAll('.mpanel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.mtab').forEach(b=>b.classList.remove('active'));
  document.getElementById('mp-analys').classList.add('active');
  document.querySelector('.mtab').classList.add('active');
}

function closeModal(){
  document.getElementById('modal-overlay').style.display='none';
  document.body.style.overflow='';
}
function showMTab(id,btn){
  document.querySelectorAll('.mpanel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.mtab').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}
function filterCards(filter,btn){
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('#cards-grid .asset-card').forEach(card=>{
    const t=card.dataset.ticker;
    const a=ALL_ASSETS.find(x=>x.ticker===t);
    let show=true;
    if(filter==='all')         show=true;
    else if(filter==='Tidig')  show=a&&a.fas.includes('Tidig');
    else if(filter==='Mellan') show=a&&a.fas.includes('Mellanfas');
    else if(filter==='Bekräftad') show=a&&a.fas.includes('Bekräftad');
    else if(filter==='Sen')    show=a&&a.fas.includes('Sen');
    else if(filter==='obv_div') show=a&&a.obv_div;
    else if(filter==='rsi_div') show=a&&a.rsi_div;
    else if(filter==='di_cross') show=a&&a.di_cross;
    else if(filter==='aktie')  show=a&&a.typ==='aktie';
    card.style.display=show?'':'none';
  });
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});
</script>
</body></html>
''')

    with open(path,'w',encoding='utf-8') as f:
        f.write(''.join(parts))
    return path


# ════════════════════════════════════════════════════════════════════════════
#  HUVUD
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='Turnaround Screener — vändpunktssignaler Sverige + Europa',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exempel:
  python turnaround_screener.py
  python turnaround_screener.py --topn 20
  python turnaround_screener.py --land SE NO DK FI
  python turnaround_screener.py --min-svaghet 25   # kräv minst -25% på 12m
  python turnaround_screener.py --min-score 50
  python turnaround_screener.py --ingen-csv
        """)

    parser.add_argument('--topn',         type=int,   default=TOP_N_DEFAULT)
    parser.add_argument('--land',         type=str,   nargs='+', default=None)
    parser.add_argument('--min-vol',      type=float, default=MIN_VOL_SEK)
    parser.add_argument('--min-svaghet',  type=float, default=abs(MIN_WEAKNESS_P12M),
                        help='Min nedgång på 12m i %% (positiv siffra, default: 15)')
    parser.add_argument('--min-score',    type=int,   default=20)
    parser.add_argument('--max-tickers',  type=int,   default=None)
    parser.add_argument('--ingen-csv',    action='store_true')
    args = parser.parse_args()

    datum     = pd.Timestamp.today().normalize()
    datum_str = str(datum)[:10]
    min_weakness = -abs(args.min_svaghet)   # gör om till negativt tal

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    W = 72
    print("╔" + "═"*W + "╗")
    print(("║  TURNAROUND SCREENER  v1.0  —  {}".format(datum_str)).ljust(W+1) + "║")
    print(("║  Letar vändpunkter: svaghet≤{}% på 12m  ·  min-score: {}".format(
        abs(min_weakness), args.min_score)).ljust(W+1) + "║")
    print(("║  Faser: 🌱 Tidig · 🌿 Mellanfas · 🌳 Bekräftad · 🍂 Sen").ljust(W+1) + "║")
    print("╚" + "═"*W + "╝")

    # Tickers
    tickers = [u[0] for u in UNIVERSE
               if not args.land or u[2].upper() in [l.upper() for l in args.land]]
    if args.max_tickers:
        tickers = tickers[:args.max_tickers]
    print("\n[INFO] {} tickers att screena".format(len(tickers)))

    # Hämta historik
    all_hist, bench_close = fetch_all_history(tickers, datum_str, datum_str)
    if not all_hist:
        print("[FEL] Ingen data hämtades!")
        sys.exit(1)

    # Regim
    regime = RegimDetalj()
    if bench_close is not None and len(bench_close) >= 210:
        regime = compute_regime(bench_close, bench_close.index[-1])
        print("\n[REGIM] {} — {} (1m: {:+.1f}%  3m: {:+.1f}%)".format(
            regime.fas, regime.enkel.value, regime.avk_1m, regime.avk_3m))

    # Förberäkning
    print("\n[INFO] Förberäknar indikatorer...")
    ticker_cache = precompute_all(all_hist, bench_close)

    # Screena
    print("[INFO] Kör turnaround-screener för {}...".format(datum_str))
    all_results = screen_turnaround(ticker_cache, datum, args.min_vol, min_weakness)
    print("[INFO] {} turnaround-kandidater hittade".format(len(all_results)))

    # Filtrera på min-score
    filtered = [r for r in all_results if r['Score'] >= args.min_score]
    print("[INFO] {} kandidater efter score-filter (≥{})".format(len(filtered), args.min_score))

    # Terminalutskrift
    top = filtered[:args.topn]
    W2 = 140
    print("\n" + "═"*W2)
    print("  TOPP {:d}  TURNAROUND-KANDIDATER  —  {}".format(len(top), datum_str))
    print("═"*W2)
    hdr = "  {:>4}  {:<12}{:<22}{:<4}{:<10}{:>6}{:>7}{:>7}{:>7}{:>7}{:>5}{:>7}{:>7}{:>9}".format(
        "#","Ticker","Namn","Land","Fas","Score","VS","p12m%","p6m%","EMA50%","RSI","ADX","CMF","VolRatio")
    print(hdr)
    print("  " + "─"*(len(hdr)-2))
    for rank, r in enumerate(top, 1):
        print("  {:>4}  {:<12}{:<22}{:<4}{:<10}{:>6}{:>7}{:>7.1f}{:>7.1f}{:>7.1f}{:>5.0f}{:>7.1f}{:>7.3f}{:>9.1f}".format(
            rank,
            r['Ticker'][:12], r.get('Namn','')[:22], r.get('Land','?')[:4],
            r['Fas'].split()[1][:10] if len(r['Fas'].split())>1 else r['Fas'][:10],
            r['Score'], r['VandStyrka'],
            r.get('p12m%',0), r.get('p6m%',0), r.get('EMA50%',0),
            r.get('RSI',50), r.get('ADX',20), r.get('CMF',0),
            r.get('VolRatio',1),
        ))

    # Regim-info för HTML
    regime_info = {
        'fas':    regime.fas,
        'enkel':  regime.enkel.value,
        'avk_1m': regime.avk_1m,
        'avk_3m': regime.avk_3m,
    }

    # HTML
    html_path = generate_html(filtered, regime_info, datum_str, OUTPUT_FOLDER)
    print("\n[INFO] HTML sparad: {}".format(html_path))

    # CSV
    if not args.ingen_csv and filtered:
        import csv
        ts_csv = datetime.now().strftime('%Y%m%d_%H%M')
        csv_path = os.path.join(OUTPUT_FOLDER, 'turnaround_{}_{}.csv'.format(
            datum_str.replace('-',''), ts_csv))
        csv_cols = ['Ticker','Namn','Land','Sektor','Cap','Kurs','Score','VandStyrka','Fas',
                    'p12m%','p6m%','p3m%','p1w%','RSI','RSISlope','RSI_bull_div','RSI_was_os',
                    'OBV_div','OBV_bull','StarkAckum','Ackum','CMF','ADX','ADX_fall',
                    'DI_plus','DI_minus','DI_cross','Squeeze','RS','RS_acc','RS_acc_turn',
                    'EMA9_cross21','EMA9_bottom','EMA50%','SMA200%','Pos52v%','MaxDD%',
                    'ATR%','ATR_exp','VolRatio','UdVol']
        with open(csv_path,'w',newline='',encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=csv_cols, extrasaction='ignore')
            w.writeheader()
            w.writerows(filtered)
        print("[INFO] CSV sparad:  {}".format(csv_path))

    print("\n[KLAR] Screening klar — {}".format(datum_str))
    print("       Öppna rapporten: {}".format(html_path))


if __name__ == '__main__':
    main()