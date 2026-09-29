"""Supervisor for the full-history backfill.

The fetchers stop themselves after five consecutive failed days, which is the
right call in the moment but would quietly leave a feed half done on a two-day
crawl. This runs them in rounds until a whole round adds nothing new, pausing
between rounds so a genuine throttle has time to clear, and reconciling the
manifest each time so days that produced no data get queued again.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime

from common import CACHE, ROOT

SCRIPTS = ROOT / "scripts"
COOLDOWN = 300          # seconds between rounds
MAX_ROUNDS = 40

JOBS = [
    ("TAIEX 1990+",   [sys.executable, "-u", "fetch_twse_index.py",
                       "--start", "1990-01-01", "--interval", "4.0"]),
    ("TWSE full",     [sys.executable, "-u", "fetch_twse.py",
                       "--feeds", "index,margin,pbr,shares", "--interval", "4.0"]),
    ("TPEx full",     [sys.executable, "-u", "fetch_tpex.py", "--interval", "3.5"]),
]


def manifest_total() -> int:
    total = 0
    for name in ("twse_manifest.json", "tpex_manifest.json"):
        path = CACHE / name
        if path.exists():
            total += sum(len(v) for v in json.loads(path.read_text()).values())
    return total


def stamp(msg: str):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def main():
    previous = manifest_total()
    stamp(f"starting; {previous} feed-days already recorded")

    for rnd in range(1, MAX_ROUNDS + 1):
        stamp(f"--- round {rnd} ---")
        # Reconcile first: it removes days that produced nothing, so running it
        # after the fetchers would cancel out their gains and look like a
        # finished backfill when it is not.
        stamp("reconciling")
        subprocess.call([sys.executable, "reconcile.py", "--apply"], cwd=SCRIPTS)
        previous = min(previous, manifest_total())

        for label, cmd in JOBS:
            stamp(f"running {label}")
            code = subprocess.call(cmd, cwd=SCRIPTS)
            if code != 0:
                stamp(f"{label} exited {code}")

        total = manifest_total()
        gained = total - previous
        stamp(f"round {rnd} added {gained} feed-days (total {total})")
        if gained <= 0:
            stamp("nothing new this round -- backfill complete")
            break
        previous = total
        stamp(f"cooling down {COOLDOWN}s")
        time.sleep(COOLDOWN)

    stamp("rebuilding site data")
    subprocess.call([sys.executable, "calibrate_params.py"], cwd=SCRIPTS)
    subprocess.call([sys.executable, "calibrate_weights.py"], cwd=SCRIPTS)
    subprocess.call([sys.executable, "build_site_data.py"], cwd=SCRIPTS)
    stamp("done")


if __name__ == "__main__":
    main()
