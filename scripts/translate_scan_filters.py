"""
一次性工具腳本：補回 app/resources/scan_filters.json 被
scripts/gen_scanner_catalog.py 意外覆寫清空的中文翻譯(category_zh/
label_zh/tooltip_zh/unit_zh)——加 ETF 篩選支援時重跑
gen_scanner_catalog.py，只顧到 scan_types.json 要合併保留舊翻譯，忘了
同一次執行也會把 scan_filters.json 整份覆蓋成空翻譯，而這份檔案是
untracked(沒進 git)，覆蓋後原本手動翻譯的內容已經無法復原。

這支腳本用 AI(跟 app/models/openrouter_client.py 同一套 pydantic-ai/
OpenRouter 設定，讀同樣的 OPENROUTER_API_KEY/OPENROUTER_MODEL 環境變數)
重新翻譯一次——*** 這不是原本被覆寫掉的那份翻譯，是全新一輪的翻譯，跑
完務必人工抽查幾筆 ***。跟 gen_scanner_catalog.py 一樣，不隨 app 一起載
入，只在需要的時候手動執行一次：
    .venv/Scripts/python.exe scripts/translate_scan_filters.py
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FILTERS_PATH = PROJECT_ROOT / "app" / "resources" / "scan_filters.json"

# main.py/pyqt.py 都在最開頭呼叫這個才能讀到 .env 裡的 OPENROUTER_API_KEY
# ——這支腳本是獨立執行的一次性工具，不會經過那兩個進入點，要自己補一次。
load_dotenv(PROJECT_ROOT / ".env")

_BATCH_SIZE = 25

_SYSTEM_PROMPT = (
    "你在幫一個美股/ETF選擇權交易 app 翻譯 Interactive Brokers 市場掃描器"
    "(market scanner)的數值篩選欄位名稱，目標讀者是台灣的交易使用者，"
    "一律用繁體中文、金融業界慣用的簡潔說法(不要逐字直譯)。每個篩選條件"
    "有一個 category(分類，例如 Prices/Fundamentals/Options)跟一或兩個"
    "實際欄位(kind=range 有 Min/Max 兩個欄位共用一個 label；kind=simple"
    "只有一個欄位)。請填：\n"
    "- category_zh：這個分類的中文名稱，同一個 category_en 一定要翻成同"
    "一個中文，全部條目要一致。\n"
    "- label_zh：這個篩選條件本身的中文名稱(不用重複「最小/最大」，UI"
    "會自動在 label 前後加 Min/Max 提示)。\n"
    "- tooltip_zh：一句話中文說明這個欄位在篩選什麼，給滑鼠移過去看的"
    "提示用，簡短即可。\n"
    "- fields[].unit_zh：這個欄位的單位(例如「%」「美元」「百萬美元」"
    "「天」「股」)，沒有單位就填空字串，不要瞎猜。\n"
    "已知幾個範例(這是之前翻譯過、要保持一致風格的參考)：\n"
    "PRICE(kind=range, priceAbove/priceBelow) → label_zh=\"股價\"\n"
    "MKTCAP(kind=range) → label_zh=\"市值\"，unit_zh=\"百萬美元\"\n"
    "OPTVOLUME(kind=range) → label_zh=\"選擇權成交量\"\n"
    "只能照候選清單裡實際給的 id 填，不要跳過任何一筆，也不要自己發明"
    "新的 id。"
)


class FieldTranslation(BaseModel):
    code: str
    unit_zh: str = ""


class FilterTranslation(BaseModel):
    id: str
    category_zh: str
    label_zh: str
    tooltip_zh: str
    fields: list[FieldTranslation]


class FilterTranslationBatch(BaseModel):
    items: list[FilterTranslation]


def _require_openrouter_config() -> str:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY 未設定，請確認 .env 檔")
    model_name = os.environ.get("OPENROUTER_MODEL")
    if not model_name:
        raise RuntimeError("OPENROUTER_MODEL 未設定，請確認 .env 檔")
    return model_name


def _translate_batch_sync(entries: list[dict], model_name: str) -> FilterTranslationBatch:
    from pydantic_ai import Agent
    from pydantic_ai.models.openrouter import OpenRouterModelSettings

    agent = Agent(
        f"openrouter:{model_name}",
        output_type=FilterTranslationBatch,
        system_prompt=_SYSTEM_PROMPT,
        model_settings=OpenRouterModelSettings(max_tokens=4000),
    )
    table = json.dumps(
        [
            {
                "id": e["id"], "category_en": e["category_en"], "kind": e["kind"],
                "fields": [{"code": f["code"], "name_en": f["name_en"]} for f in e["fields"]],
            }
            for e in entries
        ],
        ensure_ascii=False,
    )
    prompt = f"請翻譯以下 {len(entries)} 筆篩選條件：\n{table}"
    result = agent.run_sync(prompt)
    return result.output


def main() -> None:
    model_name = _require_openrouter_config()
    data = json.loads(FILTERS_PATH.read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in data}

    for i in range(0, len(data), _BATCH_SIZE):
        batch = data[i : i + _BATCH_SIZE]
        print(f"翻譯第 {i + 1}~{i + len(batch)} 筆(共 {len(data)} 筆)...")
        result = _translate_batch_sync(batch, model_name)
        for item in result.items:
            entry = by_id.get(item.id)
            if entry is None:
                print(f"  警告：AI 回傳了不存在的 id {item.id!r}，略過")
                continue
            entry["category_zh"] = item.category_zh
            entry["label_zh"] = item.label_zh
            entry["tooltip_zh"] = item.tooltip_zh
            unit_by_code = {f.code: f.unit_zh for f in item.fields}
            for field in entry["fields"]:
                if field["code"] in unit_by_code:
                    field["unit_zh"] = unit_by_code[field["code"]]

    missing = [e["id"] for e in data if not e["label_zh"]]
    if missing:
        print(f"警告：{len(missing)} 筆還是空的：{missing}")

    FILTERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已寫回 {FILTERS_PATH}")


if __name__ == "__main__":
    main()
