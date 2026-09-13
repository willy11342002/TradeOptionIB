#!/usr/bin/env bash
# 在本機執行(跟 app 同一台機器)。透過 GCP IAP TCP forwarding 把 VM 上
# 的 IB API port 轉發到 localhost，不需要 VM 有對外開放的 API port，
# 也不需要固定外部 IP，靠 gcloud 登入的 IAM 權限驗證。
#
# 這台 app 照舊連 127.0.0.1:4001(live)/4002(paper)，跟本機真的裝
# Gateway 時的連線設定一樣，見 app/services/ib_prefs.py——VM 上真正對外
# 開放的是 socat 轉出來的 14001/14002(Gateway 本身只綁 127.0.0.1:4001/
# 4002，同一台 VM 上 live/paper 沒有 docker 那種獨立網路命名空間，兩個
# Gateway 各自的 4001/4002 早就被佔用，只能另開 port 讓 socat 轉發，見
# gce/scripts/startup.sh)，本機這邊用 --local-host-port 轉回 4001/4002，
# app 端完全不用感覺到這個轉換。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "找不到 ${ENV_FILE}，先複製 gce/.env.example 成 gce/.env 並填值" >&2
  exit 1
fi
set -a
source "${ENV_FILE}"
set +a

# 背景各開一個 tunnel，PID 印出來方便之後手動關閉。
# 連的是 VM 上 socat 轉出來的 14001/14002，本機這邊映射回 4001/4002。
if [[ "${ENABLE_LIVE:-false}" == "true" ]]; then
  gcloud compute start-iap-tunnel "${VM_NAME}" 14001 \
    --local-host-port=localhost:4001 \
    --zone="${GCP_ZONE}" --project="${GCP_PROJECT_ID}" &
  echo "live tunnel PID: $!"
else
  echo "ENABLE_LIVE=false，VM 上沒有跑 live，跳過 live tunnel"
fi

gcloud compute start-iap-tunnel "${VM_NAME}" 14002 \
  --local-host-port=localhost:4002 \
  --zone="${GCP_ZONE}" --project="${GCP_PROJECT_ID}" &
echo "paper tunnel PID: $!"

wait
