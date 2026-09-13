#!/usr/bin/env bash
# 改完 gce/.env(帳密、ENABLE_LIVE、SWAP_SIZE_GB、IBC_VERSION 等)之後跑
# 這支：把新的帳密推去 Secret Manager 加一個版本、非機密設定推上
# instance metadata，重開機讓 startup.sh 重新套用——全程不用 SSH 進去
# 手動改任何檔案。VM 會重開機，Gateway 會斷線個幾分鐘，避開交易時間執
# 行。
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

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
printf '%s' "${IB_LOGIN_LIVE:-}"     > "${TMP_DIR}/ib-userid-live"
printf '%s' "${IB_PASSWORD_LIVE:-}"  > "${TMP_DIR}/ib-password-live"
printf '%s' "${IB_LOGIN_PAPER:-}"    > "${TMP_DIR}/ib-userid-paper"
printf '%s' "${IB_PASSWORD_PAPER:-}" > "${TMP_DIR}/ib-password-paper"

# 每個 secret 只留最新一個 active 版本(舊版本 disable 掉)，避免帳密改
# 久了 active 版本數超過 Secret Manager 每月 6 個免費額度，變成要收費。
for secret_name in ib-userid-live ib-password-live ib-userid-paper ib-password-paper; do
  new_version="$(gcloud secrets versions add "${secret_name}" \
    --data-file="${TMP_DIR}/${secret_name}" --format='value(name)')"
  for old_version in $(gcloud secrets versions list "${secret_name}" \
      --filter="state:enabled AND name!=${new_version}" --format='value(name)'); do
    gcloud secrets versions disable "${old_version}" --secret="${secret_name}" -q
  done
done

gcloud compute instances add-metadata "${VM_NAME}" \
  --zone="${GCP_ZONE}" \
  --metadata="enable-live=${ENABLE_LIVE:-false},swap-size-gb=${SWAP_SIZE_GB:-2},ibc-version=${IBC_VERSION}" \
  --metadata-from-file="startup-script=${SCRIPT_DIR}/startup.sh"

echo "Secret Manager 版本 + metadata 更新完成，重開機套用新設定..."
gcloud compute instances reset "${VM_NAME}" --zone="${GCP_ZONE}"

echo "重開機中，大概等 2-3 分鐘，用以下指令看進度："
echo "  gcloud compute ssh ${VM_NAME} --zone=${GCP_ZONE} --tunnel-through-iap --command='sudo journalctl -u google-startup-scripts -f'"
