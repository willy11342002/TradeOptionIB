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
from typing import Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from app.models import economic_calendar_client, finmind_client, openrouter_client, taifex_vix_client
from app.models.capital_kline_client import CapitalKLineClient
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
    """獨立於分析流程之外，單純抓圖表要畫的加權指數K棒+台指期K棒，跟
    「分析」按鈕的四資料源+LLM流程無關 (那邊的選擇權籌碼/VIX/財經日曆沒有
    群益對應的資料源，繼續走 finmind_client，不受這裡影響)。

    改用群益 CapitalKLineClient(SKQuoteLib_RequestKLineAMByDate) 查歷史K棒
    取代原本的 FinMind：可以查到分/日/週/月線、日盤或全盤(含夜盤)，不用
    自己維護本機 CSV 增量快取。這支API本身是「呼叫一次->事件陸續回傳」的
    非同步模式(comtypes COM事件)，不是阻塞式I/O，所以不需要再開背景執行緒。"""

    chart_ready = pyqtSignal(list, list)  # (primary_history, overlay_history)
    chart_failed = pyqtSignal(str)
    chart_waiting = pyqtSignal()  # 上一批查詢還沒收完，正在等，不算失敗

    PRIMARY_SYMBOL = "TSEA"  # 加權指數
    OVERLAY_SYMBOL = "TX00"  # 台指期近月合約
    TAG = "chart"  # 跟 kline_client 上其他呼叫端(例如背景的日K重試確認)區分
    _RETRY_MS = 300

    def __init__(self, kline_client: CapitalKLineClient):
        super().__init__()
        self._kline_client = kline_client
        self._kline_client.kline_ready.connect(self._on_kline_ready)
        self._kline_client.kline_failed.connect(self._on_kline_failed)
        self._pending: set = set()
        self._results: dict = {}
        # run_async() 呼叫時如果上一批(不管是自己前一次呼叫、還是背景重試
        # 確認剛好撞在一起)還沒收完，不能立刻對同一代碼再發一次(會被
        # kline_client 拒絕)，先記住「使用者現在真正想要的參數」，等
        # is_pending 全部清空了再真的送出——這樣使用者連續切換下拉選單時，
        # 只有最後一次選的會真的送出去，不會每切一次就噴一次失敗訊息。
        self._desired: Optional[tuple] = None
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._try_fire)

    def is_pending(self, symbol: str) -> bool:
        return symbol in self._pending

    def run_async(self, days: int, kline_type: int, trade_session: int, minute_number: int = 1):
        """kline_type: capital_kline_client.KLINE_TYPE_*；trade_session:
        SESSION_FULL(全盤,含夜盤) 或 SESSION_AM(僅日盤)；minute_number 只在
        kline_type=KLINE_TYPE_MINUTE 時有意義 (1/5/30分等)。"""
        self._desired = (days, kline_type, trade_session, minute_number)
        self._try_fire()

    def _try_fire(self):
        if self._desired is None:
            return
        if self._kline_client.is_pending(self.PRIMARY_SYMBOL) or self._kline_client.is_pending(self.OVERLAY_SYMBOL):
            self.chart_waiting.emit()
            self._retry_timer.start(self._RETRY_MS)
            return

        days, kline_type, trade_session, minute_number = self._desired
        self._desired = None
        self._results = {}
        self._pending = {self.PRIMARY_SYMBOL, self.OVERLAY_SYMBOL}
        end = datetime.date.today()
        start = end - datetime.timedelta(days=days)
        self._kline_client.request_range(self.TAG, self.PRIMARY_SYMBOL, kline_type, trade_session, start, end, minute_number)
        self._kline_client.request_range(self.TAG, self.OVERLAY_SYMBOL, kline_type, trade_session, start, end, minute_number)

    def _on_kline_ready(self, tag: str, symbol: str, bars: list):
        if tag != self.TAG or symbol not in self._pending:
            return  # 不是這一輪查詢等的代碼(可能是別的呼叫端、或上一輪的殘留結果)，忽略
        self._results[symbol] = bars
        self._pending.discard(symbol)
        if not self._pending:
            if self._desired is None:
                self.chart_ready.emit(
                    self._results.get(self.PRIMARY_SYMBOL, []),
                    self._results.get(self.OVERLAY_SYMBOL, []),
                )
            # self._desired 不是 None 代表使用者在這批查詢還沒收完時又選了
            # 別的週期/盤別——這批已經過時了，不用畫出來，讓下面 _try_fire()
            # 直接送出使用者真正想要的那次，畫面才不會先閃一下舊的再跳到新的。
        self._try_fire()

    def _on_kline_failed(self, tag: str, symbol: str, message: str):
        if tag != self.TAG or symbol not in self._pending:
            return
        self._pending.discard(symbol)
        self.chart_failed.emit(f"{symbol}: {message}")
        self._try_fire()


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
