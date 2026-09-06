"""
包裝 kgisuperpy 的登入。

即時報價現在完全由 RTD (rtd_client.py) 負責，跟這支檔案無關。這裡只管
登入，之後要做下單 (FutOrder/FutAccount) 會建立在登入後拿到的 self.api
物件上。
"""
import threading
from PyQt5.QtCore import QObject, pyqtSignal


class KgiClient(QObject):
    login_succeeded = pyqtSignal()
    login_failed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.api = None

    @property
    def is_logged_in(self) -> bool:
        return self.api is not None

    def login_async(self, person_id: str, person_pwd: str, simulation: bool):
        threading.Thread(
            target=self._login_worker,
            args=(person_id, person_pwd, simulation),
            daemon=True,
        ).start()

    def _login_worker(self, person_id: str, person_pwd: str, simulation: bool):
        try:
            import kgisuperpy as kgi
        except ImportError:
            self.login_failed.emit(
                "找不到 kgisuperpy 套件，請先執行 uv add kgisuperpy"
            )
            return
        try:
            self.api = kgi.login(person_id, person_pwd, simulation)
        except Exception as exc:  # noqa: BLE001 - 顯示給使用者看
            self.login_failed.emit(str(exc))
            return
        self.login_succeeded.emit()
