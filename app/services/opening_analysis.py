"""
「開倉」分頁的分析流程協調者。背景執行緒依序打財經日曆/大盤價格/選擇權
波動率指數/選擇權籌碼分布四個外部資料源，交給 LLM 質化判斷振幅跟波動率
高低，再用固定規則 (STRATEGY_MAP) 對應出策略，最後把判斷結果連同抓到的
原始資料一起存進本機 JSONL 歷史檔 (方便之後在 UI 上點開回顧當時的原始
資料)。

沿用 kgi_client.py 已經在用的 QObject + threading.Thread + pyqtSignal
模式，確保網路 I/O 不會卡住 PyQt 主執行緒。
"""
import datetime
import json
import os
import threading

from PyQt5.QtCore import QObject, pyqtSignal

from app.models import economic_calendar_client, finmind_client, openrouter_client, taifex_vix_client
from app.paths import PROJECT_ROOT

HISTORY_FILE = PROJECT_ROOT / "open_position_history.jsonl"

# 振幅低的兩象限共用鐵禿鷹；策略內容本身之後再談，這裡先只定象限對應規則。
STRATEGY_MAP = {
    ("low", "low"): "鐵禿鷹",
    ("low", "high"): "鐵禿鷹",
    ("high", "low"): "雙買+小台",
    ("high", "high"): "大區間",
}


def append_history(record: dict) -> None:
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    records = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 單行壞掉不影響其他歷史紀錄的讀取
    return records


def _rewrite_history(records: list[dict]) -> None:
    with HISTORY_FILE.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def delete_history_entry(timestamp: str) -> None:
    """用 timestamp 當識別碼刪除單筆紀錄 (JSONL 不能原地刪一行，整檔重寫)。"""
    records = [r for r in load_history() if r.get("timestamp") != timestamp]
    _rewrite_history(records)


def update_history_reasoning(timestamp: str, reasoning: str) -> None:
    """使用者在「本次分析結果」detail 裡手動修改理由文字後，寫回歷史檔。"""
    records = load_history()
    for record in records:
        if record.get("timestamp") == timestamp:
            record["reasoning"] = reasoning
            break
    _rewrite_history(records)


class OpeningAnalysisService(QObject):
    progress = pyqtSignal(str)
    analysis_ready = pyqtSignal(dict)
    analysis_failed = pyqtSignal(str)

    def run_async(self):
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            self.progress.emit("抓取財經日曆中…")
            calendar_events = economic_calendar_client.get_upcoming_events()

            self.progress.emit("抓取台股加權指數近期價格中…")
            price_history = finmind_client.get_taiex_price_history()

            self.progress.emit("抓取選擇權波動率指數中…")
            vix_history = taifex_vix_client.get_option_vix_history()

            self.progress.emit("抓取選擇權籌碼分布中…")
            option_chain_summary = finmind_client.get_option_chain_summary()

            self.progress.emit("AI 分析中（含網路搜尋最新消息）…")
            judgment = openrouter_client.judge_quadrant(
                calendar_events, price_history, vix_history, option_chain_summary
            )

            strategy = STRATEGY_MAP[(judgment.amplitude, judgment.volatility)]

            record = {
                "timestamp": datetime.datetime.now().astimezone().isoformat(),
                "llm_model": os.environ.get("OPENROUTER_MODEL"),
                "calendar_events": calendar_events,
                "price_history": price_history,
                "vix_history": vix_history,
                "option_chain_summary": option_chain_summary,
                "amplitude": judgment.amplitude,
                "volatility": judgment.volatility,
                "resistance_levels": judgment.resistance_levels,
                "support_levels": judgment.support_levels,
                "reasoning": judgment.reasoning,
                "strategy": strategy,
            }
            append_history(record)
            self.analysis_ready.emit(record)
        except Exception as exc:  # noqa: BLE001 - 顯示給使用者看
            self.analysis_failed.emit(str(exc))
