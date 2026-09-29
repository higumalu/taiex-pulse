"""Bring data/store up to date: the daily job that runs in CI.

Fetches every trading day since the last complete one -- plus the last few
days again, because some reports (margin, shares) are published late in the
evening and a run can catch a day half-published -- reduces each to its daily
aggregates, and upserts them. Days that fail are simply left incomplete and
picked up by the next run.

Never needs the full per-stock history: 多空排列 runs over the rolling window of
closes kept in data/store, and everything else is per day.
"""
from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

import pandas as pd

import fetch_ndc
import fetch_taifex
import fetch_tpex
import fetch_twse
import fetch_twse_index
import indicators as I
import store as S
from common import Throttle, session

REFETCH_DAYS = 5        # weekdays re-fetched even when already complete
MAX_DAYS = 45           # catch-up cap per run; the next run continues
ON_CI = os.environ.get("GITHUB_ACTIONS") == "true"
problems: list[str] = []


def warn(msg: str):
    problems.append(msg)
    print(f"::warning::{msg}" if ON_CI else f"WARNING {msg}", flush=True)


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


CORE = ["close_0050", "margin_loans", "cap", "up"]
LOOKBACK = 30           # trading days checked for holes on every run


def last_complete(tw: pd.DataFrame) -> date | None:
    ok = tw.dropna(subset=[c for c in CORE if c in tw.columns])
    return date.fromisoformat(ok.index[-1]) if len(ok) else None


def holes(tw: pd.DataFrame, tp: pd.DataFrame, taiex: pd.DataFrame) -> list[date]:
    """Recent trading days with a missing or half-filled row.

    A day that failed in one run is otherwise only retried while it is still
    among the last REFETCH_DAYS, so a failure followed by later successes
    would leave a permanent gap. The TAIEX calendar says which days traded.
    """
    days = list(taiex.index[-LOOKBACK:]) if len(taiex) else []
    full = set(tw.dropna(subset=[c for c in CORE if c in tw.columns]).index)
    tp_ok = set(tp.dropna(subset=["cap"]).index) if len(tp) else set()
    return [date.fromisoformat(d) for d in days
            if d not in full or (len(tp) and d not in tp_ok)]


def fetch_days(module, feeds, days, referer, interval) -> dict[str, list[pd.DataFrame]]:
    """{table name: [frames]} for every feed and day that came back complete."""
    sess, throttle = session(referer=referer), Throttle(interval)
    frames: dict[str, list[pd.DataFrame]] = {}
    for d in days:
        for feed in feeds:
            parsed = module.fetch_day(sess, throttle, feed, d)
            if parsed is None:
                warn(f"{module.__name__} {feed} {d}: no valid reply, will retry next run")
                continue
            if parsed == "closed":
                continue
            for name, df in parsed.items():
                if df is not None and len(df):
                    frames.setdefault(name, []).append(df)
        print(f"  {module.__name__} {d} done", flush=True)
    return frames


def table(frames, name) -> pd.DataFrame:
    parts = frames.get(name)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def update_twse(days):
    f = fetch_days(fetch_twse, ("index", "margin", "pbr", "shares"), days,
                   "https://www.twse.com.tw/", 4.0)
    prices = table(f, "twse_prices")
    window = S.read("recent_twse_close", index=None)
    if len(prices):
        window = pd.concat([window, prices], ignore_index=True)
        window = window.drop_duplicates(["date", "code"], keep="last")
    need = max(I.STACK_MAS)
    if len(prices) and window["date"].nunique() < need + prices["date"].nunique():
        warn(f"only {window['date'].nunique()} days of closes in the window; "
             f"多空排列 needs {need} before the first new day")
    agg = S.twse_aggregates(prices, table(f, "twse_breadth"),
                            table(f, "twse_margin_stock"), table(f, "twse_margin_total"),
                            table(f, "twse_pbr"), table(f, "twse_shares"),
                            stack_prices=window if len(window) else None)
    if len(agg):
        S.upsert("twse_daily", agg)
        S.write("recent_twse_close", S.trim_window(window), index=False)
    if len(prices):
        # A day filled in late changes the moving averages of every day after
        # it, so recount all of them -- but only days with a full MA history
        # in the window, or the oldest ones would come out as zeros.
        counts = I.stack_counts(window)
        counts = counts.iloc[need - 1:]
        counts = counts[counts.index >= prices["date"].min()]
        S.upsert("twse_daily", counts)
    print(f"TWSE: {len(agg)} days upserted", flush=True)


def update_tpex(days):
    f = fetch_days(fetch_tpex, ("quotes", "pbr"), days, "https://www.tpex.org.tw/", 3.5)
    agg = S.tpex_aggregates(table(f, "tpex_quotes"), table(f, "tpex_pbr"))
    if len(agg):
        S.upsert("tpex_daily", agg)
    print(f"TPEx: {len(agg)} days upserted", flush=True)


def update_taiex(days):
    sess, throttle = session(referer="https://www.twse.com.tw/"), Throttle(4.0)
    rows = []
    for month in sorted({d.replace(day=1) for d in days}):
        try:
            rows += fetch_twse_index.month_rows(sess, throttle, month)
        except Exception as e:
            warn(f"FMTQIK {month:%Y-%m}: {type(e).__name__}: {e}")
    if rows:
        S.upsert("taiex", pd.DataFrame(rows).drop_duplicates("date").set_index("date"))
    print(f"TAIEX: {len(rows)} month-rows upserted", flush=True)


def update_taifex(start: date, end: date):
    sess = session(referer=fetch_taifex.REFERER)
    raw = fetch_taifex.fetch_chunk(sess, Throttle(1.5), start, end)
    if raw is None or not len(raw):
        warn(f"TAIFEX {start}..{end}: no CSV")
        return
    daily = fetch_taifex.net_oi(fetch_taifex.tidy_positions(raw)).set_index("date")
    S.upsert("taifex_net_oi", daily)
    # an earlier run may have stored an unpublished day as zeros
    stored = S.read("taifex_net_oi")
    if (stored["net_oi_value_k"] == 0).any():
        S.write("taifex_net_oi", stored[stored["net_oi_value_k"] != 0])
    print(f"TAIFEX: {len(daily)} days upserted", flush=True)


def update_ndc():
    for name, (page, endpoint) in fetch_ndc.SOURCES.items():
        try:
            df = fetch_ndc.to_frame(fetch_ndc.fetch(page, endpoint))
        except Exception as e:
            warn(f"NDC {name}: {type(e).__name__}: {e}")
            continue
        if len(df):
            S.write(name, df, index=False)      # small enough to replace whole
    print("NDC: refreshed", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default=None, help="last day to fetch (default today)")
    args = ap.parse_args()

    I.load_params()
    S.check_meta()
    tw = S.read("twse_daily")
    if tw.empty:
        raise SystemExit("data/store is empty -- seed it with `python store.py export`")

    end = date.fromisoformat(args.end) if args.end else date.today()
    last = last_complete(tw)
    recent = weekdays(end - timedelta(days=14), end)[-REFETCH_DAYS:]
    missing = weekdays(last + timedelta(days=1), end) if last else []
    gaps = holes(tw, S.read("tpex_daily"), S.read("taiex"))
    if gaps:
        print(f"re-fetching {len(gaps)} incomplete trading days: "
              f"{', '.join(map(str, gaps))}", flush=True)
    days = sorted(set(missing[:MAX_DAYS]) | set(recent) | set(gaps))
    if len(missing) > MAX_DAYS:
        warn(f"{len(missing)} weekdays behind; fetching the oldest {MAX_DAYS} this run")
    print(f"last complete day {last}; fetching {len(days)} weekdays "
          f"{days[0]} .. {days[-1]}", flush=True)

    update_taiex(days)
    update_twse(days)
    update_tpex(days)
    update_taifex(days[0] - timedelta(days=10), end)
    update_ndc()
    S.write_meta(updated=pd.Timestamp.now(tz="Asia/Taipei").strftime("%Y-%m-%d %H:%M"))

    tw = S.read("twse_daily")
    print(f"done; twse_daily now ends {tw.index[-1]}, last complete "
          f"{last_complete(tw)}; {len(problems)} warnings", flush=True)


if __name__ == "__main__":
    main()
