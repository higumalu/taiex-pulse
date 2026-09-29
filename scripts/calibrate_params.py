"""Recover the moving-average lengths FinLab never published.

Their descriptions say things like "取指標的長短均線差值作為訊號" without giving
the windows. The archived series is the answer key, so search the windows that
reproduce it and write them to data/params.json.

Scored on sign agreement first -- that is what the composite consumes -- with
correlation as the tie-break.
"""
from __future__ import annotations

import json
from itertools import combinations

import numpy as np
import pandas as pd

import indicators as I
from build_site_data import read_years
from common import ROOT
from validate_daily import archived

OUT = ROOT / "data" / "params.json"
WINDOWS = [3, 5, 10, 20, 40, 60, 120, 240]


def score(ours: pd.Series, ref: pd.Series):
    common = ours.dropna().index.intersection(ref.dropna().index)
    if len(common) < 200:
        return None
    a, b = ours[common], ref[common]
    return float(((a > 0) == (b > 0)).mean()), float(a.corr(b)), len(common)


def calibrate_stack(prices: pd.DataFrame, ref: pd.Series):
    wide = prices.pivot(index="date", columns="code", values="close").sort_index()
    mas = {w: wide.rolling(w).mean() for w in WINDOWS}
    best = None
    for s, m, l in combinations(WINDOWS, 3):
        bull = ((mas[s] > mas[m]) & (mas[m] > mas[l])).sum(axis=1)
        bear = ((mas[s] < mas[m]) & (mas[m] < mas[l])).sum(axis=1)
        spread = (bull - bear).astype(float)
        for ss, ll in combinations(WINDOWS, 2):
            ind = spread.rolling(ss).mean() - spread.rolling(ll).mean()
            got = score(ind, ref)
            if got and (best is None or got[:2] > best[0][:2]):
                best = (got, (s, m, l), (ss, ll))
    return best


def calibrate_adl(breadth: pd.DataFrame, ref: pd.Series):
    b = breadth.drop_duplicates("date").set_index("date").sort_index()
    variants = {
        "cumulative": (b["up"].fillna(0) - b["down"].fillna(0)).cumsum(),
        "ratio": ((b["up"] - b["down"]) /
                  (b["up"] + b["down"] + b.get("unchanged", 0))).cumsum(),
    }
    best = None
    for label, line in variants.items():
        for ss, ll in combinations(WINDOWS, 2):
            ind = line.rolling(ss).mean() - line.rolling(ll).mean()
            got = score(ind, ref)
            if got and (best is None or got[:2] > best[0][:2]):
                best = (got, label, (ss, ll))
    return best


def calibrate_macd(px: pd.Series, ref: pd.Series):
    ema = lambda s, n: s.ewm(span=n, adjust=False).mean()
    best = None
    for rule in ("W", "W-FRI"):
        w = px.copy()
        w.index = pd.to_datetime(w.index)
        w = w.resample(rule).last().dropna()
        for fast, slow in [(12, 26), (5, 35), (10, 20), (6, 19), (8, 17)]:
            for sig in (9, 5, 6):
                dif = ema(w, fast) - ema(w, slow)
                hist = dif - ema(dif, sig)
                h = hist.copy()
                h.index = h.index.strftime("%Y-%m-%d")
                got = score(h, ref)
                if got and (best is None or got[:2] > best[0][:2]):
                    best = (got, rule, (fast, slow, sig))
    return best


def main():
    arch = archived()
    prices = read_years("twse_prices")
    breadth = read_years("twse_breadth")
    params = {}

    print("台股多空排列家數 -- searching MA stacks ...", flush=True)
    got = calibrate_stack(prices, arch["台股多空排列家數"]["ind"])
    if got:
        (sign, corr, n), stack, sig = got
        print(f"  best: stack={stack} signal MAs={sig}  "
              f"sign-agree={sign:.1%} corr={corr:+.4f} over {n}d")
        params["STACK_MAS"] = list(stack)
        params["STACK_SIGNAL_MAS"] = list(sig)

    print("騰落線指標(ADL) -- searching ...", flush=True)
    got = calibrate_adl(breadth, arch["騰落線指標(ADL)"]["ind"])
    if got:
        (sign, corr, n), variant, sig = got
        print(f"  best: line={variant} MAs={sig}  "
              f"sign-agree={sign:.1%} corr={corr:+.4f} over {n}d")
        params["ADL_MAS"] = list(sig)
        params["ADL_LINE"] = variant

    print("大盤週線MACD -- searching ...", flush=True)
    px = (prices[prices["code"] == "0050"].drop_duplicates("date")
          .set_index("date")["close"].sort_index())
    got = calibrate_macd(px, arch["大盤週線MACD"]["0050"])
    if got:
        (sign, corr, n), rule, mp = got
        print(f"  best: resample={rule} params={mp}  "
              f"sign-agree={sign:.1%} corr={corr:+.4f} over {n}d")
        params["MACD_PARAMS"] = list(mp)
        params["MACD_RESAMPLE"] = rule

    OUT.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
