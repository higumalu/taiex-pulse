"""One-glance progress report for the backfill."""
from __future__ import annotations

import json
from datetime import date

from common import CACHE, PARQUET, ROOT

# feed -> (label, first date with data, source)
TARGETS = {
    "twse": {
        "index":  ("上市 指數/個股/漲跌", date(2004, 2, 11)),
        "margin": ("上市 融資餘額",       date(2004, 2, 11)),
        "pbr":    ("上市 股價淨值比",     date(2005, 9, 1)),
        "shares": ("上市 發行股數",       date(2004, 2, 11)),
    },
    "tpex": {
        "quotes": ("上櫃 收盤/股數",      date(2007, 1, 1)),
        "pbr":    ("上櫃 股價淨值比",     date(2007, 1, 1)),
        "margin": ("上櫃 融資餘額",       date(2007, 1, 1)),
    },
}


def trading_days(start: date) -> int:
    """Rough count: weekdays minus the ~9 public holidays a year."""
    days = (date.today() - start).days
    return max(int(days * 5 / 7) - int(days / 365 * 9), 1)


def main():
    print(f"{'feed':<22}{'done':>8}{'target':>9}{'':>4}progress")
    total_done = total_target = 0
    for source, feeds in TARGETS.items():
        path = CACHE / f"{source}_manifest.json"
        manifest = json.loads(path.read_text()) if path.exists() else {}
        for feed, (label, first) in feeds.items():
            done = len(manifest.get(feed, [])) + len(manifest.get(feed + "_closed", []))
            target = trading_days(first)
            total_done += done
            total_target += target
            pct = min(100, 100 * done / target)
            bar = "#" * int(pct / 4)
            print(f"{label:<22}{done:>8}{target:>9}    {bar:<25}{pct:5.1f}%")

    pct = 100 * total_done / total_target
    left = max(total_target - total_done, 0)
    print(f"\n{'total':<22}{total_done:>8}{total_target:>9}    {pct:5.1f}%")
    print(f"remaining ~{left} requests, roughly {left * 5 / 3600:.1f} h at the "
          f"current pacing")

    sizes = sum(p.stat().st_size for p in PARQUET.glob("*.parquet"))
    print(f"parquet on disk: {sizes / 1e6:.0f} MB")

    log = ROOT / "logs" / "backfill.log"
    if log.exists():
        tail = [ln for ln in log.read_text(encoding="utf-8",
                                           errors="replace").splitlines() if ln.strip()]
        print("\nlast lines:")
        for ln in tail[-4:]:
            print("  " + ln)


if __name__ == "__main__":
    main()
