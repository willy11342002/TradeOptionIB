"""
NiceGUI 版的主題設定——固定深色，沒有淺色模式/切換開關：這是本機看盤/
下單工具，長時間盯盤深色比較不刺眼，畫面上也從來沒有一顆能切換的按
鈕，之前留著的深/淺色偏好持久化(`app/services/theme.py`)只是死碼，一
併刪掉。

*** NiceGUI 預設外觀偏樸素(Quasar 預設配色+間距)，這裡刻意套一組自訂配
色/字型當基礎主題 ***：不要讓每個新頁面各自長得不一樣、拖到最後才發現
整個「看起來沒設計過」。之後每個新頁面只要呼叫一次 `apply()` 就好。
"""
from nicegui import ui

_FONT_FAMILY = "'Inter', -apple-system, 'Segoe UI', sans-serif"

# positive/negative 刻意用金融慣例的漲跌配色(綠漲紅跌)，跟報價表格/損益
# 圖既有的配色邏輯一致，不在網頁版另外發明一套。
_PRIMARY = "#3b82f6"     # 主色：偏亮的藍，按鈕/連結/選取狀態
_SECONDARY = "#64748b"   # 次要色：中性灰藍，次要按鈕/邊框
_ACCENT = "#8b5cf6"      # 強調色：紫，少量點綴用(標籤/焦點提示)
_POSITIVE = "#22c55e"    # 上漲/獲利
_NEGATIVE = "#ef4444"    # 下跌/虧損
_DARK = "#1a1d23"        # 卡片/表面色
_DARK_PAGE = "#111318"   # 頁面底色


def apply() -> None:
    """套用自訂配色/字型+固定深色模式。每個頁面(`@ui.page` handler)開頭
    呼叫一次即可，NiceGUI 的 `ui.colors()`/`ui.dark_mode()` 是綁定「目前
    這個瀏覽器分頁」的，不是全域一次生效。"""
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
    ui.dark_mode(True)
