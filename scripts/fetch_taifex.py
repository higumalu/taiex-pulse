"""Fetch TAIFEX daily institutional (三大法人) futures positions.

The public CSV export accepts a date range; TAIFEX rejects ranges that are too
wide, so we walk it in chunks and cache each chunk on disk.
"""
from __future__ import annotations

import io
import sys
from datetime import date, timedelta

import pandas as pd

from common import CACHE, Throttle, save, session

URL = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
REFERER = "https://www.taifex.com.tw/cht/3/futContractsDate"
CHUNK_DAYS = 90
# TAIFEX only serves this export for roughly the last two years; anything
# older has to come from the archived FinLab series (see README).
START = date(2024, 1, 1)

# 大台 / 小台 / 微台 -- the three TAIEX-sized futures FinLab aggregates
TAIEX_FUTURES = {"臺股期貨", "小型臺指期貨", "微型臺指期貨"}


def post_range(sess, throttle, d0: date, d1: date) -> str:
    throttle.wait()
    r = sess.post(URL, timeout=60, data={
        "queryType": "2", "doQuery": "1", "commodityId": "",
        "queryStartDate": f"{d0:%Y/%m/%d}", "queryEndDate": f"{d1:%Y/%m/%d}",
    })
    r.raise_for_status()
    # TAIFEX serves the export as Big5, not UTF-8
    return r.content.decode("cp950", errors="replace")


def fetch_chunk(sess, throttle, d0: date, d1: date) -> pd.DataFrame | None:
    cache = CACHE / f"taifex_{d0:%Y%m%d}_{d1:%Y%m%d}.csv"
    if cache.exists():
        text = cache.read_text(encoding="utf-8")
    else:
        # A range that ends on a day with no data yet (today before the 15:00
        # publication, or a holiday) comes back as an HTML error page for the
        # whole range. Pull the end date back until the export is a CSV.
        end = d1
        text = post_range(sess, throttle, d0, end)
        while "日期" not in text[:200] and end > d0 and (d1 - end).days < 7:
            end -= timedelta(days=1)
            text = post_range(sess, throttle, d0, end)
        # Only a range that is closed for good may be cached; a CSV that stops
        # short of d1 would otherwise hide the missing days forever.
        if "日期" in text[:200] and end == d1 and d1 < date.today():
            cache.write_text(text, encoding="utf-8")
    if "日期" not in text[:200]:
        return None
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    return df


def tidy_positions(raw: pd.DataFrame) -> pd.DataFrame:
    """Net open-interest value per date, product and investor, TAIEX-sized futures only."""
    raw = raw.copy()
    raw["date"] = pd.to_datetime(raw["日期"]).dt.strftime("%Y-%m-%d")
    net = "多空未平倉契約金額淨額(千元)"
    sub = raw[raw["商品名稱"].isin(TAIEX_FUTURES)].copy()
    sub[net] = pd.to_numeric(sub[net].astype(str).str.replace(",", ""), errors="coerce")
    # Before the 15:00 publication the export already lists today, with every
    # figure 0. A genuine day never nets to exactly zero across all groups.
    published = sub.groupby("date")[net].apply(lambda v: v.abs().sum() > 0)
    sub = sub[sub["date"].map(published)]
    return (sub.groupby(["date", "商品名稱", "身份別"], as_index=False)[net]
               .sum()
               .rename(columns={"商品名稱": "product", "身份別": "investor",
                                net: "net_oi_value_k"}))


def net_oi(tidy: pd.DataFrame) -> pd.DataFrame:
    """The three institutional groups summed: one net value per date."""
    return tidy.groupby("date", as_index=False)["net_oi_value_k"].sum()


def main():
    end = date.today()
    sess = session(referer=REFERER)
    throttle = Throttle(1.5)
    frames, cur = [], START
    while cur <= end:
        stop = min(cur + pd.Timedelta(days=CHUNK_DAYS - 1).to_pytimedelta(), end)
        df = fetch_chunk(sess, throttle, cur, stop)
        if df is not None and len(df):
            frames.append(df)
            print(f"  {cur} .. {stop}  rows={len(df)}", flush=True)
        else:
            print(f"  {cur} .. {stop}  (empty)", flush=True)
        cur = stop + pd.Timedelta(days=1).to_pytimedelta()

    raw = pd.concat(frames, ignore_index=True)
    print("products found:", sorted(raw["商品名稱"].dropna().unique())[:20])
    tidy = tidy_positions(raw)
    save(tidy, "taifex_inst_futures")

    daily = net_oi(tidy)
    print(f"  aggregate span {daily.date.min()} .. {daily.date.max()}  n={len(daily)}")
    save(daily, "taifex_inst_net_oi")


if __name__ == "__main__":
    sys.exit(main())
