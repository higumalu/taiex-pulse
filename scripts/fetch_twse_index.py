"""Fetch the daily TAIEX close from TWSE's FMTQIK report.

MI_INDEX only carries the index tables from 2009 onwards, but FMTQIK returns a
whole month of daily closes per request and reaches back to 1990 -- so the whole
history costs a few hundred requests instead of one per trading day.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

import pandas as pd

from common import Throttle, roc_to_date, save, session

URL = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
FIRST = date(1990, 1, 1)


def month_rows(sess, throttle, d: date) -> list[dict]:
    throttle.wait()
    r = sess.get(f"{URL}?date={d:%Y%m}01&response=json", timeout=60)
    r.raise_for_status()
    payload = r.json()
    if payload.get("stat") != "OK":
        return []
    cols = payload.get("fields") or []
    if "發行量加權股價指數" not in cols:
        return []
    di, ci = cols.index("日期"), cols.index("發行量加權股價指數")
    rows = []
    for row in payload.get("data", []):
        close = str(row[ci]).replace(",", "").strip()
        if not close or close in ("--", "0.00"):
            continue
        rows.append({"date": roc_to_date(str(row[di]).strip()), "close": float(close)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=FIRST.isoformat())
    ap.add_argument("--interval", type=float, default=3.5)
    args = ap.parse_args()

    sess = session(referer="https://www.twse.com.tw/zh/trading/historical/fmtqik.html")
    throttle = Throttle(args.interval)
    cur = date.fromisoformat(args.start).replace(day=1)
    today = date.today()

    rows, empty_months = [], 0
    while cur <= today:
        got = month_rows(sess, throttle, cur)
        rows += got
        if got:
            empty_months = 0
        else:
            empty_months += 1
            if empty_months >= 6 and cur.year > today.year - 2:
                break
        if cur.month % 12 == 0:
            print(f"  {cur:%Y}  rows so far: {len(rows)}", flush=True)
        cur = (cur.replace(day=28) + timedelta(days=7)).replace(day=1)

    df = (pd.DataFrame(rows).drop_duplicates("date").sort_values("date")
          .reset_index(drop=True))
    print(f"TAIEX daily closes: {len(df)} rows, {df.date.min()} .. {df.date.max()}")
    save(df, "twse_taiex")


if __name__ == "__main__":
    sys.exit(main())
