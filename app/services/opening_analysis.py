"""
「開倉」分頁的分析流程協調者。OpeningAnalysisService 背景執行緒依序打
財經日曆/大盤價格/選擇權波動率指數/選擇權籌碼分布四個外部資料源，交給
LLM 質化判斷振幅跟波動率高低，再用固定規則 (STRATEGY_MAP) 對應出策略，
最後把判斷結果連同抓到的原始資料一起存進本機 JSONL 歷史檔 (方便之後在
UI 上點開回顧當時的原始資料)。ChartDataService 是另一個獨立的服務，只
負責抓主畫面圖表要畫的價格資料，跟分析流程無關。

用 QObject + threading.Thread + pyqtSignal 模式，確保網路 I/O 不會卡住
PyQt 主執行緒。
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


def update_history_record(timestamp: str, updates: dict) -> None:
    """使用者在分析詳細視窗裡手動修改理由文字/壓力支撐價位後，寫回歷史檔。
    updates 是要合併進該筆紀錄的欄位 (例如 {"reasoning": ..., "resistance_levels": [...], "support_levels": [...]})。"""
    records = load_history()
    for record in records:
        if record.get("timestamp") == timestamp:
            record.update(updates)
            break
    _rewrite_history(records)


class ChartDataService(QObject):
    """獨立於分析流程之外，單純抓圖表要畫的加權指數 K 棒 + 台指期收盤價，
    跟「分析」按鈕的四資料源+LLM流程無關，分開一個服務避免混在一起。"""

    chart_ready = pyqtSignal(list, list)  # (taiex_history, futures_history)
    chart_failed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._running = False

    def run_async(self, days: int = 60):
        if self._running:
            return  # 上一次還在跑，不重複開執行緒 (CSV 快取有鎖擋著不會壞，但沒必要重工)
        self._running = True
        threading.Thread(target=self._worker, args=(days,), daemon=True).start()

    def _worker(self, days: int):
        try:
            taiex_history = finmind_client.get_taiex_price_history(days=days)
            futures_history = finmind_client.get_futures_price_history(days=days)
            self.chart_ready.emit(taiex_history, futures_history)
        except Exception as exc:  # noqa: BLE001 - 顯示給使用者看
            self.chart_failed.emit(str(exc))
        finally:
            self._running = False


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
