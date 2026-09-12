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

# *** 模擬(paper)/正式(live) IB 環境各自獨立的交易資料，不要混在一起 ***
# 踩過的坑：拿群益時代沒有這個問題(登入時就選好環境、整個process只連一
# 個環境)，但IB這邊寫測試腳本時，就算只是連paper帳戶跑，只要用到的是
# 「真正的」OrderBookManager/AutoCloseManager(不是完全隔離的mock)，一樣
# 會把測試產生的委託/規則寫進本機檔案；如果之後連到live環境，這些
# paper測試留下的資料會被誤讀成好像是live帳戶的真實委託/規則。委託記錄
# (order_book_pref.json)、自動平倉規則(auto_close_pref.json)、手動分組
# (position_groups_pref.json)這三個「代表真實交易狀態」的檔案，改成依連
# 線環境分別存放在 pref/paper/ 或 pref/live/ 底下；主題/版面配置/查詢條
# 件這種純UI偏好不受影響，繼續放在 pref/ 底下共用。
# 預設(還沒呼叫 set_environment 之前)當成 paper——防呆用途：萬一有程式
# 路徑在真正連線建立、選好環境之前就去讀寫這幾個store，寧可誤存進paper
# 資料夾，也不要誤存進live。
_environment = {"simulation": True}


def set_environment(simulation: bool) -> None:
    """main.py 的 ConnectDialog 連線成功後呼叫一次，決定接下來這個
    process 的交易資料要存/讀 pref/paper/ 還是 pref/live/。"""
    _environment["simulation"] = simulation


def trading_pref_dir() -> Path:
    """委託紀錄/自動平倉規則/手動分組這幾個代表真實交易狀態的 store 要
    用這個，不要直接用 PREF_DIR。"""
    sub = "paper" if _environment["simulation"] else "live"
    d = PREF_DIR / sub
    d.mkdir(parents=True, exist_ok=True)
    return d
