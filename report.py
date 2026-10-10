"""Bygger den gemensamma HTML-rapporten (två flikar) och CSV-filer."""
import csv
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent


def sanitize(o):
    """Gör data JSON-säker: numpy-typer till vanliga, NaN/inf till None."""
    if isinstance(o, dict):
        return {str(k): sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [sanitize(v) for v in o]
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    return o


def sv(x, d=1):
    return f"{x:.{d}f}".replace(".", ",")


def build_report(payload, out_file, template=ROOT / "template.html"):
    data = json.dumps(sanitize(payload), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = Path(template).read_text(encoding="utf-8").replace("__DATA__", data)
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding="utf-8")
    return out_file


def write_csvs(out_dir, fund, turn, categories):
    out_dir = Path(out_dir)
    with open(out_dir / "fundamental.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Ticker", "Bolag", "Land", "Börsvärde", "Sektor", "Totalpoäng"] + list(categories))
        for c in fund:
            w.writerow([c["ticker"], c["name"], c["land"], c["cap"], c["sector"], sv(c["total"])] +
                       [("" if c["cats"][k]["s"] is None else sv(c["cats"][k]["s"])) for k in categories])
    cols = ["Ticker", "Namn", "Land", "Cap", "Sektor", "Kurs", "Score", "VandStyrka", "p12m%", "p6m%", "p3m%", "p1w%",
            "RSI", "ADX", "CMF", "VolRatio", "UdVol", "EMA50%", "SMA200%", "Pos52v%"]
    with open(out_dir / "turnaround.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(cols)
        for r in turn:
            w.writerow([str(r.get(k, "")).replace(".", ",") if isinstance(r.get(k), float) else r.get(k, "") for k in cols])
