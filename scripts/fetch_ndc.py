"""Fetch NDC (國發會) monthly macro series: PMI, NMI, 景氣指標/對策信號.

Since 2026-11-12 index.ndc.gov.tw (the JSON API behind the NDC charts) answers
GitHub's runners with a Cloudflare 403, so we read NDC's open-data releases
on data.gov.tw instead. Their files are served from ws.ndc.gov.tw, which the
runners can still reach:

  data.gov.tw 6100  臺灣採購經理人指數   CSV  Date,PMI,NMI (PMI seasonally adjusted)
  data.gov.tw 6099  景氣指標及燈號       ZIP  景氣指標與燈號.csv and others

The open data carries fewer series than the old API (no PMI sub-indices, no
未來六個月展望, no unadjusted PMI). update_daily merges what it gets into the
stored tables, so series it no longer gets keep their history and stop there.
"""
from __future__ import annotations

import io
import time
import zipfile

import pandas as pd
import requests

from common import save, session

API = "https://data.gov.tw/api/v2/rest/dataset/{}"
# Last-known download links, used when the data.gov.tw catalogue is unreachable.
FALLBACK = {
    6100: "https://ws.ndc.gov.tw/Download.ashx?u=LzAwMS9hZG1pbmlzdHJhdG9yLzEwL3JlbGZpbGUvNTc4MS82MzkxL2JmOGE0ZWI3LTEwZmUtNGZhMC1iNjQ2LTMwZTg5MGQwMjE4YS5jc3Y%3d&n=6Ie654Gj5o6h6LO857aT55CG5Lq65oyH5pW4KHBtaeWPim5taSkuY3N2&icon=.csv",
    6099: "https://ws.ndc.gov.tw/Download.ashx?u=LzAwMS9hZG1pbmlzdHJhdG9yLzEwL3JlbGZpbGUvNTc4MS82MzkyL2VhMjM1YmQ5LWQwNTItNGE2OS1hYmZjLWQ1Yzc4NWQzZDBlMi56aXA%3d&n=5pmv5rCj5oyH5qiZ5Y%2bK54eI6JmfLnppcA%3d%3d&icon=.zip",
}

# open-data column -> (stored name, stored unit); names match the old API's so
# build_site_data and the existing history line up.
PMI_COLUMNS = {"PMI": ("製造業PMI(季調值)", "(%)")}
NMI_COLUMNS = {"NMI": ("臺灣非製造業NMI", "(%)")}
ECO_FILE = "景氣指標與燈號.csv"
ECO_COLUMNS = {
    "領先指標綜合指數": ("領先指標綜合指數", "(點)"),
    "領先指標不含趨勢指數": ("領先指標不含趨勢指數", "(點)"),
    "同時指標綜合指數": ("同時指標綜合指數", "(點)"),
    "同時指標不含趨勢指數": ("同時指標不含趨勢指數", "(點)"),
    "落後指標綜合指數": ("落後指標綜合指數", "(點)"),
    "落後指標不含趨勢指數": ("落後指標不含趨勢指數", "(點)"),
    "景氣對策信號綜合分數": ("景氣對策信號", "(分)"),
    "景氣對策信號": ("景氣對策信號", "(燈號)"),
}
# the old API coded the light as 1..5
LIGHTS = {"藍": 1, "黃藍": 2, "綠": 3, "黃紅": 4, "紅": 5}

# stored table -> (dataset id, columns)
SOURCES = {
    "ndc_pmi": (6100, PMI_COLUMNS),
    "ndc_nmi": (6100, NMI_COLUMNS),
    "ndc_eco": (6099, ECO_COLUMNS),
}


def download_url(sess, dataset: int) -> str:
    try:
        r = sess.get(API.format(dataset), timeout=30)
        r.raise_for_status()
        return r.json()["result"]["distribution"][0]["resourceDownloadUrl"]
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        print(f"  data.gov.tw {dataset}: {type(e).__name__}; using the last-known link",
              flush=True)
        return FALLBACK[dataset]


def download(dataset: int, attempts: int = 3) -> bytes:
    sess = session(referer="https://data.gov.tw/")
    url = download_url(sess, dataset)
    for i in range(attempts):
        r = sess.get(url, timeout=60)
        if r.status_code < 500 or i == attempts - 1:
            break
        time.sleep(10 * (i + 1))
    r.raise_for_status()
    return r.content


def table(dataset: int, raw: bytes) -> pd.DataFrame:
    """The dataset's CSV as strings, one row per month (Date = YYYYMM)."""
    if dataset == 6099:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for info in z.infolist():
                # the zip stores Big5 names without the UTF-8 flag
                name = (info.filename if info.flag_bits & 0x800
                        else info.filename.encode("cp437").decode("cp950", "replace"))
                if name == ECO_FILE:
                    raw = z.read(info)
                    break
            else:
                raise RuntimeError(f"{ECO_FILE} missing from dataset 6099")
    df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    return df


def to_frame(df: pd.DataFrame, columns: dict) -> pd.DataFrame:
    """Long format matching the stored tables: month, series_id, name, code, unit, value."""
    missing = set(columns) - set(df.columns)
    if missing:
        raise RuntimeError(f"columns missing from NDC open data: {sorted(missing)}")
    month = df["Date"].str.strip().str[:4] + "-" + df["Date"].str.strip().str[4:6]
    rows = []
    for col, (name, unit) in columns.items():
        v = df[col].str.strip()
        v = v.map(LIGHTS) if unit == "(燈號)" else pd.to_numeric(v, errors="coerce")
        rows.append(pd.DataFrame({"month": month, "series_id": None, "name": name,
                                  "code": None, "unit": unit, "value": v}))
    out = pd.concat(rows, ignore_index=True).dropna(subset=["value"])
    out["value"] = out["value"].astype(float)
    return out.sort_values(["name", "unit", "month"]).reset_index(drop=True)


def fetch_all() -> dict[str, pd.DataFrame]:
    """One frame per stored table; each dataset is downloaded once."""
    raw = {ds: table(ds, download(ds)) for ds in sorted({ds for ds, _ in SOURCES.values()})}
    return {name: to_frame(raw[ds], cols) for name, (ds, cols) in SOURCES.items()}


def merge(stored: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Replace the (name, unit) series `fresh` carries; keep every other stored series."""
    if stored is None or not len(stored):
        return fresh
    key = lambda d: d["name"].astype(str) + "|" + d["unit"].fillna("").astype(str)
    kept = stored[~key(stored).isin(set(key(fresh)))]
    return (pd.concat([kept, fresh], ignore_index=True)
              .sort_values(["name", "unit", "month"]).reset_index(drop=True))


def main():
    for name, df in fetch_all().items():
        for (nm, unit), g in df.groupby(["name", "unit"]):
            print(f"  {name}: {nm}{unit:<8s} {g.month.min()} .. {g.month.max()}  n={len(g)}")
        save(df, name)


if __name__ == "__main__":
    main()
