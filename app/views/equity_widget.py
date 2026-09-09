"""期貨/選擇權帳戶權益查詢 (GetFutureRights/OnFutureRights)。

期貨、選擇權在群益共用同一個期貨帳戶，這支查詢回傳的權益數已經合併選擇
權部位 (未沖銷買方/賣方市值等)，不需要另外查一支「選擇權權益」。

欄位定義/幣別參數對照 app/models/capital_order_client.py 的
FUTURE_RIGHTS_FIELDS/FUTURE_RIGHTS_LABELS 註解，核對自官方文件
《策略王COM元件使用說明_V2.13.59.docx》4-2-38 GetFutureRights /
4-2-i OnFutureRights，不是猜的。
"""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.models.capital_order_client import (
    CapitalOrderClient, FUTURE_RIGHTS_LABELS, FUTURE_RIGHTS_FIELDS,
)

# 文件沒有給明確的查詢間隔秒數 (只寫「不要連續呼叫，太頻繁會回
# SK_ERROR_QUERY_IN_PROCESSING」)，這裡用按鈕冷卻時間自保，避免使用者連
# 點；秒數是保守估計，不是文件白紙黑字的規定。
_QUERY_COOLDOWN_MS = 3000


class EquityWidget(QWidget):
    """權益查詢 dock 內容：一顆查詢按鈕 + 欄位/數值表格，唯讀。"""

    def __init__(self, order_client: CapitalOrderClient, parent=None):
        super().__init__(parent)
        self._order_client = order_client
        self._order_client.future_rights.connect(self._on_future_rights)
        self._order_client.future_rights_failed.connect(self._on_failed)

        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self.query_btn = QPushButton("查詢/更新權益")
        self.query_btn.clicked.connect(self._on_query_clicked)
        top_row.addWidget(self.query_btn)
        self.status_label = QLabel("尚未查詢")
        top_row.addWidget(self.status_label)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        self.table = QTableWidget(len(FUTURE_RIGHTS_LABELS), 2)
        self.table.setHorizontalHeaderLabels(["欄位", "數值"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for row, label in enumerate(FUTURE_RIGHTS_LABELS):
            label_item = QTableWidgetItem(label)
            label_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.table.setItem(row, 0, label_item)
            value_item = QTableWidgetItem("")
            value_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, 1, value_item)
        layout.addWidget(self.table)

        # 開窗當下先讓畫面畫出來，下一輪事件圈才送出查詢 (SendOptionOrder
        # 那邊踩過在建構子裡直接打阻塞式/COM 呼叫卡住畫面的坑，這裡沿用
        # 同樣的作法)。
        QTimer.singleShot(0, self._on_query_clicked)

    def _on_query_clicked(self):
        self.query_btn.setEnabled(False)
        QTimer.singleShot(_QUERY_COOLDOWN_MS, lambda: self.query_btn.setEnabled(True))
        self.status_label.setText("查詢中...")
        try:
            self._order_client.query_future_rights()
        except RuntimeError as exc:
            # 還沒登入完成時 _require_login 會丟例外，開窗當下的自動查詢
            # 常常還沒登入完成，安靜顯示狀態就好，不要跳錯誤訊息框。
            self.status_label.setText(str(exc))

    def _on_failed(self, message: str):
        self.status_label.setText(f"查詢失敗：{message}")

    def _on_future_rights(self, rights: dict):
        for row, key in enumerate(FUTURE_RIGHTS_FIELDS):
            self.table.item(row, 1).setText(rights.get(key, ""))
        self.status_label.setText("已更新")
