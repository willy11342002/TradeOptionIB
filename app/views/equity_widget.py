"""帳戶權益查詢，改接 IB 的 ib.accountSummaryAsync()。

跟舊版(群益 GetFutureRights/OnFutureRights)最大的差異：IB 的欄位
(tag)是英文、動態的一份清單(不是固定41欄的表)，而且第一次以後其實是
讀本地快取(ib_async 連線時就自動訂閱、持續在背景更新)，不需要像
GetFutureRights 那樣每次都非同步查詢、還要顧慮查詢間隔限制——這裡改成
單純「重新整理」=重新讀一次目前的本地快取，不是真的重新發送查詢。

*** 一定要用 accountSummaryAsync()，不能用同步版 accountSummary() ***：
這支 app 用 qasync 讓 Qt 事件迴圈本身就是 asyncio 迴圈(見 main.py)，同
步版內部的 loop.run_until_complete() 在這個架構下一定會撞上「這個事件
迴圈已經在跑了」，見 app/models/ib_client.py 開頭的說明。
"""
import asyncio

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from qasync import asyncSlot

from app.models.ib_client import IBClient

_COLUMNS = ["欄位", "數值", "幣別"]

# accountSummaryAsync() 固定查的欄位(見 ib_async ib.py
# reqAccountSummaryAsync 的 tags 字串)，IB 回傳的 tag 是英文代碼，這裡
# 對照成中文顯示——查不到對照的 tag(例如 IB 之後新增的欄位)就照原文顯
# 示，不會噴錯。$LEDGER:ALL 展開後每個幣別會多出 CashBalance/
# StockMarketValue 這幾個子欄位，一併列在下面。
_TAG_LABELS = {
    "AccountType": "帳戶類型",
    "NetLiquidation": "淨清算價值",
    "TotalCashValue": "現金總額",
    "SettledCash": "已交割現金",
    "AccruedCash": "應計現金",
    "BuyingPower": "購買力",
    "EquityWithLoanValue": "含融資額度的權益",
    "PreviousDayEquityWithLoanValue": "前一日含融資額度的權益",
    "GrossPositionValue": "部位總市值",
    "RegTEquity": "Reg T 權益",
    "RegTMargin": "Reg T 保證金",
    "SMA": "特別備忘錄帳戶(SMA)",
    "InitMarginReq": "原始保證金需求",
    "MaintMarginReq": "維持保證金需求",
    "AvailableFunds": "可用資金",
    "ExcessLiquidity": "剩餘流動性",
    "Cushion": "安全緩衝比例",
    "FullInitMarginReq": "完整原始保證金需求",
    "FullMaintMarginReq": "完整維持保證金需求",
    "FullAvailableFunds": "完整可用資金",
    "FullExcessLiquidity": "完整剩餘流動性",
    "LookAheadNextChange": "下次保證金規則變動時間",
    "LookAheadInitMarginReq": "預期原始保證金需求",
    "LookAheadMaintMarginReq": "預期維持保證金需求",
    "LookAheadAvailableFunds": "預期可用資金",
    "LookAheadExcessLiquidity": "預期剩餘流動性",
    "HighestSeverity": "最高風險等級",
    "DayTradesRemaining": "當日可用當沖次數",
    "DayTradesRemainingT+1": "T+1 可用當沖次數",
    "DayTradesRemainingT+2": "T+2 可用當沖次數",
    "DayTradesRemainingT+3": "T+3 可用當沖次數",
    "DayTradesRemainingT+4": "T+4 可用當沖次數",
    "Leverage": "槓桿倍數",
    "CashBalance": "現金餘額",
    "TotalCashBalance": "現金餘額合計",
    "StockMarketValue": "股票市值",
    "OptionMarketValue": "選擇權市值",
    "FutureOptionValue": "期貨選擇權市值",
    "FuturesPNL": "期貨損益",
    "NetLiquidationByCurrency": "各幣別淨清算價值",
    "UnrealizedPnL": "未實現損益",
    "RealizedPnL": "已實現損益",
    "ExchangeRate": "匯率",
    "AccruedDividend": "應計股利",
    "AccruedInterest": "應計利息",
}


class EquityWidget(QWidget):
    """權益查詢 dock 內容：一顆重新整理按鈕 + 欄位/數值表格，唯讀。"""

    def __init__(self, ib_client: IBClient, parent=None):
        super().__init__(parent)
        self._ib = ib_client.ib
        self._ib.accountSummaryEvent += self._on_account_summary_event

        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self.query_btn = QPushButton("重新整理")
        self.query_btn.clicked.connect(self._refresh)
        top_row.addWidget(self.query_btn)
        self.status_label = QLabel("尚未查詢")
        top_row.addWidget(self.status_label)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(self.table)

        asyncio.ensure_future(self._refresh())

    @asyncSlot()
    async def _refresh(self):
        rows = [av for av in await self._ib.accountSummaryAsync() if av.account != "All"]
        self.table.setRowCount(len(rows))
        for row, av in enumerate(rows):
            self._set_cell(row, 0, _TAG_LABELS.get(av.tag, av.tag))
            self._set_cell(row, 1, av.value)
            self._set_cell(row, 2, av.currency)
        self.status_label.setText(f"已更新 ({len(rows)} 筆)" if rows else "查無資料，確認是否已連線")

    def _set_cell(self, row: int, col: int, text: str):
        item = QTableWidgetItem(text)
        # 「數值」欄(col 1)靠右對齊、緊貼「幣別」欄——「幣別」欄(col 2)
        # 改靠左，多出來的 Stretch 空間留在文字右邊，不會把數字跟幣別中
        # 間撐開一大塊空白。
        if col == 0:
            alignment = Qt.AlignLeft | Qt.AlignVCenter
        else:
            alignment = Qt.AlignRight | Qt.AlignVCenter
        item.setTextAlignment(alignment)
        self.table.setItem(row, col, item)

    def _on_account_summary_event(self, value) -> None:
        # 帳戶權益有變動時 ib_async 會自動推播，這裡不用逐欄位更新，整批
        # 重新畫一次表格最簡單、也不會有欄位對不上的問題。這是
        # ib_async 自己的 Event callback(不是 Qt 訊號)，不能直接掛
        # @asyncSlot() 版的 _refresh，要自己排程。
        asyncio.ensure_future(self._refresh())
