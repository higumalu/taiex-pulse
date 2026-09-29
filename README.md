# taiex-pulse

A self-hosted rebuild of the FinLab 台股大盤綜合指標 dashboard
(`https://ai.finlab.tw/tw_market`), which is being shut down.

Everything is recomputed from free public sources. The original site's data was
archived first and is kept **only as a validation target** — every indicator we
publish is computed by our own pipeline and checked against FinLab's series.
The site never shows an archived value: an indicator whose inputs have not been
crawled far enough is left off the page instead.

> Picking this up from someone else? Read **`HANDOVER.md`** first — current
> state, the traps already paid for, and what is worth doing next.

## Layout

```
data/store/      the published daily aggregates: a checkout of the `data` branch
data/finlab_archive/  archived FinLab Firestore documents -- validation only, not in the repo
data/raw/        raw NDC responses
data/cache/      crawler manifests and raw chunk cache
data/parquet/    tidy tables produced by the fetchers
scripts/         fetchers, indicator builders, validators
site/            the dashboard itself
```

## The archive

The live site was a Nuxt SPA that fetched one Firestore document and only
rendered it; all computation happened on FinLab's backend. Two documents were
captured on 2026-09-22 (project `fdata-299302`, collection `twMarket`):

| file | contents |
| --- | --- |
| `twMarket_holdIndicator_20260922.json` | the composite plus its 10 sub-indicators, full history |
| `twMarket_futuresPositioning_20260922.json` | 法人期貨部位動能, daily since 2010-01-04 |

They live in `data/finlab_archive/` and are read only through
`scripts/finlab_archive.py`, by the calibration and validation scripts.
`build_site_data.py` does not import it, and the folder is not committed: the
repository ships code, not FinLab's data. The descriptions on the page are our
own wording of what our code computes.

Each entry carries `title`, `description` (FinLab's own wording, which is where
most of the indicator definitions come from), `index` (dates), `values`
(`benchmark` = TAIEX, `ind` = the indicator) and `latest_signal`.

## Data sources (all verified working, no auth)

| source | endpoint | notes |
| --- | --- | --- |
| TWSE every stock's close and up/down mark | `rwd/zh/afterTrading/MI_INDEX?type=ALL` | ~5 MB/day, from 2004-02-11 |
| TWSE daily TAIEX close | `rwd/zh/afterTrading/FMTQIK` | a whole month per request, **back to 1990** |
| TPEx daily quotes | `tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes` | close **and 發行股數** in one report, plus up/down marks |
| TPEx P/B per stock | `tpex.org.tw/www/zh-tw/afterTrading/peQryDate` | 上櫃 side |
| TPEx margin balances | `tpex.org.tw/www/zh-tw/margin/balance` | board lots only, no loan amount; back to at least 2007 |
| TWSE margin balances | `rwd/zh/marginTrading/MI_MARGN?selectType=ALL` | per-stock lots + market totals |
| TWSE P/B per stock | `rwd/zh/afterTrading/BWIBBU_d?selectType=ALL` | from 2005-09 |
| TWSE shares outstanding | `rwd/zh/fund/MI_QFIIS?selectType=ALLBUT0999` | only free daily source of 發行股數 |
| NDC PMI | POST `index.ndc.gov.tw/n/json/data/PMI/total` | 2012-07 onwards |
| NDC NMI | POST `index.ndc.gov.tw/n/json/data/NMI/total` | 2014-08 onwards |
| NDC 景氣指標/對策信號 | POST `index.ndc.gov.tw/n/json/data/eco/indicators` | 1984-01 onwards |
| TAIFEX 三大法人期貨 | POST `taifex.com.tw/cht/3/futContractsDateDown` | **only ~2 years back** |

### TWSE truncates silently under load

TWSE does not rate-limit with HTTP 429. It answers an over-eager client with
`stat: "OK"` and a payload that is simply **missing tables** -- the same request
replayed later returns the full document. A first backfill at a 2.0 s interval
recorded 2,900 days and lost 609 of them (21 %) this way, with no error in the
log beyond a single timeout.

So the crawler declares the tables each feed must return (`REQUIRED` in
`fetch_twse.py`) and treats a reply missing any of them as a throttling signal:
back off, retry, and do **not** write the day to the manifest. Days the market
was genuinely closed are recorded under a separate `<feed>_closed` key so they
are not retried forever. `reconcile.py` walks an existing manifest against what
actually landed in parquet and re-queues anything with no data behind it.
A 40-day probe at 3.5 s saw zero truncation.

Two tables also only appear in recent years, so neither can be relied on:
the index tables inside MI_INDEX start in 2009 (hence FMTQIK above), and
漲跌證券數合計 starts in 2011 -- advances and declines are counted from the
per-stock 漲跌(+/-) column instead, which works from 2004.

The NDC endpoints are POST-only and CSRF-protected: fetch the matching HTML page
first, read `<meta name="csrf-token">`, and send it back with the session cookie.
TAIFEX serves its CSV as Big5, not UTF-8.

## Indicator definitions

Descriptions are FinLab's; the "how we compute it" column is what this repo does.

| indicator | our construction | status |
| --- | --- | --- |
| 生命線指標 | TAIEX vs its SMA(20) | **exact** — the archived `ind` matches SMA(20) to 0.0 |
| 台股多空排列家數 | count of stocks in bullish vs bearish MA stacking, then short-minus-long MA of the spread | needs backfill |
| 騰落線指標 (ADL) | cumulative advance-minus-decline line, short-minus-long MA | needs backfill |
| 大盤週線MACD | MACD(12,26,9) histogram on weekly 0050 | value scale matches 0050, not the index |
| 大盤融資維持率 | Σ(margin lots × 1000 × close) ÷ margin balance in TWD, TWSE only | 1.8980 vs FinLab 1.9036 on 2026-09-18 (**0.3 %**). TPEx publishes margin only in board lots, never the loan amount, and TWSE alone already matches — FinLab looks to be 上市-only here too |
| 大盤股價淨值比 | Σ(close × shares) ÷ Σ(close × shares ÷ P/B), 上市 + 上櫃 | 4.11 vs 3.64 — level differs by a stable 1.14×, threshold rescaled; see gaps |
| 製造業PMI / 未來六個月展望 | NDC published value | **exact match** |
| 非製造業NMI | NDC published value | **exact match** |
| 台灣景氣對策燈號 | NDC score, MA(3) vs MA(12) crossover | series matches |
| 法人期貨部位動能 | 三大法人 net OI value across 大台+小台+微台, 10-day change ÷ 250-day stdev | corr **0.9977**, MAE 0.07 |

### The composite (大盤綜合指標)

A weighted sum of the ten binary sub-signals on a 0.5 grid, displayed 0–10.
Measuring how far the archived composite moves when exactly one sub-signal flips
gives each indicator's step size:

| indicator | flips | composite also moved | step |
| --- | --- | --- | --- |
| 台股多空排列家數 | 202 | 99 % | 1.5 |
| 生命線指標 | 295 | 95 % | 1.0 |
| 騰落線指標(ADL) | 84 | 100 % | 1.5 |
| 大盤週線MACD | 51 | 90 % | 1.0 |
| 大盤融資維持率 | 20 | 100 % | 2.0 |
| 大盤股價淨值比 | 19 | 84 % | 1.0 |
| 製造業PMI | 28 | 93 % | 1.5 |
| PMI未來6月展望 | 16 | 94 % | 1.5 |
| 非製造業NMI | 24 | 92 % | 1.0 |
| 景氣對策燈號 | 16 | 100 % | 2.0 |

Those steps sum to 14, not 10, and grouping the 2,964 archived days by their
ten-signal pattern leaves 39 % of days in groups holding two composite values
exactly 1.0 apart. So FinLab's composite has at least one input that the page
never showed, or thresholds that move (rolling quantiles). The exact formula is
**not recoverable** from public data; this repo fits its own weights against the
archived composite instead and says so on the page. Only the fitted numbers
(`data/weights.json`) reach the site. On days where some sub-signals have no
data yet, the score is rescaled over the weight that is present, and a day is
only scored once that covers at least 60 % of the total.

Frontend colour thresholds, read out of the original bundle, are reproduced
exactly: gauge 0–4 green / 4–6 yellow / 6–10 red; 融資維持率 ≤1.6 / 1.6–1.7 / >1.7;
股價淨值比 ≤1.4 / 1.4–2 / >2; PMI and NMI split at 50; PMI 展望 at 40 and 60;
景氣燈號 9–17 blue, 17–23 yellow-blue, 23–32 green, 32–38 yellow-red, 38–45 red.
Red means "up" in the Taiwan convention, so green is not "good".

## Known gaps

0. **What genuinely cannot be obtained.** Only three things, none of them
   fixable with more crawling: 法人期貨部位動能 before 2024 (TAIFEX's export
   reaches back ~2 years, and the single-date page returns no table for 2008,
   2015 or 2020); any per-stock data before 2004-02-11 (MI_INDEX refuses it
   outright, STOCK_DAY starts 2010-01-04); and FinLab's own composite weights.
   Everything else on the page can be computed from public sources, most of it
   with *more* history than FinLab had -- see the table above.

1. **大盤股價淨值比 level.** We compute this ourselves without trouble -- over 28
   days where both exchanges are in hand, our series tracks FinLab's at
   **correlation 0.9993**, differing only by a constant factor of **1.1440**
   (range 1.1420-1.1469, standard deviation 0.0012). Adding TPEx changed the
   level by -0.4 %, so the missing 櫃買 half was never the explanation.

   The numerator checks out stock by stock (2330 at close 2,460 x 25.932 bn
   shares = NT$63.79 tn cap, P/B 9.92), so FinLab's implied aggregate book value
   is simply ~14 % larger than the one behind the exchanges' published per-share
   P/B. Total equity including non-controlling interests and preferred shares is
   the obvious candidate, but without FinLab's source it stays unproven.

   With the factor that steady, the level difference carries no information, so
   rather than bend our number to match, `calibrate_weights.py` measures the ratio
   on the overlapping days and writes the fitted P/B threshold onto our scale.
   The signal is then exactly equivalent.

   `market_pbr` refuses to emit a date unless **both** exchanges are present and
   each kept a plausible number of stocks. An earlier version quietly published
   上櫃-only figures for dates where the 上市 share-count feed had not been
   crawled yet -- a 3.92 that looked like a market P/B but was one exchange.
2. **法人期貨部位動能 before 2024.** TAIFEX's public export only reaches back about
   two years. History before that exists only in the archive, so the page shows
   this indicator from 2025-02 only.
3. **多空排列家數 and ADL before 2004-02-11.** MI_INDEX does not go back further,
   so these start ~3 years later than FinLab's series.
4. **The composite weights** are our own fit, not FinLab's (see above).

## Daily updates and the `data` branch

The site is built from `data/store/` only, a checkout of the `data` branch that
holds one row per trading day of aggregates (counts, market cap, book value,
margin totals, ...) as CSV, plus a rolling window of per-stock closes for
多空排列 -- about 3 MB in all, growing by a few KB a day. See
`scripts/store.py` for the file layout.

The `Daily update` workflow runs at 21:30 Taipei on weekdays:
`update_daily.py` fetches the new trading days (and re-fetches the last five,
since margin and share-count reports can land late), upserts the aggregates,
commits them to `data`, then `build_site_data.py` rebuilds the page and it is
deployed to GitHub Pages. No per-stock history is needed in CI.

To work on the site locally, check the branch out where the build expects it:

```bash
git worktree add data/store data
cd scripts && python build_site_data.py
```

## Rebuilding from a full crawl

Only needed to re-derive history, e.g. after changing an indicator's windows
(the aggregates bake in `STACK_MAS`; `build_site_data.py` refuses to run on a
store exported with different ones).

```bash
cd scripts
python fetch_ndc.py                 # monthly macro, seconds
python fetch_taifex.py              # institutional futures, ~1 minute
python fetch_twse.py --interval 2.0 # the long one, resumable, see below
```

`fetch_twse.py` walks four feeds one trading day at a time, parses each response
on arrival (raw MI_INDEX is far too large to keep) and appends per-year parquet
files. A manifest in `data/cache/twse_manifest.json` records finished days, so
the crawl can be interrupted and restarted freely. It backs off on HTTP 429, 5xx
and the HTML error pages TWSE serves when it is throttling.

Full backfill is roughly 21,600 requests. Measured rate is about 8 trading days
per minute (MI_INDEX alone is ~5 MB/day and the parse dominates), so budget
**18-24 hours**, and a few hundred MB of parquet. Use `--start`/`--end`/`--limit`
for partial runs.

Then re-export the store and rebuild the page:

```bash
python calibrate_weights.py   # fit composite weights (needs the FinLab archive)
python store.py export        # parquet -> data/store, then commit on `data`
python build_site_data.py     # write site/data/market.json
cd ../site && python -m http.server 8765 --bind 127.0.0.1
```

ECharts is vendored at `site/vendor/echarts.min.js` rather than loaded from a
CDN, so the dashboard also works with no network. The page must be served over
HTTP -- opening `index.html` from the filesystem blocks the `fetch` of
`data/market.json`.
