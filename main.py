import os
import sys

from dotenv import load_dotenv

from app.paths import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

from PyQt5.QtWidgets import QApplication, QDialog
from app.views.login_dialog import LoginDialog
from app.views.main_window import MainWindow


def main():
    app = QApplication(sys.argv)

    login = LoginDialog()
    if login.exec_() != QDialog.Accepted:
        return

    window = MainWindow(kgi_client=login.kgi_client)
    window.show()
    app.exec_()
    # RTD 用的 comtypes COM 物件在 Python 直譯器自己收尾(GC)時，跟原生
    # vtable 的釋放時機對不上會直接 segfault；closeEvent 裡該做的清理
    # (取消訂閱、ServerTerminate)都已經做完了，這裡跳過 Python 自己的
    # 收尾流程直接結束 process，避開這個已知問題。
    os._exit(0)


if __name__ == "__main__":
    main()
