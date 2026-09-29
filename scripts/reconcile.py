"""Drop manifest days that produced no data, so the crawler re-fetches them.

TWSE will answer a fast client with stat "OK" and a payload missing its tables.
Older runs recorded those days as finished; this walks the manifest against what
actually landed in parquet and removes any day with nothing to show for it.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter

import pandas as pd

from common import CACHE, PARQUET
from fetch_twse import FEEDS, MANIFEST, REQUIRED


def days_present(prefix: str) -> set[str]:
    days: set[str] = set()
    for path in PARQUET.glob(f"{prefix}_*.parquet"):
        days |= set(pd.read_parquet(path, columns=["date"])["date"].unique())
    return days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the cleaned manifest (default is a dry run)")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    cleaned, report = {}, []

    for feed in FEEDS:
        cleaned[feed + "_closed"] = manifest.get(feed + "_closed", [])
        done = manifest.get(feed, [])
        if not done:
            continue
        have = set.intersection(*(days_present(p) for p in REQUIRED[feed])) \
            if REQUIRED[feed] else set()
        keep = [d for d in done if d in have]
        drop = [d for d in done if d not in have]
        cleaned[feed] = keep
        by_year = Counter(d[:4] for d in drop)
        report.append((feed, len(done), len(keep), len(drop), dict(sorted(by_year.items()))))

    print(f"{'feed':<9}{'recorded':>10}{'with data':>11}{'to refetch':>12}   by year")
    for feed, total, keep, drop, by_year in report:
        print(f"{feed:<9}{total:>10}{keep:>11}{drop:>12}   {by_year}")

    total_drop = sum(r[3] for r in report)
    if not args.apply:
        print(f"\ndry run: {total_drop} days would be re-fetched. Pass --apply to write.")
        return

    backup = CACHE / "twse_manifest.backup.json"
    backup.write_text(json.dumps(manifest, indent=0, sort_keys=True))
    MANIFEST.write_text(json.dumps(cleaned, indent=0, sort_keys=True))
    print(f"\nwrote cleaned manifest ({total_drop} days queued for refetch); "
          f"previous copy at {backup.name}")


if __name__ == "__main__":
    main()
