"""Bring data/store up to date: the daily job that runs in CI.

Fetches every trading day since the last complete one -- plus the last few
days again, because some reports (margin, shares) are published late in the
evening and a run can catch a day half-published -- reduces each to its daily
aggregates, and upserts them. Days that fail are simply left incomplete and
picked up by the next run.

Never needs the full per-stock history: 多空排列 runs over the rolling window of
closes kept in data/store, and everything else is per day.

Failure handling: each source runs on its own, so one failing source never
costs the others their data (CI commits whatever was saved, then fails the
job if needed). A network error is a warning -- the next run retries. Any
other exception is an error and fails the job at the end. A source with
trouble in HEALTH_LIMIT runs in a row also fails the job, so a source that
quietly stopped updating cannot hide behind a green run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, timedelta

import pandas as pd
import requests

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
SOURCES = ("TAIEX", "TWSE", "TPEx", "TAIFEX", "NDC")
HEALTH_LIMIT = 3        # runs in a row with trouble before a source fails the job
HEALTH = S.STORE / "health.json"

problems: list[tuple[str, str]] = []    # (source, message): warnings
errors: list[tuple[str, str]] = []      # (source, message): fail the job
_source = "general"                     # the source whose update is running


def warn(msg: str):
    problems.append((_source, msg))
    print(f"::warning::{msg}" if ON_CI else f"WARNING {msg}", flush=True)


def run_source(name: str, fn, *args):
    """Run one source's update; whatever it raises stays with that source.

    A network error (requests.RequestException) is a warning: the site is down
    or slow, and the next run retries. Anything else -- a changed format, a
    block page that parses wrong, a bug -- needs a person, so it is an error.
    """
    global _source
    _source = name
    try:
        fn(*args)
    except requests.RequestException as e:
        warn(f"{name}: {type(e).__name__}: {e}")
    except Exception as e:
        errors.append((name, f"{type(e).__name__}: {e}"))
        traceback.print_exc()
        print(f"::error title={name}::{type(e).__name__}: {e}" if ON_CI
              else f"ERROR {name}: {type(e).__name__}: {e}", flush=True)
    finally:
        _source = "general"


def update_health() -> dict:
    """Count, per source, the runs in a row that had a warning or an error.

    Kept in data/store/health.json on the data branch. A healthy source is
    stored as just {"failing_runs": 0}, so a clean run leaves the file as it was.
    """
    try:
        health = json.loads(HEALTH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        health = {}
    now = pd.Timestamp.now(tz="Asia/Taipei").strftime("%Y-%m-%d %H:%M")
    for src in SOURCES:
        msgs = [m for s, m in errors + problems if s == src]
        if msgs:
            h = health.get(src) or {}
            health[src] = {"failing_runs": h.get("failing_runs", 0) + 1,
                           "since": h.get("since") or now,
                           "last_problem": msgs[0][:300]}
        else:
            health[src] = {"failing_runs": 0}
    HEALTH.write_text(json.dumps(health, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    return health


def report(health: dict):
    """Print the run's outcome per source; on CI also as the job summary."""
    lines = ["| source | this run | runs in a row with trouble | first problem |",
             "| --- | --- | --- | --- |"]
    for src in SOURCES:
        err = [m for s, m in errors if s == src]
        warns = [m for s, m in problems if s == src]
        state = ("❌ error" if err else f"⚠️ {len(warns)} warning(s)" if warns else "✅ ok")
        n = health[src]["failing_runs"]
        flag = f"**{n}** (limit {HEALTH_LIMIT})" if n >= HEALTH_LIMIT else str(n)
        first = (err + warns)[0].replace("|", "/")[:200] if err or warns else ""
        lines.append(f"| {src} | {state} | {flag} | {first} |")
    other = [f"{s}: {m}" for s, m in errors + problems if s not in SOURCES]
    if other:
        lines += ["", "Other:"] + [f"- {m}" for m in other]
    text = "\n".join(lines) + "\n"
    print(text, flush=True)
    if ON_CI and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("### Daily update: sources\n\n" + text)


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
    frames = fetch_ndc.fetch_all()
    for name, df in frames.items():
        if len(df):
            # merge, not replace: the open data lacks some series the old API had
            S.write(name, fetch_ndc.merge(S.read(name, index=None), df), index=False)
    print("NDC: refreshed", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default=None, help="last day to fetch (default today)")
    ap.add_argument("--fail-test", action="store_true",
                    help="inject an error after the real update, to check that CI still "
                         "saves the data, deploys the site and then fails the run")
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

    run_source("TAIEX", update_taiex, days)
    run_source("TWSE", update_twse, days)
    run_source("TPEx", update_tpex, days)
    run_source("TAIFEX", update_taifex, days[0] - timedelta(days=10), end)
    run_source("NDC", update_ndc)
    if args.fail_test:
        def injected():
            raise RuntimeError("injected by --fail-test")
        run_source("fail-test", injected)
    S.write_meta(updated=pd.Timestamp.now(tz="Asia/Taipei").strftime("%Y-%m-%d %H:%M"))

    tw = S.read("twse_daily")
    print(f"done; twse_daily now ends {tw.index[-1]}, last complete "
          f"{last_complete(tw)}; {len(problems)} warnings, {len(errors)} errors", flush=True)

    health = update_health()
    report(health)
    stale = [s for s in SOURCES if health[s]["failing_runs"] >= HEALTH_LIMIT]
    if errors or stale:
        why = ([f"{s}: {m}" for s, m in errors]
               + [f"{s}: trouble in {health[s]['failing_runs']} runs in a row "
                  f"since {health[s]['since']}" for s in stale])
        print(f"::error title=Daily update failed::{'; '.join(why)}" if ON_CI
              else "FAILED: " + "; ".join(why), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
