from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QCheckBox,
    QPushButton, QLabel, QMessageBox, QApplication,
)

from kgi_client import KgiClient
import credential_store
import theme


class LoginDialog(QDialog):
    """獨立的凱基帳號登入視窗。登入成功後 accept()，main.py 再接著開主畫面。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("凱基帳號登入")
        self.setMinimumWidth(320)

        self.kgi_client = KgiClient()
        self.kgi_client.login_succeeded.connect(self._on_login_succeeded)
        self.kgi_client.login_failed.connect(self._on_login_failed)

        self._build_ui()
        self._load_saved_credentials()
        self._load_saved_theme()

    def _build_ui(self):
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("身分證字號")
        self.id_edit.setEchoMode(QLineEdit.Password)

        self.pwd_edit = QLineEdit()
        self.pwd_edit.setPlaceholderText("密碼")
        self.pwd_edit.setEchoMode(QLineEdit.Password)

        self.sim_checkbox = QCheckBox("模擬環境 (勾選=測試環境，不勾=正式環境)")

        self.show_checkbox = QCheckBox("顯示帳密")
        self.show_checkbox.toggled.connect(self._on_show_toggled)

        self.dark_mode_checkbox = QCheckBox("深色模式")
        self.dark_mode_checkbox.toggled.connect(self._on_dark_mode_toggled)

        self.login_btn = QPushButton("登入")
        self.login_btn.clicked.connect(self._on_login_clicked)
        self.login_btn.setDefault(True)

        self.status_label = QLabel("")

        form = QFormLayout()
        form.addRow("身分證字號", self.id_edit)
        form.addRow("密碼", self.pwd_edit)
        form.addRow("", self.sim_checkbox)
        form.addRow("", self.show_checkbox)
        form.addRow("", self.dark_mode_checkbox)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.login_btn)
        layout.addWidget(self.status_label)

    def _on_show_toggled(self, checked: bool):
        mode = QLineEdit.Normal if checked else QLineEdit.Password
        self.id_edit.setEchoMode(mode)
        self.pwd_edit.setEchoMode(mode)

    def _load_saved_theme(self):
        saved_theme = theme.load_theme()
        self.dark_mode_checkbox.setChecked(saved_theme == "dark")
        QApplication.instance().setStyleSheet(theme.qss_for(saved_theme))
        theme.apply_titlebar_theme(self)

    def _on_dark_mode_toggled(self, checked: bool):
        chosen = "dark" if checked else "light"
        QApplication.instance().setStyleSheet(theme.qss_for(chosen))
        theme.save_theme(chosen)
        theme.apply_titlebar_theme(self)

    def _load_saved_credentials(self):
        saved = credential_store.load_credentials()
        if saved is None:
            return
        person_id, person_pwd, simulation = saved
        self.id_edit.setText(person_id)
        self.pwd_edit.setText(person_pwd)
        self.sim_checkbox.setChecked(simulation)

    def _on_login_clicked(self):
        person_id = self.id_edit.text().strip()
        person_pwd = self.pwd_edit.text()
        if not person_id or not person_pwd:
            QMessageBox.warning(self, "提醒", "請輸入身分證字號與密碼")
            return
        self.login_btn.setEnabled(False)
        self.status_label.setText("登入中，請稍候...")
        self.kgi_client.login_async(person_id, person_pwd, self.sim_checkbox.isChecked())

    def _on_login_succeeded(self):
        credential_store.save_credentials(
            self.id_edit.text().strip(),
            self.pwd_edit.text(),
            self.sim_checkbox.isChecked(),
        )
        self.accept()

    def _on_login_failed(self, message: str):
        self.login_btn.setEnabled(True)
        self.status_label.setText(f"登入失敗: {message}")
        QMessageBox.critical(self, "登入失敗", message)
