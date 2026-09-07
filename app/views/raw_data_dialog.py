import json

from PyQt5.QtWidgets import QDialog, QVBoxLayout, QTabWidget, QTextEdit

TAB_LABELS = {
    "calendar_events": "財經日曆",
    "price_history": "大盤價格(TAIEX)",
    "vix_history": "選擇權波動率指數",
    "option_chain_summary": "選擇權籌碼分布",
}


class RawDataDialog(QDialog):
    """顯示某一筆歷史紀錄當初抓到的原始資料，每種資料一個分頁，方便回顧
    當時 AI 判斷的依據是什麼。舊紀錄可能沒有某些欄位 (功能是逐步加上去
    的)，只顯示紀錄裡實際有的欄位，不會因為缺欄位而出錯。"""

    def __init__(self, record: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"原始資料 - {record.get('timestamp', '')}")
        self.resize(700, 500)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        for key, label in TAB_LABELS.items():
            if key not in record:
                continue
            text_edit = QTextEdit()
            text_edit.setReadOnly(True)
            text_edit.setPlainText(json.dumps(record[key], ensure_ascii=False, indent=2))
            tabs.addTab(text_edit, label)

        if tabs.count() == 0:
            placeholder = QTextEdit()
            placeholder.setReadOnly(True)
            placeholder.setPlainText("這筆紀錄沒有保存原始資料（可能是較舊的紀錄）。")
            tabs.addTab(placeholder, "無資料")
