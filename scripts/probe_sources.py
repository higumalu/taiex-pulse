"""Check that every data source answers from this machine, with real data.

Fetches one recent trading day from each feed through the crawlers' own request
code and reports what came back. HTTP 200 is not enough: TWSE signals
throttling by dropping tables, so a feed only passes when its parser finds
every table it requires. Writes nothing to data/.

On GitHub Actions each result is also emitted as an annotation, so the outcome
can be read from the run page without downloading logs.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta

import requests

import fetch_ndc
import fetch_taifex
import fetch_tpex
import fetch_twse
import fetch_twse_index
from common import Throttle, session

ON_CI = os.environ.get("GITHUB_ACTIONS") == "true"
results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<24s} {detail}", flush=True)
    if ON_CI:
        level = "notice" if ok else "error"
        print(f"::{level} title={name}::{'PASS' if ok else 'FAIL'} {detail}", flush=True)


def probe(name: str, fn):
    t0 = time.monotonic()
    try:
        ok, detail = fn()
    except Exception as e:                       # report, never abort the sweep
        ok, detail = False, f"{type(e).__name__}: {str(e)[:160]}"
    report(name, ok, f"{detail} ({time.monotonic() - t0:.1f}s)")


def recent_weekdays(n: int = 7):
    d = date.today() - timedelta(days=1)
    while n:
        if d.weekday() < 5:
            yield d
            n -= 1
        d -= timedelta(days=1)


def day_feed(module, sess, feed: str):
    """Walk back from yesterday until a day with data; holidays answer 'closed'."""
    throttle = Throttle(3.5)
    for d in recent_weekdays():
        parsed = module.fetch_day(sess, throttle, feed, d)
        if parsed is None:
            return False, f"{d}: no valid reply after retries (throttled or blocked)"
        if parsed == "closed":
            continue
        rows = {k: len(v) for k, v in parsed.items() if v is not None}
        return True, f"{d}: " + ", ".join(f"{k}={v}" for k, v in rows.items())
    return False, "every recent weekday answered 'closed'"


def main():
    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
        print(f"egress IP: {ip}", flush=True)
    except requests.RequestException:
        pass

    twse = session(referer="https://www.twse.com.tw/")
    for feed in ("index", "margin", "pbr", "shares"):
        probe(f"TWSE {feed}", lambda f=feed: day_feed(fetch_twse, twse, f))

    def fmtqik():
        rows = fetch_twse_index.month_rows(twse, Throttle(3.5), date.today().replace(day=1))
        return bool(rows), f"{len(rows)} days this month" if rows else "no rows"
    probe("TWSE FMTQIK", fmtqik)

    tpex = session(referer="https://www.tpex.org.tw/")
    for feed in ("quotes", "pbr", "margin"):
        probe(f"TPEx {feed}", lambda f=feed: day_feed(fetch_tpex, tpex, f))

    for name, (page, endpoint) in fetch_ndc.SOURCES.items():
        def ndc(page=page, endpoint=endpoint):
            df = fetch_ndc.to_frame(fetch_ndc.fetch(page, endpoint))
            return len(df) > 0, f"{len(df)} rows, latest {df.iloc[:, 0].max()}"
        probe(f"NDC {name}", ndc)

    def taifex():
        # fetch_chunk never caches a range that reaches today, so this writes nothing.
        d1 = date.today()
        s = session(referer=fetch_taifex.REFERER)
        df = fetch_taifex.fetch_chunk(s, Throttle(1.5), d1 - timedelta(days=10), d1)
        if df is None or not len(df):
            return False, "no CSV for any end date in the last week"
        return True, f"{len(df)} rows, latest {df['日期'].max()}"
    probe("TAIFEX futures", taifex)

    failed = [n for n, ok, _ in results if not ok]
    summary = (f"{len(results) - len(failed)}/{len(results)} sources OK"
               + (f"; failed: {', '.join(failed)}" if failed else ""))
    print(summary, flush=True)
    if ON_CI:
        print(f"::{'error' if failed else 'notice'} title=Summary::{summary}", flush=True)
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("| source | result | detail |\n| --- | --- | --- |\n")
            for n, ok, d in results:
                f.write(f"| {n} | {'✅' if ok else '❌'} | {d} |\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
