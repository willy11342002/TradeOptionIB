"""
NiceGUI 版的主題設定，取代 `app/services/theme.py` 裡 Qt 專屬的那幾個函式
(`qss_for()`/`make_theme_toggle()`/`apply_titlebar_theme()`/
`set_titlebar_dark()`——QSS 語法跟 Windows DWM 標題列 API 都是 Qt 桌面版
專屬，網頁版沒有這些概念)。

深色/淺色偏好的持久化沿用 `theme.py` 既有的 `load_theme()`/`save_theme()`
(純 JSON 讀寫，不含任何 Qt 依賴)，不重寫一份。

*** NiceGUI 預設外觀偏樸素(Quasar 預設配色+間距)，這裡刻意套一組自訂配
色/字型當基礎主題 ***：不要讓每個新頁面各自長得不一樣、拖到最後才發現
整個「看起來沒設計過」。之後每個新頁面只要呼叫一次 `apply()` 就好。
"""
from nicegui import ui

from app.services.theme import load_theme, save_theme

_FONT_FAMILY = "'Inter', -apple-system, 'Segoe UI', sans-serif"

# 深色為主的配色：這是本機看盤/下單工具，長時間盯盤深色比較不刺眼。
# positive/negative 刻意用金融慣例的漲跌配色(綠漲紅跌，跟 main_window.py
# 報價表格既有的配色邏輯一致，不要在網頁版另外發明一套)。
_PRIMARY = "#3b82f6"     # 主色：偏亮的藍，按鈕/連結/選取狀態
_SECONDARY = "#64748b"   # 次要色：中性灰藍，次要按鈕/邊框
_ACCENT = "#8b5cf6"      # 強調色：紫，少量點綴用(標籤/焦點提示)
_POSITIVE = "#22c55e"    # 上漲/獲利
_NEGATIVE = "#ef4444"    # 下跌/虧損
_DARK = "#1a1d23"        # 卡片/表面色
_DARK_PAGE = "#111318"   # 頁面底色


def apply(dark: bool | None = None) -> None:
    """套用自訂配色/字型，`dark` 不給的話讀使用者上次存的偏好(沿用
    `theme.py` 的 JSON 檔，深淺色切換的記憶行為跟舊版 Qt app 一致)。每個
    頁面(`@ui.page` handler)開頭呼叫一次即可，NiceGUI 的 `ui.colors()`/
    `ui.dark_mode()` 是綁定「目前這個瀏覽器分頁」的，不是全域一次生效。"""
    if dark is None:
        dark = load_theme() == "dark"

    ui.add_head_html(
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" '
        'rel="stylesheet">'
    )
    ui.add_head_html(f"<style>body {{ font-family: {_FONT_FAMILY}; }}</style>")

    ui.colors(
        primary=_PRIMARY, secondary=_SECONDARY, accent=_ACCENT,
        positive=_POSITIVE, negative=_NEGATIVE, dark=_DARK, dark_page=_DARK_PAGE,
    )
    ui.dark_mode(dark)


def set_dark(dark: bool) -> None:
    """深色模式切換開關用——套用+存檔一次做完，呼叫端(頁面上的切換元件)
    不用自己記得兩件事都要做。"""
    ui.dark_mode(dark)
    save_theme("dark" if dark else "light")
