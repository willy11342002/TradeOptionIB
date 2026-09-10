"""
專案根目錄路徑，給需要在固定位置讀寫本機檔案的模組用
(帳密快取、主題偏好設定這類執行期狀態檔)，不要用 Path(__file__).parent
直接算，不然檔案模組搬家(這次從根目錄搬進 app/ 底下)存檔位置就會跟著
變，舊的快取檔案會變成孤兒、抓不到。

PREF_DIR：所有「記憶狀態」的本機檔案(帳密快取/版面配置/查詢條件等)統一
放這裡，不要散在專案根目錄——根目錄之前一路累積了 6、7 個沒有前綴規則
的點檔案，肉眼看不出哪些是設定檔、哪些是原始碼。只有 .env 例外，維持
放在根目錄(python-dotenv 等工具預設就是找根目錄的 .env，換位置沒有實
際好處，只會增加設定成本)。
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PREF_DIR = PROJECT_ROOT / "pref"
PREF_DIR.mkdir(exist_ok=True)
