"""ThetaData 回補資料(`pref/backtest/thetadata/`)的雲端鏡像：Cloudflare R2(S3 相容 API)。

本機 parquet 永遠是唯一的讀寫對象，R2 只是「這台機器/這個雲端 session 本機沒有時的來源」：
`scripts/backfill_thetadata.py` 抓到新月份存檔後順便上傳，之後任何一台機器(包含每次重開的雲端
session)本機缺哪個月份，就先試著從 R2 下載那個檔案回本機，只有 R2 也沒有才會真的去打 ThetaData
API(backfill 腳本)或直接跳過(`option_chain.py`)——不改變「只有本機(含 R2 補齊後)已回補過的標的
能回測」這條規則，R2 只是本機硬碟以外的第二個「本機」。

需要 `.env` 四個變數(沒設定就整支模組不會有任何網路動作，`enabled()` 回 False，行為退回純本機)：
    R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT_URL / R2_BUCKET_NAME

*** boto3 只在真的要連線時才 import(`_client()` 裡)，這支檔案本身沒有頂層重量級 import，
跟 `option_chain.py` 一樣可以放心被其他模組頂層 import ***。key 命名是「ticker/YYYY-MM.parquet」，
跟本機 `THETADATA_DIR` 底下的相對路徑完全一致，方便對照。
"""
from __future__ import annotations

import os
from pathlib import Path

_ENV_KEYS = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME")


def enabled() -> bool:
    """四個環境變數都有設定才視為啟用；缺任何一個就當作沒有雲端鏡像，呼叫端照舊只用本機檔案。"""
    return all(os.environ.get(k) for k in _ENV_KEYS)


def _client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def upload(local_path: Path, key: str) -> None:
    """把本機已經存好的檔案上傳到 R2，覆蓋同名 key。呼叫端(backfill 腳本)自己判斷要不要呼叫。"""
    _client().upload_file(str(local_path), os.environ["R2_BUCKET_NAME"], key)


def download(key: str, local_path: Path) -> bool:
    """R2 有這個 key 就下載到 local_path 並回傳 True；不存在回傳 False(不是錯誤，呼叫端照舊當
    「還沒回補過」處理)。先寫暫存檔再取代，中途中斷不會留下寫一半的壞檔(跟 backfill 腳本存檔同邏輯)。"""
    from botocore.exceptions import ClientError

    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = local_path.with_suffix(".parquet.tmp")
    try:
        _client().download_file(os.environ["R2_BUCKET_NAME"], key, str(tmp))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return False
        raise
    os.replace(tmp, local_path)
    return True
