"""Read the archived FinLab dashboard -- the answer key, not a data source.

The two Firestore documents captured on 2026-09-22 are kept only so our own
series can be validated and our unpublished parameters calibrated against them.
Nothing read here may reach the published site: build_site_data.py does not
import this module, and the archive folder is excluded from the repository.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import indicators as I
from common import FINLAB

HOLD = FINLAB / "twMarket_holdIndicator_20260922.json"
FUTURES = FINLAB / "twMarket_futuresPositioning_20260922.json"


def read_archive(path: Path = HOLD) -> dict:
    """{title: {"description": str, "frame": DataFrame}} for one document."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found -- the FinLab archive is not part of the repository; "
            "calibration and validation need a local copy")
    doc = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for entry in doc["fields"]["data"]["arrayValue"]["values"]:
        f = entry["mapValue"]["fields"]
        idx = [x["stringValue"] for x in f["index"]["arrayValue"]["values"]]
        cols = {k: [float(list(x.values())[0]) for x in v["arrayValue"]["values"]]
                for k, v in f["values"]["mapValue"]["fields"].items()}
        out[f["title"]["stringValue"]] = {
            "description": f["description"]["stringValue"],
            "frame": pd.DataFrame(cols, index=idx),
        }
    return out


def archived(path: Path = HOLD) -> dict[str, pd.DataFrame]:
    """Just the frames, keyed by title."""
    return {k: v["frame"] for k, v in read_archive(path).items()}


def signal_from_ind(title: str, frame: pd.DataFrame) -> pd.Series | None:
    """Re-derive a sub-signal from an archived line, using our own rules.

    The archive ships the plotted line only, not the 0/1 signal the composite
    consumes, so calibrate_weights.py rebuilds the signals this way before
    fitting weights to the archived composite.
    """
    # 大盤週線MACD stores its histogram under "0050", not "ind"
    ind = frame["0050"] if "0050" in frame else frame.get("ind")
    if ind is None:
        return None
    if title in ("台股多空排列家數", "騰落線指標(ADL)", "大盤週線MACD"):
        return (ind > 0).astype(float)
    if title == "生命線指標":
        return (frame["benchmark"] > ind).astype(float)
    if title == "大盤融資維持率":
        return (ind < I.MARGIN_THRESHOLD).astype(float)
    if title == "大盤股價淨值比":
        return (ind < I.PBR_THRESHOLD).astype(float)
    if title.startswith("台灣") and "採購經理人" in title:
        return (ind > 50).astype(float)
    if title == "台灣景氣對策燈號":
        return (frame["ind_ma3"] > frame["ind_ma12"]).astype(float)
    return None
