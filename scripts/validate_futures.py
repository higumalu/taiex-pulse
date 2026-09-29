"""Check our TAIFEX-derived 法人期貨部位動能 against the archived FinLab series.

FinLab's own description: sum the 三大法人 net open-interest VALUE across
大台/小台/微台, take the 10-day change, then standardise by the 250-day stdev.
"""
import json

import numpy as np
import pandas as pd

from common import load
from finlab_archive import FUTURES

arch = json.loads(FUTURES.read_text(encoding="utf-8"))
f = arch["fields"]["data"]["arrayValue"]["values"][0]["mapValue"]["fields"]
num = lambda v: float(list(v.values())[0])
ref = pd.Series(
    [num(x) for x in f["values"]["mapValue"]["fields"]["ind"]["arrayValue"]["values"]],
    index=[x["stringValue"] for x in f["index"]["arrayValue"]["values"]],
).astype(float)

tidy = load("taifex_inst_futures")
print("investors:", sorted(tidy.investor.unique()), "| products:", sorted(tidy["product"].unique()))

daily = tidy.groupby("date")["net_oi_value_k"].sum().sort_index()
for chg in (10,):
    for win in (250,):
        mom = daily.diff(chg)
        ours = mom / mom.rolling(win).std()
        ours = ours.dropna()
        common = ours.index.intersection(ref.index)
        if len(common) < 30:
            print(f"chg={chg} win={win}: only {len(common)} overlapping days")
            continue
        a, b = ours[common], ref[common]
        print(f"chg={chg} win={win}  n={len(common)}  corr={np.corrcoef(a, b)[0,1]:.4f}  "
              f"mae={np.abs(a-b).mean():.4f}  ours[-3]={a.values[-3:].round(3)}  ref[-3]={b.values[-3:].round(3)}")
