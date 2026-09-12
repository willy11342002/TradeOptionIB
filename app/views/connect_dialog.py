from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QCheckBox,
    QPushButton, QLabel, QMessageBox,
)
from qasync import asyncSlot

from app import paths
from app.models.ib_client import IBClient
from app.services import ib_prefs
from app.services import theme


class ConnectDialog(QDialog):
    """IB Gateway 連線視窗。連線成功後 accept()，main.py 再接著開主畫面。

    跟舊版群益的 LoginDialog 不一樣：IB 沒有 API 層級的帳密登入，Gateway
    本身要先手動開好、登入好，這裡單純是拿 host/port/clientId 去 socket
    連線，不需要等帳號回報事件、也不需要選帳號(帳號是 Gateway 登入時就
    決定好的)。用的是 Gateway 不是 TWS，模擬/正式環境預設 port 不同
    (4002/4001)，勾選框只在 port 欄位目前是空的或還是另一個環境的預設
    值時才自動帶入對應 port，不會蓋掉手動輸入的自訂 port(例如透過 SSH
    tunnel 轉發的情境)。
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("IB Gateway 連線")
        self.setMinimumWidth(320)

        self.ib_client = IBClient()
        self.ib_client.error.connect(self._on_error)

        self._build_ui()
        self._load_saved()

    def _build_ui(self):
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("127.0.0.1")

        self.sim_checkbox = QCheckBox("模擬環境 (勾選=Paper，不勾=正式環境Live)")
        self.sim_checkbox.toggled.connect(self._on_sim_toggled)

        self.port_edit = QLineEdit()
        self.port_edit.setPlaceholderText(str(ib_prefs.DEFAULT_PORT_PAPER))

        self.client_id_edit = QLineEdit()
        self.client_id_edit.setPlaceholderText("1")

        self.dark_mode_checkbox = theme.make_theme_toggle(self)

        self.connect_btn = QPushButton("連線")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        self.connect_btn.setDefault(True)

        self.status_label = QLabel("")

        form = QFormLayout()
        form.addRow("Host", self.host_edit)
        form.addRow("", self.sim_checkbox)
        form.addRow("Port", self.port_edit)
        form.addRow("Client ID", self.client_id_edit)
        form.addRow("", self.dark_mode_checkbox)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.connect_btn)
        layout.addWidget(self.status_label)

    def _on_sim_toggled(self, checked: bool):
        current = self.port_edit.text().strip()
        known_ports = {str(ib_prefs.DEFAULT_PORT_PAPER), str(ib_prefs.DEFAULT_PORT_LIVE)}
        if current == "" or current in known_ports:
            default_port = ib_prefs.DEFAULT_PORT_PAPER if checked else ib_prefs.DEFAULT_PORT_LIVE
            self.port_edit.setText(str(default_port))

    def _load_saved(self):
        host, port, client_id, simulation = ib_prefs.load()
        self.host_edit.setText(host)
        self.port_edit.setText(str(port))
        self.client_id_edit.setText(str(client_id))
        self.sim_checkbox.setChecked(simulation)

    @asyncSlot()
    async def _on_connect_clicked(self):
        # *** @asyncSlot() 讓這個 Qt 訊號 handler 可以直接是 async def
        # ***：qasync 會把它排成一個 task，跟其他 Qt 事件一起被同一個
        # 事件迴圈處理，不需要自己手動 asyncio.ensure_future()。
        host = self.host_edit.text().strip() or ib_prefs.DEFAULT_HOST
        try:
            port = int(self.port_edit.text().strip())
            client_id = int(self.client_id_edit.text().strip())
        except ValueError:
            QMessageBox.warning(self, "提醒", "Port / Client ID 要是整數")
            return

        self.connect_btn.setEnabled(False)
        self.status_label.setText("連線中，請稍候...")

        ok = await self.ib_client.connect_async(host, port, client_id)
        if ok:
            simulation = self.sim_checkbox.isChecked()
            ib_prefs.save(host, port, client_id, simulation)
            # 一定要在 MainWindow 建構(main.py 接下來會做)之前設定好，
            # 委託紀錄/自動平倉規則/手動分組這幾個 store 才會讀寫到對的
            # 環境資料夾，不會把 paper/live 的資料混在一起(見
            # app/paths.py::trading_pref_dir() 的說明)。
            paths.set_environment(simulation)
            self.status_label.setText("連線成功")
            # *** 這個對話框的任務到這裡結束，之後 MainWindow 會接手同
            # 一個 IBClient 實例繼續用——一定要解除這裡的 error 訊號連
            # 結，不然這個對話框(跟它的 QMessageBox.critical)會活在整
            # 個程式生命週期裡，之後任何一次報價訂閱的錯誤(即使已經被
            # IBClient 過濾成資訊性訊息之外的其他錯誤)都還會跳出「連線
            # 失敗」對話框，跟連線本身早就成功這件事完全不符。***
            self.ib_client.error.disconnect(self._on_error)
            self.accept()
        else:
            self.connect_btn.setEnabled(True)
            self.status_label.setText("連線失敗，檢查 IB Gateway 是否開著、port 對不對")

    def _on_error(self, req_id: int, error_code: int, message: str):
        self.connect_btn.setEnabled(True)
        self.status_label.setText(f"連線錯誤: {message}")
        QMessageBox.critical(self, "連線失敗", f"({error_code}) {message}")
