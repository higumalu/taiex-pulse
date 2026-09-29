"""Resumable, throttled TWSE daily crawler.

Three endpoints are needed, one request per trading day each:

  MI_INDEX  (type=ALL)       indices, advance/decline counts, every stock's close
  MI_MARGN  (selectType=ALL) per-stock margin balance, plus market margin totals
  BWIBBU_d  (selectType=ALL) per-stock P/B

The raw payloads are large (MI_INDEX is ~5 MB/day), so each day is parsed on
arrival and only the tidy columns are kept, appended into per-year parquet
files. A manifest records finished days so the crawl can be stopped and resumed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta

import pandas as pd
import requests

from common import CACHE, PARQUET, Throttle, session

BASE = "https://www.twse.com.tw/rwd/zh"
# 4-digit codes = listed common stock (1101-9958) plus ETFs (0050, 0056, ...).
# 0050 is needed for the weekly MACD, so ETFs must not be filtered out here;
# indicator code decides later what to include. Warrants are 6 digits.
LISTED = re.compile(r"^\d{4}$")

FEEDS = {
    # name:    (path under BASE,                extra query,      first date with data)
    "index":  ("afterTrading/MI_INDEX",        "type=ALL",       date(2004, 2, 11)),
    "margin": ("marginTrading/MI_MARGN",       "selectType=ALL", date(2004, 2, 11)),
    "pbr":    ("afterTrading/BWIBBU_d",        "selectType=ALL", date(2005, 9, 1)),
    # MI_QFIIS is the only free daily source of shares outstanding, which the
    # market-cap weighting for 大盤股價淨值比 needs.
    "shares": ("fund/MI_QFIIS",     "selectType=ALLBUT0999", date(2004, 2, 11)),
}
MANIFEST = CACHE / "twse_manifest.json"


def load_manifest():
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}


def save_manifest(m):
    MANIFEST.write_text(json.dumps(m, indent=0, sort_keys=True))


def num(x):
    """TWSE numbers arrive as strings with commas; missing shows as '--' or ''."""
    if x is None:
        return None
    s = str(x).replace(",", "").strip()
    if s in ("", "--", "---", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def payload_is_empty(payload):
    """True when the server answered normally but has nothing for that day.

    A truncated reply is missing whole tables; a genuinely empty day still
    declares its fields and just carries no rows. MI_QFIIS does this for real
    dates (2026-06-19, reproducibly), so retrying is pointless.
    """
    tables = payload.get("tables")
    if tables:
        return not any(t.get("data") for t in tables)
    return bool(payload.get("fields")) and not payload.get("data")


def table_by_fields(payload, *required):
    for t in payload.get("tables", []):
        fields = t.get("fields") or []
        if all(any(r in f for f in fields) for r in required):
            return t
    return None


def parse_index(payload, day):
    out = {}

    idx_rows = []
    for t in payload.get("tables", []):
        fields = t.get("fields") or []
        if not fields or fields[0] not in ("指數", "報酬指數"):
            continue
        for row in t.get("data", []):
            idx_rows.append({"date": day, "index_name": str(row[0]).strip(),
                             "close": num(row[1])})
    if idx_rows:
        out["twse_index"] = pd.DataFrame(idx_rows).drop_duplicates(["date", "index_name"])

    quotes = table_by_fields(payload, "證券代號", "收盤價")
    if quotes:
        cols = quotes["fields"]
        ci, cc = cols.index("證券代號"), cols.index("收盤價")
        cd = next((i for i, c in enumerate(cols) if "漲跌(+/-)" in c), None)
        rows, up, down, flat = [], 0, 0, 0
        for r in quotes.get("data", []):
            code = str(r[ci]).strip()
            if not LISTED.match(code):
                continue
            rows.append({"date": day, "code": code, "close": num(r[cc])})
            if cd is None or code.startswith("0"):   # ETFs are not "股票"
                continue
            # the cell is markup like '<p style= color:green>-</p>'
            mark = re.sub(r"<[^>]+>", "", str(r[cd])).strip()
            if "+" in mark:
                up += 1
            elif "-" in mark:
                down += 1
            else:
                flat += 1
        out["twse_prices"] = pd.DataFrame(rows)
        # 漲跌證券數合計 only appears from 2011, so count it ourselves instead
        out["twse_breadth"] = pd.DataFrame([{"date": day, "up": float(up),
                                             "down": float(down),
                                             "unchanged": float(flat)}])
    return out


def parse_margin(payload, day):
    """MI_MARGN has a 3-row market summary plus one row per security.

    The per-security table repeats the same column names for 融資 and 融券, so
    the first '今日餘額' (index 6) is the margin-buy balance, in board lots.
    """
    out = {}
    per_stock = next((t for t in payload.get("tables", [])
                      if (t.get("fields") or [])[:2] == ["代號", "名稱"]
                      and "今日餘額" in (t.get("fields") or [])), None)
    if per_stock:
        cols = per_stock["fields"]
        ci = cols.index("代號")
        bal = cols.index("今日餘額")
        rows = [{"date": day, "code": str(r[ci]).strip(), "margin_lots": num(r[bal])}
                for r in per_stock.get("data", []) if LISTED.match(str(r[ci]).strip())]
        out["twse_margin_stock"] = pd.DataFrame(rows)

    totals = table_by_fields(payload, "項目", "今日餘額")
    if totals:
        rec = {"date": day}
        for r in totals.get("data", []):
            label = str(r[0])
            if "融資金額" in label:            # 仟元 -> TWD
                v = num(r[-1])
                rec["margin_balance_twd"] = v * 1000 if v is not None else None
            elif "融資" in label:              # board lots
                rec["margin_lots_total"] = num(r[-1])
        if len(rec) > 1:
            out["twse_margin_total"] = pd.DataFrame([rec])
    return out


def parse_pbr(payload, day):
    cols = payload.get("fields") or []
    if not cols:
        return {}
    try:
        ci = cols.index("證券代號")
        cp = next(i for i, c in enumerate(cols) if "淨值比" in c)
    except (ValueError, StopIteration):
        return {}
    rows = [{"date": day, "code": str(r[ci]).strip(), "pbr": num(r[cp])}
            for r in payload.get("data", []) if LISTED.match(str(r[ci]).strip())]
    return {"twse_pbr": pd.DataFrame(rows)}


def parse_shares(payload, day):
    cols = payload.get("fields") or []
    if "證券代號" not in cols or "發行股數" not in cols:
        return {}
    ci, cs = cols.index("證券代號"), cols.index("發行股數")
    rows = [{"date": day, "code": str(r[ci]).strip(), "shares": num(r[cs])}
            for r in payload.get("data", []) if LISTED.match(str(r[ci]).strip())]
    return {"twse_shares": pd.DataFrame(rows)}


PARSERS = {"index": parse_index, "margin": parse_margin, "pbr": parse_pbr,
           "shares": parse_shares}

# TWSE answers an over-eager client with stat "OK" and a payload that is simply
# missing tables -- no 429, no error. Without this check those days get written
# to the manifest as done and the data is lost silently.
REQUIRED = {
    "index": ("twse_prices",),
    "margin": ("twse_margin_stock", "twse_margin_total"),
    "pbr": ("twse_pbr",),
    "shares": ("twse_shares",),
}


def fetch_day(sess, throttle, feed, d):
    path, extra, _ = FEEDS[feed]
    url = f"{BASE}/{path}?date={d:%Y%m%d}&{extra}&response=json"
    for _ in range(5):
        throttle.wait()
        try:
            r = sess.get(url, timeout=90)
        except requests.RequestException as e:
            iv = throttle.penalise()
            print(f"    {d} {feed}: {type(e).__name__}, backing off to "
                  f"{iv:.1f}s", flush=True)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            iv = throttle.penalise()
            print(f"    {d} {feed}: HTTP {r.status_code}, backing off to "
                  f"{iv:.1f}s", flush=True)
            continue
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError:
            # TWSE answers with an HTML error page when it is throttling us
            iv = throttle.penalise()
            print(f"    {d} {feed}: non-JSON reply, backing off to "
                  f"{iv:.1f}s", flush=True)
            continue
        if payload.get("stat") not in ("OK", None):
            # market closed, or before this feed starts -- genuinely no data,
            # as opposed to a truncated reply. Recorded separately so the
            # reconciler does not queue these forever.
            return "closed"
        if payload_is_empty(payload):
            return "closed"
        parsed = PARSERS[feed](payload, f"{d:%Y-%m-%d}")
        missing = [k for k in REQUIRED[feed]
                   if k not in parsed or parsed[k] is None or not len(parsed[k])]
        if missing:
            iv = throttle.penalise()
            print(f"    {d} {feed}: truncated reply (missing {', '.join(missing)}), "
                  f"backing off to {iv:.1f}s", flush=True)
            continue
        throttle.reward()
        return parsed
    return None


def append(buffers, parsed):
    for name, df in parsed.items():
        if df is not None and len(df):
            buffers.setdefault(name, []).append(df)


def flush(buffers, year):
    for name, frames in buffers.items():
        if not frames:
            continue
        path = PARQUET / f"{name}_{year}.parquet"
        new = pd.concat(frames, ignore_index=True)
        if path.exists():
            new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
            keys = [c for c in ("date", "code", "index_name") if c in new.columns]
            new = new.drop_duplicates(subset=keys, keep="last")
        new.to_parquet(path, index=False)
        print(f"    flushed {path.name}  rows={len(new)}", flush=True)
    buffers.clear()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", default="index,margin,pbr,shares")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD")
    ap.add_argument("--interval", type=float, default=2.5,
                    help="seconds between requests")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N fetched days (calibration runs)")
    args = ap.parse_args()

    manifest = load_manifest()
    sess = session(referer="https://www.twse.com.tw/zh/trading/historical/mi-index.html")
    throttle = Throttle(args.interval)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)

    for feed in [f.strip() for f in args.feeds.split(",") if f.strip()]:
        start = date.fromisoformat(args.start) if args.start else FEEDS[feed][2]
        done = set(manifest.get(feed, []))
        closed = set(manifest.get(feed + "_closed", []))
        buffers, count, year, failures = {}, 0, start.year, 0
        d = start
        while d <= end:
            if d.weekday() >= 5 or d.isoformat() in done or d.isoformat() in closed:
                d += timedelta(days=1)
                continue
            if d.year != year:
                flush(buffers, year)
                save_manifest(manifest)
                year = d.year
            parsed = fetch_day(sess, throttle, feed, d)
            if parsed is None:
                # skip this day rather than abandoning the feed, but stop if
                # failures are piling up -- that means we really are throttled
                failures += 1
                print(f"  {feed} {d}: giving up after retries "
                      f"({failures} consecutive)", flush=True)
                if failures >= 5:
                    print(f"  {feed}: too many consecutive failures, stopping",
                          flush=True)
                    break
                d += timedelta(days=1)
                continue
            failures = 0
            if parsed == "closed":
                closed.add(d.isoformat())
                manifest[feed + "_closed"] = sorted(closed)
            else:
                append(buffers, parsed)
                done.add(d.isoformat())
                manifest[feed] = sorted(done)
            count += 1
            if count % 20 == 0:
                print(f"  {feed} {d}  ({count} days)", flush=True)
            if count % 100 == 0:
                flush(buffers, year)
                save_manifest(manifest)
            if args.limit and count >= args.limit:
                break
            d += timedelta(days=1)
        flush(buffers, year)
        save_manifest(manifest)
        print(f"{feed}: {count} new days, reached {d}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
