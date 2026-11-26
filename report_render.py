"""Rendering for the Quantify Capital Portfolio Report Engine (see system.md).

Layout, colours and components follow the sample report ("Portfolio for Shantanu.pdf"):
white cover with blue accents, KPI strip with a blue CAGR tile, blue/orange scheme bars,
donut allocation, holdings cards, 3x2 risk tiles and a dark navy footer, fitted to two
A4 pages. Every number arrives pre-computed from report_engine.py; this module only
formats and lays out, then prints to PDF with headless Chrome/Edge.
"""
from __future__ import annotations

import html
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

MINUS = "\u2212"

# Sample palette. Beats Nifty 50 = blue, below = orange (CVD-safe pair; every bar also
# carries a visible value label, and the legend names both states).
BLUE = "#2563EB"
ORANGE = "#D97706"
GREEN = "#16A34A"
BLUES = ["#2563EB", "#1D4ED8", "#60A5FA", "#1E40AF", "#93C5FD", "#3B82F6", "#1E3A8A", "#BFDBFE"]
ORANGES = ["#D97706", "#F59E0B", "#B45309", "#FBBF24", "#92400E", "#FCD34D", "#78350F", "#FDE68A"]


class RenderError(Exception):
    pass


# --------------------------------------------------------------------------- formatting
def indian_group(n: int) -> str:
    s = str(abs(int(n)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return s


def inr(x: float, signed: bool = False) -> str:
    """Indian grouping; values over ₹1L shown in lakhs (₹64.25L)."""
    ax = abs(x)
    if ax > 100000:
        whole, frac = divmod(round(ax / 1000), 100)      # lakhs to 2 decimals
        body = f"₹{indian_group(whole)}.{frac:02d}L"
        is_zero = False
    else:
        r = round(ax)
        body = f"₹{indian_group(r)}"
        is_zero = r == 0
    if is_zero:
        return body
    if x < 0:
        return MINUS + body
    return ("+" + body) if signed else body


def inr_full(x: float) -> str:
    """Full Indian grouping (₹25,00,000), as the sample shows input amounts."""
    r = round(x)
    return (MINUS if r < 0 else "") + f"₹{indian_group(r)}"


def pct(x: float, signed: bool = False) -> str:
    """Fraction -> percentage string to 2 decimals."""
    v = round(x * 100, 2)
    if v == 0:
        return "0.00%"
    if v < 0:
        return f"{MINUS}{abs(v):.2f}%"
    return f"+{v:.2f}%" if signed else f"{v:.2f}%"


def num2(x: float) -> str:
    v = round(x, 2)
    if v == 0:
        return "0.00"
    return f"{MINUS}{abs(v):.2f}" if v < 0 else f"{v:.2f}"


def weight_str(w: Decimal) -> str:
    return format(w.normalize(), "f")


def dmy(d: date) -> str:
    return d.strftime("%d %b %Y")


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def split_name(name: str) -> tuple[str, str]:
    """'Bandhan Small Cap Fund - Regular Plan - Growth' -> ('Bandhan Small Cap Fund', 'Regular Plan Growth')."""
    parts = [p.strip() for p in re.split(r"\s+-\s+|\s+-$|^-\s+", name) if p.strip(" -")]
    return (parts[0], " ".join(parts[1:])) if parts else (name, "")


def initials(name: str) -> str:
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    s = (words[0][0] + words[-1][0]) if len(words) >= 2 else words[0][:2]
    return s.upper()


def chips(f: dict) -> tuple[str, str, str]:
    cat = f["category"].split(" - ", 1)[-1] if f["category"] else ""
    cat = re.sub(r"\s+(Funds?|Scheme|Allocation)$", "", cat).strip()
    low = f["name"].lower()
    plan = "Direct Plan" if "direct" in low else "Regular Plan"
    option = "IDCW" if re.search(r"idcw|dividend|payout|reinvest|bonus", low) else "Growth"
    return cat, plan, option


# --------------------------------------------------------------------------- charts
def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi - lo < 1e-12:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / max(n - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw - 1e-12)
    start = math.floor(lo / step + 1e-9) * step
    end = math.ceil(hi / step - 1e-9) * step
    ticks, v = [], start
    while v <= end + step * 1e-6:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _lakh_tick(v: float) -> str:
    v = round(v, 2)
    if v == 0:
        return "₹0"
    s = f"{abs(v):.2f}".rstrip("0").rstrip(".")
    return f"{MINUS if v < 0 else ''}₹{s}L"


def alpha_chart_svg(points: list[tuple[date, float]], x_start: date, x_end: date, height: int) -> str:
    """Cumulative alpha (portfolio value − Nifty value), ₹ lakhs: blue line + soft area."""
    W, H = 720, height
    ml, mr, mt, mb = 52, 70, 8, 20
    pw, ph = W - ml - mr, H - mt - mb
    vals = [v / 100000 for _, v in points]
    ticks = _nice_ticks(min(0.0, min(vals)), max(0.0, max(vals)), 4)
    y_lo, y_hi = ticks[0], ticks[-1]
    x0, x1 = x_start.toordinal(), x_end.toordinal()

    def X(d: date) -> float:
        return ml + (d.toordinal() - x0) / max(x1 - x0, 1) * pw

    def Y(v: float) -> float:
        return mt + (y_hi - v) / (y_hi - y_lo) * ph

    out = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" '
           f'aria-label="Cumulative alpha versus Nifty 50, monthly, in rupees lakhs">']
    for t in ticks:
        y = Y(t)
        stroke, sw = ("#CBD5E1", 1) if t == 0 else ("#E2E8F0", 0.8)
        out.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="{stroke}" stroke-width="{sw}"/>')
        out.append(f'<text x="{ml - 8}" y="{y + 3.6:.1f}" text-anchor="end" class="ax">{esc(_lakh_tick(t))}</text>')
    for k in range(0, 6):
        try:
            d = x_start.replace(year=x_start.year + k)
        except ValueError:
            d = x_start.replace(year=x_start.year + k, day=28)
        if d.toordinal() > x1:
            break
        out.append(f'<text x="{X(d):.1f}" y="{mt + ph + 14}" text-anchor="middle" class="ax">{d.strftime("%b %Y")}</text>')
    pts = [(X(d), Y(v / 100000)) for d, v in points]
    y0 = Y(0)
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pts[0][0]:.1f},{y0:.1f} " + line + f" {pts[-1][0]:.1f},{y0:.1f}"
    out.append(f'<polygon points="{area}" fill="{BLUE}" fill-opacity="0.10" stroke="none"/>')
    out.append(f'<polyline points="{line}" fill="none" stroke="{BLUE}" stroke-width="2" '
               f'stroke-linejoin="round" stroke-linecap="round"/>')
    lx, ly = pts[-1]
    out.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4.5" fill="{BLUE}" stroke="#ffffff" stroke-width="2"/>')
    out.append(f'<text x="{lx + 9:.1f}" y="{ly + 4:.1f}" class="endlbl">{esc(inr(points[-1][1], signed=True))}</text>')
    out.append("</svg>")
    return "".join(out)


def donut_svg(segs: list[tuple[float, str]], count: int) -> str:
    r, sw, c = 38, 15, 50
    C = 2 * math.pi * r
    gap = 1.4 if len(segs) > 1 else 0
    out = ['<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Allocation donut">']
    off = 0.0
    for frac, col in segs:
        L = frac * C
        dash = max(L - gap, 0.01)
        out.append(f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="{col}" stroke-width="{sw}" '
                   f'stroke-dasharray="{dash:.3f} {C - dash:.3f}" stroke-dashoffset="{-off:.3f}" '
                   f'transform="rotate(-90 {c} {c})"/>')
        off += L
    out.append(f'<text x="50" y="52" text-anchor="middle" class="dn">{count}</text>'
               f'<text x="50" y="62" text-anchor="middle" class="dl">scheme{"s" if count != 1 else ""}</text></svg>')
    return "".join(out)


def spark_svg(vals: list[float], color: str) -> str:
    if len(vals) < 2:
        return ""
    W, H = 200, 40
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = [(i / (n - 1) * W, H - 3 - (v - lo) / span * (H - 8)) for i, v in enumerate(vals)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return (f'<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
            f'<polygon points="0,{H} {line} {W},{H}" fill="{color}" fill-opacity="0.08"/>'
            f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.6" '
            f'vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>')


# --------------------------------------------------------------------------- layout
CSS = r"""
@page { size: A4; margin: 0; }
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
:root {
  --ink:#0F172A; --ink2:#334155; --mut:#64748B; --lab:#94A3B8; --line:#E2E8F0; --bg:#F8FAFC; --bg2:#F1F5F9;
  --blue:#2563EB; --blue-l:#EFF6FF; --blue-m:#DBEAFE; --blue-t:#93C5FD;
  --green:#16A34A; --green-l:#F0FDF4; --green-b:#BBF7D0;
  --orange:#D97706; --amber-l:#FFFBEB; --amber-b:#FDE68A; --navy:#111827; --navy2:#0B1120;
}
body { font-family: Inter, "Segoe UI Variable Text", "Segoe UI", system-ui, "DejaVu Sans", sans-serif; color:var(--ink);
  line-height:1.35; font-variant-numeric: tabular-nums; }
.page { width:210mm; min-height:296mm; padding:0 14mm; display:flex; flex-direction:column; gap:var(--gap);
  font-size:var(--fs); position:relative; overflow:hidden; }
.page > * { position:relative; z-index:1; }
.p1 { break-after: page; page-break-after: always; }
.page.d0 { --fs:7.6pt; --gap:2.6mm; --h2:11pt; --eb:5.8pt; --kpi:16pt; --title:20pt; --brow:6.4mm; --spark:7.5mm; --pad:2.1mm; --tile:13.5pt; --cov:5mm; --row:0.85mm; --donut:29mm; --hb:1.3mm; }
.page.d1 { --fs:7.3pt; --gap:2.1mm; --h2:10.5pt; --eb:5.6pt; --kpi:15pt; --title:18pt; --brow:5.6mm; --spark:6mm; --pad:1.8mm; --tile:12.5pt; --cov:4.5mm; --row:0.7mm; --donut:25mm; --hb:1.1mm; }
.page.d2 { --fs:7pt; --gap:1.4mm; --h2:10pt; --eb:5.4pt; --kpi:14pt; --title:16pt; --brow:4.3mm; --spark:9mm; --pad:1.5mm; --tile:11.5pt; --cov:4mm; --row:0.55mm; --donut:21mm; --hb:0.6mm; }
.page.d2 .hold { gap:1.8mm 3mm; }
.page.d2 .p2head { padding:3.4mm 14mm 3mm; }
.page.d2 .ftop { padding:2.8mm 14mm 2.4mm; }
.page.d2 .fdisc { padding:2mm 14mm 2.8mm; }

.glow { position:absolute !important; z-index:0 !important; right:-45mm; top:-45mm; width:130mm; height:130mm;
  background: radial-gradient(circle, #EAF0FB 0%, rgba(234,240,251,0.6) 35%, rgba(255,255,255,0) 70%); }
.logo { font-weight:700; font-size:13pt; letter-spacing:-0.01em; color:var(--ink); }
.logo span { color:var(--blue); }
.logo.light { color:#fff; }
.logo.light span { color:#3B82F6; }
.k { font-size:var(--eb); color:var(--lab); font-weight:600; letter-spacing:0.1em; text-transform:uppercase; }

/* cover */
.cover { padding-top:var(--cov); display:flex; flex-direction:column; }
.ctop, .crow2 { display:flex; justify-content:space-between; align-items:center; }
.crow2 { margin-top:1.6mm; }
.repno { border:1px solid var(--line); background:var(--bg2); color:var(--lab); font-weight:700; font-size:6.3pt;
  letter-spacing:0.14em; padding:1.1mm 3.2mm; border-radius:10mm; }
.who { display:flex; align-items:center; gap:2.8mm; }
.who .nm { font-weight:700; font-size:8.6pt; text-align:right; }
.who .hn { color:var(--lab); font-size:6.8pt; text-align:right; }
.avatar { width:9mm; height:9mm; border-radius:50%; background:var(--blue); color:#fff; font-weight:700;
  font-size:9.5pt; display:flex; align-items:center; justify-content:center; box-shadow:0 0 0 1mm #DBEAFE; }
.badge { background:var(--blue-l); border:1px solid var(--blue-m); color:var(--blue);
  font-weight:700; font-size:6.3pt; letter-spacing:0.14em; padding:1.3mm 3.8mm; border-radius:10mm; }
.title { font-size:var(--title); font-weight:800; line-height:1.0; letter-spacing:-0.025em; margin-top:1.6mm; }
.title span { color:var(--blue); }
.prep { color:var(--mut); font-size:8pt; margin-top:1.2mm; }
.prep + .prep { margin-top:0.6mm; }
.prep b { color:var(--ink); font-weight:700; }
.strip5 { display:grid; grid-template-columns:1.25fr 1fr 1fr 1fr 0.85fr; border:1px solid var(--line); border-radius:2mm;
  background:var(--bg); margin-top:2.8mm; }
.strip5 > div { padding:1.7mm 3mm; border-left:1px solid var(--line); }
.strip5 > div:first-child { border-left:0; }
.strip5 .v { font-weight:600; font-size:7.8pt; margin-top:0.7mm; }
.genrow { display:flex; justify-content:space-between; align-items:center; border-top:1px solid var(--line);
  margin-top:2.6mm; padding-top:1.9mm; color:var(--lab); font-size:6.6pt; gap:4mm; }
.genrow b { color:var(--ink2); font-weight:600; }
.conf { background:var(--amber-l); border:1px solid var(--amber-b); color:var(--orange); font-weight:700;
  letter-spacing:0.08em; padding:1mm 3.2mm; border-radius:10mm; font-size:6.5pt; white-space:nowrap; }
.grule { height:0.75mm; margin:0 -14mm; background:linear-gradient(90deg, #2563EB 0%, #60A5FA 45%, #CBD5E1 80%, rgba(203,213,225,0) 100%); }

/* section headings */
.eb { color:var(--blue); font-weight:700; font-size:var(--eb); letter-spacing:0.14em; text-transform:uppercase; }
h2 { font-size:var(--h2); font-weight:800; letter-spacing:-0.015em; color:var(--ink); margin:0.2mm 0 var(--hb); line-height:1.15; }

/* KPI strip */
.kpis { display:grid; grid-template-columns:repeat(4, 1fr); border:1px solid var(--line); border-radius:2.4mm; overflow:hidden; }
.kpi { padding:var(--pad) 3.6mm; border-left:1px solid var(--line); }
.kpi:first-child { border-left:0; }
.kpi .v { font-size:var(--kpi); font-weight:800; letter-spacing:-0.02em; margin-top:0.8mm; line-height:1.1; }
.kpi .s { color:var(--lab); font-size:6.5pt; margin-top:0.7mm; }
.kpi.hl { background:var(--blue); color:#fff; border-left-color:var(--blue); }
.kpi.hl .k, .kpi.hl .s { color:#BFDBFE; }
.pos { color:var(--green); }
.neg { color:var(--orange); }

/* performance table */
.tbl { border:1px solid var(--line); border-radius:2.4mm; overflow:hidden; }
table { width:100%; border-collapse:collapse; }
th { background:var(--bg); color:var(--lab); font-size:6pt; font-weight:600; letter-spacing:0.1em; text-transform:uppercase;
  text-align:right; padding:1.5mm 3.4mm; border-bottom:1px solid var(--line); }
th:first-child, td:first-child { text-align:left; }
td { padding:1.6mm 3.4mm; text-align:right; border-bottom:1px solid var(--line); font-size:8pt; color:var(--ink2); }
tr:last-child td { border-bottom:0; }
td:first-child { color:var(--ink); font-weight:600; }
tr.you td { background:var(--blue-l); color:var(--blue); font-weight:600; }
tr.you td:first-child { color:var(--blue); font-weight:700; }
.callout { display:flex; justify-content:space-between; align-items:center; gap:4mm; background:var(--blue-l);
  border:1px solid var(--blue-m); border-radius:2.4mm; padding:1.9mm 4mm; margin-top:2mm; }
.callout .c1 { color:var(--blue); font-weight:700; font-size:8pt; }
.callout .c2 { color:#60A5FA; font-size:6.8pt; margin-top:0.5mm; }
.callout .big { text-align:right; white-space:nowrap; }
.callout .big .v { color:var(--blue); font-weight:800; font-size:15pt; line-height:1; letter-spacing:-0.02em; }
.callout .big .l { color:#60A5FA; font-size:6.8pt; margin-top:0.6mm; }
.p1note { color:var(--mut); font-size:6.4pt; margin-top:1.2mm; }

/* chart */
.chartbox { background:var(--bg); border:1px solid var(--line); border-radius:2.4mm; padding:1.9mm 3.4mm 1mm; }
.chartbox .cap { color:var(--lab); font-size:6.5pt; margin-bottom:0.6mm; }
.chartbox svg { width:100%; height:auto; display:block; }
.ax { font-size:10px; fill:#94A3B8; font-family: Inter, "Segoe UI Variable Text", "Segoe UI", "DejaVu Sans", sans-serif; }
.endlbl { font-size:11px; fill:#2563EB; font-weight:700; font-family: Inter, "Segoe UI Variable Text", "Segoe UI", "DejaVu Sans", sans-serif; }

/* attribution */
.attr { display:grid; grid-template-columns:1fr 1fr; gap:3.4mm; }
.acard { border:1px solid var(--line); border-radius:2.4mm; padding:var(--pad) 4mm 0.8mm; }
.ahead { display:flex; align-items:center; gap:2.2mm; margin-bottom:0.3mm; font-weight:700; font-size:8pt; }
.pill { font-size:6.3pt; font-weight:600; padding:0.6mm 2.4mm; border-radius:10mm; }
.pill.b { background:var(--blue-l); color:var(--blue); border:1px solid var(--blue-m); }
.pill.g { background:var(--green-l); color:var(--green); border:1px solid var(--green-b); }
.arow { display:flex; justify-content:space-between; padding:var(--row) 0; border-bottom:1px solid #EEF2F7; }
.arow:last-child { border-bottom:0; }
.arow span { color:var(--ink2); }
.arow b { font-weight:700; color:var(--ink); }
.acard.b .hl { color:var(--blue); }
.acard.g .hl { color:var(--green); }
.note { background:var(--bg); border:1px solid var(--line); border-radius:2mm; padding:1.2mm 3.2mm; color:var(--mut);
  font-size:6.4pt; margin-top:1.8mm; }

/* findings */
.finds { display:grid; grid-template-columns:1fr 1fr; gap:2.2mm 3.4mm; }
.find { display:flex; gap:2.8mm; border:1px solid var(--line); border-radius:2.4mm; padding:var(--pad) 3.6mm; }
.ico { flex:none; width:6.6mm; height:6.6mm; border-radius:1.6mm; background:var(--blue-l); color:var(--blue);
  display:flex; align-items:center; justify-content:center; font-weight:700; font-size:9pt; }
.ico.a { background:var(--amber-l); color:var(--orange); }
.find .t { color:var(--mut); line-height:1.38; }
.find .t b { color:var(--ink); font-weight:700; }

/* page 2 header */
.p2head { margin:0 -14mm; background:var(--navy); padding:4.4mm 14mm 4mm; display:flex; justify-content:space-between; align-items:center; }
.p2head .r { text-align:right; }
.p2head .r1 { color:#fff; font-weight:700; font-size:9pt; }
.p2head .r2 { color:#64748B; font-size:7pt; margin-top:0.6mm; }

/* scheme bars */
.legend { display:flex; justify-content:space-between; color:var(--mut); font-size:6.5pt; margin:-0.6mm 0 0.8mm; }
.legend .l > span { margin-right:4.5mm; }
.dashc { display:inline-block; width:3.4mm; height:1.1mm; border-radius:1mm; vertical-align:middle; margin-right:1.3mm; }
.legend .ref { color:var(--lab); }
.legend .ref i { display:inline-block; width:0; height:2.6mm; border-left:1px solid #94A3B8; vertical-align:middle; margin-right:1.6mm; }
.brow { display:grid; grid-template-columns:58mm 1fr 19mm; align-items:center; column-gap:4mm; min-height:var(--brow); }
.bname { font-weight:600; color:var(--ink); line-height:1.2; }
.bsub { color:var(--mut); font-size:6.6pt; font-weight:400; }
.page.d2 .bsub { display:inline; margin-left:1.4mm; font-size:6.2pt; }
.btrack { position:relative; height:2.4mm; background:var(--bg2); border-radius:2mm; }
.bfill { position:absolute; top:0; bottom:0; border-radius:2mm; }
.bref { position:absolute; top:-1.8mm; bottom:-1.8mm; border-left:1px solid #CBD5E1; }
.bzero { position:absolute; top:-1mm; bottom:-1mm; border-left:1px solid #94A3B8; }
.bval { text-align:right; font-weight:700; font-size:8.6pt; }

/* allocation */
.alloc { display:grid; grid-template-columns:var(--donut) 1fr; gap:9mm; align-items:center; }
.alloc svg { width:var(--donut); height:var(--donut); display:block; }
.dn { font-size:17px; font-weight:800; fill:#0F172A; font-family: Inter, "Segoe UI Variable Text", "Segoe UI", "DejaVu Sans", sans-serif; }
.dl { font-size:6.5px; fill:#94A3B8; font-family: Inter, "Segoe UI Variable Text", "Segoe UI", "DejaVu Sans", sans-serif; }
.aleg.two { display:grid; grid-template-columns:1fr 1fr; column-gap:7mm; }
.ar .w .amt::before { content:"· "; }
.aleg.two .w .amt { display:block; }
.aleg.two .w .amt::before { content:""; }
.ar { display:grid; grid-template-columns:3mm 1fr auto; gap:2.6mm; align-items:center; padding:var(--row) 0; }
.dot { width:2.6mm; height:2.6mm; border-radius:50%; }
.ar .nm { color:var(--ink); }
.ar .w { font-weight:700; color:var(--ink); text-align:right; white-space:nowrap; }
.ar .w span { font-weight:400; color:var(--lab); }

/* holdings */
.hold { display:grid; grid-template-columns:1fr 1fr; gap:2.4mm 3.4mm; }
.hc { border:1px solid var(--line); border-left:1.1mm solid var(--blue); border-radius:2.4mm; padding:var(--pad) 3.8mm; break-inside:avoid;
  display:grid; grid-template-columns:1fr; grid-template-areas:"hh" "hm" "chips" "assume" "spark"; }
.hc .hh { grid-area:hh; } .hc .hm { grid-area:hm; } .hc .chips { grid-area:chips; } .hc .assume { grid-area:assume; }
.hc .hsp { grid-area:spark; }
.page.d2 .hc { grid-template-columns:1fr 30mm; column-gap:3mm; grid-template-areas:"hh hh" "hm spark" "chips spark" "assume assume"; }
.page.d2 .hc .hsp { align-self:end; }
.hc.lag { border-left-color:var(--orange); }
.hh { display:flex; justify-content:space-between; gap:2mm; align-items:flex-start; }
.hn { font-weight:600; color:var(--ink); font-size:8.1pt; line-height:1.25; }
.wp { background:var(--bg2); color:var(--ink2); font-weight:700; font-size:6.6pt; padding:0.6mm 2.2mm; border-radius:10mm; white-space:nowrap; }
.hm { display:grid; grid-template-columns:repeat(3, 1fr); margin-top:1.2mm; }
.hm .v { font-weight:700; font-size:9.4pt; margin-top:0.3mm; letter-spacing:-0.01em; }
.chips { display:flex; gap:1.4mm; margin-top:1.2mm; flex-wrap:wrap; }
.chip { border:1px solid var(--line); background:var(--bg); color:var(--mut); font-size:6.3pt; padding:0.5mm 2.2mm; border-radius:10mm; }
.chip.b { background:var(--blue-l); border-color:var(--blue-m); color:var(--blue); }
.chip.o { background:var(--amber-l); border-color:var(--amber-b); color:var(--orange); }
.navlbl { margin-top:1.3mm; }
.page.d2 .navlbl { display:none; }
.spark { height:var(--spark); width:100%; display:block; margin-top:0.8mm; }
.assume { color:var(--mut); font-size:6.8pt; margin-top:0.8mm; }

/* risk tiles */
.risks { display:grid; grid-template-columns:repeat(3, 1fr); gap:2.4mm 3.4mm; }
.rt { border:1px solid var(--line); border-radius:2.4mm; padding:var(--pad) 3.8mm; }
.rt.f { background:var(--blue); border-color:var(--blue); color:#fff; }
.rt .v { font-size:var(--tile); font-weight:800; letter-spacing:-0.02em; margin-top:0.8mm; line-height:1.1; }
.rt .s { color:var(--lab); font-size:6.4pt; margin-top:0.6mm; line-height:1.3; }
.rt.f .k, .rt.f .s { color:#BFDBFE; }
.page.d2 .risks { grid-template-columns:repeat(6, 1fr); gap:2mm; }
.page.d2 .rt { padding:1.8mm 2.4mm; }
.page.d2 .rt .s { display:none; }

/* end of page 2 */
.end { margin:auto -14mm 0; }
.footnote { margin:0 14mm 2.4mm; color:var(--mut); font-size:6.3pt; line-height:1.4; border-top:1px solid var(--line); padding-top:1.5mm; }
.footnote b { color:var(--ink2); }
.fband { background:var(--navy); color:#fff; }
.ftop { display:flex; justify-content:space-between; align-items:center; padding:3.6mm 14mm 3.2mm; }
.fname { font-weight:700; font-size:9.2pt; }
.fmeta { color:#64748B; font-size:7pt; margin-top:0.6mm; }
.fright { text-align:right; }
.fdisc { background:var(--navy2); color:#6B7280; font-size:6.1pt; line-height:1.5; padding:2.6mm 14mm 3.6mm; border-top:1px solid #1F2937; }
"""

FOOTER_TEXT = (
    "This report is for informational purposes only and does not constitute investment advice. "
    "Past performance does not guarantee future returns. All mutual fund investments are subject "
    "to market risks. Please read all scheme-related documents carefully. Consult a SEBI-registered "
    "investment advisor before making any investment decisions. Data sourced from AMFI (mfapi.in); "
    "NAVs are real and unsmoothed. SIP attribution figures are estimates based on contribution "
    "schedule and may differ from actuals."
)

ICONS = {"up": "↑", "down": "↓", "check": "✓", "diamond": "◈"}


def _sec(eyebrow: str, title: str) -> str:
    return f'<div class="eb">{esc(eyebrow)}</div><h2>{title}</h2>'


def build_html(R: dict, d1: int, d2: int, only: int | None = None) -> str:
    """Full 2-page document, or just page `only` (1 or 2) for a fit test."""
    P, B = R["port"], R["bench"]
    funds = R["funds"]
    n = len(funds)
    any_short = any(f["short"] for f in funds)
    chart_h = {0: 104, 1: 94, 2: 84}[d1]
    today, client = R["today"], R["client"]

    def dag(f) -> str:
        return " †" if f["short"] else ""

    # ================================================================ page 1
    cover = f"""
<div class="cover">
  <div class="ctop">
    <div class="logo">Quantify <span>Capital</span></div>
    <div class="repno">REPORT #{esc(R['report_no'])}</div>
  </div>
  <div class="crow2">
    <div class="badge">5-YEAR BACKTEST REPORT</div>
    <div class="who"><div><div class="nm">{esc(client)}</div><div class="hn">HNI Investor</div></div>
      <div class="avatar">{esc(initials(client))}</div></div>
  </div>
  <div class="title">Portfolio<br><span>Analysis</span></div>
  <div class="prep">Prepared for <b>{esc(client)}</b> &nbsp;·&nbsp; As on {dmy(today)}</div>
  <div class="prep">Benchmark: Nifty 50 via UTI Nifty 50 Index Fund — Direct Growth</div>
  <div class="strip5">
    <div><div class="k">Period</div><div class="v">{dmy(R['start'])} –<br>{dmy(today)}</div></div>
    <div><div class="k">Initial Lumpsum</div><div class="v">{esc(inr_full(R['capital']))}</div></div>
    <div><div class="k">Monthly SIP</div><div class="v">{esc(inr_full(R['sip']))}</div></div>
    <div><div class="k">Data Source</div><div class="v">AMFI · mfapi.in</div></div>
    <div><div class="k">Schemes</div><div class="v">{n} Active</div></div>
  </div>
  <div class="genrow"><span>Generated by <b>Quantify Capital Analytics</b> · zenith.quantifycapital.in · NAVs as of {dmy(R['val_date'])}</span>
    <span class="conf">⚠ CONFIDENTIAL DOCUMENT</span></div>
</div>
<div class="grule"></div>"""

    gain_cls = "pos" if P["gain"] >= 0 else "neg"
    kpis = f"""
<div class="kpis">
  <div class="kpi"><div class="k">Total Invested</div><div class="v">{esc(inr(P['invested']))}</div><div class="s">Lumpsum + monthly SIPs</div></div>
  <div class="kpi"><div class="k">Portfolio Value</div><div class="v">{esc(inr(P['value']))}</div><div class="s">Mark-to-market, real NAVs{' †' if any_short else ''}</div></div>
  <div class="kpi"><div class="k">Total Gain</div><div class="v {gain_cls}">{esc(inr(P['gain']))}</div><div class="s">{esc(pct(P['abs_ret'], signed=True))} absolute return</div></div>
  <div class="kpi hl"><div class="k">CAGR / XIRR</div><div class="v">{esc(pct(P['xirr']))}</div><div class="s">Nifty 50 benchmark: {esc(pct(B['xirr']))}</div></div>
</div>"""

    ahead = round(R["alpha_rs"]) >= 0
    c1 = (f"Portfolio ahead by {inr(R['alpha_rs'])} — CAGR gap of {pct(R['alpha_cagr'], signed=True)} over the Nifty 50"
          if ahead else
          f"Portfolio trails by {inr(R['alpha_rs'])} — CAGR gap of {pct(R['alpha_cagr'], signed=True)} vs the Nifty 50")
    c2 = (f"Max drawdown {pct(abs(P['mdd']))} vs Nifty 50's {pct(abs(B['mdd']))} · identical cash flows into the benchmark")
    perf = f"""
<section>{_sec("Performance Summary", "Portfolio vs Nifty 50 Benchmark")}
<div class="tbl"><table>
<thead><tr><th>Strategy</th><th>Final Value</th><th>Abs. Return</th><th>CAGR / XIRR</th><th>Max Drawdown</th><th>Schemes</th></tr></thead>
<tbody>
<tr class="you"><td>Your Portfolio</td><td>{esc(inr(P['value']))}</td><td>{esc(pct(P['abs_ret'], signed=True))}</td><td>{esc(pct(P['xirr']))}</td><td>{esc(pct(abs(P['mdd'])))}</td><td>{n}</td></tr>
<tr><td>Nifty 50 Index</td><td>{esc(inr(B['value']))}</td><td>{esc(pct(B['abs_ret'], signed=True))}</td><td>{esc(pct(B['xirr']))}</td><td>{esc(pct(abs(B['mdd'])))}</td><td>1</td></tr>
</tbody></table></div>
<div class="callout"><div><div class="c1">{esc(c1)}</div><div class="c2">{esc(c2)}</div></div>
<div class="big"><div class="v">{esc(pct(R['alpha_cagr'], signed=True))}</div><div class="l">annual alpha</div></div></div>
{'<div class="p1note">† Includes an assumed return for a fund with under 5 years of NAV history; see the note on page 2.</div>' if any_short else ''}
</section>"""

    a0, a1 = R["alpha_series"][0][0], R["alpha_series"][-1][0]
    chart = f"""
<section>{_sec("Visual", "Cumulative Alpha Over Nifty 50")}
<div class="chartbox"><div class="cap">Portfolio value minus Nifty 50 value on identical cash flows (₹ lakhs) · {a0:%b %Y} – {a1:%b %Y}</div>
{alpha_chart_svg(R['alpha_series'], R['start'], max(today, R['val_date']), chart_h)}</div>
</section>"""

    L, S = R["buckets"]["lumpsum"], R["buckets"]["sip"]
    attrib = f"""
<section>{_sec("Return Attribution", "SIP vs Lumpsum Performance Breakdown")}
<div class="attr">
  <div class="acard b"><div class="ahead">Initial Lumpsum <span class="pill b">{esc(inr(L['invested']))} invested</span></div>
    <div class="arow"><span>Amount Invested</span><b>{esc(inr_full(L['invested']))}</b></div>
    <div class="arow"><span>Current Value (est.)</span><b class="hl">{esc(inr(L['value']))}</b></div>
    <div class="arow"><span>Absolute Return</span><b class="hl">{esc(pct(L['abs_ret'], signed=True))}</b></div>
    <div class="arow"><span>CAGR</span><b class="hl">{esc(pct(L['xirr']))} p.a.</b></div>
    <div class="arow"><span>Gain</span><b class="hl">{esc(inr(L['gain'], signed=True))}</b></div>
  </div>
  <div class="acard g"><div class="ahead">Monthly SIP <span class="pill g">{esc(inr(S['invested']))} contributed</span></div>
    <div class="arow"><span>Total SIP Invested</span><b>{esc(inr_full(S['invested']))}</b></div>
    <div class="arow"><span>Current Value (est.)</span><b class="hl">{esc(inr(S['value']))}</b></div>
    <div class="arow"><span>Absolute Return</span><b class="hl">{esc(pct(S['abs_ret'], signed=True))}</b></div>
    <div class="arow"><span>XIRR</span><b class="hl">{esc(pct(S['xirr']))} p.a.</b></div>
    <div class="arow"><span>Gain</span><b class="hl">{esc(inr(S['gain'], signed=True))}</b></div>
  </div>
</div>
<div class="note">ℹ SIP attribution is based on {esc(inr_full(R['sip']))}/month × 60 months = {esc(inr(S['invested']))} total, invested in the same weights as the lumpsum.</div>
</section>"""

    finds = "".join(
        f'<div class="find"><div class="ico{" a" if x["icon"] == "diamond" else ""}">{ICONS[x["icon"]]}</div>'
        f'<div class="t"><b>{esc(x["lead"])}</b> — {esc(x["rest"])}</div></div>' for x in R["findings"])
    findings = f'<section>{_sec("Insights", "Key Findings")}<div class="finds">{finds}</div></section>'

    brk = "p1" if only is None else ""
    page1 = (f'<div class="page {brk} d{d1}"><div class="glow"></div>{cover}{kpis}{perf}{chart}{attrib}{findings}</div>')

    # ================================================================ page 2
    head2 = f"""
<div class="p2head"><div class="logo light">Quantify <span>Capital</span></div>
<div class="r"><div class="r1">Scheme-Level Detail — Page 2</div><div class="r2">{esc(client)} · As on {dmy(today)}</div></div></div>"""

    by_cagr = sorted(funds, key=lambda f: f["xirr"], reverse=True)
    vals = [f["xirr"] for f in funds] + [B["xirr"]]
    lo, hi = min(0.0, min(vals)), max(0.0, max(vals))
    span = (hi - lo) * 1.06 or 1.0
    hi = lo + span

    def P_(v):
        return (v - lo) / span * 100

    rows = []
    for f in by_cagr:
        col = BLUE if f["beats"] else ORANGE
        head, tail = split_name(f["name"])
        left, width = P_(min(f["xirr"], 0)), abs(f["xirr"]) / span * 100
        zero = f'<span class="bzero" style="left:{P_(0):.3f}%"></span>' if lo < 0 else ""
        rows.append(
            f'<div class="brow"><div class="bname">{esc(head)}{dag(f)}<div class="bsub">{esc(tail)}</div></div>'
            f'<div class="btrack"><span class="bfill" style="left:{left:.3f}%;width:{width:.3f}%;background:{col}"></span>'
            f'{zero}<span class="bref" style="left:{P_(B["xirr"]):.3f}%"></span></div>'
            f'<div class="bval" style="color:{col}">{esc(pct(f["xirr"]))}{dag(f)}</div></div>')
    bars = f"""
<section>{_sec("Scheme Breakdown", f"Scheme-wise CAGR vs Nifty 50 ({esc(pct(B['xirr']))})")}
<div class="legend"><div class="l"><span><i class="dashc" style="background:{BLUE}"></i>Beats Nifty 50</span>
<span><i class="dashc" style="background:{ORANGE}"></i>Below Nifty 50</span>{'<span>† assumed return</span>' if any_short else ''}</div>
<div class="ref"><i></i>Nifty 50 reference: {esc(pct(B['xirr']))}</div></div>
{"".join(rows)}
</section>"""

    bi = oi = 0
    colors = {}
    for f in funds:
        if f["beats"]:
            colors[id(f)] = BLUES[bi % len(BLUES)]
            bi += 1
        else:
            colors[id(f)] = ORANGES[oi % len(ORANGES)]
            oi += 1
    segs = [(float(f["weight"]) / 100.0, colors[id(f)]) for f in funds]
    legend = "".join(
        f'<div class="ar"><span class="dot" style="background:{colors[id(f)]}"></span>'
        f'<span class="nm">{esc(split_name(f["name"])[0])}{dag(f)}</span>'
        f'<span class="w">{esc(weight_str(f["weight"]))}% <span class="amt">{esc(inr(f["invested"]))} → {esc(inr(f["value"]))}</span></span></div>'
        for f in funds)
    wtitle = " / ".join(weight_str(f["weight"]) for f in funds)
    alloc = f"""
<section>{_sec("Allocation", f"Portfolio Allocation — {esc(wtitle)}")}
<div class="alloc">{donut_svg(segs, n)}<div class="aleg{' two' if n > 5 else ''}">{legend}</div></div>
</section>"""

    cards = []
    for f in funds:
        head, tail = split_name(f["name"])
        col = BLUE if f["beats"] else ORANGE
        cat, plan, option = chips(f)
        catc = "b" if f["beats"] else "o"
        trend = f.get("trend") or []
        if f["short"]:
            lbl = f"NAV since launch ({dmy(f['first_nav'])}) · actual"
            assume = f'<div class="assume">CAGR = advisor\'s assumed {esc(pct(f["assumed"]))} p.a. (under 5 years of NAV).</div>'
        else:
            lbl, assume = "NAV trend (5Y, actual)", ""
        spark = spark_svg(trend, col)
        cards.append(f"""
<div class="hc{' lag' if not f['beats'] else ''}">
  <div class="hh"><div class="hn">{esc(head)}{' — ' + esc(tail) if tail else ''}{dag(f)}</div><div class="wp">{esc(weight_str(f['weight']))}%</div></div>
  <div class="hm"><div><div class="k">Invested</div><div class="v">{esc(inr(f['invested']))}</div></div>
    <div><div class="k">Current</div><div class="v">{esc(inr(f['value']))}</div></div>
    <div><div class="k">CAGR{dag(f)}</div><div class="v" style="color:{col}">{esc(pct(f['xirr']))}</div></div></div>
  <div class="chips">{f'<span class="chip {catc}">{esc(cat)}</span>' if cat else ''}<span class="chip">{plan}</span><span class="chip">{option}</span></div>
  {assume}<div class="hsp"><div class="k navlbl">{esc(lbl)}</div>{spark}</div>
</div>""")
    holdings = f'<section>{_sec("Scheme Detail", "Holdings — NAV Trend &amp; Fund Profile")}<div class="hold">{"".join(cards)}</div></section>'

    max_w = max(f["weight"] for f in funds)
    dd_cls = "pos" if round(abs(P["mdd"]), 4) <= round(abs(B["mdd"]), 4) else "neg"
    risks = f"""
<section>{_sec("Risk Profile", "The Numbers That Matter")}
<div class="risks">
  <div class="rt f"><div class="k">Alpha vs Nifty 50</div><div class="v">{esc(inr(R['alpha_rs']))}</div><div class="s">Portfolio value minus Nifty 50 value on identical cash flows</div></div>
  <div class="rt f"><div class="k">CAGR Gap</div><div class="v">{esc(pct(R['alpha_cagr'], signed=True))}</div><div class="s">Portfolio CAGR minus Nifty 50 CAGR</div></div>
  <div class="rt"><div class="k">Schemes Held</div><div class="v">{n}</div><div class="s">Largest weight: {esc(weight_str(max_w))}%</div></div>
  <div class="rt"><div class="k">Max Drawdown</div><div class="v {dd_cls}">{esc(pct(abs(P['mdd'])))}</div><div class="s">Nifty 50: {esc(pct(abs(B['mdd'])))}</div></div>
  <div class="rt"><div class="k">Sharpe Ratio</div><div class="v">{esc(num2(P['sharpe']))}</div><div class="s">Return per unit of risk at 7.00% risk-free rate</div></div>
  <div class="rt"><div class="k">Ann. Volatility</div><div class="v">{esc(pct(P['vol']))}</div><div class="s">Annualised standard deviation of portfolio returns</div></div>
</div></section>"""

    short = [f for f in funds if f["short"]]
    footnote = ""
    if short:
        items = "; ".join(f"{esc(f['name'])} (first NAV {dmy(f['first_nav'])}, assumed {esc(pct(f['assumed']))} p.a.)" for f in short)
        footnote = (f'<div class="footnote"><b>† Note:</b> Under 5 years of NAV history: {items}. Its figures use the '
                    f'expected annual return provided by the advisor, compounded over the full period on the same '
                    f'cash-flow schedule. This is an assumption, not actual NAV performance; portfolio totals, '
                    f'volatility and drawdown include it as steady growth.</div>')
    end = f"""
<div class="end">{footnote}
<footer class="fband">
  <div class="ftop"><div><div class="fname">Sourabh Sisodiya, CFA</div>
    <div class="fmeta">Quantify Capital · {dmy(today)} · Report #{esc(R['report_no'])}</div></div>
    <div class="fright"><div class="logo light">Quantify <span>Capital</span></div><div class="fmeta">zenith.quantifycapital.in</div></div></div>
  <div class="fdisc">{esc(FOOTER_TEXT)}</div>
</footer></div>"""

    page2 = f'<div class="page p2 d{d2}">{head2}{bars}{alloc}{holdings}{risks}{end}</div>'

    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<title>{esc(R["file_stem"])}</title><style>{CSS}</style></head>'
            f'<body>{page1 if only != 2 else ""}{page2 if only != 1 else ""}</body></html>')


# --------------------------------------------------------------------------- PDF
def _browser() -> str:
    """Chrome/Chromium/Edge: CHROME_PATH if set, else the usual Windows paths, else PATH."""
    env = os.environ.get("CHROME_PATH")
    if env:
        if Path(env).exists():
            return env
        raise RenderError(f"CHROME_PATH is set to {env}, which does not exist")
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome",
                 "msedge", "microsoft-edge"):
        p = shutil.which(name)
        if p:
            return p
    raise RenderError("No Chrome or Edge browser found to print the PDF")


def _print_pdf(html_text: str, pdf_path: Path, work: Path) -> int:
    try:
        import pymupdf as fitz
    except ImportError:          # older PyMuPDF
        import fitz

    src = work / "report.html"
    src.write_text(html_text, encoding="utf-8")
    profile = work / "profile"
    cmd = [_browser(), "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", "--disable-extensions", f"--user-data-dir={profile}",
           "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}", src.as_uri()]
    # Linux servers/containers: Chromium's sandbox is unavailable as root or inside Docker.
    if os.name != "nt" and (os.environ.get("CHROME_NO_SANDBOX") == "1" or os.geteuid() == 0):
        cmd[1:1] = ["--no-sandbox", "--disable-dev-shm-usage"]
    subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise RenderError("PDF printing failed")
    with fitz.open(pdf_path) as doc:
        return doc.page_count


DENSITIES = (0, 1, 2)   # 0 = full size; 2 = smallest cards and type. A page is never added.


def render_pdf(R: dict, out_path: Path, keep_html: Path | None = None) -> tuple[int, int]:
    """Fit each page independently at the largest density that stays on one A4 sheet,
    then print the combined document and confirm it is exactly 2 pages."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qc_report_") as tmp:
        work = Path(tmp)
        chosen = {}
        for page in (1, 2):
            for d in DENSITIES:
                args = (d, 0) if page == 1 else (0, d)
                if _print_pdf(build_html(R, *args, only=page), work / f"fit_{page}_{d}.pdf", work) == 1:
                    chosen[page] = d
                    break
            else:
                raise RenderError(f"Page {page} does not fit on one A4 page even at the smallest card size")
        d1, d2 = chosen[1], chosen[2]
        html_text = build_html(R, d1, d2)
        final = work / "final.pdf"
        if _print_pdf(html_text, final, work) != 2:
            raise RenderError("Report did not print as exactly 2 A4 pages")
        shutil.copyfile(final, out_path)
        if keep_html:
            keep_html.write_text(html_text, encoding="utf-8")
        return d1, d2
