"""
一次性工具腳本：把 reqScannerParametersAsync() 的原始 XML dump
(scanner_parameters.xml，見 ib.reqScannerParametersAsync() 的說明；這份
dump 是連線 IB 後手動存檔的，跑法可參考 test.ipynb) 轉成
app/resources/scan_filters.json / scan_types.json 這兩份給
app/models/scanner_catalog.py 讀的精簡資料檔。

跟 scripts/ 底下其他一次性 IB 測試腳本一樣，不隨 app 一起載入、也不是
每次啟動都要重跑——IB 的掃描參數幾乎不會變。

*** scan_types.json 會合併既有翻譯，不是整份覆蓋 ***：`_extract_scan_types()`
現在收「STK 或 ETF.EQ.US 支援」的掃描代碼(不再只收 STK，為了讓
ETF 篩選有代碼可選)，寫檔前 `main()` 會先讀出 `app/resources/scan_types.json`
目前已有的內容，對每個 `code` 已經存在的項目，沿用舊檔案裡的
`name_zh`/`tooltip_zh`(手動翻譯過的)，只有這次新出現的代碼(通常是只
支援 ETF、STK 篩選時本來不會列進來的那些)才會是空字串，等之後再手動
補。scan_filters.json 仍然整份覆蓋——那份本來就不分商品類型，沒有這個
問題，重跑前一樣要注意如果之後改成分商品類型會需要一樣的合併邏輯。

只處理 uiFilters 這份完整版 FilterList(FilterList[varName="uiFilters"]，
是舊版 filterList 的超集)，並且只保留 AbstractField type 落在
DoubleField/IntField/BooleanField 的 RangeFilter/SimpleFilter
(TripleComboFilter「產業分類」、ThemeFilter「投資主題」跟 ComboField/
DateField/StringField/StringListField/SubstrListField/ConidField 這幾種
特殊型態一律跳過——IB 公開 API 沒有提供這些欄位實際可選值的清單，也大多
是股票以外的資產類別(債券評等/到期日等)專用，不在這次「數值型 Min/Max
篩選列」的範圍內)。
"""
import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_NUMERIC_TYPES = {
    "scanner.filter.DoubleField": "double",
    "scanner.filter.IntField": "int",
    "scanner.filter.BooleanField": "bool",
}

# scanCode 沒有官方 searchName 時，把底線分隔的代碼人性化成一句英文短
# 語，純粹當中譯時的參考提示，不要求完全通順——常見縮寫先展開，其餘照
# title-case 拼回去。
_ABBR = {
    "OPT": "Option", "IMP": "Implied", "VOLAT": "Volatility", "PERC": "Percent",
    "AVG": "Average", "NUM": "Number", "DIV": "Dividend", "USD": "USD",
    "IB": "IB", "ST": "Short-Term", "PC": "Put/Call",
}


def _humanize_scan_code(code: str) -> str:
    words = [w for w in code.split("_") if w]
    out = []
    for w in words:
        if w in _ABBR:
            out.append(_ABBR[w])
        elif w.isupper() and len(w) > 1:
            out.append(w.capitalize())
        else:
            out.append(w)
    return " ".join(out)


def _extract_filters(root: ET.Element) -> list[dict]:
    filter_lists = root.findall("FilterList")
    ui_filters = next(fl for fl in filter_lists if fl.get("varName") == "uiFilters")

    entries = []
    skipped_kind = {}
    for child in ui_filters:
        if child.tag not in ("RangeFilter", "SimpleFilter"):
            skipped_kind[child.tag] = skipped_kind.get(child.tag, 0) + 1
            continue

        abstract_fields = child.findall("AbstractField")
        types = {af.get("type") for af in abstract_fields}
        if len(types) != 1 or next(iter(types)) not in _NUMERIC_TYPES:
            skipped_kind[next(iter(types), "?")] = skipped_kind.get(next(iter(types), "?"), 0) + 1
            continue
        value_type = _NUMERIC_TYPES[next(iter(types))]

        fields = [
            {"code": af.findtext("code"), "name_en": af.findtext("displayName"), "unit_zh": ""}
            for af in abstract_fields
        ]
        entries.append({
            "id": child.findtext("id"),
            "kind": "range" if len(fields) == 2 else "simple",
            "category_en": child.findtext("category") or "",
            "category_zh": "",
            "label_zh": "",
            "tooltip_zh": "",
            "value_type": value_type,
            "fields": fields,
        })

    entries.sort(key=lambda e: (e["category_en"], e["id"]))
    print(f"篩選欄位：保留 {len(entries)} 筆，跳過 {skipped_kind}")
    return entries


_SUPPORTED_INSTRUMENTS = ("STK", "ETF.EQ.US")


def _extract_scan_types(root: ET.Element) -> list[dict]:
    scan_types = root.find("ScanTypeList").findall("ScanType")

    entries = []
    for st in scan_types:
        raw_instruments = (st.findtext("instruments") or "").split(",")
        instruments = [i for i in _SUPPORTED_INSTRUMENTS if i in raw_instruments]
        if not instruments:
            continue
        code = st.findtext("scanCode")
        search_name = st.findtext("searchName")
        is_guess = not search_name or search_name == "None"
        name_en = _humanize_scan_code(code) if is_guess else search_name
        entries.append({
            "code": code,
            "name_en": name_en,
            "name_en_is_guess": is_guess,
            "name_zh": "",
            "tooltip_zh": "",
            "access": st.findtext("access"),
            "instruments": instruments,
        })

    entries.sort(key=lambda e: e["code"])
    stk_n = sum("STK" in e["instruments"] for e in entries)
    etf_n = sum("ETF.EQ.US" in e["instruments"] for e in entries)
    print(f"掃描代碼：{len(entries)} 筆(STK {stk_n} 筆／ETF.EQ.US {etf_n} 筆)，"
          f"其中 {sum(e['name_en_is_guess'] for e in entries)} 筆沒有官方顯示名稱")
    return entries


def _merge_scan_type_translations(entries: list[dict], existing_path: Path) -> list[dict]:
    """保留既有 scan_types.json 裡已經手動填好的 name_zh/tooltip_zh，只
    有這次新出現的代碼(這次改成也收 ETF.EQ.US 專用的代碼，STK 掃描時看
    不到)才會是空字串。用 code 當 key 對應，其他欄位(name_en/access/
    instruments)一律用這次重新解析出來的最新值，不沿用舊檔——那些不是
    手動填的，沒有保留舊值的理由。"""
    if not existing_path.exists():
        return entries
    old_by_code = {e["code"]: e for e in json.loads(existing_path.read_text(encoding="utf-8"))}
    reused = 0
    for entry in entries:
        old = old_by_code.get(entry["code"])
        if old and (old.get("name_zh") or old.get("tooltip_zh")):
            entry["name_zh"] = old.get("name_zh", "")
            entry["tooltip_zh"] = old.get("tooltip_zh", "")
            reused += 1
    print(f"沿用既有翻譯：{reused}/{len(entries)} 筆")
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", default=str(PROJECT_ROOT / "scanner_parameters.xml"))
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "app" / "resources"))
    args = parser.parse_args()

    tree = ET.parse(args.xml)
    root = tree.getroot()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    filters = _extract_filters(root)
    (out_dir / "scan_filters.json").write_text(
        json.dumps(filters, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    scan_types_path = out_dir / "scan_types.json"
    scan_types = _merge_scan_type_translations(_extract_scan_types(root), scan_types_path)
    scan_types_path.write_text(json.dumps(scan_types, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"已寫入 {out_dir / 'scan_filters.json'}")
    print(f"已寫入 {out_dir / 'scan_types.json'}")


if __name__ == "__main__":
    main()
