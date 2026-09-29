"""Resumable TPEx (櫃買) daily crawler: quotes, P/B and margin balances.

TPEx is the other half of the market. Its daily quote report conveniently
carries 發行股數 alongside the close, so market-cap weighting needs only two
requests per day here, plus one for margin.
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

BASE = "https://www.tpex.org.tw/www/zh-tw"
LISTED = re.compile(r"^\d{4}$")
MANIFEST = CACHE / "tpex_manifest.json"

FEEDS = {
    # name:     (path,                    first date with data)
    "quotes":  ("afterTrading/dailyQuotes", date(2007, 1, 1)),
    "pbr":     ("afterTrading/peQryDate",   date(2007, 1, 1)),
    "margin":  ("margin/balance",           date(2007, 1, 1)),
}
REQUIRED = {"quotes": ("tpex_quotes",), "pbr": ("tpex_pbr",),
            "margin": ("tpex_margin_stock",)}


def num(x):
    s = str(x).replace(",", "").strip()
    if not re.match(r"^-?\d+(\.\d+)?$", s):
        return None
    return float(s)


def first_table(payload):
    tables = payload.get("tables") or []
    return tables[0] if tables else None


def parse_quotes(payload, day):
    t = first_table(payload)
    if not t or "代號" not in (t.get("fields") or []):
        return {}
    f = t["fields"]
    ic, icl = f.index("代號"), f.index("收盤")
    ish = f.index("發行股數") if "發行股數" in f else None
    icd = f.index("漲跌") if "漲跌" in f else None
    rows, up, down, flat = [], 0, 0, 0
    for r in t.get("data", []):
        code = str(r[ic]).strip()
        if not LISTED.match(code):
            continue
        rows.append({"date": day, "code": code, "close": num(r[icl]),
                     "shares": num(r[ish]) if ish is not None else None})
        if icd is None or code.startswith("0"):
            continue
        mark = re.sub(r"<[^>]+>", "", str(r[icd])).strip()
        up, down, flat = (up + 1, down, flat) if mark.startswith("+") else \
                         (up, down + 1, flat) if mark.startswith("-") else \
                         (up, down, flat + 1)
    return {"tpex_quotes": pd.DataFrame(rows),
            "tpex_breadth": pd.DataFrame([{"date": day, "up": float(up),
                                           "down": float(down),
                                           "unchanged": float(flat)}])}


def parse_pbr(payload, day):
    t = first_table(payload)
    if not t or "股票代號" not in (t.get("fields") or []):
        return {}
    f = t["fields"]
    ic = f.index("股票代號")
    ip = next((i for i, c in enumerate(f) if "淨值比" in c), None)
    if ip is None:
        return {}
    rows = [{"date": day, "code": str(r[ic]).strip(), "pbr": num(r[ip])}
            for r in t.get("data", []) if LISTED.match(str(r[ic]).strip())]
    return {"tpex_pbr": pd.DataFrame(rows)}


def parse_margin(payload, day):
    out = {}
    for t in payload.get("tables", []):
        f = t.get("fields") or []
        if "代號" in f and any("融資" in c or "餘額" in c for c in f):
            ic = f.index("代號")
            bal = next((i for i, c in enumerate(f) if "餘額" in c), None)
            if bal is None:
                continue
            rows = [{"date": day, "code": str(r[ic]).strip(), "margin_lots": num(r[bal])}
                    for r in t.get("data", []) if LISTED.match(str(r[ic]).strip())]
            if rows:
                out["tpex_margin_stock"] = pd.DataFrame(rows)
                break
    return out


PARSERS = {"quotes": parse_quotes, "pbr": parse_pbr, "margin": parse_margin}


def fetch_day(sess, throttle, feed, d):
    path, _ = FEEDS[feed]
    url = f"{BASE}/{path}?date={d:%Y/%m/%d}&response=json"
    if feed == "margin":
        url += "&type=Daily"
    for _ in range(5):
        throttle.wait()
        try:
            r = sess.get(url, timeout=90)
        except requests.RequestException as e:
            iv = throttle.penalise()
            print(f"    {d} {feed}: {type(e).__name__}, backing off to "
                  f"{iv:.1f}s", flush=True)
            continue
        if r.status_code >= 429:
            throttle.penalise()
            continue
        try:
            payload = r.json()
        except ValueError:
            throttle.penalise()
            continue
        t = first_table(payload)
        if t is None or not (t.get("data") or []):
            return "closed"          # market shut, or nothing published that day
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


def flush(buffers, year):
    for name, frames in buffers.items():
        if not frames:
            continue
        path = PARQUET / f"{name}_{year}.parquet"
        new = pd.concat(frames, ignore_index=True)
        if path.exists():
            new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
            keys = [c for c in ("date", "code") if c in new.columns]
            new = new.drop_duplicates(subset=keys, keep="last")
        new.to_parquet(path, index=False)
        print(f"    flushed {path.name}  rows={len(new)}", flush=True)
    buffers.clear()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", default="quotes,pbr,margin")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--interval", type=float, default=3.5)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    sess = session(referer="https://www.tpex.org.tw/")
    throttle = Throttle(args.interval)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)

    for feed in [f.strip() for f in args.feeds.split(",") if f.strip()]:
        start = date.fromisoformat(args.start) if args.start else FEEDS[feed][1]
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
                MANIFEST.write_text(json.dumps(manifest, indent=0, sort_keys=True))
                year = d.year
            parsed = fetch_day(sess, throttle, feed, d)
            if parsed is None:
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
                for name, df in parsed.items():
                    if df is not None and len(df):
                        buffers.setdefault(name, []).append(df)
                done.add(d.isoformat())
                manifest[feed] = sorted(done)
            count += 1
            if count % 20 == 0:
                print(f"  {feed} {d}  ({count} days)", flush=True)
            if count % 100 == 0:
                flush(buffers, year)
                MANIFEST.write_text(json.dumps(manifest, indent=0, sort_keys=True))
            if args.limit and count >= args.limit:
                break
            d += timedelta(days=1)
        flush(buffers, year)
        MANIFEST.write_text(json.dumps(manifest, indent=0, sort_keys=True))
        print(f"{feed}: {count} new days, reached {d}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
