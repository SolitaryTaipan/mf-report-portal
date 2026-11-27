"""Quantify Capital Portfolio Report Engine — implements system.md.

Usage:
    python report_engine.py input.json [--out-dir reports]

input.json:
    {"client_name": "...", "capital": 1000000, "sip_amount": 5000,
     "funds": [{"scheme_name": "...", "weight_pct": 40}, ...]}

On success prints exactly three lines (system.md §9) and saves one A4 PDF.
On any failure prints a single ERROR line and exits with status 1.
All NAVs are fetched live from mfapi.in on every run; nothing is cached.
"""
from __future__ import annotations

import argparse
import calendar
from collections import Counter
import difflib
import json
import math
import re
import sys
import time
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from scipy.optimize import brentq

import report_render as rr

API = "https://api.mfapi.in/mf"
BENCHMARK_NAME = "UTI Nifty 50 Index Fund - Direct Plan - Growth"
RISK_FREE = 0.07
N_SIPS = 60
YEARS = 5
TRADING_DAYS = 252          # annualisation factor for daily volatility
MAX_STALE_DAYS = 7          # latest NAV older than this (vs today) = data unavailable
TOL = 0.01                  # reconciliation tolerance in ₹ (one paisa)
BAD_FILE_CHARS = set('<>:"/\\|?*')


class EngineError(Exception):
    """Message is the text after 'ERROR: '."""


def fail(msg: str):
    raise EngineError(msg)


# --------------------------------------------------------------------------- mfapi.in
_session = requests.Session()
_session.headers["User-Agent"] = "qc-report-engine/1.0"


def _get_json(url: str, what: str):
    last = None
    for attempt in range(3):
        try:
            r = _session.get(url, timeout=60)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    fail(f"Data unavailable from mfapi.in for {what} ({type(last).__name__})")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def fetch_scheme_list() -> list[dict]:
    data = _get_json(API, "the scheme list")
    if not isinstance(data, list) or not data:
        fail("Data unavailable from mfapi.in for the scheme list")
    out = []
    for x in data:
        name = x.get("schemeName")
        code = x.get("schemeCode")
        if isinstance(name, str) and code is not None:
            n = norm(name)
            out.append({"code": int(code), "name": name.strip(), "norm": n, "tokens": set(n.split())})
    return out


def _top3(query: str, cands: list[dict], universe: list[dict]) -> str:
    """Closest names, ranked by shared distinctive words (IDF-weighted), then string similarity."""
    n = len(universe)
    df = Counter(t for s in universe for t in s["tokens"])
    idf = {t: math.log(n / c) for t, c in df.items()}
    q = norm(query)
    qt = set(q.split())
    qw = sum(idf.get(t, math.log(n)) for t in qt) or 1.0

    def score(s):
        shared = sum(idf[t] for t in qt & s["tokens"]) / qw
        extra = sum(idf[t] for t in s["tokens"] - qt) / qw
        return shared - 0.15 * extra

    pre = sorted(cands, key=score, reverse=True)[:30]
    ranked = sorted(pre, key=lambda s: (-round(score(s), 3),
                                        -difflib.SequenceMatcher(None, q, s["norm"]).ratio(), s["code"]))
    return "; ".join(f"'{s['name']}' (code {s['code']})" for s in ranked[:3])


def resolve_scheme(name: str, schemes: list[dict]) -> dict:
    """Exactly one scheme must carry this exact name (case, spacing and punctuation ignored).
    No partial or fuzzy matching: a near-miss returns the top 3 candidates instead."""
    q = norm(name)
    exact = [s for s in schemes if s["norm"] == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        fail(f"Scheme '{name}' matches more than one scheme on mfapi.in. "
             f"Fix: Choose one of: {_top3(name, exact, schemes)}.")
    qt = set(q.split())
    partial = [s for s in schemes if qt and qt <= s["tokens"]]
    if len(partial) > 1:
        fail(f"Scheme '{name}' matches more than one scheme on mfapi.in. "
             f"Fix: Use the exact name of one of: {_top3(name, partial, schemes)}.")
    fail(f"Scheme '{name}' does not match exactly one scheme on mfapi.in. "
         f"Fix: Use the exact name of one of the closest: {_top3(name, schemes, schemes)}.")


def fetch_nav(code: int, label: str, today: date) -> tuple[dict, pd.Series]:
    j = _get_json(f"{API}/{code}", f"'{label}'")
    if not isinstance(j, dict) or j.get("status") != "SUCCESS" or not j.get("data"):
        fail(f"Scheme '{label}' has no NAV data on mfapi.in")
    rows = []
    for r in j["data"]:
        try:
            d = pd.to_datetime(r["date"], format="%d-%m-%Y")
            v = float(r["nav"])
        except (KeyError, ValueError, TypeError):
            continue
        if math.isfinite(v) and v > 0 and d.date() <= today:
            rows.append((d, v))
    if not rows:
        fail(f"Scheme '{label}' has no NAV data on mfapi.in")
    df = pd.DataFrame(rows, columns=["date", "nav"]).drop_duplicates()
    if df["date"].duplicated().any():
        bad = df.loc[df["date"].duplicated(), "date"].iloc[0].date()
        fail(f"mfapi.in returns conflicting NAVs for '{label}' on {bad:%d-%m-%Y}")
    s = df.set_index("date")["nav"].sort_index()
    return j.get("meta") or {}, s


# --------------------------------------------------------------------------- validation
def _num(v) -> Decimal | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return Decimal(str(v))


def validate(raw) -> dict:
    """system.md §2. Stops at the first failure, in the documented order."""
    if not isinstance(raw, dict):
        fail("Input is not a JSON object. Fix: Provide client_name, capital and funds.")
    missing = [k for k in ("client_name", "capital", "funds") if raw.get(k) is None]
    if missing:
        fail(f"Missing required input: {', '.join(missing)}. Fix: Provide {', '.join(missing)}.")
    client = raw["client_name"]
    if not isinstance(client, str) or not client.strip():
        fail("client_name is empty or not text. Fix: Provide the client name as text.")
    if any(c in BAD_FILE_CHARS or ord(c) < 32 for c in client) or client != client.rstrip(" ."):
        fail("client_name cannot be used in the PDF file name. "
             "Fix: Remove \\ / : * ? \" < > | and trailing spaces or dots from client_name.")
    funds = raw["funds"]
    if not isinstance(funds, list):
        fail("funds is not a list. Fix: Provide funds as a list of {scheme_name, weight_pct}.")

    # Rule 1: fund count
    if not 1 <= len(funds) <= 8:
        fail(f"Fund count is {len(funds)}; it must be 1 to 8. Fix: Provide between 1 and 8 funds.")
    items = []
    for i, f in enumerate(funds, 1):
        if not isinstance(f, dict) or not isinstance(f.get("scheme_name"), str) or not f["scheme_name"].strip():
            fail(f"Fund {i} has no scheme_name. Fix: Give every fund a scheme_name and weight_pct.")
        w = _num(f.get("weight_pct"))
        if w is None or w <= 0:
            fail(f"weight_pct for '{f['scheme_name']}' is not a positive number. "
                 f"Fix: Give every fund a positive numeric weight_pct.")
        exp = None
        if f.get("expected_return_pct") is not None:      # used only for funds with < 5 years of NAV
            exp = _num(f["expected_return_pct"])
            if exp is None or not (Decimal(-100) < exp <= Decimal(100)):
                fail(f"expected_return_pct for '{f['scheme_name']}' is not a number between -100 and 100. "
                     f"Fix: Enter the expected annual return in %, e.g. 12.")
        items.append({"input_name": f["scheme_name"].strip(), "weight": w, "expected": exp})

    # Rule 2: weights sum to exactly 100
    total = sum(x["weight"] for x in items)
    if total != Decimal(100):
        fail(f"Weights sum to {format(total.normalize(), 'f')}, not 100. "
             f"Fix: Change weight_pct values so they total exactly 100.")

    # Rule 3: duplicates (by name)
    seen = {}
    for x in items:
        k = norm(x["input_name"])
        if k in seen:
            fail(f"Scheme '{x['input_name']}' is listed more than once. Fix: List each scheme only once.")
        seen[k] = x

    # Rule 4: each scheme matches exactly one mfapi.in scheme
    schemes = fetch_scheme_list()
    codes = {}
    for x in items:
        s = resolve_scheme(x["input_name"], schemes)
        if s["code"] in codes:
            fail(f"Schemes '{codes[s['code']]}' and '{x['input_name']}' are the same mfapi.in scheme "
                 f"'{s['name']}'. Fix: List each scheme only once.")
        codes[s["code"]] = x["input_name"]
        x["code"], x["name"] = s["code"], s["name"]
    bench = [s for s in schemes if s["norm"] == norm(BENCHMARK_NAME)]
    if len(bench) != 1:
        fail(f"Benchmark '{BENCHMARK_NAME}' does not match exactly one scheme on mfapi.in")

    # Rule 5: capital and sip_amount positive
    capital = _num(raw["capital"])
    if capital is None or capital <= 0:
        fail("capital is not a positive number. Fix: Provide capital as a positive number in ₹.")
    if raw.get("sip_amount") is None:
        sip = (capital * Decimal("0.005")).quantize(Decimal("1E2"), rounding=ROUND_HALF_UP)
    else:
        sip = _num(raw["sip_amount"])
    if sip is None or sip <= 0:
        fail("sip_amount is not a positive number. Fix: Provide sip_amount as a positive number in ₹"
             " (or a capital of at least ₹10,000 if sip_amount is left out).")

    return {"client": client, "capital": capital, "sip": sip, "funds": items,
            "bench_code": bench[0]["code"], "bench_name": bench[0]["name"]}


# --------------------------------------------------------------------------- maths
def minus_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year - n)
    except ValueError:              # 29 Feb
        return d.replace(year=d.year - n, day=28)


def add_months(d: date, k: int) -> date:
    m = d.month - 1 + k
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def xirr(cashflows: list[tuple[pd.Timestamp, float]], label: str) -> float:
    d0 = min(d for d, _ in cashflows)
    t = np.array([(d - d0).days / 365.0 for d, _ in cashflows])
    c = np.array([a for _, a in cashflows])

    def npv(r):
        return float(np.sum(c / (1.0 + r) ** t))

    lo, hi = -0.9999, 1.0
    while npv(hi) > 0 and hi < 1e4:
        hi *= 2
    if npv(lo) * npv(hi) > 0:
        fail(f"XIRR could not be solved for {label}")
    return brentq(npv, lo, hi, xtol=1e-12, maxiter=500)


def max_drawdown(values: pd.Series) -> float:
    v = values[values > 0]
    if v.empty:
        return 0.0
    return float((v / v.cummax() - 1.0).min())


def summary_metrics(flows: pd.DataFrame, final_value: float, val_ts: pd.Timestamp,
                    series: pd.Series | None, label: str) -> dict:
    invested = float(flows["amount"].sum())
    cfs = [(d, -a) for d, a in zip(flows["exec"], flows["amount"])] + [(val_ts, final_value)]
    m = {"invested": invested, "value": final_value, "gain": final_value - invested,
         "abs_ret": (final_value - invested) / invested, "xirr": xirr(cfs, label)}
    if series is not None:
        m["mdd"] = max_drawdown(series)
    return m


def compute(v: dict, today: date) -> dict:
    capital, sip = float(v["capital"]), float(v["sip"])
    start = minus_years(today, YEARS)
    schedule = [("lumpsum", start, capital)] + [("sip", add_months(start, k), sip) for k in range(N_SIPS)]

    funds = v["funds"]
    start_ts = pd.Timestamp(start)
    for f in funds:
        f["meta"], f["nav"] = fetch_nav(f["code"], f["input_name"], today)
        f["first_nav"] = f["nav"].index[0].date()
        f["short"] = bool(f["nav"].index[0] > start_ts)
        # Short history (system.md §4): never calculated from NAVs; the advisor supplies the return.
        if f["short"] and f["expected"] is None:
            fail(f"'{f['input_name']}' has under 5 years of NAV history (first NAV "
                 f"{f['first_nav']:%d-%m-%Y}). Fix: Provide expected_return_pct, the expected annual "
                 f"return in %, for this fund.")
    bmeta, bnav = fetch_nav(v["bench_code"], v["bench_name"], today)

    # common valuation date: latest date for which every NAV-based scheme and the benchmark has a NAV
    everyone = [f for f in funds if not f["short"]] + [{"input_name": v["bench_name"], "nav": bnav}]
    val_ts = min(f["nav"].index[-1] for f in everyone)
    stale = min(everyone, key=lambda f: f["nav"].index[-1])
    if (today - val_ts.date()).days > MAX_STALE_DAYS:
        fail(f"mfapi.in NAV data for '{stale['input_name']}' ends on {val_ts:%d-%m-%Y}, "
             f"more than {MAX_STALE_DAYS} days before today")

    # NAV series used for the maths: actual NAVs, or for a short-history fund a modelled series
    # growing at the advisor's expected return, on the benchmark's trading days for the full period.
    grid = bnav.index[(bnav.index >= start_ts) & (bnav.index <= val_ts)]
    for f in funds:
        if f["short"]:
            r = float(f["expected"]) / 100.0
            f["calc_nav"] = pd.Series([(1.0 + r) ** ((d - start_ts).days / 365.0) for d in grid], index=grid)
        else:
            f["calc_nav"] = f["nav"]

    def exec_on(nav: pd.Series, d: pd.Timestamp, label: str) -> tuple[pd.Timestamp, float]:
        i = nav.index.searchsorted(d, side="left")          # next available NAV
        if i >= len(nav) or nav.index[i] > val_ts:
            fail(f"No NAV available for '{label}' on or after {d:%d-%m-%Y}")
        return nav.index[i], float(nav.iloc[i])

    rows = []
    for fi, f in enumerate(funds):
        w = float(f["weight"]) / 100.0
        for bucket, d, amt in schedule:
            ex, nav = exec_on(f["calc_nav"], pd.Timestamp(d), f["input_name"])
            a = amt * w
            bex, bn = exec_on(bnav, ex, v["bench_name"])       # identical cash flow into benchmark
            rows.append({"fund": fi, "bucket": bucket, "sched": pd.Timestamp(d), "exec": ex,
                         "amount": a, "units": a / nav, "b_exec": bex, "b_units": a / bn})
    flows = pd.DataFrame(rows)

    # daily value series on the union of NAV dates inside the period
    idx = bnav.index
    for f in funds:
        idx = idx.union(f["calc_nav"].index)
    idx = idx[(idx >= start_ts) & (idx <= val_ts)]

    fund_series = {}
    for fi, f in enumerate(funds):
        units = flows[flows.fund == fi].groupby("exec")["units"].sum().reindex(idx, fill_value=0.0).cumsum()
        nav = f["calc_nav"].reindex(f["calc_nav"].index.union(idx)).ffill().reindex(idx)
        fund_series[fi] = (units * nav).fillna(0.0)
    port_series = pd.DataFrame(fund_series).sum(axis=1)
    b_units = flows.groupby("b_exec")["b_units"].sum().reindex(idx, fill_value=0.0).cumsum()
    b_nav = bnav.reindex(bnav.index.union(idx)).ffill().reindex(idx)
    bench_series = (b_units * b_nav).fillna(0.0)

    def nav_at(nav: pd.Series) -> float:
        return float(nav.loc[:val_ts].iloc[-1])

    # per-scheme
    out_funds = []
    for fi, f in enumerate(funds):
        ff = flows[flows.fund == fi]
        value = float(ff["units"].sum()) * nav_at(f["calc_nav"])
        m = summary_metrics(ff, value, val_ts, fund_series[fi], f["input_name"])
        meta = f["meta"]
        act = f["nav"][(f["nav"].index >= start_ts) & (f["nav"].index <= val_ts)]     # actual NAVs only
        monthly = act.groupby(act.index.to_period("M")).last()
        trend = [float(x) for x in (monthly if len(monthly) >= 6 else act)]
        out_funds.append({**m, "trend": trend, "name": f["name"], "weight": f["weight"], "short": f["short"],
                          "assumed": float(f["expected"]) / 100.0 if f["short"] else None,
                          "first_nav": f["first_nav"], "house": meta.get("fund_house") or "",
                          "category": meta.get("scheme_category") or ""})

    # portfolio (value from the daily series: an independent path from per-scheme values)
    port_value = float(port_series.iloc[-1])
    port = summary_metrics(flows, port_value, val_ts, port_series, "the portfolio")
    cf = flows.groupby("exec")["amount"].sum().reindex(idx, fill_value=0.0)
    prev = port_series.shift(1)
    ok = prev > 0
    rets = ((port_series - cf)[ok] / prev[ok]) - 1.0
    port["vol"] = float(rets.std(ddof=1) * math.sqrt(TRADING_DAYS))
    port["sharpe"] = (port["xirr"] - RISK_FREE) / port["vol"]

    # benchmark on identical cash flows
    bflows = flows.rename(columns={"exec": "fund_exec"}).rename(columns={"b_exec": "exec"})
    b_value = float(flows["b_units"].sum()) * nav_at(bnav)
    bench = summary_metrics(bflows, b_value, val_ts, bench_series, "the benchmark")
    bench["name"] = v["bench_name"]

    # attribution buckets
    final_nav = {fi: nav_at(f["calc_nav"]) for fi, f in enumerate(funds)}
    buckets = {}
    for b in ("lumpsum", "sip"):
        bf = flows[flows.bucket == b]
        val = float(sum(u * final_nav[fi] for fi, u in zip(bf["fund"], bf["units"])))
        buckets[b] = summary_metrics(bf, val, val_ts, None, f"the {b} bucket")

    # monthly cumulative alpha (₹): portfolio value − benchmark value at each month end
    alpha_daily = port_series - bench_series
    monthly = alpha_daily.groupby(idx.to_period("M")).tail(1)
    alpha_series = [(idx[0].date(), float(alpha_daily.iloc[0]))] + \
                   [(d.date(), float(a)) for d, a in monthly.items()]

    return {"start": start, "val_date": val_ts.date(), "funds": out_funds, "port": port,
            "bench": bench, "buckets": buckets, "alpha_series": alpha_series,
            "alpha_rs": port["value"] - bench["value"], "alpha_cagr": port["xirr"] - bench["xirr"],
            "flows": flows}


# --------------------------------------------------------------------------- reconcile
def reconcile(v: dict, R: dict):
    P, L, S, funds = R["port"], R["buckets"]["lumpsum"], R["buckets"]["sip"], R["funds"]
    expected = float(v["capital"] + v["sip"] * N_SIPS)
    checks = [
        ("Invested = capital + sip_amount × 60", abs(P["invested"] - expected) <= TOL),
        ("Scheme invested amounts sum to portfolio invested",
         abs(sum(f["invested"] for f in funds) - P["invested"]) <= TOL),
        ("Scheme values sum to portfolio value", abs(sum(f["value"] for f in funds) - P["value"]) <= TOL),
        ("Lumpsum bucket + SIP bucket = portfolio value",
         abs(L["value"] + S["value"] - P["value"]) <= TOL),
        ("Lumpsum bucket + SIP bucket = portfolio invested",
         abs(L["invested"] + S["invested"] - P["invested"]) <= TOL),
        ("Weights sum to 100", sum(f["weight"] for f in funds) == Decimal(100)),
        ("Benchmark invested = portfolio invested", abs(R["bench"]["invested"] - P["invested"]) <= TOL),
        ("Cash flow count = funds × 61", len(R["flows"]) == len(funds) * (N_SIPS + 1)),
    ]
    for name, passed in checks:
        if not passed:
            fail(f"Reconciliation failed on {name}.")


# --------------------------------------------------------------------------- findings
def findings(R: dict) -> list[dict]:
    """The fixed 4 Key Findings (system.md §7): facts and computed numbers only, same tone either way.
    Each card is an icon, a bold lead and the rest of the sentence, as in the sample report."""
    P, B, funds = R["port"], R["bench"], R["funds"]
    inr, pct = rr.inr, rr.pct
    short_name = lambda f: rr.split_name(f["name"])[0]
    a = R["alpha_rs"]
    ahead = round(a) >= 0
    c1 = {"icon": "up" if ahead else "down",
          "lead": f"{'Outperforms' if ahead else 'Trails'} the Nifty 50 by {inr(a)}",
          "rest": f"CAGR gap of {pct(R['alpha_cagr'], signed=True)} ({pct(P['xirr'])} vs {pct(B['xirr'])}) "
                  f"on identical cash flows across the 5-year window."}

    beat = [f for f in funds if f["beats"]]
    lag = sorted([f for f in funds if not f["beats"]], key=lambda f: -f["xirr"])
    rest = f"on a CAGR basis ({pct(B['xirr'])})"
    rest += ("; " + ", ".join(f"{short_name(f)} lagged at {pct(f['xirr'])}" for f in lag)) if lag else             "; no scheme lagged"
    assumed = [f for f in funds if f["short"]]
    if assumed:
        rest += "; " + ", ".join(f"{short_name(f)} † is counted at its assumed {pct(f['assumed'])} return"
                                 for f in assumed)
    c2 = {"icon": "check", "lead": f"{len(beat)} of {len(funds)} schemes beat the Nifty 50", "rest": rest + "."}

    pd_, bd = round(abs(P["mdd"]) * 100, 2), round(abs(B["mdd"]) * 100, 2)
    lead = ("Lower drawdown than the index" if pd_ < bd else
            "Higher drawdown than the index" if pd_ > bd else "Same drawdown as the index")
    c3 = {"icon": "down", "lead": lead,
          "rest": f"portfolio max drawdown {pct(abs(P['mdd']))} vs Nifty 50's {pct(abs(B['mdd']))}."}

    big = [f for f in funds if f["weight"] > 40]
    if big:
        c4 = {"icon": "diamond",
              "lead": " and ".join(f"{short_name(f)} held at {rr.weight_str(f['weight'])}%" for f in big),
              "rest": "above 40% of the portfolio."}
    else:
        c4 = {"icon": "diamond", "lead": f"{len(funds)} scheme{'s' if len(funds) != 1 else ''} held",
              "rest": f"no single scheme above 40%; largest weight "
                      f"{rr.weight_str(max(f['weight'] for f in funds))}%."}
    return [c1, c2, c3, c4]


# --------------------------------------------------------------------------- main
def run(raw, out_dir: Path, today: date, keep_html: bool = False, client_render: bool = False) -> list[str]:
    """client_render=True saves <stem>.json (page variants) for the browser to fit and print,
    instead of printing a PDF with headless Chrome on this machine."""
    v = validate(raw)
    R = compute(v, today)
    reconcile(v, R)
    for f in R["funds"]:
        f["beats"] = f["xirr"] > R["bench"]["xirr"]
    stem = f"{v['client']}_Portfolio_Report_{today:%Y-%m-%d}"
    R.update({"client": v["client"], "capital": float(v["capital"]), "sip": float(v["sip"]),
              "today": today, "report_no": f"QC-{today:%Y}-{today:%m%d}", "file_stem": stem})
    R["findings"] = findings(R)
    pdf = out_dir / f"{stem}.pdf"
    if client_render:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / f".{stem}.json.tmp"
        tmp.write_text(json.dumps(rr.build_variants(R), ensure_ascii=False), encoding="utf-8")
        tmp.replace(out_dir / f"{stem}.json")
        file_line = stem
    else:
        try:
            rr.render_pdf(R, pdf, keep_html=(out_dir / f"{stem}.html") if keep_html else None)
        except rr.RenderError as e:
            fail(str(e))
        except PermissionError:
            fail(f"Cannot save {pdf.name} because the file is open in another program. "
                 f"Fix: Close the PDF and generate again.")
        file_line = pdf.name

    flags = []
    short = [f for f in R["funds"] if f["short"]]
    lag = [f for f in R["funds"] if not f["beats"]]
    if short:
        flags.append("Assumed return (under 5 yrs NAV): " +
                     ", ".join(f"{f['name']} † at {rr.pct(f['assumed'])} p.a." for f in short))
    if lag:
        flags.append("Lagging Nifty 50: " + ", ".join(f"{f['name']} ({rr.pct(f['xirr'])})" for f in lag))
    P, B = R["port"], R["bench"]
    return [
        f"Portfolio CAGR: {P['xirr'] * 100:.2f}% vs Nifty 50: {B['xirr'] * 100:.2f}% | "
        f"Alpha: ₹{R['alpha_rs'] / 100000:.2f} L",
        f"Flags: {'; '.join(flags) if flags else 'None'}",
        f"File: {file_line}",
    ]


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Quantify Capital portfolio report engine")
    ap.add_argument("input", help="input JSON file")
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "reports"),
                    help="folder for the PDF (default: reports/ next to this script)")
    ap.add_argument("--keep-html", action="store_true", help="also save the HTML source (debug)")
    a = ap.parse_args(argv)
    try:
        try:
            raw = json.loads(Path(a.input).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            fail(f"Input file could not be read as JSON ({type(e).__name__}). Fix: Provide a valid JSON input file.")
        lines = run(raw, Path(a.out_dir), date.today(), keep_html=a.keep_html)
    except EngineError as e:
        print(f"ERROR: {e}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
