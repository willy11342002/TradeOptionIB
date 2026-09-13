#!/usr/bin/env bash
# 在本機執行(需要先裝好 gcloud CLI 並登入)。
# 建立 VM，把 startup.sh 設成開機腳本，帳密存進 Secret Manager、非機密
# 設定寫進 instance metadata——開機後 VM 自己裝好整套 IB Gateway 並啟
# 動，不用手動 SSH 進去改任何檔案。防火牆只留 SSH/IAP，API relay port
# (14001/14002)也只放行 IAP 來源，不對外開放。
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

gcloud config set project "${GCP_PROJECT_ID}"
gcloud services enable secretmanager.googleapis.com

SA_NAME="ib-gateway-vm"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# 專門給這台 VM 用的 service account，只授權它讀這 4 個 secret，跟專案
# 裡其他共用預設 service account 的東西隔開。
if ! gcloud iam service-accounts describe "${SA_EMAIL}" &>/dev/null; then
  gcloud iam service-accounts create "${SA_NAME}" --display-name="IB Gateway VM"
  # IAM 新建的 service account 有短暫的 propagation delay，緊接著拿去
  # 授權/建 VM 偶爾會噴「service account does not exist」，等一下比較
  # 保險(不是無意義的輪詢，是已知的 GCP IAM 最終一致性問題)。
  sleep 10
fi

# 帳密進 Secret Manager，不進 instance metadata(見對話紀錄的取捨說
# 明：metadata 任何有 compute.viewer 權限的人或 VM 上任何程序都能讀，
# Secret Manager 要明確 IAM 授權才能讀，而且有存取稽核紀錄)。透過暫存
# 檔傳值，不要直接把密碼寫進指令列(避免留在 shell 歷史紀錄)。
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
printf '%s' "${IB_LOGIN_LIVE:-}"     > "${TMP_DIR}/ib-userid-live"
printf '%s' "${IB_PASSWORD_LIVE:-}"  > "${TMP_DIR}/ib-password-live"
printf '%s' "${IB_LOGIN_PAPER:-}"    > "${TMP_DIR}/ib-userid-paper"
printf '%s' "${IB_PASSWORD_PAPER:-}" > "${TMP_DIR}/ib-password-paper"

for secret_name in ib-userid-live ib-password-live ib-userid-paper ib-password-paper; do
  if gcloud secrets describe "${secret_name}" &>/dev/null; then
    gcloud secrets versions add "${secret_name}" --data-file="${TMP_DIR}/${secret_name}"
  else
    gcloud secrets create "${secret_name}" \
      --replication-policy=automatic \
      --data-file="${TMP_DIR}/${secret_name}"
  fi
  gcloud secrets add-iam-policy-binding "${secret_name}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" \
    > /dev/null
done

gcloud compute instances create "${VM_NAME}" \
  --zone="${GCP_ZONE}" \
  --machine-type="${MACHINE_TYPE}" \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size="${DISK_SIZE_GB}GB" \
  --boot-disk-type=pd-standard \
  --service-account="${SA_EMAIL}" \
  --scopes=cloud-platform \
  --metadata="enable-live=${ENABLE_LIVE:-false},swap-size-gb=${SWAP_SIZE_GB:-2},ibc-version=${IBC_VERSION}" \
  --metadata-from-file="startup-script=${SCRIPT_DIR}/startup.sh"

gcloud compute firewall-rules create allow-iap-ssh \
  --network=default \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:22 \
  --source-ranges=35.235.240.0/20 \
  --description="Only allow SSH from GCP Identity-Aware Proxy" \
  || echo "firewall rule allow-iap-ssh 可能已存在，略過"

# IB Gateway API 的 socat 轉發 port(14001/14002)也只准 IAP 來源連。
gcloud compute firewall-rules create allow-iap-ibgateway \
  --network=default \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:14001,tcp:14002 \
  --source-ranges=35.235.240.0/20 \
  --description="Only allow IB Gateway API relay ports from GCP Identity-Aware Proxy" \
  || echo "firewall rule allow-iap-ibgateway 可能已存在，略過"

echo ""
echo "VM 建立完成，開機後 startup-script 會自動裝好 paper 環境(約 2-3 分鐘)。"
echo "看安裝進度："
echo "  gcloud compute ssh ${VM_NAME} --zone=${GCP_ZONE} --tunnel-through-iap --command='sudo journalctl -u google-startup-scripts -f'"
echo "裝完後本機執行 gce/scripts/02_connect_tunnel.sh 接上 app。"
