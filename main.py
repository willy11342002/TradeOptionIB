import sys
from PyQt5.QtWidgets import QApplication, QDialog
from login_dialog import LoginDialog
from main_window import MainWindow


def main():
    app = QApplication(sys.argv)

    login = LoginDialog()
    if login.exec_() != QDialog.Accepted:
        sys.exit(0)

    window = MainWindow(kgi_client=login.kgi_client)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
