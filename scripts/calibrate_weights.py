"""Fit our composite weights against the archived FinLab composite.

FinLab's own weights are not recoverable (see README), so instead of guessing we
fit non-negative weights summing to 10 that reproduce the archived score as
closely as possible from the ten sub-signals. The result is written to
data/weights.json and picked up by build_site_data.py.

This is the only place the archived composite shapes what the site shows, and
only through the fitted numbers -- no archived value is carried across.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import indicators as I
from common import ROOT
from build_site_data import WEIGHTS, read_years
from finlab_archive import read_archive, signal_from_ind

SCALE = 10.0
OUT = ROOT / "data" / "weights.json"


def build_matrix():
    arch = read_archive()
    comp = arch["大盤綜合指標"]["frame"]["ind"].dropna()
    calendar = comp.index

    signals = {}
    for title in WEIGHTS:
        sig = signal_from_ind(title, arch[title]["frame"])
        if sig is None:
            raise RuntimeError(f"no signal rule for {title}")
        sig = sig.dropna()
        full = sig.reindex(sig.index.union(calendar)).ffill().reindex(calendar)
        signals[title] = full

    X = pd.DataFrame(signals)
    keep = X.notna().all(axis=1)
    return X[keep], comp[keep]


def fit(X: pd.DataFrame, y: pd.Series, sum_to: float = SCALE,
        penalty: float = 1e3) -> pd.Series:
    """Least squares with a heavy soft constraint that the weights sum to `sum_to`,
    then clipped at zero and renormalised."""
    A = np.vstack([X.values, np.full((1, X.shape[1]), penalty)])
    b = np.concatenate([y.values, [penalty * sum_to]])
    w, *_ = np.linalg.lstsq(A, b, rcond=None)
    w = np.clip(w, 0, None)
    if w.sum() > 0:
        w *= sum_to / w.sum()
    return pd.Series(w, index=X.columns)


def report(X, y, w, label):
    pred = X.values @ w.values
    err = np.abs(pred - y.values)
    print(f"{label:<22s} corr={np.corrcoef(pred, y.values)[0,1]:.4f}  "
          f"MAE={err.mean():.3f}  within1={100*(err<=1).mean():.1f}%  "
          f"same-side of 5={100*((pred>5)==(y.values>5)).mean():.1f}%")


def search_thresholds():
    """The two level-based signals need their cut points fitted too.

    With 融資維持率 < 1.4 the signal is on for 0.5 % of days, carries no
    information and the fit zeroes its weight -- even though flip analysis shows
    the original composite clearly reacts to it. So scan both cut points and
    keep whichever pair the weights fit best.
    """
    arch = read_archive()
    best = None
    for margin in [1.4, 1.5, 1.55, 1.6, 1.65, 1.7, 1.75, 1.8]:
        for pbr in [1.4, 1.6, 1.8, 2.0, 2.2, 2.5]:
            I.MARGIN_THRESHOLD, I.PBR_THRESHOLD = margin, pbr
            X, y = build_matrix()
            w = fit(X, y)
            mae = np.abs(X.values @ w.values - y.values).mean()
            if best is None or mae < best[0]:
                best = (mae, margin, pbr, w, X, y)
    return best


def pbr_scale(archived_line: pd.Series):
    """Ratio of our P/B level to FinLab's on the days both exist.

    Our 大盤股價淨值比 sits about 14 % above FinLab's (a book-value definition
    difference, see indicators.market_pbr). The cut point is fitted on FinLab's
    scale, so it has to be moved by this ratio before the site can apply it to
    our own series.
    """
    tq, tp = read_years("tpex_quotes"), read_years("tpex_pbr")
    tpex = tq.merge(tp, on=["date", "code"]) if len(tq) and len(tp) else None
    pbr, prices, shares = read_years("twse_pbr"), read_years("twse_prices"), read_years("twse_shares")
    if not (len(pbr) and len(prices) and len(shares)):
        return None
    ours = I.market_pbr(pbr, prices, shares, tpex)["ind"]
    common = ours.dropna().index.intersection(archived_line.dropna().index)
    if len(common) < 30:
        return None
    return float((ours[common] / archived_line[common]).median()), len(common)


def main():
    mae, margin, pbr, w_best, X, y = search_thresholds()
    I.MARGIN_THRESHOLD, I.PBR_THRESHOLD = margin, pbr
    print(f"fitting on {len(X)} days, {X.index[0]} .. {X.index[-1]}")
    print(f"best cut points: 融資維持率 < {margin}, 股價淨值比 < {pbr}\n")

    measured = pd.Series(WEIGHTS).reindex(X.columns)
    report(X, y, measured * SCALE / measured.sum(), "step-size weights")

    w = fit(X, y)
    report(X, y, w, "fitted weights")

    # round to a half-point grid, which is how the original scored, and re-check
    rounded = (w * 2).round() / 2
    if rounded.sum() > 0:
        rounded *= SCALE / rounded.sum()
    report(X, y, rounded, "fitted, rounded")

    print("\nfitted weights (sum = %.2f):" % w.sum())
    for k, v in w.sort_values(ascending=False).items():
        print(f"  {k:<32s} {v:5.2f}")

    # The site never sees FinLab's P/B line, so store the cut point on our scale.
    scaled = pbr_scale(read_archive()["大盤股價淨值比"]["frame"]["ind"])
    if scaled:
        ratio, n = scaled
        pbr_ours = round(pbr * ratio, 4)
        print(f"P/B scale vs archive: {ratio:.4f} over {n} days -> "
              f"threshold {pbr} (FinLab scale) = {pbr_ours} (ours)")
    else:
        ratio, pbr_ours = None, pbr
        print("P/B scale: too little overlap with the archive, threshold left unscaled")

    OUT.write_text(json.dumps({
        "weights": {k: round(float(v), 4) for k, v in w.items()},
        "margin_threshold": margin, "pbr_threshold": pbr_ours,
        "pbr_threshold_finlab_scale": pbr,
        "pbr_scale": None if ratio is None else round(ratio, 4),
        "fit": {"days": len(X), "mae": round(float(np.abs(X.values @ w.values - y.values).mean()), 4)},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
