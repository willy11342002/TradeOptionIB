from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QTextEdit

# setMarkdown() 轉出來的內建樣式沒什麼顏色層次，這裡疊一層 QTextDocument
# 的 default stylesheet 讓標題/粗體/程式碼有顏色，深色模式跟淺色模式各一套。
LIGHT_MARKDOWN_CSS = """
h1, h2, h3 { color: #1a5fb4; }
strong { color: #c01c28; }
code { background-color: #eeeeee; padding: 1px 4px; border-radius: 3px; }
"""
DARK_MARKDOWN_CSS = """
h1, h2, h3 { color: #62a0ea; }
strong { color: #ff7b63; }
code { background-color: #3c3f41; padding: 1px 4px; border-radius: 3px; }
"""

DEFAULT_FONT_POINT_SIZE = 13


class MarkdownTextEdit(QTextEdit):
    """會把內容當 Markdown 渲染的 QTextEdit，並支援 Ctrl+滾輪縮放字體
    (跟瀏覽器/編輯器一樣的慣例，滑鼠單純滾動還是正常捲動內容，不會跟縮放
    衝突)。"""

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoomIn(1)
            elif event.angleDelta().y() < 0:
                self.zoomOut(1)
            event.accept()
        else:
            super().wheelEvent(event)


def apply_markdown_style(text_edit: MarkdownTextEdit, dark: bool) -> None:
    font = text_edit.font()
    font.setPointSize(DEFAULT_FONT_POINT_SIZE)
    text_edit.setFont(font)
    text_edit.document().setDefaultStyleSheet(DARK_MARKDOWN_CSS if dark else LIGHT_MARKDOWN_CSS)
