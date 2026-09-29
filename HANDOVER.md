# Handover

Written 2026-09-23, updated 2026-09-29. Read this before touching anything; `README.md` covers the
what, this covers the state, the traps, and what is worth doing next.

## Read this first: state of the crawl

As of 2026-09-29 the TWSE backfill has stopped, but not every year landed --
`twse_breadth` is missing 2007-2010, for one. Run `python status.py` to see
what is there, and `python reconcile.py` to re-queue days with no data behind
them. A TPEx backfill (`fetch_tpex.py --feeds quotes,pbr`, 2007 onwards) was
started on 2026-09-29 and takes about 13 hours; 大盤股價淨值比 stays off the
page until it finishes.

After any large backfill, re-run `calibrate_params.py`, `calibrate_weights.py`
and `build_site_data.py`. The calibration currently in `data/params.json` was
searched over a price series that still had holes in it, so it is provisional.
Both calibration scripts need a local copy of the FinLab archive, which is not
in the repository.

Crawls are resumable. If you stop one, stop it by PID, never by image name --
other long-running Python processes may share the machine.

## What this is

`https://ai.finlab.tw/tw_market` is shutting down. This rebuilds its dashboard
from free public sources. The original site's Firestore documents were archived
first (`data/finlab_archive/twMarket_*.json`) and are used **only as a validation
target** — every indicator is recomputed here and scored against FinLab's series.
Only `scripts/finlab_archive.py` reads them (for calibration and validation);
the site build never does, and the folder is not in the repository.

The archive is irreplaceable. Everything else can be re-crawled. Back it up
somewhere other than this machine.

## Where things stand

| indicator | source now | agreement with the archive |
| --- | --- | --- |
| 生命線指標 | computed, 1990-02 onwards | SMA(20) of the index, exact to 0.0 |
| 台股多空排列家數 | computed | sign-agree 89.7 %, corr 0.92 |
| 騰落線指標(ADL) | computed | sign-agree 94.2 %, corr 0.99 |
| 大盤週線MACD | computed | corr 0.14 — **still wrong, see below** |
| 大盤融資維持率 | computed | 1.8980 vs 1.9036 on one day (0.3 %) |
| 大盤股價淨值比 | **not shown** — TPEx only crawled for 2026 | corr 0.9993, constant 1.1440x level |
| PMI / 未來六個月展望 / NMI | computed | identical, every point |
| 台灣景氣對策燈號 | computed | MA3 exact; MA12 differs 0.083 (NDC revision) |
| 法人期貨部位動能 | computed, 2025-02 onwards | corr 0.9977, MAE 0.07 |
| 大盤綜合指標 | fitted | corr 0.95, MAE 0.46, 89 % within 1 point |

Composite weight coverage is 9.27 / 10.0. The one hold-out is 股價淨值比, which
needs **both** exchanges crawled over the same ≥60 days; the 上市 發行股數 feed
and the whole 上櫃 side are the last things the crawl gets to.

## Traps already paid for

Each of these cost real time. Do not re-discover them.

1. **TWSE truncates silently.** It does not answer with 429. Under load it
   returns `stat: "OK"` with tables simply *missing*, and the same request
   replayed later is complete. A first backfill at 2.0 s lost **609 of 2,900
   days (21 %)** this way with one timeout in the log as the only clue. Every
   feed now declares the tables it must get back (`REQUIRED` in `fetch_twse.py`)
   and treats a reply missing any of them as throttling. At 4.0 s with jitter
   the loss rate is 6 days in 3,056.

2. **An empty day is not a truncated day.** MI_QFIIS returns `stat: "OK"` with
   zero rows for real dates (2026-06-19, reproducibly, at any hour). Truncation
   drops whole tables; a genuinely empty day still declares its fields. Conflate
   them and a feed burns its retries and stops. `payload_is_empty()` separates
   them; empty days go into a `<feed>_closed` manifest key and are never retried.

3. **`^[1-9]\d{3}$` silently drops 0050.** ETF codes start with 0, and the weekly
   MACD is computed on 0050. The filter is `^\d{4}$` now, and indicator code
   excludes ETFs where it wants common stock only.

4. **MI_INDEX has no index tables before 2009** and no 漲跌證券數合計 before 2011.
   The index comes from FMTQIK instead (a whole month per request, back to 1990 —
   `fetch_twse_index.py`), and advances/declines are counted from the per-stock
   漲跌(+/-) column, which works from 2004.

5. **`market_pbr` used to publish one-exchange numbers.** When 上市 發行股數 had
   not been crawled for a date, it quietly emitted the 上櫃-only aggregate — a
   3.92 that looked exactly like a market P/B. It now refuses any date without
   both exchanges and a plausible stock count. Watch for this shape of bug
   anywhere two sources get concatenated.

6. **Manifest days are not proof of data.** `reconcile.py` walks the manifest
   against what actually reached parquet and re-queues the difference. Run it
   after any crawler change.

7. **TAIFEX serves Big5**, and its CSV export only reaches back about two years.
   The NDC endpoints are POST-only and CSRF-protected — fetch the HTML page,
   read `<meta name="csrf-token">`, send it with the session cookie.

## Open problems, roughly in order of value

1. **大盤週線MACD, corr 0.14.** Diagnosed but not fixed: our 0050 series had a
   2016–2022 gap, so the MACD was computed across a 70.50 → 110.75 seam. The
   backfill has since filled 2004–2026 for `twse_prices`, so **re-run
   `calibrate_params.py` and re-measure before doing anything else** — this may
   already be solved. If it is not, suspect that FinLab uses an adjusted
   (dividend-reinvested) 0050 price; ours is the raw close.

2. **股價淨值比 level, a constant 1.1440x.** Correlation is 0.9993 over 28 clean
   days and the ratio's standard deviation is 0.0012, so the shape is right and
   only the denominator's definition differs. Our numerator is verified stock by
   stock (2330: close 2,460 x 25.932 bn shares = NT$63.79 tn, P/B 9.92). FinLab's
   implied aggregate book value is ~14 % larger — total equity including
   non-controlling interests and preferred shares is the obvious candidate. It is
   **unproven**; do not write it up as fact. Present handling: the threshold is
   rescaled onto our level automatically, which makes the signal equivalent.
   Chasing the exact definition is optional, not blocking.

3. **The composite formula is not recoverable.** Measuring how far the archived
   composite jumps when exactly one sub-signal flips gives per-indicator steps
   (1.5 / 1.0 / 1.5 / 1.0 / 2.0 / 1.0 / 1.5 / 1.5 / 1.0 / 2.0) that sum to 14,
   not 10. Grouping the 2,964 archived days by their ten-signal pattern leaves
   39 % of days in groups holding two composite values exactly 1.0 apart — so
   FinLab's score has at least one input the page never showed, or thresholds
   that move. `calibrate_weights.py` fits our own weights instead (MAE 0.317 on
   the fit, 0.46 end to end) and the page says so. **Do not present fitted
   weights as FinLab's.**

4. **法人期貨部位動能 starts 2025-02.** TAIFEX's public export only goes back
   ~2 years and the single-date HTML page returns no table for 2008/2015/2020.
   The archive has 2010 onwards, but archived values are never published, so
   the page shows our short series only. This indicator is **not** in the composite (FinLab's own
   description says 不計入大盤總分).

5. **Nothing runs on a schedule.** Once the backfill is done the pipeline needs a
   daily job: the fetchers with no `--start`, then `build_site_data.py`. There is
   no incremental mode beyond the manifest, which is enough.

6. **`site/index.html` is a single file with inline JS.** Fine for now. It reads
   `site/data/market.json` over HTTP — opening the file from disk breaks the
   fetch. ECharts is vendored at `site/vendor/` deliberately: the CDN path was
   wrong once and the page failed with `echarts is not defined`, rendering text
   and no charts.

## How to run things

```bash
cd D:\agent_workspace\taiex-pulse\scripts

python status.py              # crawl progress and ETA
python run_all.py             # supervised backfill, rounds until nothing new
python reconcile.py           # dry run: manifest vs parquet
python reconcile.py --apply   # re-queue days that produced nothing

python fetch_ndc.py           # monthly macro, seconds
python fetch_taifex.py        # institutional futures, ~1 minute
python fetch_twse_index.py    # TAIEX daily close 1990+, ~440 requests
python fetch_twse.py  --interval 4.0   # 上市, 4 feeds, resumable
python fetch_tpex.py  --interval 3.5   # 上櫃, 3 feeds, resumable

python calibrate_params.py    # recover the unpublished MA windows
python calibrate_weights.py   # fit composite weights and cut points
python build_site_data.py     # write site/data/market.json

cd ..\site && python -m http.server 8765 --bind 127.0.0.1
```

Pacing is deliberate: 4.0 s for TWSE, 3.5 s for TPEx, with ±15–25 % jitter,
x1.6 backoff to a 45 s ceiling, and slow recovery after 40 clean replies. The
user asked explicitly to stay polite to these servers. Do not lower it without
re-measuring the truncation rate.

## Verifying your own changes

`validate_daily.py` and `validate_futures.py` score computed series against the
archive. The habit that has caught every bug in this project: **derive the rule
from FinLab's own `description` text, then score it against the archived series
and look at the number.** Every wrong assumption here — TPEx explaining the P/B
gap, the MA windows, the truncation — was caught that way and not by reading
code.
