"""
專案根目錄路徑，給需要在固定位置讀寫本機檔案的模組用
(帳密快取、主題偏好設定這類執行期狀態檔)，不要用 Path(__file__).parent
直接算，不然檔案模組搬家(這次從根目錄搬進 app/ 底下)存檔位置就會跟著
變，舊的快取檔案會變成孤兒、抓不到。
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
