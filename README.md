# taiex-pulse

**線上儀表板：[higumalu.github.io/taiex-pulse](https://higumalu.github.io/taiex-pulse/)**（每個交易日台北時間 21:30 自動更新）

用免費公開資料重建的「台股大盤綜合指標」儀表板。指標構想來自 FinLab 即將關閉的
[台股大盤綜合指標](https://ai.finlab.tw/tw_market) 頁面，但所有數字都由本專案從
證交所、櫃買中心、期交所與國發會的公開資料自行計算。

原站資料在關站前先封存下來，**只拿來驗證與校準**，網頁上從不顯示封存值：輸入資料還不夠長的指標，會直接不顯示，而不是用別人的數字補上。

僅供參考，不構成投資建議。

## 目錄結構

```
data/store/           網站用的每日彙總資料，是 `data` branch 的 worktree
data/params.json      校準出的均線長度
data/weights.json     校準出的綜合指標權重與門檻
data/finlab_archive/  FinLab 封存（僅供驗證，不在 repo 裡）
data/parquet/         爬蟲產出的逐股資料表（不在 repo 裡）
data/cache/           爬蟲 manifest 與快取（不在 repo 裡）
data/raw/             國發會原始回應（不在 repo 裡）
scripts/              爬蟲、指標計算、校準、驗證
site/                 儀表板頁面
```

## 指標

| 指標 | 本專案的算法 | 與 FinLab 封存的吻合度 |
| --- | --- | --- |
| 生命線指標 | 加權指數 vs 20 日均線 | 完全一致 |
| 台股多空排列家數 | 均線呈多頭／空頭排列的個股家數差，再取短減長均線 | 方向一致 89.7 %，相關 0.92 |
| 騰落線指標（ADL） | 上漲減下跌家數的累積線，短減長均線 | 方向一致 94.2 %，相關 0.99 |
| 大盤週線MACD | 0050 週線 MACD(12,26,9) 柱狀體 | 數值尺度對得上 0050，但相關僅 0.14，**仍待修正** |
| 大盤融資維持率 | Σ(融資張數 × 1000 × 收盤價) ÷ 融資餘額，僅上市 | 1.8980 vs 1.9036（差 0.3 %） |
| 大盤股價淨值比 | Σ(收盤價 × 股數) ÷ Σ(收盤價 × 股數 ÷ 本淨比)，上市＋上櫃 | 相關 0.9993，水準固定高 1.14 倍，門檻已依比例換算 |
| 製造業 PMI／未來六個月展望 | 國發會公布值 | 完全一致 |
| 非製造業 NMI | 國發會公布值 | 完全一致 |
| 台灣景氣對策燈號 | 國發會分數，3 月均線 vs 12 月均線 | 一致 |
| 法人期貨部位動能 | 三大法人大台＋小台＋微台淨未平倉金額，10 日變化 ÷ 250 日標準差 | 相關 0.9977，MAE 0.07 |

月資料一律依「實際公布日」對齊：PMI、NMI 在次月初，景氣燈號在次月 27 日左右，所以不會用到當時還沒公布的數字。法人期貨部位動能不計入綜合分數。

### 綜合指標

十項子指標各自判斷偏多（1）或偏空（0），加權後換算成 0–10 分。

FinLab 的原始公式**無法從公開資料還原**。量測封存資料裡「只有一個子訊號翻轉」時總分的跳動幅度，推算出來的權重加總是 14 而不是 10。而且把 2,964 天依十個訊號的組合分組，有 39 % 的日子落在同一組內、總分卻正好差 1.0 的群組裡。這表示原站至少有一個頁面上沒顯示的輸入，或是會隨時間移動的門檻。

所以本專案改用自己的權重：`calibrate_weights.py` 對封存總分擬合出一組權重，寫進 `data/weights.json`。網站只用得到這些擬合出的數字。某些子指標在當天還沒有資料時，就用現有子指標的權重重新換算；權重涵蓋率未達 60 % 的日子不給分。之後的方向是以這組權重為起點自由調整，不追求和原站一致。

頁面的配色門檻沿用原站：總分 0–4 綠、4–6 黃、6–10 紅；融資維持率 1.6／1.7；股價淨值比 1.4／2（已換算到本專案的尺度）；PMI、NMI 以 50 為界；PMI 展望 40／60；景氣燈號 17／23／32／38。依台股慣例，紅色代表上漲，綠色不代表「好」。

## 資料來源

全部免登入，並已實測可以從 GitHub Actions 取得（`scripts/probe_sources.py`）。

| 來源 | 端點 | 說明 |
| --- | --- | --- |
| 證交所 個股收盤與漲跌 | `rwd/zh/afterTrading/MI_INDEX?type=ALL` | 每天約 5 MB，從 2004-02-11 起 |
| 證交所 加權指數收盤 | `rwd/zh/afterTrading/FMTQIK` | 一次一個月，可回溯到 1990 |
| 證交所 融資餘額 | `rwd/zh/marginTrading/MI_MARGN?selectType=ALL` | 個股張數與市場總額 |
| 證交所 個股本淨比 | `rwd/zh/afterTrading/BWIBBU_d?selectType=ALL` | 從 2005-09 起 |
| 證交所 發行股數 | `rwd/zh/fund/MI_QFIIS?selectType=ALLBUT0999` | 唯一免費的每日發行股數來源 |
| 櫃買 每日行情 | `tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes` | 同一份報表就有收盤價、發行股數和漲跌 |
| 櫃買 個股本淨比 | `tpex.org.tw/www/zh-tw/afterTrading/peQryDate` | 從 2007 起 |
| 櫃買 融資餘額 | `tpex.org.tw/www/zh-tw/margin/balance` | 只有張數、沒有金額 |
| 國發會 PMI／NMI | POST `index.ndc.gov.tw/n/json/data/PMI/total`、`.../NMI/total` | 2012-07、2014-08 起 |
| 國發會 景氣對策信號 | POST `index.ndc.gov.tw/n/json/data/eco/indicators` | 1984-01 起 |
| 期交所 三大法人期貨 | POST `taifex.com.tw/cht/3/futContractsDateDown` | **只能回溯約兩年** |

### 已知的來源陷阱

- **證交所被打太快時不回 429**：它會回 `stat: "OK"`，但悄悄少掉表格，稍後重送同一個請求又是完整的。第一次用 2 秒間隔回補，2,900 天裡有 609 天（21 %）就這樣遺失，log 裡幾乎看不出來。所以爬蟲會宣告每個 feed 必須回傳哪些表（`fetch_twse.py` 的 `REQUIRED`），少了任何一張就視為被限流，退避後重試，而且不記入 manifest。真正休市的日子另外記在 `<feed>_closed`，避免無限重試。
- **證交所每天 13:30–13:45 暫停「查詢全部資料」**：回應是「每日1:30PM到1:45PM為網站尖峰時間，查詢全部資料功能暫停使用!」。這不是休市，爬蟲會等暫停結束再抓。
- **MI_INDEX 裡的指數表從 2009 年才有，漲跌家數合計從 2011 年才有**：所以加權指數改用 FMTQIK，漲跌家數改由個股的漲跌欄位自己數，這樣可以回溯到 2004。
- **國發會的端點只接受 POST，並有 CSRF 保護**：要先抓對應的 HTML 頁面，讀出 `<meta name="csrf-token">`，再連同 session cookie 送回去。
- **期交所的 CSV 是 Big5 編碼**。查詢區間的結束日如果還沒有資料（例如下午 3 點前的今天，或是休市日），整段區間都會回傳錯誤頁。而且公布前的今天，數值會全部是 0。

## 每日更新與 `data` branch

網站只讀 `data/store/`，也就是 `data` branch：每個交易日一列的彙總值 CSV（家數、市值、淨值、融資金額等），外加最近 80 個交易日的逐股收盤價，給多空排列計算均線用。總共約 3 MB，每天只增加幾 KB。檔案格式寫在 `scripts/store.py`。

`Daily update` workflow 在平日台北時間 21:30 執行，程式碼推到 `dev` 時也會觸發一次：

1. `update_daily.py` 抓最新的交易日，並重抓兩種日子：
   - 最近 5 個平日，因為融資和股數要到晚上才公布。
   - 最近 30 個交易日裡欄位不齊的日子。
2. 把彙總值寫回 `data/store/`，再 commit 到 `data` branch。
3. `build_site_data.py` 重建 `site/data/market.json`。
4. 部署到 GitHub Pages。

CI 不需要逐股的完整歷史。多空排列只要窗口內的收盤價就能算，結果已驗證和用完整歷史算的逐格相同。

### 在本機跑網站

```bash
pip install -r requirements.txt
git worktree add data/store data
cd scripts && python build_site_data.py
cd ../site && python -m http.server 8765 --bind 127.0.0.1
```

頁面必須透過 HTTP 開啟；直接打開 `index.html` 的話，瀏覽器會擋掉讀取 `data/market.json`。ECharts 放在 `site/vendor/` 裡，離線也能用。

## 從完整爬取重建歷史

只有要重算歷史時才需要，例如改了均線長度。彙總值裡已經內含 `STACK_MAS`，如果參數和 store 匯出時用的不同，`build_site_data.py` 會拒絕執行。

```bash
cd scripts
python fetch_twse_index.py              # 加權指數 1990 起，約 440 次請求
python fetch_twse.py --interval 4.0     # 上市 4 個 feed，可中斷續跑
python fetch_tpex.py --interval 3.5     # 上櫃，可中斷續跑
python fetch_taifex.py                  # 法人期貨，約 1 分鐘
python fetch_ndc.py                     # 國發會月資料，幾秒鐘
python status.py                        # 看進度
python reconcile.py --apply             # 把沒留下資料的日子重新排進佇列
```

證交所完整回補大約 21,600 次請求，以 4 秒間隔要 **一天以上**，會產生數百 MB 的 parquet。請維持這個間隔，對方伺服器很容易限流。

爬完後：

```bash
python calibrate_params.py    # 找回原站沒公開的均線長度（需要 FinLab 封存）
python calibrate_weights.py   # 擬合綜合指標權重與門檻（需要 FinLab 封存）
python store.py export        # parquet → data/store
python update_daily.py        # 補上本機爬蟲之後的日子
python build_site_data.py
```

然後進 `data/store/` 裡 commit 並推送 `data` branch。

## 已知限制

- **真正拿不到的只有三樣**，再怎麼爬也補不回來：
  - 2024 年以前的法人期貨部位（期交所只提供約兩年）。
  - 2004-02-11 以前的逐股資料（MI_INDEX 不提供）。
  - FinLab 原本的綜合指標權重。
- **大盤股價淨值比的水準比 FinLab 高約 14 %**：兩者相關 0.9993，比例穩定在 1.144 倍（標準差 0.0012）。加入上櫃只讓數值變動 -0.4 %，個股分子也逐檔核對過，所以差異應該來自淨值的定義。最可能的解釋是原站用含非控制權益的總權益，而交易所公布的本淨比用的是母公司普通股權益。本專案保留自己的數字，把門檻依比例換算，所以訊號完全等價。
- **上櫃資料正在回補中**：補完之前，大盤股價淨值比不會顯示。
- **法人期貨部位動能只從 2025-02 開始**：期交所的資料最早只到 2024，再扣掉 250 日標準差的暖身期。
- **大盤週線MACD 和原站相關度還很低**（0.14）：數值尺度對得上 0050，但走勢還沒對上，算法仍待修正。

## FinLab 封存

原站是一個 Nuxt SPA，只讀取並顯示一份 Firestore 文件，所有計算都在 FinLab 後端完成。2026-09-22 封存了兩份文件（project `fdata-299302`、collection `twMarket`）：

| 檔案 | 內容 |
| --- | --- |
| `twMarket_holdIndicator_20260922.json` | 綜合指標與 10 項子指標的完整歷史 |
| `twMarket_futuresPositioning_20260922.json` | 法人期貨部位動能，2010-01-04 起的日資料 |

它們放在 `data/finlab_archive/`，只透過 `scripts/finlab_archive.py` 給校準和驗證腳本讀取，`build_site_data.py` 不會引用。這個資料夾不在 repo 裡：這個 repo 只發佈程式碼，不發佈 FinLab 的資料。網頁上的指標說明文字，也是本專案針對自己的算法重新撰寫的。

## 授權

程式碼採 [Apache License 2.0](LICENSE)。資料來自上述政府機關與交易所的公開資訊，使用時請註明出處。
