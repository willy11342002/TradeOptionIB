"""
IB Gateway 連線設定(host/port/clientId/模擬環境)記憶，重開程式自動套用。

跟 credential_store.py 不一樣：這裡沒有帳密，只有連線位置資訊，不是機
密，不需要 DPAPI 加解密，純 json 就夠，寫法跟 query_pref.py/layout_store.py
一致。

用的是 IB Gateway，不是 TWS(跟 test.ipynb 一致)，模擬/正式環境預設 port
不一樣(4002/4001)，這裡存 port 本身(不是只存環境旗標再推算)，因為使用
者可能透過 SSH tunnel 之類的方式轉發到自訂 port，不能每次都用環境推算
值覆蓋掉手動改過的設定——`simulation` 欄位單純只是記住上次視窗上勾選框
的狀態，方便重開程式時預設勾對環境，不是 port 的唯一真相來源。
"""
import json

from app.paths import PREF_DIR

IB_PREF_FILE = PREF_DIR / "ib_connection_pref.json"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT_PAPER = 4002  # IB Gateway 模擬帳戶(paper)
DEFAULT_PORT_LIVE = 4001   # IB Gateway 正式帳戶(live)
DEFAULT_CLIENT_ID = 1
DEFAULT_SIMULATION = True  # 預設模擬環境，比較安全


def save(host: str, port: int, client_id: int, simulation: bool) -> None:
    try:
        IB_PREF_FILE.write_text(
            json.dumps(
                {"host": host, "port": port, "client_id": client_id, "simulation": simulation},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass  # 記不住下次的連線設定不影響本次連線，跟其他 pref store 一致靜默吞掉


def load():
    """回傳 (host, port, client_id, simulation)，沒存過/讀失敗就回傳預設值。"""
    try:
        data = json.loads(IB_PREF_FILE.read_text(encoding="utf-8"))
        simulation = bool(data.get("simulation", DEFAULT_SIMULATION))
        default_port = DEFAULT_PORT_PAPER if simulation else DEFAULT_PORT_LIVE
        return (
            data.get("host", DEFAULT_HOST),
            int(data.get("port", default_port)),
            int(data.get("client_id", DEFAULT_CLIENT_ID)),
            simulation,
        )
    except Exception:
        return DEFAULT_HOST, DEFAULT_PORT_PAPER, DEFAULT_CLIENT_ID, DEFAULT_SIMULATION
