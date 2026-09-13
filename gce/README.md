# IB Gateway 部署到 GCP（原生安裝，不用 docker，開機自動全套配置）

把 IB Gateway 搬到雲端常駐，本機不用再開著 Gateway 軟體。這裡選的是
**原生安裝**（IB Gateway 官方 installer + IBC + Xvfb + systemd），不用
docker，理由是 GCP 沒有適合這個工作負載（常駐、有狀態、走裸 TCP、還要
跑 GUI）的免費容器服務——Cloud Run 只接受 HTTP/gRPC ingress 而且免費額
度要求 scale-to-zero，跟 Gateway 要一直維持登入 session 互斥；省掉
docker daemon 的開銷對擠進 `e2-micro` 1GB 免費額度也比較有幫助。

**部署完全自動化，不用手動 SSH 進去改任何檔案**：帳密跟設定值放在本機
`gce/.env`，帳密進 **Secret Manager**、非機密設定(是否開 live、swap 大
小等)進 GCE instance metadata，VM 開機時的 startup-script
(`gce/scripts/startup.sh`)自己去讀、裝好整套、啟動服務。之後要改帳密
或設定，改 `.env` 跑一支腳本重開機套用就好。

## 架構

- 1 台 Compute Engine VM，跑 **live** 跟/或 **paper** 兩組獨立的 IB
  Gateway（各自獨立的 Linux user / Jts 設定目錄 / Xvfb 顯示編號 /
  systemd service），Gateway 本身用預設 port：live=4001、paper=4002。
  `.env` 的 `ENABLE_LIVE` 控制要不要開 live，預設 `false`(先跑穩
  paper)。
- IB Gateway 的 API socket **只綁 `127.0.0.1`**（這是所有 IB Gateway
  docker 方案都內建 `socat` 的原因，原生安裝一樣要處理），所以用
  `socat` 把 `0.0.0.0:14001 -> 127.0.0.1:4001`(live)、
  `0.0.0.0:14002 -> 127.0.0.1:4002`(paper) 轉出來。外部 port 跟
  Gateway 真正的 port 不同號，是因為同一台 VM 上沒有 docker 那種獨立網
  路命名空間，`socat` 不能跟 Gateway 自己搶同一個 port 監聽。
- `14001`/`14002` 這兩個 relay port **也不直接對外開放**，防火牆只允許
  GCP IAP 的來源 IP 範圍連。本機透過 GCP IAP TCP forwarding 連進去
  （`gcloud compute start-iap-tunnel`），並在本機端映射回
  `127.0.0.1:4001`/`4002`——app 端完全不用改，連線設定跟本機真的裝
  Gateway 時一樣，跟現有 [ib_prefs.py](../app/services/ib_prefs.py) 存
  port 本身（而非只存環境旗標）的設計一致，靠 Google 帳號 IAM 驗證，不
  需要固定外部 IP。IB API 本身是未加密裸 TCP，直接對外開洞是不安全的
  作法。
- 帳密存在 **Secret Manager**(`ib-userid-live`/`ib-password-live`/
  `ib-userid-paper`/`ib-password-paper` 四個 secret)，VM 掛一個專用的
  service account(`ib-gateway-vm@...`)，只被授權讀這 4 個 secret，比
  直接放 instance metadata 曝光面小(metadata 任何有 `compute.viewer`
  權限的人、或 VM 上任何程序都能讀)，而且每次存取有稽核紀錄。用量遠低
  於 Secret Manager 每月 6 個 active 版本 + 1 萬次存取的免費額度，
  `03_update_and_restart.sh` 更新帳密時也會把舊版本 disable 掉，長期
  不會累積出費用。

## 資料夾內容

```
gce/
  .env.example              # GCP 專案/VM 規格/IBKR 帳密/live開關，複製成 .env 填值
  scripts/
    01_create_vm.sh         # 本機執行：建 service account + secrets + VM + 防火牆規則(只做一次)
    02_connect_tunnel.sh    # 本機執行：開 IAP tunnel，把 VM 的 14001/14002 轉發到本機 4001/4002
    03_update_and_restart.sh # 本機執行：.env 改過之後，更新 secrets/metadata + 重開機套用
    startup.sh               # VM 開機自動執行，不含機密，裝好整套 Gateway 並啟動
```

`gce/.env`（含帳密）已加進 `.gitignore`，不會進版控，`startup.sh` 本身
不含任何機密（帳密是開機時現查 Secret Manager 拿的）可以安心進版控。

## 部署步驟

### 0. 前置(本機，只做一次)

```bash
gcloud auth login
gcloud services enable compute.googleapis.com iap.googleapis.com
cp gce/.env.example gce/.env   # 再填入 GCP_PROJECT_ID、IB_LOGIN_*/IB_PASSWORD_* 等
```

### 1. 建立 VM(本機執行)

```bash
bash gce/scripts/01_create_vm.sh
```

會依序：啟用 Secret Manager API、建立專用的 service account、把帳密存
成 4 個 secret 並只授權這個 service account 讀取、建 VM(掛上這個
service account)、寫入防火牆規則(SSH 跟 API relay port 都只放行 IAP
來源)。VM 開機後 `startup.sh` 會自動：裝 Xvfb/socat、建 swap file、下
載安裝 IB Gateway 跟 IBC、從 Secret Manager 拿帳密寫好 `config.ini`、
建立並啟動 `xvfb-paper`/`ibgateway-paper`/`socat-paper` 這組 systemd
service(`ENABLE_LIVE=true` 的話 live 那組也會一起裝起來)，大約 2-3 分
鐘完成。

想看安裝進度(這是唯一會用到 SSH 的地方，純粹看 log，不用手動改檔案)：

```bash
gcloud compute ssh ib-gateway-vm --zone=<你的GCP_ZONE> --tunnel-through-iap \
  --command='sudo journalctl -u google-startup-scripts -f'
```

### 2. 本機接上 app

```bash
bash gce/scripts/02_connect_tunnel.sh
```

開著這個視窗，另開 app，連線設定用 `127.0.0.1:4002`(paper，模擬環境勾
選)，跟本機真的裝 Gateway 時一樣，見
[ib_prefs.py](../app/services/ib_prefs.py)。

### 3. 之後要改帳密/開 live/調 swap 大小

改 `gce/.env`，再跑：

```bash
bash gce/scripts/03_update_and_restart.sh
```

會把新帳密加進 Secret Manager 的新版本(舊版本自動 disable，不會累積出
費用)、把非機密設定更新到 instance metadata、重開機讓 `startup.sh` 重
新套用——一樣不用手動 SSH 進去改檔案。VM 重開機時 Gateway 會斷線幾分
鐘，避開交易時間執行。改 `ENABLE_LIVE=true` 前，建議先確認 paper 已經
穩定跑過幾天(見下方「已知的坑」)。

## 成本(2026 現價，僅供估算)

| 項目 | 費用 |
|---|---|
| `e2-micro`(us-west1，目前選擇) | 每月免費(GCP Always Free)，但 1GB RAM 容易被 OOM |
| `e2-small`(us-central1/us-west1) | 約 $12/月，1年期 CUD 折抵後約 $8/月 |
| Persistent Disk 30GB | 免費額度內 |
| 網路出口流量 | 每月 1GB 到北美免費，一般 API 控制流量用不到這麼多 |
| 保留靜態外部 IP | 用 ephemeral IP，VM 運作中免費，不用額外保留 |

## 延遲(台北視角)

選 `us-west1` 是用「免費」換「延遲」——本機(台北)↔VM 這段跨太平洋約
150-180ms，VM↔IBKR 實際伺服器(帳號多半導去香港/新加坡/東京)又要再繞
一次太平洋。這個延遲對停損/停利這種秒級反應的策略沒有實質影響，純粹
是取捨紀錄：

- 如果之後想換低延遲、接受付費：`asia-east1`(台灣) 的 `e2-small` 約
  $14/月，本機↔VM 近乎 0ms；`e2-micro` 在 `asia-east1` 不是免費的，約
  $7/月。
- IBKR 實際幫你的帳號服務的伺服器是哪個機房，可以在 TWS/Gateway 介面
  查「Connected IB Server Location」確認，不要憑猜測決定要不要換
  zone。

## 已知的坑

- **`gatewaystart.sh` 一定要加 `-inline`**：這支 IBC 腳本不加
  `-inline` 的話，最後一步是把真正的 Gateway 丟進 `xterm ... &` 背景
  執行，自己馬上 `exit 0`，systemd 會誤判成服務掛了不斷重啟，Gateway
  永遠來不及完成登入(實測踩過的坑，`startup.sh` 已經處理)。
- **`gatewaystart.sh` 的設定是寫死的變數賦值，不是環境變數覆蓋**：
  `TWS_MAJOR_VRSN=1045` 這種寫法在腳本裡是無條件賦值，systemd 的
  `EnvironmentFile` 傳同名變數進去也會被蓋掉，所以 `startup.sh` 改成
  每組帳號各自複製一份 `gatewaystart.sh` 用 `sed` 改內容。
- **IB Gateway 安裝路徑要包含版本號子目錄**：`-dir` 直接指到
  `Jts/ibgateway` 裝出來是扁平結構，IBC 找不到對應的
  `${TWS_PATH}/ibgateway/${TWS_MAJOR_VRSN}/` 路徑；`startup.sh` 先裝到
  暫存目錄，從安裝出來的 `.desktop` 檔名解析版本號，再搬進正確路徑。
- **2FA**：IBKR 的雙因子驗證在無人值守的自動重開流程中可能會卡住，目
  前沒有查到 100% 保證繞過的做法，建議先在 paper 帳號上實測幾天自動重
  開流程，確認穩定再切 `ENABLE_LIVE=true`。
- **每日/每週重開**：Gateway session 會過期，`startup.sh` 寫進
  `config.ini` 的 `AutoRestartTime`/`ColdRestartTime` 排在收盤空檔，避
  免交易時間中重開。
- **e2-micro 記憶體**：live+paper 同時開風險偏高，建議照預設先只跑
  paper，穩定後再視情況切 `ENABLE_LIVE=true` 或升級 `MACHINE_TYPE`。
- **service account 的 scope**：VM 掛的 `cloud-platform` scope 範圍很
  廣，但實際權限被 IAM 卡在只能讀那 4 個 secret(這是目前公認的標準作
  法：scope 給寬、IAM role 收窄)，之後這個 service account 不要另外授
  權其他資源，維持最小權限。
