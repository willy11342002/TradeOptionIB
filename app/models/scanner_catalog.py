"""
IB 市場掃描器的靜態參考資料（篩選欄位／掃描代碼），讀
app/resources/scan_filters.json 跟 scan_types.json 這兩份精簡資料檔——
這兩份是 scripts/gen_scanner_catalog.py 從 reqScannerParametersAsync() 的
原始 XML dump 一次性轉出來、再手動填上中文翻譯的結果，不是每次啟動都重
新解析那份 1.8MB 的 XML。

跟 app/models/screener.py 分開放：這裡是純參考資料查詢(給篩選條件挑選
UI、掃描代碼搜尋 UI、AI 建議、歷史紀錄的標籤還原共用)，不是「查詢/復篩
選擇權」那條業務邏輯的一部分。
"""
from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field

from app.paths import PROJECT_ROOT

_RESOURCES_DIR = PROJECT_ROOT / "app" / "resources"


@dataclass
class FilterFieldDef:
    code: str        # TagValue 的 tag，直接送進 reqScannerDataAsync 的 scannerSubscriptionFilterOptions
    name_en: str
    unit_zh: str = ""


@dataclass
class FilterDef:
    id: str
    kind: str  # "range"（兩個欄位 Min/Max 共用一列）或 "simple"（一個欄位）
    category_en: str
    category_zh: str
    label_zh: str
    tooltip_zh: str
    value_type: str  # "double" | "int" | "bool"
    fields: list[FilterFieldDef]


@dataclass
class ScanTypeDef:
    code: str
    name_en: str
    name_zh: str
    tooltip_zh: str
    access: str | None
    name_en_is_guess: bool = False
    # 這個掃描代碼實際支援哪些商品類型(IB 掃描器的 instrument 參數值)，
    # 目前這個 app 只關心 "STK"(股票)/"ETF.EQ.US"(美股 ETF)——見
    # scripts/gen_scanner_catalog.py::_extract_scan_types() 從原始 XML
    # 的 ScanType.instruments 屬性篩出來，UI 選了哪個商品類型，掃描代碼
    # 挑選器就只顯示這個欄位有包含該代碼的項目(app/views/
    # web_screener_widget.py::_current_scan_type_catalog())。
    instruments: list[str] = field(default_factory=lambda: ["STK"])


@functools.lru_cache
def load_filter_catalog() -> list[FilterDef]:
    data = json.loads((_RESOURCES_DIR / "scan_filters.json").read_text(encoding="utf-8"))
    return [
        FilterDef(
            id=entry["id"], kind=entry["kind"],
            category_en=entry["category_en"], category_zh=entry["category_zh"],
            label_zh=entry["label_zh"], tooltip_zh=entry["tooltip_zh"],
            value_type=entry["value_type"],
            fields=[FilterFieldDef(**f) for f in entry["fields"]],
        )
        for entry in data
    ]


@functools.lru_cache
def load_scan_type_catalog() -> list[ScanTypeDef]:
    data = json.loads((_RESOURCES_DIR / "scan_types.json").read_text(encoding="utf-8"))
    return [ScanTypeDef(**entry) for entry in data]


def filter_by_id(filter_id: str) -> FilterDef | None:
    return next((f for f in load_filter_catalog() if f.id == filter_id), None)


def scan_type_by_code(code: str) -> ScanTypeDef | None:
    return next((s for s in load_scan_type_catalog() if s.code == code), None)
