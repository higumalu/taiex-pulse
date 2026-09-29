"""Spot-check the daily TWSE-derived indicators against the archived FinLab series.

Only the days already crawled are compared, so this is a shape/level check that
can run long before the full backfill finishes.
"""
from __future__ import annotations

import pandas as pd

from common import PARQUET
from finlab_archive import archived


def read_years(prefix: str) -> pd.DataFrame:
    parts = sorted(PARQUET.glob(f"{prefix}_*.parquet"))
    if not parts:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)


def compare(label: str, ours: pd.Series, ref: pd.Series):
    common = ours.dropna().index.intersection(ref.dropna().index)
    if len(common) == 0:
        print(f"{label:<22s} no overlapping dates")
        return
    a, b = ours[common], ref[common]
    diff = (a - b).abs()
    print(f"{label:<22s} n={len(common):<4d} maxdiff={diff.max():.5f} "
          f"meandiff={diff.mean():.5f}")
    print(f"{'':<22s} ours {a.tail(3).round(4).to_dict()}")
    print(f"{'':<22s} ref  {b.tail(3).round(4).to_dict()}")


def main():
    arch = archived()

    idx = read_years("twse_index")
    taiex = (idx[idx.index_name.str.contains("發行量加權股價指數", na=False)]
             .set_index("date")["close"].sort_index())
    print(f"TAIEX rows crawled: {len(taiex)}  {taiex.index.min()} .. {taiex.index.max()}\n")

    # 1. 生命線 = SMA(20) of the index. Needs 20 prior days, so only checkable
    #    once the backfill has run; report what we can.
    ref_life = arch["生命線指標"]["ind"]
    if len(taiex) >= 20:
        compare("生命線 SMA20", taiex.rolling(20).mean(), ref_life)
    else:
        print(f"生命線 SMA20          needs 20 days, have {len(taiex)} -- after backfill\n")

    # 2. 大盤融資維持率 = sum(margin lots x 1000 x close) / margin balance in TWD
    stock = read_years("twse_margin_stock")
    total = read_years("twse_margin_total")
    price = read_years("twse_prices")
    if len(stock) and len(total) and len(price):
        merged = stock.merge(price, on=["date", "code"], how="inner")
        mv = (merged.margin_lots * 1000 * merged.close).groupby(merged.date).sum()
        denom = total.set_index("date")["margin_balance_twd"]
        ours = (mv / denom).sort_index()
        compare("融資維持率", ours, arch["大盤融資維持率"]["ind"])

    # 3. ADL input: advance/decline counts
    breadth = read_years("twse_breadth")
    if len(breadth):
        b = breadth.set_index("date").sort_index()
        print(f"\n漲跌家數 (ADL 原料) 最新三日:\n{b[['up', 'down']].tail(3)}")

    # 4. 大盤股價淨值比 needs share counts to weight; show the unweighted proxy
    #    so the gap to the archived series is visible.
    pbr = read_years("twse_pbr")
    if len(pbr):
        med = pbr.groupby("date")["pbr"].median().sort_index()
        ref_pb = arch["大盤股價淨值比"]["ind"]
        common = med.index.intersection(ref_pb.index)
        if len(common):
            print(f"\n股價淨值比 (未加權中位數 vs FinLab 市值加權):")
            print(f"  ours median {med[common].tail(3).round(3).to_dict()}")
            print(f"  FinLab      {ref_pb[common].tail(3).round(3).to_dict()}")


if __name__ == "__main__":
    main()
