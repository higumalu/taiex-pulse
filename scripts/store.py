"""The published dataset: small daily CSVs on the `data` branch.

The crawlers keep every stock on every day (tens of MB of parquet). The
indicators only ever need a handful of numbers per day -- counts, market cap,
book value, margin totals -- so those per-day aggregates are what gets
published, as CSV so that git stores each day's appended row as a tiny delta.
The one exception is 多空排列, whose moving averages need per-stock closes; a
short rolling window of those is kept alongside.

    data/store/                 (a checkout of the `data` branch)
      taiex.csv                 date, close
      twse_daily.csv            date, up, down, unchanged, stack_bull, stack_bear,
                                close_0050, margin_value, margin_loans, cap, book, n
      tpex_daily.csv            date, cap, book, n
      taifex_net_oi.csv         date, net_oi_value_k
      ndc_pmi.csv / ndc_nmi.csv / ndc_eco.csv    as fetched
      recent_twse_close.csv     date, code, close  (last WINDOW trading days)
      meta.json                 parameters the aggregates were computed with

build_site_data.py reads only this. `python store.py export` rebuilds it from
the local parquet; update_daily.py appends to it in CI.
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

import indicators as I
from common import DATA, PARQUET

STORE = DATA / "store"
# Trading days of per-stock closes kept for 多空排列. Only max(STACK_MAS) are
# needed; the rest is slack so a calibration that lengthens the long MA a
# little does not force a full re-export.
WINDOW = 80

TWSE_COLS = ["up", "down", "unchanged", "stack_bull", "stack_bear", "close_0050",
             "margin_value", "margin_loans", "cap", "book", "n"]
PBR_COLS = ["cap", "book", "n"]


# --- IO -----------------------------------------------------------------------

def read(name: str, index: str | None = "date") -> pd.DataFrame:
    path = STORE / f"{name}.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, dtype={"date": str, "month": str, "code": str,
                                  "series_id": str})
    return df.set_index(index).sort_index() if index else df


def write(name: str, df: pd.DataFrame, index: bool = True):
    STORE.mkdir(parents=True, exist_ok=True)
    df.to_csv(STORE / f"{name}.csv", index=index, float_format="%.10g",
              lineterminator="\n")


def upsert(name: str, new: pd.DataFrame):
    """Replace rows for the dates in `new`, keep every other date as it was.

    Columns missing from `new` keep their stored values, so a day where one
    feed failed can be filled in by a later run without touching the rest.
    """
    old = read(name)
    if old.empty:
        out = new
    else:
        out = new.combine_first(old)
        out.update(new)
        out = out[[c for c in old.columns if c in out.columns]
                  + [c for c in out.columns if c not in old.columns]]
    write(name, out.sort_index())


def read_meta() -> dict:
    path = STORE / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_meta(**extra):
    meta = read_meta()
    meta.update({"stack_mas": list(I.STACK_MAS), "window": WINDOW, **extra})
    STORE.mkdir(parents=True, exist_ok=True)
    (STORE / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def check_meta():
    """Refuse to build on aggregates computed with different MA windows."""
    got = read_meta().get("stack_mas")
    if got is not None and tuple(got) != tuple(I.STACK_MAS):
        raise SystemExit(
            f"data/store was aggregated with STACK_MAS={tuple(got)} but params.json "
            f"now says {I.STACK_MAS}; re-run `python store.py export` locally")


# --- aggregation (shared by export and the daily update) ----------------------

def twse_aggregates(prices, breadth, margin_stock, margin_total, pbr, shares,
                    stack_prices=None) -> pd.DataFrame:
    """One row per date from the per-stock TWSE frames. Any frame may be empty.

    `stack_prices` is the close history 多空排列 runs over; it defaults to
    `prices` but the daily update passes the rolling window plus the new days.
    """
    parts = []
    if len(breadth):
        parts.append(breadth.drop_duplicates("date", keep="last")
                     .set_index("date")[["up", "down", "unchanged"]])
    if len(prices):
        # Counts only for the days in `prices`: the rest of stack_prices is
        # history for the moving averages, and its oldest days have too little
        # of it to produce a count at all.
        counts = I.stack_counts(prices if stack_prices is None else stack_prices)
        parts.append(counts.loc[counts.index.isin(prices["date"])])
    if len(prices):
        etf = prices[prices["code"] == "0050"].drop_duplicates("date")
        parts.append(etf.set_index("date")["close"].rename("close_0050").to_frame())
    if len(margin_stock) and len(prices):
        parts.append(I.margin_value(margin_stock, prices).rename("margin_value").to_frame())
    if len(margin_total):
        parts.append(margin_total.drop_duplicates("date").set_index("date")
                     ["margin_balance_twd"].rename("margin_loans").to_frame())
    if len(pbr) and len(prices) and len(shares):
        parts.append(I.pbr_totals(pbr.merge(prices, on=["date", "code"])
                                     .merge(shares, on=["date", "code"])))
    if not parts:
        return pd.DataFrame(columns=TWSE_COLS)
    out = pd.concat(parts, axis=1).sort_index()
    out.index.name = "date"
    return out.reindex(columns=TWSE_COLS)


def tpex_aggregates(quotes, pbr) -> pd.DataFrame:
    if not (len(quotes) and len(pbr)):
        return pd.DataFrame(columns=PBR_COLS)
    out = I.pbr_totals(quotes.merge(pbr, on=["date", "code"]))
    out.index.name = "date"
    return out


def trim_window(closes: pd.DataFrame) -> pd.DataFrame:
    dates = sorted(closes["date"].unique())[-WINDOW:]
    return (closes[closes["date"].isin(dates)]
            .drop_duplicates(["date", "code"], keep="last")
            .sort_values(["date", "code"]))


# --- export from the local full-history parquet -------------------------------

def read_years(prefix: str) -> pd.DataFrame:
    parts = sorted(PARQUET.glob(f"{prefix}_*.parquet"))
    return pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True) if parts \
        else pd.DataFrame()


def local_taiex() -> pd.Series:
    """FMTQIK back to 1990, with MI_INDEX's own index close preferred where present."""
    taiex = pd.Series(dtype=float)
    fm = PARQUET / "twse_taiex.parquet"
    if fm.exists():
        t = pd.read_parquet(fm)
        taiex = t.drop_duplicates("date").set_index("date")["close"].sort_index()
    idx = read_years("twse_index")
    if len(idx):
        alt = (idx[idx["index_name"].str.contains("發行量加權股價指數", na=False)]
               .drop_duplicates("date").set_index("date")["close"].sort_index())
        taiex = alt.combine_first(taiex).sort_index() if len(taiex) else alt
    taiex.index.name = "date"
    return taiex


def export():
    I.load_params()
    STORE.mkdir(parents=True, exist_ok=True)

    write("taiex", local_taiex().rename("close").to_frame())

    prices = read_years("twse_prices")
    tw = twse_aggregates(prices, read_years("twse_breadth"),
                         read_years("twse_margin_stock"), read_years("twse_margin_total"),
                         read_years("twse_pbr"), read_years("twse_shares"))
    write("twse_daily", tw)
    write("tpex_daily", tpex_aggregates(read_years("tpex_quotes"), read_years("tpex_pbr")))
    write("recent_twse_close", trim_window(prices), index=False)

    for name in ("taifex_inst_net_oi", "ndc_pmi", "ndc_nmi", "ndc_eco"):
        path = PARQUET / f"{name}.parquet"
        if path.exists():
            out = "taifex_net_oi" if name == "taifex_inst_net_oi" else name
            write(out, pd.read_parquet(path), index=False)

    write_meta()
    for p in sorted(STORE.glob("*.csv")):
        print(f"  {p.name:<24s} {p.stat().st_size/1e6:6.2f} MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["export"])
    ap.parse_args()
    export()
