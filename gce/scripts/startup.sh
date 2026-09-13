#!/usr/bin/env bash
# GCE startup-script：VM 開機時由 google-startup-scripts.service 自動以
# root 執行，每次開機都會跑一次，寫成冪等的(可以重複執行不出錯)。
#
# 非機密設定(是否開 live、swap 大小、IBC 版本)從 GCE instance metadata
# 讀；IBKR 帳密改從 Secret Manager 讀(VM 掛的 service account 只被授權
# 讀這 4 個 secret，比 metadata 曝光面小、有存取稽核紀錄)——這支腳本本
# 身不含機密，可以進版控。要改帳密/是否開 live/swap 大小，改 gce/.env
# 之後跑 gce/scripts/03_update_and_restart.sh 推新設定、重開機讓這支腳
# 本重新套用，不用手動登入 VM 改任何檔案。
set -euo pipefail

metadata() {
  curl -sf -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1" 2>/dev/null || true
}

apt-get update
apt-get install -y xvfb x11vnc unzip curl socat jq

PROJECT_ID="$(curl -sf -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/project/project-id")"
ACCESS_TOKEN="$(curl -sf -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | jq -r .access_token)"

secret() {
  curl -sf -H "Authorization: Bearer ${ACCESS_TOKEN}" \
    "https://secretmanager.googleapis.com/v1/projects/${PROJECT_ID}/secrets/$1/versions/latest:access" \
    | jq -r '.payload.data' | base64 -d
}

TWS_USERID_LIVE="$(secret ib-userid-live)"
TWS_PASSWORD_LIVE="$(secret ib-password-live)"
TWS_USERID_PAPER="$(secret ib-userid-paper)"
TWS_PASSWORD_PAPER="$(secret ib-password-paper)"
ENABLE_LIVE="$(metadata enable-live)"; ENABLE_LIVE="${ENABLE_LIVE:-false}"
SWAP_SIZE_GB="$(metadata swap-size-gb)"; SWAP_SIZE_GB="${SWAP_SIZE_GB:-2}"
IBC_VERSION="$(metadata ibc-version)"; IBC_VERSION="${IBC_VERSION:-3.21.0}"

IB_GATEWAY_URL="https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh"
IBC_FILE="IBCLinux-${IBC_VERSION}.zip"
IBC_URL="https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/${IBC_FILE}"
IBC_PATH="/opt/ibc"

# --- swap file：e2-micro(1GB RAM) 用來緩解 OOM，e2-small 以上可以把
#     gce/.env 的 SWAP_SIZE_GB 設 0 跳過 ---
if [[ "${SWAP_SIZE_GB}" != "0" && ! -f /swapfile ]]; then
  fallocate -l "${SWAP_SIZE_GB}G" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

# --- IBC(live/paper 共用一份，只裝一次) ---
if [[ ! -f "${IBC_PATH}/gatewaystart.sh" ]]; then
  mkdir -p "${IBC_PATH}"
  curl -sSL -o "/tmp/${IBC_FILE}" "${IBC_URL}"
  unzip -o "/tmp/${IBC_FILE}" -d "${IBC_PATH}"
  chmod +x "${IBC_PATH}"/*.sh "${IBC_PATH}"/scripts/*.sh 2>/dev/null || true
fi

# 參數名稱查自 IbcAlpha/IBC 官方 config.ini/userguide.md，不同 IBC 版本
# 可能略有差異，升級 IBC_VERSION 後如果服務起不來，先查
# /opt/ibc/gatewaystart.sh 對一次變數名稱。
setup_instance() {
  local mode="$1" userid="$2" password="$3" display="$4" api_port="$5" relay_port="$6"
  local user="ibgw-${mode}"
  local home="/home/${user}"

  id "${user}" &>/dev/null || useradd -m -s /bin/bash "${user}"
  mkdir -p "${home}/ibc"

  # 判斷「已經裝好」的標準是有沒有版本號子目錄(IBC 預期的
  # ${TWS_PATH}/ibgateway/${TWS_MAJOR_VRSN}/ 結構)，不是單純看資料夾存
  # 不存在——舊版腳本裝出來的是扁平結構(沒有版本號子目錄，version 解析
  # 出來是垃圾值)，這裡偵測到這種壞掉的結構會自動清掉重裝，不會卡住。
  local existing_version_dir
  existing_version_dir="$(find "${home}/Jts/ibgateway" -maxdepth 1 -mindepth 1 -type d -regextype posix-extended -regex '.*/[0-9]+' 2>/dev/null | head -1)"
  if [[ -z "${existing_version_dir}" ]]; then
    rm -rf "${home}/Jts/ibgateway"
    sudo -u "${user}" curl -sSL -o "${home}/ibgateway-installer.sh" "${IB_GATEWAY_URL}"
    sudo -u "${user}" chmod u+x "${home}/ibgateway-installer.sh"
    # -q 靜默安裝，先裝到暫存目錄；版本號要等裝完才知道，裝完後從安裝
    # 出來的 .desktop 捷徑檔名(例如 "IB Gateway 10.45.desktop")解析出
    # 真正的版本號，再搬進 IBC 預期的路徑結構。
    sudo -u "${user}" "${home}/ibgateway-installer.sh" -q -dir "${home}/Jts/ibgateway_install"
    local desktop_file version
    desktop_file="$(ls "${home}/Jts/ibgateway_install/"*.desktop 2>/dev/null | head -1)"
    version="$(basename "${desktop_file}" | grep -oE '[0-9]+\.[0-9]+' | tr -d '.')"
    mkdir -p "${home}/Jts/ibgateway"
    mv "${home}/Jts/ibgateway_install" "${home}/Jts/ibgateway/${version}"
    chown -R "${user}:${user}" "${home}/Jts"
  fi
  local version
  version="$(ls "${home}/Jts/ibgateway" | head -1)"

  # config.ini 每次開機都重寫，Secret Manager 是唯一真相來源，不會有本
  # 機改過又被蓋掉的問題(因為本來就不該手動改 VM 上的檔案)。
  cat > "${home}/ibc/config.ini" <<EOF
IbLoginId=${userid}
IbPassword=${password}

TradingMode=${mode}

ExistingSessionDetectedAction=manual

AutoRestartTime=05:30 AM
ColdRestartTime=05:00 AM

SecondFactorAuthenticationTimeout=180
ReloginAfterSecondFactorAuthenticationTimeout=no
ReadOnlyLogin=no

# 全新安裝的 Gateway 這個沒特別設的話沿用 TWS 內部預設值，實測預設是
# 唯讀(下單會噴 "(321) ... Read-Only mode")，要明確設 no 才能下單。
ReadOnlyApi=no

# API 連線只會從同一台 VM 的 socat relay(127.0.0.1)進來，理論上不會跳
# 「Incoming connection」彈窗，但無人值守環境保險起見設 accept，避免萬
# 一彈窗卡住整條自動化流程沒人能點。
AcceptIncomingConnectionAction=accept
EOF
  chown "${user}:${user}" "${home}/ibc/config.ini"
  chmod 600 "${home}/ibc/config.ini"
  mkdir -p "${home}/ibc/logs"
  chown "${user}:${user}" "${home}/ibc/logs"

  # gatewaystart.sh 裡的設定是直接寫死在檔案開頭的變數賦值(TWS_MAJOR_VRSN=1045
  # 這種)，不是能用 systemd EnvironmentFile 覆蓋的設計——實測過，就算傳
  # 同名環境變數進去，腳本自己的賦值一樣會覆蓋掉，所以改成每組帳號各自
  # 複製一份，直接用 sed 改內容。
  cp "${IBC_PATH}/gatewaystart.sh" "${home}/gatewaystart.sh"
  sed -i \
    -e "s|^TWS_MAJOR_VRSN=.*|TWS_MAJOR_VRSN=${version}|" \
    -e "s|^IBC_INI=.*|IBC_INI=${home}/ibc/config.ini|" \
    -e "s|^TRADING_MODE=.*|TRADING_MODE=${mode}|" \
    -e "s|^IBC_PATH=.*|IBC_PATH=${IBC_PATH}|" \
    -e "s|^TWS_PATH=.*|TWS_PATH=${home}/Jts|" \
    -e "s|^TWS_SETTINGS_PATH=.*|TWS_SETTINGS_PATH=${home}/Jts|" \
    -e "s|^LOG_PATH=.*|LOG_PATH=${home}/ibc/logs|" \
    "${home}/gatewaystart.sh"
  chown "${user}:${user}" "${home}/gatewaystart.sh"

  cat > "/etc/systemd/system/xvfb-${mode}.service" <<EOF
[Unit]
Description=Xvfb virtual display for IB Gateway (${mode})
[Service]
ExecStart=/usr/bin/Xvfb :${display} -screen 0 1024x768x16
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

  # gatewaystart.sh 不加 -inline 的話，最後一步是把真正的 Gateway 丟進
  # `xterm ... &` 背景執行，自己馬上 exit 0——systemd 會誤判成服務掛
  # 了，每 RestartSec 重啟一次，Gateway 永遠來不及完成登入(實測抓到的
  # bug，已核對 gatewaystart.sh 原始內容)。加 -inline 讓它用 exec 頂替
  # 成前景 process，systemd 才追蹤得到真正在跑的東西。
  cat > "/etc/systemd/system/ibgateway-${mode}.service" <<EOF
[Unit]
Description=IB Gateway (${mode})
Requires=xvfb-${mode}.service
After=xvfb-${mode}.service network-online.target
Wants=network-online.target
[Service]
User=${user}
Environment=DISPLAY=:${display}
ExecStart=${home}/gatewaystart.sh -inline
Restart=always
RestartSec=15
[Install]
WantedBy=multi-user.target
EOF

  # IB Gateway 的 API socket 只綁 127.0.0.1，同一台 VM 上 socat 不能跟
  # Gateway 搶同一個 port 監聽，所以 relay port 另外開一個號碼(這是
  # 所有 IB Gateway docker 方案都內建 socat 的原因)。
  cat > "/etc/systemd/system/socat-${mode}.service" <<EOF
[Unit]
Description=socat relay for IB Gateway API (${mode}): ${relay_port} -> 127.0.0.1:${api_port}
Requires=ibgateway-${mode}.service
After=ibgateway-${mode}.service
[Service]
ExecStart=/usr/bin/socat TCP-LISTEN:${relay_port},fork,reuseaddr TCP:127.0.0.1:${api_port}
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
}

# paper 一定裝(e2-micro 記憶體有限，先跑穩 paper 這組)
setup_instance paper "${TWS_USERID_PAPER}" "${TWS_PASSWORD_PAPER}" 2 4002 14002
systemctl daemon-reload
systemctl enable --now xvfb-paper ibgateway-paper socat-paper

# live 由 gce/.env 的 ENABLE_LIVE 控制，預設關閉(e2-micro 同時開兩組風
# 險偏高，見 README)
if [[ "${ENABLE_LIVE}" == "true" ]]; then
  setup_instance live "${TWS_USERID_LIVE}" "${TWS_PASSWORD_LIVE}" 1 4001 14001
  systemctl daemon-reload
  systemctl enable --now xvfb-live ibgateway-live socat-live
else
  systemctl disable --now xvfb-live ibgateway-live socat-live 2>/dev/null || true
fi
