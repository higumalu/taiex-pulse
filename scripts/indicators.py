"""Indicator definitions.

Every function takes tidy frames from data/parquet and returns a DataFrame with
an `ind` column (the plotted line) and a `signal` column (the 0/1 the composite
consumes), indexed by date string.

Parameters that FinLab never published (moving-average lengths, mostly) are
module-level defaults here so `calibrate.py` can grid-search them against the
archived series.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# --- tunables ---------------------------------------------------------------
# These are the moving-average lengths FinLab never published. The values below
# are only starting points; calibrate_params.py searches them against the
# archived series and writes the winners to data/params.json, which
# load_params() applies. Re-run that after any large backfill -- a search over
# a series with gaps in it will settle on the wrong windows.
STACK_MAS = (5, 20, 60)      # 多空排列: short > mid > long means bullish
STACK_SIGNAL_MAS = (20, 60)  # short/long MA applied to the 多頭-空頭 spread
ADL_MAS = (20, 60)           # 騰落線: short/long MA of the cumulative line
LIFELINE_MA = 20             # 生命線: the "monthly line"
MACD_PARAMS = (12, 26, 9)
MACD_RESAMPLE = "W"
ADL_LINE = "cumulative"
MARGIN_THRESHOLD = 1.4       # 融資維持率 below this is the bullish signal
PBR_THRESHOLD = 2.0          # 股價淨值比 below this is the bullish signal
LIGHT_MAS = (3, 12)          # 景氣對策信號: MA3 vs MA12


def load_params(path=None):
    """Apply the calibrated windows from data/params.json, if it exists."""
    import json
    from pathlib import Path
    path = path or Path(__file__).resolve().parents[1] / "data" / "params.json"
    if not path.exists():
        return {}
    params = json.loads(path.read_text(encoding="utf-8"))
    g = globals()
    for key, value in params.items():
        if key in g:
            g[key] = tuple(value) if isinstance(value, list) else value
    return params


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _out(ind: pd.Series, signal: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({"ind": ind, "signal": signal.astype(float)}).dropna(subset=["ind"])


def lifeline(taiex: pd.Series) -> pd.DataFrame:
    """生命線: is the index above its 20-day mean?

    Verified against the archive: the plotted line is exactly SMA(20) of the
    index, to 0.0 mean absolute difference.
    """
    ma = taiex.rolling(LIFELINE_MA).mean()
    return _out(ma, taiex > ma)


def bull_bear_stack(prices: pd.DataFrame) -> pd.DataFrame:
    """台股多空排列家數.

    A stock is bullish when its short MA sits above its mid MA above its long
    MA, bearish when the order reverses. The plotted line is the short-minus-long
    moving average of (bullish count - bearish count).
    """
    wide = prices.pivot(index="date", columns="code", values="close").sort_index()
    s, m, l = (wide.rolling(n).mean() for n in STACK_MAS)
    spread = (((s > m) & (m > l)).sum(axis=1) - ((s < m) & (m < l)).sum(axis=1)).astype(float)
    short, long = STACK_SIGNAL_MAS
    ind = spread.rolling(short).mean() - spread.rolling(long).mean()
    return _out(ind, ind > 0)


def adl(breadth: pd.DataFrame) -> pd.DataFrame:
    """騰落線指標: cumulative advances minus declines, then MA spread."""
    b = breadth.set_index("date").sort_index()
    if ADL_LINE == "ratio":
        denom = b["up"] + b["down"] + b.get("unchanged", 0)
        line = ((b["up"] - b["down"]) / denom).cumsum()
    else:
        line = (b["up"].fillna(0) - b["down"].fillna(0)).cumsum()
    short, long = ADL_MAS
    ind = line.rolling(short).mean() - line.rolling(long).mean()
    return _out(ind, ind > 0)


def weekly_macd(px: pd.Series) -> pd.DataFrame:
    """大盤週線MACD.

    The archived values sit in the -9..+15 range, which matches a MACD computed
    on 0050's price rather than on the index itself, so that is what we use.
    """
    weekly = px.copy()
    weekly.index = pd.to_datetime(weekly.index)
    w = weekly.resample(MACD_RESAMPLE).last().dropna()
    fast, slow, sig = MACD_PARAMS
    dif = _ema(w, fast) - _ema(w, slow)
    hist = dif - _ema(dif, sig)
    hist.index = hist.index.strftime("%Y-%m-%d")
    return _out(hist, hist > 0)


def margin_ratio(margin_stock: pd.DataFrame, margin_total: pd.DataFrame,
                 prices: pd.DataFrame) -> pd.DataFrame:
    """大盤融資維持率 = value of margin-bought shares / margin loans outstanding.

    Margin balances are in board lots of 1,000 shares.
    """
    merged = margin_stock.merge(prices, on=["date", "code"], how="inner")
    value = (merged["margin_lots"] * 1000 * merged["close"]).groupby(merged["date"]).sum()
    loans = margin_total.set_index("date")["margin_balance_twd"]
    ind = (value / loans).sort_index()
    return _out(ind, ind < MARGIN_THRESHOLD)


def market_pbr(pbr: pd.DataFrame, prices: pd.DataFrame, shares: pd.DataFrame,
               tpex: pd.DataFrame | None = None) -> pd.DataFrame:
    """大盤股價淨值比 = total market cap / total book value, 上市 + 上櫃.

    Book value per stock is backed out of the exchange's published P/B, then
    both sides are summed with market-cap weights.

    Note this lands about 14 % above FinLab's series (4.11 vs 3.64, a ratio that
    held at 1.135/1.149/1.138 on three separate dates). Adding TPEx moved it by
    only -0.4 %, so the gap is a book-value definition difference -- most likely
    total equity including non-controlling interests, against the
    parent-company common equity behind the exchanges' published P/B. Our figure
    is internally consistent; the signal threshold is rescaled to match rather
    than the level being forced onto FinLab's.
    """
    def aggregate(df, label):
        """Per-date cap and book, keeping only dates with plausible coverage."""
        df = df[(df["pbr"] > 0) & df["close"].notna() & df["shares"].notna()]
        df = df[~df["code"].astype(str).str.startswith("0")]     # drop ETFs
        if not len(df):
            return None
        cap = (df["close"] * df["shares"]).groupby(df["date"]).sum()
        book = ((df["close"] * df["shares"]) / df["pbr"]).groupby(df["date"]).sum()
        n = df.groupby("date")["code"].size()
        # a date that kept only a handful of stocks means an input feed has not
        # been crawled that far yet -- averaging it in would be silently wrong
        keep = n >= 0.5 * n.median()
        dropped = int((~keep).sum())
        if dropped:
            print(f"    market_pbr: dropped {dropped} {label} dates with thin coverage")
        return cap[keep], book[keep]

    tw = aggregate(pbr.merge(prices, on=["date", "code"])
                      .merge(shares, on=["date", "code"]), "上市")
    tp = aggregate(tpex, "上櫃") if tpex is not None and len(tpex) else None
    if tw is None:
        return _out(pd.Series(dtype=float), pd.Series(dtype=bool))
    if tp is None:
        cap, book = tw
    else:
        # Only dates where both exchanges are in hand: mixing full-market days
        # with 上市-only days would put a step in the series, not a signal.
        both = tw[0].index.intersection(tp[0].index)
        missing = len(tw[0].index) - len(both)
        if missing:
            print(f"    market_pbr: {missing} dates have 上市 but no 上櫃 yet, held back")
        cap, book = tw[0][both] + tp[0][both], tw[1][both] + tp[1][both]

    ind = (cap / book).sort_index()
    return _out(ind, ind < PBR_THRESHOLD)


def monthly_level(series: pd.Series, threshold: float = 50.0,
                  publish_lag_months: int = 1) -> pd.DataFrame:
    """PMI / NMI style: a monthly reading compared against a fixed level.

    The reading for month M is published early in M+1, so it is shifted forward
    before being used as a daily signal.
    """
    s = series.sort_index()
    idx = pd.PeriodIndex(s.index, freq="M") + publish_lag_months
    s.index = idx.to_timestamp().strftime("%Y-%m-%d")
    return _out(s, s > threshold)


def business_light(score: pd.Series, publish_lag_months: int = 1,
                   publish_day: int = 27) -> pd.DataFrame:
    """台灣景氣對策信號: short vs long moving average of the monthly score.

    NDC releases month M near the end of M+1, so the reading is dated the 27th
    of the following month. Dating it to the 1st instead would let the signal
    act on figures nobody had yet.
    """
    s = score.sort_index()
    short, long = LIGHT_MAS
    ma_short, ma_long = s.rolling(short).mean(), s.rolling(long).mean()
    dates = ((pd.PeriodIndex(s.index, freq="M") + publish_lag_months).to_timestamp()
             + pd.Timedelta(days=publish_day - 1)).strftime("%Y-%m-%d")
    out = pd.DataFrame({"ind": s.values, "ma_short": ma_short.values,
                        "ma_long": ma_long.values,
                        "signal": (ma_short > ma_long).astype(float).values},
                       index=dates)
    return out.dropna(subset=["ind"])


def futures_momentum(net_oi: pd.Series, change_days: int = 10,
                     std_window: int = 250) -> pd.DataFrame:
    """法人期貨部位動能, per FinLab's own description.

    Net open-interest value of the three institutional groups across 大台/小台/
    微台, its N-day change, standardised by the trailing standard deviation.
    Validated at corr 0.9977 against the archived series.
    """
    s = net_oi.sort_index()
    mom = s.diff(change_days)
    ind = mom / mom.rolling(std_window).std()
    return _out(ind, ind > 0)


def to_daily(monthly: pd.DataFrame, calendar: pd.Index) -> pd.DataFrame:
    """Forward-fill a monthly indicator onto the trading calendar."""
    out = monthly.reindex(monthly.index.union(calendar)).ffill()
    return out.reindex(calendar)


def composite(signals: dict[str, pd.Series], weights: dict[str, float]) -> pd.Series:
    """Weighted sum of the binary sub-signals, on the same 0-10 scale as FinLab's.

    FinLab's exact weights are not recoverable from public data (see README), so
    these come from fitting against the archived composite.
    """
    frame = pd.DataFrame(signals)
    w = pd.Series(weights).reindex(frame.columns).fillna(0.0)
    return (frame * w).sum(axis=1, min_count=1)
