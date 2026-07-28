#!/usr/bin/env python3
"""
Daily screener — quality at a reasonable price, Indian equities (NSE).

WHAT THIS DOES
  1. Fetches the current Nifty Total Market list from NSE (top ~750 NSE
     companies by size — Nifty 500 plus the next 250 midcap/smallcap names).
  2. Pulls fundamentals for each name from Yahoo Finance.
  3. Applies hard disqualifiers (loss-making, over-leveraged, no cash conversion).
  4. Scores every survivor on Quality (50), Value (35) and Growth (15).
  5. Checks recent news headlines for red-flag keywords and demotes/removes names.
  6. Writes data.json — a ranked shortlist with a full score breakdown per stock.

WHAT THIS IS NOT
  It is not advice, and the output is not a buy list. It is a reproducible
  shortlist of names that scored well on stated, visible criteria, intended as
  the STARTING point for reading an annual report. The score cannot see
  management quality, accounting games, competitive threats, pending litigation,
  or anything else that matters most.

DESIGN NOTE — why news is only ever used negatively
  Search results and headlines about stocks are saturated with promotional
  material. Ranking ON news would rank whatever is being pushed hardest. So
  news is used strictly as a disqualifier: it can remove a name from the list,
  never promote one onto it.

DATA CAVEAT
  Yahoo Finance is free but unofficial, and its Indian coverage has gaps and
  occasional errors. Treat every figure as needing verification against the
  company's own filings before you act on anything. If a number looks wrong,
  it may well be wrong.

Usage:  python screen.py                 # full Nifty Total Market (~750 names)
        python screen.py --limit 60      # first 60 names, for a quick test
        python screen.py --no-news       # skip the news pass (much faster)
"""

import argparse
import concurrent.futures as cf
import csv
import io
import json
import math
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

try:
    import yfinance as yf
except ImportError:
    sys.exit("Missing dependency. Run:  pip install yfinance")

IST = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
# Nifty Total Market = the 750 largest NSE-listed companies (Nifty 500 + Nifty
# Midsmallcap 250). Widened from Nifty 500 on 2026-07-28 after a user check
# showed a smaller name (Pyramid Technoplast, ~Rs 720 cr mcap) wasn't covered.
# Note: even this wider list is still the top ~750 by size — anything smaller
# still won't appear, and that's intentional (see hard filters: mcap > Rs 1,000 cr
# for the shortlist; anything under ~Rs 5,000 cr is flagged as thin in the UI).
NIFTY_UNIVERSE_CSV = "https://archives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv"

# Sectors excluded from ranking. Banks, NBFCs and insurers borrow and lend as
# their business, so debt/equity, ROCE and cash-conversion tests are meaningless
# for them and would produce nonsense scores.
EXCLUDED_SECTORS = {"Financial Services"}

# Headline keywords that remove a name from the shortlist outright.
RED_FLAGS = [
    "fraud", "scam", "sebi bans", "sebi bars", "sebi order", "sebi probe",
    "investigation", "probe into", "raid", "searches at", "income tax raid",
    "auditor resigns", "auditor resignation", "resigns as auditor",
    "cfo resigns", "cfo quits", "md resigns", "ceo resigns", "board resigns",
    "insolvency", "nclt", "ibc proceedings", "default", "defaults on",
    "downgrade to", "rating downgrade", "credit rating cut",
    "pledge", "shares pledged", "promoter stake sale", "promoter sells",
    "accounting", "misstatement", "restated", "whistleblower",
    "arrest", "arrested", "chargesheet", "ed summons", "money laundering",
    "delisting", "suspended from trading", "gst notice", "tax demand",
]

# Softer keywords: worth surfacing to the reader, not grounds for removal.
WATCH_WORDS = [
    "profit falls", "profit declines", "loss widens", "misses estimates",
    "guidance cut", "plant shut", "strike", "recall", "cyberattack",
    "resigns", "steps down", "capex", "qip", "rights issue", "stake sale",
]


# ----------------------------------------------------------------------------
# universe
# ----------------------------------------------------------------------------
def fetch_universe(limit=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(NIFTY_UNIVERSE_CSV, headers=UA)
    raw = urllib.request.urlopen(req, timeout=30, context=ctx).read().decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))
    out = [
        {"symbol": r["Symbol"].strip(), "name": r["Company Name"].strip(),
         "industry": r["Industry"].strip()}
        for r in rows if r.get("Symbol")
    ]
    return out[:limit] if limit else out


# ----------------------------------------------------------------------------
# fundamentals
# ----------------------------------------------------------------------------
def g(d, key):
    """Get a numeric field, treating None/NaN/inf as missing."""
    v = d.get(key)
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def fetch_one(stock):
    sym = stock["symbol"]
    try:
        t = yf.Ticker(sym + ".NS")
        i = t.info or {}
        if not i.get("longName") and not i.get("shortName"):
            return None

        price = g(i, "currentPrice") or g(i, "regularMarketPrice")
        eps = g(i, "trailingEps")
        ocf = g(i, "operatingCashflow")
        ni = g(i, "netIncomeToCommon")

        # yfinance reports debtToEquity as a PERCENTAGE (e.g. 45.2 means 0.452x)
        de_pct = g(i, "debtToEquity")
        de = de_pct / 100.0 if de_pct is not None else None

        # 3-year price range, for "where in its own range is it trading"
        pos_in_range = None
        try:
            h = t.history(period="3y", interval="1wk")
            if len(h) > 30 and price:
                lo, hi = float(h["Close"].min()), float(h["Close"].max())
                if hi > lo:
                    pos_in_range = (price - lo) / (hi - lo)  # 0 = 3y low, 1 = 3y high
        except Exception:
            pass

        # Last completed session's move and volume, for the pre-market "movers"
        # list. This is what already happened yesterday — never a prediction of
        # what happens today. Computed from daily bars, not from currentPrice vs
        # previousClose, because before market open those two are often identical
        # (no new trade has happened yet) and would show a false 0% change.
        day_change_pct, volume_ratio, last_session_date = None, None, None
        try:
            d = t.history(period="1mo", interval="1d")
            d = d[d["Volume"] > 0]
            if len(d) >= 2:
                last_close = float(d["Close"].iloc[-1])
                prior_close = float(d["Close"].iloc[-2])
                if prior_close > 0:
                    day_change_pct = (last_close - prior_close) / prior_close * 100
                last_session_date = str(d.index[-1].date())
                if len(d) >= 6:
                    last_vol = float(d["Volume"].iloc[-1])
                    avg_vol = float(d["Volume"].iloc[-11:-1].mean()) if len(d) >= 11 \
                        else float(d["Volume"].iloc[:-1].mean())
                    if avg_vol > 0:
                        volume_ratio = last_vol / avg_vol
        except Exception:
            pass

        rec = {
            "symbol": sym,
            "name": i.get("longName") or i.get("shortName") or stock["name"],
            "sector": i.get("sector") or stock["industry"],
            "industry": i.get("industry") or stock["industry"],
            "price": price,
            "mcap_cr": (g(i, "marketCap") / 1e7) if g(i, "marketCap") else None,
            "eps": eps,
            "book_value": g(i, "bookValue"),
            "pe": g(i, "trailingPE"),
            "pb": g(i, "priceToBook"),
            "roe": (g(i, "returnOnEquity") or 0) * 100 if g(i, "returnOnEquity") is not None else None,
            "roa": (g(i, "returnOnAssets") or 0) * 100 if g(i, "returnOnAssets") is not None else None,
            "opm": (g(i, "operatingMargins") or 0) * 100 if g(i, "operatingMargins") is not None else None,
            "npm": (g(i, "profitMargins") or 0) * 100 if g(i, "profitMargins") is not None else None,
            "de": de,
            "rev_growth": (g(i, "revenueGrowth") or 0) * 100 if g(i, "revenueGrowth") is not None else None,
            "eps_growth": (g(i, "earningsGrowth") or 0) * 100 if g(i, "earningsGrowth") is not None else None,
            "div_yield": g(i, "dividendYield"),
            "cash_conv": (ocf / ni) if (ocf and ni and ni > 0) else None,
            "pos_in_3y_range": pos_in_range,
            "day_change_pct": day_change_pct,
            "volume_ratio": volume_ratio,
            "last_session_date": last_session_date,
        }
        return rec
    except Exception:
        return None


def fetch_all(universe, workers=12):
    out, done = [], 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, s): s for s in universe}
        for f in cf.as_completed(futs):
            done += 1
            r = f.result()
            if r:
                out.append(r)
            if done % 25 == 0:
                print(f"  fetched {done}/{len(universe)}", flush=True)
    return out


# ----------------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------------
def ramp(v, lo, hi):
    """Map v into 0..1 across the band lo..hi."""
    if v is None:
        return None
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def ramp_down(v, good, bad):
    """Lower is better: `good` scores 1, `bad` scores 0."""
    if v is None:
        return None
    if bad == good:
        return 0.0
    return max(0.0, min(1.0, (bad - v) / (bad - good)))


def weighted(parts):
    """parts = [(score_0_1_or_None, weight, label)] -> (0..100, [detail])"""
    tot = w_tot = 0.0
    detail = []
    for s, w, label in parts:
        if s is None:
            detail.append({"factor": label, "score": None, "weight": w})
            continue
        tot += s * w
        w_tot += w
        detail.append({"factor": label, "score": round(s * 100), "weight": w})
    return (tot / w_tot * 100 if w_tot else 0.0), detail, w_tot


def disqualify(r):
    """Hard exclusions. Returns a reason string, or None if it passes."""
    if r["sector"] in EXCLUDED_SECTORS:
        return "Lender or insurer — the quality tests here do not apply to their business model"
    if not r["price"] or not r["eps"]:
        return "Insufficient data"
    if r["eps"] <= 0:
        return "Loss-making on a trailing basis"
    if r["mcap_cr"] is not None and r["mcap_cr"] < 1000:
        return "Market cap under ₹1,000 cr — too small and illiquid to learn on"
    if r["de"] is not None and r["de"] > 1.5:
        return f"Debt/equity of {r['de']:.2f}x is too high"
    if r["pe"] is not None and r["pe"] > 80:
        return f"P/E of {r['pe']:.0f} leaves no margin for error"
    if r["cash_conv"] is not None and r["cash_conv"] < 0.4:
        return f"Profits are not converting to cash (CFO/PAT {r['cash_conv']:.2f}x)"
    if r["roe"] is not None and r["roe"] < 10:
        return f"ROE of {r['roe']:.1f}% is below the 10% floor"
    return None


def score(r, sector_median_pe):
    # -- Quality, 50% of the total ------------------------------------------
    q_parts = [
        (ramp(r["roe"], 10, 25),          30, "Return on equity"),
        (ramp(r["roa"], 3, 15),           15, "Return on assets"),
        (ramp(r["opm"], 8, 25),           15, "Operating margin"),
        (ramp_down(r["de"], 0.0, 1.2),    20, "Low debt"),
        (ramp(r["cash_conv"], 0.5, 1.1),  20, "Profit converts to cash"),
    ]
    quality, q_detail, q_cov = weighted(q_parts)

    # -- Value, 35% ---------------------------------------------------------
    # Earnings yield, price-to-book, P/E versus the sector's median, and where
    # the price sits inside its own 3-year range.
    ey = (r["eps"] / r["price"] * 100) if (r["eps"] and r["price"]) else None
    rel_pe = (r["pe"] / sector_median_pe) if (r["pe"] and sector_median_pe) else None
    v_parts = [
        (ramp(ey, 2, 9),                       35, "Earnings yield"),
        (ramp_down(r["pb"], 1.0, 8.0),         20, "Price to book"),
        (ramp_down(rel_pe, 0.6, 1.6),          30, "P/E vs sector median"),
        (ramp_down(r["pos_in_3y_range"], 0.15, 0.95), 15, "Position in 3-year range"),
    ]
    value, v_detail, v_cov = weighted(v_parts)

    # -- Growth, 15% --------------------------------------------------------
    g_parts = [
        (ramp(r["rev_growth"], 0, 20), 50, "Revenue growth"),
        (ramp(r["eps_growth"], 0, 25), 50, "Earnings growth"),
    ]
    growth, g_detail, g_cov = weighted(g_parts)

    total = quality * 0.50 + value * 0.35 + growth * 0.15

    # Confidence = how much of the scoring data was actually available.
    coverage = (q_cov / 100 * 0.5 + v_cov / 100 * 0.35 + g_cov / 100 * 0.15)

    r["scores"] = {
        "total": round(total, 1),
        "quality": round(quality, 1),
        "value": round(value, 1),
        "growth": round(growth, 1),
        "coverage": round(coverage * 100),
        "detail": {"quality": q_detail, "value": v_detail, "growth": g_detail},
    }
    r["sector_median_pe"] = round(sector_median_pe, 1) if sector_median_pe else None
    return r


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


# ----------------------------------------------------------------------------
# news — used only to remove or annotate, never to promote
#
# Two layers:
#   (a) BROAD FEEDS — Moneycontrol and CNBC-TV18 RSS, fetched once each. These are
#       market-wide, so six HTTP calls cover every stock in the shortlist. Indian
#       financial press catches things that a generic search misses, particularly
#       results announcements and regulatory news.
#   (b) PER-STOCK SEARCH — Google News RSS query per shortlisted name, for depth.
#
# Both are used strictly to REMOVE or annotate. Nothing in the news can promote a
# stock up the ranking, because news coverage tracks promotion, not quality.
# ----------------------------------------------------------------------------
BROAD_FEEDS = [
    ("Moneycontrol — markets",  "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Moneycontrol — business", "https://www.moneycontrol.com/rss/business.xml"),
    ("Moneycontrol — results",  "https://www.moneycontrol.com/rss/results.xml"),
    ("CNBC-TV18 — markets",     "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml"),
    ("CNBC-TV18 — business",    "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/business.xml"),
    ("CNBC — world markets",
     "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
]

# Single words that, on their own, name a whole business group rather than
# one listed company — several unrelated group companies would otherwise all
# match headlines about any one of them (the Adani, Reliance/Anil Ambani,
# Aditya Birla and Essar groups all have multiple separately listed entities
# sharing one root name).
AMBIGUOUS_GROUP_ROOTS = {
    "adani", "reliance", "birla", "essar", "videocon", "jaypee", "unitech",
    "sahara", "hinduja", "shriram", "srei", "religare",
}

# Words stripped before matching a company name against a headline.
NAME_NOISE = re.compile(
    r"\b(limited|ltd|ltd\.|corporation|corp|company|co\.|"
    r"industries|industry|enterprises|holdings|india|"
    r"the|and|of|&|private|pvt|plc|inc)\b", re.I)


def parse_feed(url, source):
    """Return [{title,url,source}] from an RSS feed. Tolerant of encoding oddities."""
    out = []
    try:
        req = urllib.request.Request(url, headers=UA)
        raw = urllib.request.urlopen(req, timeout=15).read()
        # Moneycontrol serves ISO-8859-1; CNBC serves UTF-8. Try both.
        try:
            xml = raw.decode("utf-8")
        except UnicodeDecodeError:
            xml = raw.decode("iso-8859-1", "ignore")
        for it in re.findall(r"<item[ >](.*?)</item>", xml, re.S)[:80]:
            m = re.search(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", it, re.S)
            l = re.search(r"<link>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</link>", it, re.S)
            if not m:
                continue
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if title:
                out.append({"title": title,
                            "url": (l.group(1).strip() if l else ""),
                            "source": source})
    except Exception:
        pass
    return out


def fetch_broad_feeds():
    """Fetch all market-wide feeds once, in parallel. Returns a flat headline pool."""
    pool = []
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for items in ex.map(lambda f: parse_feed(f[1], f[0]), BROAD_FEEDS):
            pool.extend(items)
    return pool


def name_keys(rec):
    """Distinctive strings that identify this company in a headline."""
    keys = set()
    sym = (rec.get("symbol") or "").strip()
    if len(sym) >= 4 and sym.lower() not in AMBIGUOUS_GROUP_ROOTS:
        keys.add(sym.lower())
    core = NAME_NOISE.sub(" ", rec.get("name") or "")
    core = re.sub(r"[^A-Za-z0-9 ]", " ", core)
    core = " ".join(core.split())
    parts = core.split()
    if len(parts) >= 2:
        # A multi-word remainder is specific enough to use whole.
        keys.add(core.lower())
        # Also the first two words, which is how the press usually writes it.
        if len(" ".join(parts[:2])) >= 6:
            keys.add(" ".join(parts[:2]).lower())
    elif len(parts) == 1 and len(parts[0]) >= 7 and parts[0].lower() not in AMBIGUOUS_GROUP_ROOTS:
        # A single leftover word is only safe to use if it's long enough to
        # be distinctive, and not itself a business-group name shared by many
        # unrelated listed companies. Short single words are exactly what
        # group-company names collapse to once suffixes are stripped — e.g.
        # "Adani Enterprises Limited" reduces to "Adani", which would then
        # match headlines about Adani Power, Adani Energy, Adani Green and
        # every other sibling company. "Reliance Industries" similarly
        # collapses to "Reliance", shared with the unrelated Anil Ambani
        # group's Reliance Power, Reliance Capital and Reliance
        # Infrastructure. Dropping these avoids attributing one company's
        # coverage to the wrong business.
        keys.add(parts[0].lower())
    return {k for k in keys if len(k) >= 5}


def match_pool(rec, pool):
    """Headlines from the broad feeds that mention this company."""
    keys = name_keys(rec)
    if not keys:
        return []
    hits, seen = [], set()
    for h in pool:
        low = h["title"].lower()
        if any(k in low for k in keys) and h["title"] not in seen:
            seen.add(h["title"])
            hits.append(h)
    return hits[:8]


def classify(heads):
    """Split headlines into red flags and watch items."""
    red, watch = [], []
    for h in heads:
        low = h["title"].lower()
        for k in RED_FLAGS:
            if k in low:
                red.append({"keyword": k, "headline": h["title"],
                            "url": h.get("url", ""), "source": h.get("source", "")})
                break
        else:
            for k in WATCH_WORDS:
                if k in low:
                    watch.append({"keyword": k, "headline": h["title"],
                                  "url": h.get("url", ""), "source": h.get("source", "")})
                    break
    return red, watch


def news_check(rec, pool=None):
    heads, ok = [], False

    # (a) matches from the Moneycontrol / CNBC market-wide pool
    if pool:
        heads.extend(match_pool(rec, pool))
        ok = True

    # (b) targeted Google News search for this specific company
    try:
        q = urllib.parse.quote(rec["name"] + " share")
        url = f"https://news.google.com/rss/search?q={q}+when:7d&hl=en-IN&gl=IN&ceid=IN:en"
        for h in parse_feed(url, "Google News")[:12]:
            heads.append(h)
        ok = True
    except Exception:
        pass

    if not ok:
        return {"headlines": [], "red": [], "watch": [], "checked": False}

    # de-duplicate on title, preferring the Indian financial press
    order = {"Moneycontrol": 0, "CNBC": 1, "Google News": 2}
    heads.sort(key=lambda h: order.get(h.get("source", "").split(" ")[0], 3))
    seen, uniq = set(), []
    for h in heads:
        t = h["title"].lower()[:90]
        if t not in seen:
            seen.add(t)
            uniq.append(h)

    red, watch = classify(uniq)
    return {"headlines": uniq[:8], "red": red, "watch": watch, "checked": True,
            "sources": sorted({h.get("source", "") for h in uniq if h.get("source")})}


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--top", type=int, default=15, help="how many names to shortlist")
    ap.add_argument("--out", default="data.json")
    args = ap.parse_args()

    print("Fetching Nifty Total Market list (top ~750 by size)...", flush=True)
    universe = fetch_universe(args.limit)
    print(f"  {len(universe)} names", flush=True)

    print("Fetching fundamentals (this takes a while)...", flush=True)
    recs = fetch_all(universe)
    print(f"  got data for {len(recs)}", flush=True)

    # Sector median P/E across everything we fetched, for relative valuation.
    by_sector = {}
    for r in recs:
        by_sector.setdefault(r["sector"] or "Other", []).append(r["pe"])
    sector_pe = {s: median(v) for s, v in by_sector.items()}
    all_pe = median([r["pe"] for r in recs])

    passed, rejected = [], []
    for r in recs:
        why = disqualify(r)
        if why:
            rejected.append({"symbol": r["symbol"], "name": r["name"], "reason": why})
            continue
        passed.append(score(r, sector_pe.get(r["sector"]) or all_pe))

    passed.sort(key=lambda x: x["scores"]["total"], reverse=True)

    # Take a generous slice, then let news thin it out.
    shortlist = passed[: args.top * 2]
    market_headlines, feed_sources = [], []
    if not args.no_news:
        print("Fetching Moneycontrol and CNBC feeds...", flush=True)
        pool = fetch_broad_feeds()
        feed_sources = sorted({h["source"] for h in pool})
        print(f"  {len(pool)} headlines from {len(feed_sources)} feeds", flush=True)
        market_headlines = pool[:25]

        print("Matching headlines to the shortlist...", flush=True)
        with cf.ThreadPoolExecutor(max_workers=10) as ex:
            for r, n in zip(shortlist, ex.map(lambda x: news_check(x, pool), shortlist)):
                r["news"] = n

    final, removed = [], []
    for r in shortlist:
        n = r.get("news") or {}
        if n.get("red"):
            removed.append({"symbol": r["symbol"], "name": r["name"],
                            "reason": "Red flag in recent headlines: " + n["red"][0]["keyword"],
                            "headline": n["red"][0]["headline"],
                            "url": n["red"][0]["url"],
                            "source": n["red"][0].get("source", "")})
            continue
        final.append(r)
        if len(final) >= args.top:
            break

    # ------------------------------------------------------------------------
    # Movers — yesterday's biggest price moves, purely descriptive.
    #
    # This is NOT a prediction of what moves today, and it is NOT a ranking of
    # what to trade. It reports a fact that already happened (the last
    # completed session's % change) and attaches whatever real news exists for
    # it. Sorted only by the size of an observed, past move — never by a
    # forecast, because nothing here forecasts anything.
    # ------------------------------------------------------------------------
    liquid = [r for r in recs
              if r.get("day_change_pct") is not None
              and r.get("mcap_cr") and r["mcap_cr"] >= 500]
    session_date = None
    if liquid:
        from collections import Counter
        session_date = Counter(r.get("last_session_date") for r in liquid).most_common(1)[0][0]

    gainers = sorted(liquid, key=lambda r: r["day_change_pct"], reverse=True)[:10]
    losers = sorted(liquid, key=lambda r: r["day_change_pct"])[:10]

    def mover_card(r):
        return {
            "symbol": r["symbol"], "name": r["name"], "sector": r["sector"],
            "price": r["price"], "mcap_cr": r["mcap_cr"],
            "day_change_pct": round(r["day_change_pct"], 2),
            "volume_ratio": round(r["volume_ratio"], 2) if r.get("volume_ratio") else None,
        }

    movers_gainers = [mover_card(r) for r in gainers]
    movers_losers = [mover_card(r) for r in losers]

    if not args.no_news and liquid:
        print("Checking headlines behind yesterday's biggest movers...", flush=True)
        mover_recs = {r["symbol"]: r for r in gainers + losers}
        with cf.ThreadPoolExecutor(max_workers=10) as ex:
            news_by_symbol = dict(zip(
                mover_recs.keys(),
                ex.map(lambda s: news_check(mover_recs[s], pool), mover_recs.keys())
            ))
        for card in movers_gainers + movers_losers:
            card["news"] = news_by_symbol.get(card["symbol"],
                                               {"headlines": [], "red": [], "watch": []})

    # ------------------------------------------------------------------------
    # Buzz — which stocks are getting unusually heavy coverage today, purely
    # as an observed fact. This is deliberately NOT a ranking of what to buy.
    #
    # It counts how many of today's Moneycontrol / CNBC-TV18 headlines mention
    # each company. That is the entire signal: attention, not quality, not
    # value, not a forecast. Heavy coverage is at least as often the mark of a
    # trade that has already happened and that retail is arriving late to, as
    # it is a sign of anything worth owning. The count can only tell you where
    # to look; it says nothing about what you'll find.
    # ------------------------------------------------------------------------
    buzz_top = []
    if not args.no_news and pool:
        print("Counting today's headline mentions across the universe...", flush=True)
        for r in recs:
            hits = match_pool(r, pool)
            if hits:
                buzz_top.append({
                    "symbol": r["symbol"], "name": r["name"], "sector": r["sector"],
                    "price": r.get("price"), "mcap_cr": r.get("mcap_cr"),
                    "mentions": len(hits),
                    "headlines": hits[:6],
                })
        buzz_top.sort(key=lambda x: x["mentions"], reverse=True)
        buzz_top = buzz_top[:15]

    out = {
        "generated_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        "universe_size": len(universe),
        "fetched": len(recs),
        "passed_filters": len(passed),
        "method": {
            "weights": {"quality": 50, "value": 35, "growth": 15},
            "quality_factors": ["Return on equity", "Return on assets",
                                "Operating margin", "Low debt", "Profit converts to cash"],
            "value_factors": ["Earnings yield", "Price to book",
                              "P/E vs sector median", "Position in 3-year range"],
            "growth_factors": ["Revenue growth", "Earnings growth"],
            "hard_filters": [
                "Profitable on a trailing basis",
                "Market cap above ₹1,000 cr",
                "Debt/equity below 1.5x",
                "P/E below 80",
                "CFO/PAT above 0.4x",
                "ROE above 10%",
                "Banks, NBFCs and insurers excluded — these tests do not fit their model",
            ],
            "news_policy": "Headlines can only REMOVE a name from the list. "
                           "They never promote one onto it.",
            "news_sources": feed_sources or ["(news pass skipped)"],
            "data_source": "Yahoo Finance (fundamentals and prices) · "
                           "Moneycontrol, CNBC-TV18 and Google News (headlines only)",
        },
        "market_headlines": market_headlines,
        "shortlist": final,
        "movers": {
            "session_date": session_date,
            "note": "The last COMPLETED trading session's move, reported after it "
                    "happened. Not a forecast, not a ranking of what to trade next, "
                    "and not a list of what will keep moving. A stock that jumped "
                    "yesterday is exactly as likely to reverse as to continue.",
            "gainers": movers_gainers,
            "losers": movers_losers,
        },
        "buzz": {
            "note": "How many of today's Moneycontrol and CNBC-TV18 headlines mention "
                    "each company. Attention only — not quality, not value, and not a "
                    "forecast. Heavy coverage is often the sign of a move that already "
                    "happened, not one still to come. This ranks by volume of mentions "
                    "alone; it is not a suggestion to buy anything on this list.",
            "sources": feed_sources or [],
            "top": buzz_top,
        },
        "removed_on_news": removed,
        "rejected_sample": rejected[:40],
        "disclaimer": "Not investment advice and not a buy list. This is a reproducible "
                      "ranking on the stated criteria, meant as a starting point for "
                      "reading an annual report. Data is from a free unofficial source "
                      "and may contain errors — verify against company filings.",
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, default=str)
    print(f"\nWrote {args.out} — {len(final)} names shortlisted "
          f"from {len(passed)} that passed filters.", flush=True)
    for i, r in enumerate(final[:10], 1):
        print(f"  {i:2d}. {r['symbol']:<14} {r['scores']['total']:5.1f}  "
              f"Q{r['scores']['quality']:.0f} V{r['scores']['value']:.0f} "
              f"G{r['scores']['growth']:.0f}  {r['name'][:38]}")

    print(f"\nMovers, session {session_date}: "
          f"{len(movers_gainers)} gainers, {len(movers_losers)} losers")
    for r in movers_gainers[:5]:
        print(f"  UP   {r['symbol']:<14} {r['day_change_pct']:+6.2f}%")
    for r in movers_losers[:5]:
        print(f"  DOWN {r['symbol']:<14} {r['day_change_pct']:+6.2f}%")


if __name__ == "__main__":
    main()
