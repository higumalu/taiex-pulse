"""Shared helpers: paths, throttled HTTP sessions, parquet IO."""
from __future__ import annotations

import random
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
CACHE = DATA / "cache"
PARQUET = DATA / "parquet"
# FinLab's archived dashboard: validation and calibration only, never published.
# Only finlab_archive.py reads it; build_site_data.py must not.
FINLAB = DATA / "finlab_archive"
for _d in (RAW, CACHE, PARQUET):
    _d.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")


class Throttle:
    """Space out requests, back off hard on trouble, recover slowly.

    TWSE does not answer with 429 -- it quietly returns a payload with tables
    missing (see README). So callers report trouble explicitly via `penalise()`,
    and the interval only creeps back toward the base rate after a long run of
    clean responses. Jitter keeps the request pattern from being perfectly
    periodic.
    """

    def __init__(self, interval: float, ceiling: float = 45.0,
                 recover_after: int = 40):
        self.base = interval
        self.interval = interval
        self.ceiling = ceiling
        self.recover_after = recover_after
        self._last = 0.0
        self._clean = 0

    def wait(self):
        delay = self.interval * random.uniform(0.85, 1.25)
        gap = time.monotonic() - self._last
        if gap < delay:
            time.sleep(delay - gap)
        self._last = time.monotonic()

    def penalise(self, reason: str = "") -> float:
        """Called when a reply looks throttled. Returns the new interval."""
        self._clean = 0
        self.interval = min(self.interval * 1.6, self.ceiling)
        return self.interval

    def reward(self):
        """Called after a clean reply; eases back toward the base rate."""
        self._clean += 1
        if self._clean >= self.recover_after and self.interval > self.base:
            self.interval = max(self.base, self.interval * 0.85)
            self._clean = 0


def session(referer: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    if referer:
        s.headers.update({"Referer": referer})
    return s


def save(df: pd.DataFrame, name: str) -> Path:
    """Write a tidy frame to data/parquet/<name>.parquet and report."""
    path = PARQUET / f"{name}.parquet"
    df.to_parquet(path, index=False)
    print(f"  -> {path.relative_to(ROOT)}  rows={len(df)}  cols={list(df.columns)}")
    return path


def load(name: str) -> pd.DataFrame:
    return pd.read_parquet(PARQUET / f"{name}.parquet")


def roc_to_date(s: str) -> str:
    """'115年09月18日' / '115/09/18' -> '2026-09-18'."""
    digits = [d for d in s.replace("年", "/").replace("月", "/").replace("日", "").split("/") if d]
    y, m, d = int(digits[0]) + 1911, int(digits[1]), int(digits[2])
    return f"{y:04d}-{m:02d}-{d:02d}"
