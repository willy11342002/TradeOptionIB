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
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        asyncio.ensure_future(self._refresh())

    @asyncSlot()
    async def _refresh(self):
        rows = [av for av in await self._ib.accountSummaryAsync() if av.account != "All"]
        self.table.setRowCount(len(rows))
        for row, av in enumerate(rows):
            self._set_cell(row, 0, av.tag)
            self._set_cell(row, 1, av.value)
            self._set_cell(row, 2, av.currency)
        self.status_label.setText(f"已更新 ({len(rows)} 筆)" if rows else "查無資料，確認是否已連線")

    def _set_cell(self, row: int, col: int, text: str):
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter if col != 0 else Qt.AlignLeft | Qt.AlignVCenter)
        self.table.setItem(row, col, item)

    def _on_account_summary_event(self, value) -> None:
        # 帳戶權益有變動時 ib_async 會自動推播，這裡不用逐欄位更新，整批
        # 重新畫一次表格最簡單、也不會有欄位對不上的問題。這是
        # ib_async 自己的 Event callback(不是 Qt 訊號)，不能直接掛
        # @asyncSlot() 版的 _refresh，要自己排程。
        asyncio.ensure_future(self._refresh())
