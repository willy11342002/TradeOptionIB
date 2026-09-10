"""
Dock 版面配置存檔。QMainWindow.saveState()/saveGeometry() 回傳的是
QByteArray，不能直接塞進 json，用 base64 轉成字串再存成本機小 json 檔，
跟 theme.py 存主題偏好的作法一致。
"""
import base64
import json

from app.paths import PREF_DIR

LAYOUT_FILE = PREF_DIR / "layout_pref.json"


def _load_all() -> dict:
    try:
        data = json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))
        data.setdefault("layouts", {})
        return data
    except Exception:
        return {"layouts": {}, "last": None}


def _save_all(data: dict) -> None:
    try:
        LAYOUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # 存版面設定失敗不影響畫面本身，純粹記不住下次的配置


def list_layouts() -> list:
    return sorted(_load_all()["layouts"].keys())


def save_layout(name: str, geometry: bytes, state: bytes) -> None:
    data = _load_all()
    data["layouts"][name] = {
        "geometry": base64.b64encode(bytes(geometry)).decode("ascii"),
        "state": base64.b64encode(bytes(state)).decode("ascii"),
    }
    data["last"] = name
    _save_all(data)


def load_layout(name: str):
    entry = _load_all()["layouts"].get(name)
    if entry is None:
        return None
    return base64.b64decode(entry["geometry"]), base64.b64decode(entry["state"])


def delete_layout(name: str) -> None:
    data = _load_all()
    data["layouts"].pop(name, None)
    if data.get("last") == name:
        data["last"] = None
    _save_all(data)


def set_last_layout_name(name: str) -> None:
    data = _load_all()
    data["last"] = name
    _save_all(data)


def get_last_layout_name():
    return _load_all().get("last")
