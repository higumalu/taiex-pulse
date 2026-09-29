"""Produce site/data/market.json for the dashboard.

Reads only data/store (the `data` branch, see store.py), so it runs the same
locally and in CI. Only series our own pipeline computes from public data are
published. A series
whose inputs have not been crawled far enough is left off the page rather than
filled in from anywhere else, and the FinLab archive is never read here -- it
lives in finlab_archive.py and is used only for calibration and validation.
"""
from __future__ import annotations

import json

import pandas as pd

import indicators as I
import store as S
from common import ROOT

OUT = ROOT / "site" / "data" / "market.json"

# A series needs this many observations before it is worth drawing.
MIN_POINTS = 60
# The composite is only scored on days where the sub-signals we have cover at
# least this share of the total weight; below it the score says too little.
MIN_COVERAGE = 0.6

# Written for this project. Each describes what our code computes, which is not
# always what the original dashboard computed (see README).
DESCRIPTIONS = {
    "大盤綜合指標":
        "十項子指標各自判斷偏多（1）或偏空（0），依權重加總後換算成 0–10 分。"
        "分數越高代表越多面向同時偏多；權重由歷史資料擬合而得。"
        "若某些子指標在當天還沒有資料，就以現有子指標的權重重新換算。",
    "台股多空排列家數":
        "統計上市個股中，短、中、長期均線呈多頭排列與空頭排列的家數差，"
        "再取這個差值的短期減長期均線。大於 0 表示多頭排列的股票正在增加。",
    "生命線指標":
        "加權指數的 20 日均線。指數站上均線視為偏多，跌破視為偏空。",
    "騰落線指標(ADL)":
        "每日上漲家數減下跌家數，累加成騰落線，再取其短期減長期均線。"
        "大於 0 表示多數個股同步走強，而不只是少數權值股撐盤。",
    "大盤週線MACD":
        "以元大台灣50（0050）的週收盤價計算 MACD 柱狀體，大於 0 代表週線多方動能。",
    "大盤融資維持率":
        "上市股票融資買進部位的市值除以融資餘額。維持率偏低代表融資戶壓力大、"
        "籌碼已經洗過，視為偏多；偏高則代表槓桿過熱。",
    "大盤股價淨值比":
        "上市加上櫃的總市值除以總淨值（由各股公告的股價淨值比回推）。"
        "低於門檻視為評價偏低、偏多。",
    "台灣製造業採購經理人指數(PMI)":
        "國發會每月公布的製造業 PMI，高於 50 代表製造業擴張。"
        "數值依公布時點（次月初）對齊，不會用到當時還沒公布的資料。",
    "台灣製造業採購經理人未來6個月展望":
        "製造業採購經理人對未來六個月景氣的看法，高於 50 代表偏樂觀。依公布時點對齊。",
    "台灣非製造業採購經理人指數(NMI)":
        "國發會每月公布的非製造業經理人指數，高於 50 代表服務業等非製造業擴張。依公布時點對齊。",
    "台灣景氣對策燈號":
        "國發會景氣對策信號的綜合分數。3 個月均線高於 12 個月均線時，視為景氣轉強。"
        "依公布時點（次月下旬）對齊。",
    "法人期貨部位動能":
        "三大法人在大台、小台、微台期貨的淨未平倉契約價值合計，取 10 日變化，"
        "再除以 250 日標準差。大於 0 表示法人正在加碼偏多部位。不計入綜合分數。",
}

# Display order on the page.
ORDER = [t for t in DESCRIPTIONS if t != "大盤綜合指標"]

COLORS = {
    "台股多空排列家數": [0], "騰落線指標(ADL)": [0], "大盤週線MACD": [0],
    "大盤融資維持率": [1.6, 1.7], "大盤股價淨值比": [1.4, 2.0],
    "台灣製造業採購經理人指數(PMI)": [50], "台灣製造業採購經理人未來6個月展望": [40, 60],
    "台灣非製造業採購經理人指數(NMI)": [50],
    "台灣景氣對策燈號": [17, 23, 32, 38],
}

# Starting point, measured from how far the archived composite jumps when one
# sub-signal flips. calibrate_weights.py replaces these with a proper fit and
# writes data/weights.json; that file wins when it exists.
WEIGHTS = {
    "台股多空排列家數": 1.5, "生命線指標": 1.0, "騰落線指標(ADL)": 1.5,
    "大盤週線MACD": 1.0, "大盤融資維持率": 2.0, "大盤股價淨值比": 1.0,
    "台灣製造業採購經理人指數(PMI)": 1.5, "台灣製造業採購經理人未來6個月展望": 1.5,
    "台灣非製造業採購經理人指數(NMI)": 1.0, "台灣景氣對策燈號": 2.0,
}


def load_calibration():
    """Use the fitted weights and cut points when calibration has been run.

    The P/B cut point in weights.json is already on our own scale
    (calibrate_weights.py moves it), so the colour bands move with it.
    """
    path = ROOT / "data" / "weights.json"
    if not path.exists():
        return
    cal = json.loads(path.read_text(encoding="utf-8"))
    WEIGHTS.clear()
    WEIGHTS.update(cal["weights"])
    I.MARGIN_THRESHOLD = cal["margin_threshold"]
    I.PBR_THRESHOLD = cal["pbr_threshold"]
    scale = cal.get("pbr_scale")
    if scale:
        COLORS["大盤股價淨值比"] = [round(v * scale, 2) for v in COLORS["大盤股價淨值比"]]


def computed() -> tuple[dict[str, pd.DataFrame], pd.Series]:
    """Every indicator the published dataset supports. Missing inputs are skipped."""
    out = {}
    taiex = S.read("taiex")
    taiex = taiex["close"].dropna() if len(taiex) else pd.Series(dtype=float)
    if len(taiex) > MIN_POINTS:
        out["生命線指標"] = I.lifeline(taiex)

    tw = S.read("twse_daily")
    if len(tw):
        stack = tw[["stack_bull", "stack_bear"]].dropna()
        # the first max(STACK_MAS) days of the crawl have no MA order yet
        stack = stack[stack.sum(axis=1) > 0]
        if len(stack) > MIN_POINTS:
            out["台股多空排列家數"] = I.bull_bear_from_counts(stack)
        px = tw["close_0050"].dropna()
        if len(px) > MIN_POINTS:
            out["大盤週線MACD"] = I.weekly_macd(px)
        breadth = tw[["up", "down", "unchanged"]].dropna(how="all")
        if len(breadth) > MIN_POINTS:
            out["騰落線指標(ADL)"] = I.adl(breadth.reset_index())
        margin = tw[["margin_value", "margin_loans"]].dropna()
        if len(margin) > MIN_POINTS:
            out["大盤融資維持率"] = I.margin_ratio_from(margin["margin_value"],
                                                   margin["margin_loans"])
        tp = S.read("tpex_daily")
        out["大盤股價淨值比"] = I.market_pbr_from(tw[S.PBR_COLS], tp if len(tp) else None)

    pmi, nmi, eco = (S.read(n, index=None) for n in ("ndc_pmi", "ndc_nmi", "ndc_eco"))
    pick = lambda df, n: df[df["name"] == n].set_index("month")["value"].sort_index()
    if len(pmi):
        out["台灣製造業採購經理人指數(PMI)"] = I.monthly_level(pick(pmi, "製造業PMI"))
        out["台灣製造業採購經理人未來6個月展望"] = I.monthly_level(pick(pmi, "未來六個月展望"))
    if len(nmi):
        out["台灣非製造業採購經理人指數(NMI)"] = I.monthly_level(pick(nmi, "臺灣非製造業NMI"))
    if len(eco):
        score = eco[(eco["name"] == "景氣對策信號") & eco["unit"].fillna("").str.contains("分")]
        out["台灣景氣對策燈號"] = I.business_light(score.set_index("month")["value"].sort_index())

    fut = S.read("taifex_net_oi")
    if len(fut):
        out["法人期貨部位動能"] = I.futures_momentum(fut["net_oi_value_k"])
    return out, taiex


def as_list(s: pd.Series, digits: int) -> list:
    return [None if pd.isna(v) else round(float(v), digits) for v in s]


def main():
    I.load_params()
    load_calibration()
    S.check_meta()
    mine, taiex = computed()
    if taiex.empty:
        raise SystemExit("no TAIEX series in data/store -- run `python store.py export` "
                         "or check out the data branch there")

    series, signals = [], {}
    for title in ORDER:
        ours = mine.get(title)
        if ours is None or len(ours["ind"].dropna()) < MIN_POINTS:
            print(f"  skipped   {title}: not enough data in data/store yet")
            continue
        ind, sig = ours["ind"], ours.get("signal")
        if title in WEIGHTS and sig is not None:
            signals[title] = sig
        series.append({
            "title": title,
            "description": DESCRIPTIONS[title],
            "starts": ind.dropna().index[0],
            "thresholds": COLORS.get(title, []),
            # 生命線 is a moving average of the index itself, so it only reads
            # correctly when drawn against the index on one shared axis.
            "same_axis": title == "生命線指標",
            "weight": WEIGHTS.get(title),
            "index": list(ind.index),
            "ind": as_list(ind, 4),
            "benchmark": as_list(taiex.reindex(ind.index).ffill(), 2),
            "signal": None if sig is None else
                      [None if pd.isna(v) else int(v) for v in sig],
        })

    # Sub-signals live on different calendars: daily ones on trading days,
    # macro ones on their publication dates. Forward-fill every signal onto the
    # trading calendar before summing, or a month-start date would see only the
    # macro signals and score near zero.
    calendar = taiex.index
    aligned = pd.DataFrame({
        k: sig.reindex(sig.index.union(calendar)).ffill().reindex(calendar)
        for k, sig in signals.items()})

    # Score each day on the signals that exist that day, rescaled to 0-10 by the
    # weight they carry, and keep only days with enough of the weight in hand.
    total = sum(WEIGHTS.values())
    w = pd.Series(WEIGHTS).reindex(aligned.columns).fillna(0.0)
    present = aligned.notna().mul(w, axis=1).sum(axis=1)
    coverage = present / total
    comp = (I.composite(aligned.to_dict("series"), WEIGHTS) * 10 / present)
    keep = (coverage >= MIN_COVERAGE) & comp.notna()
    comp, coverage = comp[keep], coverage[keep]
    if comp.empty:
        raise SystemExit(f"no day has {MIN_COVERAGE:.0%} of the weight covered yet")

    payload = {
        "generated": pd.Timestamp.now(tz="Asia/Taipei").strftime("%Y-%m-%d %H:%M"),
        "composite": {
            "title": "大盤綜合指標",
            "description": DESCRIPTIONS["大盤綜合指標"],
            "weight_covered": round(float(present[comp.index[-1]]), 2),
            "weight_total": round(total, 2),
            "missing": [t for t in WEIGHTS if t not in signals],
            "index": list(comp.index),
            "ind": as_list(comp, 2),
            "coverage": as_list(coverage, 3),
            "benchmark": as_list(taiex.reindex(comp.index).ffill(), 2),
        },
        "series": series,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")

    print(f"wrote {OUT.relative_to(ROOT)}  {OUT.stat().st_size/1e6:.1f} MB")
    print(f"composite: {comp.index[0]} .. {comp.index[-1]}, latest weight covered "
          f"{payload['composite']['weight_covered']}/{payload['composite']['weight_total']}")
    for s in series:
        print(f"  computed  {s['title']:<32s} n={len(s['index']):<6d} "
              f"{s['index'][0]} .. {s['index'][-1]}")


if __name__ == "__main__":
    main()
