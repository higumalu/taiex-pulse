"""Fetch NDC (國發會) monthly macro series: PMI, NMI, 景氣指標/對策信號.

The site is a Laravel + AngularJS app whose data endpoints are POST-only and
CSRF-protected, so every call needs a session cookie plus the csrf-token that
the corresponding HTML page carries in a <meta> tag.
"""
from __future__ import annotations

import re

import pandas as pd

from common import RAW, save, session

BASE = "https://index.ndc.gov.tw"

# page providing the CSRF token  ->  POST endpoint returning the series
SOURCES = {
    "ndc_pmi": ("/n/zh_tw/data/PMI", "/n/json/data/PMI/total"),
    "ndc_nmi": ("/n/zh_tw/data/NMI", "/n/json/data/NMI/total"),
    "ndc_eco": ("/n/zh_tw/data/eco", "/n/json/data/eco/indicators"),
}


def fetch(page: str, endpoint: str) -> dict:
    s = session(referer=BASE + page)
    html = s.get(BASE + page, timeout=30).text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    if not m:
        raise RuntimeError(f"no csrf-token on {page}")
    s.headers.update({"X-CSRF-TOKEN": m.group(1), "X-Requested-With": "XMLHttpRequest"})
    r = s.post(BASE + endpoint, timeout=60)
    r.raise_for_status()
    return r.json()


def to_frame(payload: dict) -> pd.DataFrame:
    """`line` maps a series id -> {name, unit, code, data:[{x:'YYYYMM', y:float}]}."""
    rows = []
    for sid, series in payload.get("line", {}).items():
        for pt in series.get("data", []):
            if pt.get("y") is None:
                continue
            x = str(pt["x"])
            rows.append({
                "month": f"{x[:4]}-{x[4:6]}",
                "series_id": sid,
                "name": series.get("name"),
                "code": series.get("code"),
                "unit": series.get("unit"),
                "value": float(pt["y"]),
            })
    return pd.DataFrame(rows).sort_values(["name", "month"]).reset_index(drop=True)


def main():
    for name, (page, endpoint) in SOURCES.items():
        print(f"{name}: POST {endpoint}")
        payload = fetch(page, endpoint)
        (RAW / f"{name}.json").write_text(
            pd.io.json.ujson_dumps(payload) if hasattr(pd.io.json, "ujson_dumps")
            else __import__("json").dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        df = to_frame(payload)
        for nm, g in df.groupby("name"):
            print(f"     {nm:<24s} {g.month.min()} .. {g.month.max()}  n={len(g)}")
        save(df, name)


if __name__ == "__main__":
    main()
