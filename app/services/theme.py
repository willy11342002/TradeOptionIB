"""
簡單的亮色/深色主題切換，用 QSS 套在整個 QApplication 上，登入視窗跟
主畫面會一起套用。偏好設定存在本機一個小 json 檔，下次開程式記得住。

注意：T 字報價表格裡的漲跌/漲跌停顏色 (main_window.py 裡用
item.setBackground()/setForeground() 逐格設定的) 不受這裡的 QSS 影響 ——
Qt 裡儲存格自己設定的顏色優先權高於樣式表，所以報價格子的白底紅字/
綠字/漲跌停配色在深色模式下還是照原樣顯示，只有其他介面元件(按鈕、
輸入框、標題列...)會變成深色。
"""
import ctypes
import json

from app.paths import PREF_DIR

PREF_FILE = PREF_DIR / "theme_pref.json"

DARK_QSS = """
QWidget { background-color: #2b2b2b; color: #e0e0e0; }
QLineEdit, QComboBox, QSpinBox {
    background-color: #3c3f41; color: #e0e0e0; border: 1px solid #555; padding: 2px;
}
QPushButton {
    background-color: #4a4d4f; color: #e0e0e0; border: 1px solid #666; padding: 4px 10px;
}
QPushButton:hover { background-color: #5a5d5f; }
QPushButton:pressed { background-color: #3a3d3f; }
QGroupBox { border: 1px solid #555; margin-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QTableWidget { background-color: #1e1e1e; gridline-color: #444; color: #e0e0e0; }
QHeaderView::section { background-color: #3c3f41; color: #e0e0e0; padding: 4px; border: 1px solid #555; }
QCheckBox, QLabel { color: #e0e0e0; }
QTabWidget::pane { border: 1px solid #555; }
QTabBar::tab {
    background-color: #3c3f41; color: #b0b0b0; padding: 6px 16px; border: 1px solid #555; border-bottom: none;
}
QTabBar::tab:selected { background-color: #2b2b2b; color: #ffffff; font-weight: bold; }
QTabBar::tab:hover { background-color: #5a5d5f; color: #ffffff; }
"""

LIGHT_QSS = ""  # 空字串 = 交給系統預設樣式


def load_theme() -> str:
    try:
        data = json.loads(PREF_FILE.read_text(encoding="utf-8"))
        theme = data.get("theme")
        return theme if theme in ("dark", "light") else "light"
    except Exception:
        return "light"


def save_theme(theme: str) -> None:
    try:
        PREF_FILE.write_text(json.dumps({"theme": theme}), encoding="utf-8")
    except Exception:
        pass  # 記憶偏好是加分項，存檔失敗不影響切換本身


def qss_for(theme: str) -> str:
    return DARK_QSS if theme == "dark" else LIGHT_QSS


def set_titlebar_dark(hwnd: int, enabled: bool) -> None:
    """Qt 的 QSS 管不到 Windows 原生標題列，要另外呼叫 DWM API。
    DWMWA_USE_IMMERSIVE_DARK_MODE 在新版 Windows 10/11 是 20，
    舊一點的 Windows 10 (2004 之前) 是 19，兩個都試一次。"""
    value = ctypes.c_int(1 if enabled else 0)
    for attribute in (20, 19):
        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), attribute, ctypes.byref(value), ctypes.sizeof(value)
        )
        if result == 0:  # S_OK
            return


def apply_titlebar_theme(widget) -> None:
    """依目前存的主題設定，把某個視窗的標題列調成對應深/淺色。"""
    try:
        set_titlebar_dark(int(widget.winId()), load_theme() == "dark")
    except Exception:
        pass  # 標題列調色是外觀加分項，失敗不影響程式其他功能


def make_theme_toggle(window, text: str = "深色模式"):
    """做一個「深色模式」核取方塊，掛上完整的套用/記憶/標題列調色邏輯，
    哪個視窗要放這個切換開關就呼叫一次，登入視窗跟主畫面共用同一套邏輯，
    不用各寫一份。"""
    from PyQt5.QtWidgets import QApplication, QCheckBox

    checkbox = QCheckBox(text)
    saved_theme = load_theme()
    checkbox.setChecked(saved_theme == "dark")
    QApplication.instance().setStyleSheet(qss_for(saved_theme))
    apply_titlebar_theme(window)

    def _on_toggled(checked: bool):
        chosen = "dark" if checked else "light"
        QApplication.instance().setStyleSheet(qss_for(chosen))
        save_theme(chosen)
        apply_titlebar_theme(window)

    checkbox.toggled.connect(_on_toggled)
    return checkbox
