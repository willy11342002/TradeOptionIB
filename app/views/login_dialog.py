from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QCheckBox, QComboBox,
    QPushButton, QLabel, QMessageBox,
)

from app.models.capital_client import CapitalClient
from app.services import credential_store
from app.services import theme


class LoginDialog(QDialog):
    """群益帳號登入視窗。登入成功後 accept()，main.py 再接著開主畫面。

    群益的登入比較特別：SKCenterLib_Login 是同步呼叫 (跟凱基的 async login
    不一樣，這裡故意不開背景執行緒——SKCOM 是 COM 物件，用單執行緒公寓模
    型，跨執行緒呼叫容易出問題，官方範例也是直接在按鈕事件裡同步呼叫)，
    但可用交易帳號是透過 OnAccount 事件非同步回報的，所以登入後還要多等
    一下、選帳號，這兩步都在同一個視窗裡完成。
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("群益帳號登入")
        self.setMinimumWidth(320)

        self.capital_client = CapitalClient()
        self.capital_client.login_failed.connect(self._on_login_failed)
        self.capital_client.accounts_ready.connect(self._on_accounts_ready)

        self._build_ui()
        self._load_saved_credentials()

    def _build_ui(self):
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("身分證字號 / 統編")
        self.id_edit.setEchoMode(QLineEdit.Password)

        self.pwd_edit = QLineEdit()
        self.pwd_edit.setPlaceholderText("密碼")
        self.pwd_edit.setEchoMode(QLineEdit.Password)

        self.sim_checkbox = QCheckBox("測試環境 (勾選=測試環境，不勾=正式環境)")

        self.show_checkbox = QCheckBox("顯示帳密")
        self.show_checkbox.toggled.connect(self._on_show_toggled)

        self.dark_mode_checkbox = theme.make_theme_toggle(self)

        self.login_btn = QPushButton("登入")
        self.login_btn.clicked.connect(self._on_login_clicked)
        self.login_btn.setDefault(True)

        self.account_combo = QComboBox()
        self.account_combo.setEnabled(False)
        self.account_combo.setVisible(False)

        self.confirm_account_btn = QPushButton("確定")
        self.confirm_account_btn.clicked.connect(self._on_confirm_account_clicked)
        self.confirm_account_btn.setVisible(False)

        self.status_label = QLabel("")

        form = QFormLayout()
        form.addRow("帳號", self.id_edit)
        form.addRow("密碼", self.pwd_edit)
        form.addRow("", self.sim_checkbox)
        form.addRow("", self.show_checkbox)
        form.addRow("", self.dark_mode_checkbox)
        form.addRow("交易帳號", self.account_combo)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.login_btn)
        layout.addWidget(self.confirm_account_btn)
        layout.addWidget(self.status_label)

    def _on_show_toggled(self, checked: bool):
        mode = QLineEdit.Normal if checked else QLineEdit.Password
        self.id_edit.setEchoMode(mode)
        self.pwd_edit.setEchoMode(mode)

    def _load_saved_credentials(self):
        saved = credential_store.load_credentials()
        if saved is None:
            return
        person_id, person_pwd, simulation = saved
        self.id_edit.setText(person_id)
        self.pwd_edit.setText(person_pwd)
        self.sim_checkbox.setChecked(simulation)

    def _on_login_clicked(self):
        user_id = self.id_edit.text().strip()
        password = self.pwd_edit.text()
        if not user_id or not password:
            QMessageBox.warning(self, "提醒", "請輸入帳號與密碼")
            return
        self.login_btn.setEnabled(False)
        self.status_label.setText("登入中，請稍候...")

        ok = self.capital_client.login(user_id, password, self.sim_checkbox.isChecked())
        if ok:
            credential_store.save_credentials(user_id, password, self.sim_checkbox.isChecked())
            self.status_label.setText("登入成功，等待交易帳號回報...")
        else:
            self.login_btn.setEnabled(True)

    def _on_accounts_ready(self, accounts):
        self.account_combo.clear()
        for acc in accounts:
            # 顯示市場別+完整帳號+原始字串，帳號選錯/組錯的話一眼就看得
            # 出來，不用再回頭加 log 才能查。
            display = f"[{acc['market']}] {acc['full_account']}  ({acc['raw']})"
            self.account_combo.addItem(display, acc["full_account"])
        self.account_combo.setEnabled(True)
        self.account_combo.setVisible(True)
        self.confirm_account_btn.setVisible(True)
        self.status_label.setText(f"收到 {len(accounts)} 個交易帳號，請選擇後按確定")
        if len(accounts) == 1:
            self._on_confirm_account_clicked()

    def _on_confirm_account_clicked(self):
        account = self.account_combo.currentData()
        if not account:
            QMessageBox.warning(self, "提醒", "請選擇交易帳號")
            return
        self.capital_client.select_account(account)
        self.accept()

    def _on_login_failed(self, message: str):
        self.login_btn.setEnabled(True)
        self.status_label.setText(f"登入失敗: {message}")
        QMessageBox.critical(self, "登入失敗", message)
