"""
選擇權報價視窗裡的「技術分析」頁籤——顯示目前查詢中標的的K線圖(蠟燭
圖)，可切換 1分K/5分K/30分K/日線/週線/月線，並支援使用者自己畫直線/斜
線。畫面/操作行為對照舊版 Qt `pyqtgraph` 圖表(`candlestick_chart.py`，
隨群益 API 一起刪除，已經不在專案裡)的設計：Y軸不能用滑鼠/滾輪縮放，
可視範圍內的最高最低價由程式自動算好、動態塞滿Y軸，使用者只能縮放/拖
曳X軸。

畫圖用 `ui.plotly`(NiceGUI 內建元件，直接傳一個 dict figure 就能動，不
需要額外安裝 `plotly` 這個 pip 套件——`ui.plotly` 只有在收到
`plotly.graph_objects.Figure` 物件時才會用到那個套件，純 dict 走另一條
路徑，見 `nicegui/elements/plotly/plotly.py::_get_figure_json()`)取代
Qt 版的 pyqtgraph：K線用 Plotly 內建的 `candlestick` trace 型別；畫線
工具用 Plotly 內建的 `drawline`/`eraseshape` modebar 按鈕(拖曳畫線
Plotly 原生就有，不用像 pyqtgraph 版那樣自己接滑鼠事件重刻一遍)。

畫線持久化用 `app/services/chart_drawing_store.py`，用標的代碼(不分時
間週期)存一份 Plotly shape 清單——故意不分時間週期各存一份，因為使用
者畫線通常是標記價位/趨勢線，跨週期還是同一組參考線比較有意義，也符合
需求「不需要太複雜」。畫出來的線刻意設成不可再編輯
(`newshape.editable=False`)，只能畫新的/用 `eraseshape` 整條刪掉重
畫——避免要另外處理 Plotly 「使用者拖曳線段端點」時 relayout 事件只會帶
部分欄位(`shapes[0].x0` 這種)的複雜情況，只有「畫新線」「整條刪除」這
兩種操作才會在 `plotly_relayout` 事件裡帶出完整的新 `shapes` 陣列，讀
一次存一次就好。
"""
import datetime
from typing import Optional

from nicegui import ui

from app.models.ib_client import IBClient
from app.services import chart_drawing_store

# (key, 顯示名稱, IB barSizeSetting, IB durationStr)——durationStr 是憑
# 經驗抓的保守值(IB 對每種 barSize 能一次要多長的歷史資料有配額限制，太
# 貪心會直接被拒絕)，不是查表得出的精確上限，先求「查得到」，之後真的
# 卡到配額再依實測調整。
_TIMEFRAMES = [
    ("1min", "1分K", "1 min", "2 D"),
    ("5mins", "5分K", "5 mins", "5 D"),
    ("30mins", "30分K", "30 mins", "1 M"),
    ("1day", "日線", "1 day", "2 Y"),
    ("1week", "週線", "1 week", "10 Y"),
    ("1month", "月線", "1 month", "20 Y"),
]
_TIMEFRAME_BY_KEY = {key: (label, bar_size, duration) for key, label, bar_size, duration in _TIMEFRAMES}
_DEFAULT_TIMEFRAME = "1day"

# Y軸「動態縮放」的上下留白比例——可視範圍內的最高/最低價抓出來之後，各
# 加一點留白，蠟燭圖才不會頂到圖表邊緣，跟舊版 pyqtgraph 圖表的
# Y_PADDING_RATIO 是同一個概念(數字不同沒關係，這裡是全新實作)。
_Y_PADDING_RATIO = 0.1


def _parse_ts(value) -> Optional[float]:
    """把 x 軸座標(我們自己送出去的 ISO 字串，或 Plotly relayout 事件回
    傳的範圍字串)轉成 epoch 秒數，用同一個函式轉兩邊才能保證比較得出正
    確結果——Plotly 回傳的日期字串格式(空白分隔)跟我們送出去時用的
    `.isoformat()`(T 分隔)不一定完全一樣，各自比字串大小小容易踩到格式
    差異的坑，統一轉成數字最單純。"""
    if value is None:
        return None
    text = str(value).strip().replace(" ", "T")
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _empty_figure() -> dict:
    return {
        "data": [{
            "type": "candlestick",
            "x": [], "open": [], "high": [], "low": [], "close": [],
            # 紅漲綠跌(台股/中文使用者慣例)，跟西方「綠漲紅跌」相反，這
            # 裡刻意選台灣慣例。
            "increasing": {"line": {"color": "#e5484d"}, "fillcolor": "#e5484d"},
            "decreasing": {"line": {"color": "#2f9e44"}, "fillcolor": "#2f9e44"},
        }],
        "layout": {
            "margin": {"l": 50, "r": 10, "t": 10, "b": 30},
            "xaxis": {"rangeslider": {"visible": False}, "type": "date"},
            # fixedrange=True：滑鼠/滾輪縮放、拖曳一律對 Y 軸沒有作用，Y
            # 軸範圍只能靠程式呼叫 relayout 改，對照 `_rescale_y()`。
            "yaxis": {"fixedrange": True, "autorange": False},
            "dragmode": "zoom",  # Y 軸鎖死的情況下，框選縮放實質上只會動到 X 軸
            "newshape": {"line": {"color": "#f5a623", "width": 2}, "editable": False},
            "shapes": [],
            "showlegend": False,
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
        },
        "config": {
            "scrollZoom": True,
            "displaylogo": False,
            "modeBarButtonsToAdd": ["drawline", "eraseshape"],
        },
    }


def build(ib_client: IBClient):
    """建立「技術分析」頁籤內容，回傳 `set_symbol(symbol, contract)` 給
    `web_quote_board_page.py` 在查詢到新標的時呼叫——`contract` 是標的股
    票的 Contract(不是選擇權合約)，這個頁籤畫的是標的走勢，不是個別選
    擇權合約的價格序列。"""
    state = {"symbol": None, "contract": None, "timeframe": _DEFAULT_TIMEFRAME, "bars": []}

    with ui.column().classes("w-full gap-2"):
        with ui.row().classes("items-center gap-2"):
            timeframe_select = ui.select(
                {key: label for key, label, _, _ in _TIMEFRAMES},
                value=_DEFAULT_TIMEFRAME, label="週期",
            ).classes("w-32")
            ui.button("重新整理", icon="refresh", on_click=lambda: _refresh())
            ui.button("清除畫線", icon="clear", on_click=lambda: _on_clear_clicked())
        status_label = ui.label("請先在「T字報價」頁籤查詢標的")
        chart = ui.plotly(_empty_figure()).classes("w-full h-96")

    def _visible_range_extent(ts0: Optional[float], ts1: Optional[float]):
        bars = state["bars"]
        if not bars:
            return None
        visible = [
            b for b in bars
            if (ts0 is None or b["ts"] is None or b["ts"] >= ts0)
            and (ts1 is None or b["ts"] is None or b["ts"] <= ts1)
        ]
        if not visible:
            visible = bars
        return min(b["low"] for b in visible), max(b["high"] for b in visible)

    def _rescale_y(x0=None, x1=None) -> None:
        extent = _visible_range_extent(_parse_ts(x0), _parse_ts(x1))
        if extent is None:
            return
        lo, hi = extent
        span = hi - lo or max(abs(hi), 1.0) * 0.02
        pad = span * _Y_PADDING_RATIO
        chart.run_plot_method("relayout", {"yaxis.range": [lo - pad, hi + pad], "yaxis.autorange": False})

    def _on_relayout(e) -> None:
        args = e.args or {}
        if "shapes" in args:
            chart_drawing_store.save(state["symbol"], args["shapes"])
            return
        if args.get("xaxis.autorange"):
            _rescale_y(None, None)
            return
        x0 = args.get("xaxis.range[0]")
        x1 = args.get("xaxis.range[1]")
        if x0 is None and x1 is None:
            rng = args.get("xaxis.range")
            if isinstance(rng, list) and len(rng) == 2:
                x0, x1 = rng
        if x0 is not None or x1 is not None:
            _rescale_y(x0, x1)

    chart.on("plotly_relayout", _on_relayout)

    async def _refresh() -> None:
        contract = state["contract"]
        if contract is None:
            return
        label, bar_size, duration = _TIMEFRAME_BY_KEY[state["timeframe"]]
        status_label.text = f"查詢 {state['symbol']} {label} 中..."
        try:
            bars = await ib_client.ib.reqHistoricalDataAsync(
                contract, endDateTime="", durationStr=duration,
                barSizeSetting=bar_size, whatToShow="TRADES", useRTH=True,
            )
        except Exception as exc:
            status_label.text = f"查詢 {label} 失敗：{exc}"
            return
        if not bars:
            state["bars"] = []
            status_label.text = f"{state['symbol']} 查無 {label} 資料"
            return
        state["bars"] = []
        for b in bars:
            x = b.date.isoformat()
            state["bars"].append({"x": x, "ts": _parse_ts(x), "open": b.open, "high": b.high, "low": b.low, "close": b.close})

        fig = chart.figure
        fig["data"][0].update({
            "x": [b["x"] for b in state["bars"]],
            "open": [b["open"] for b in state["bars"]],
            "high": [b["high"] for b in state["bars"]],
            "low": [b["low"] for b in state["bars"]],
            "close": [b["close"] for b in state["bars"]],
        })
        fig["layout"]["shapes"] = chart_drawing_store.load(state["symbol"])
        chart.update_figure(fig)
        _rescale_y(None, None)
        status_label.text = f"{state['symbol']}｜{label}｜共 {len(state['bars'])} 根"

    async def _on_timeframe_change(e) -> None:
        state["timeframe"] = e.value
        await _refresh()

    def _on_clear_clicked() -> None:
        if state["symbol"] is None:
            return
        chart_drawing_store.save(state["symbol"], [])
        fig = chart.figure
        fig["layout"]["shapes"] = []
        chart.update_figure(fig)

    timeframe_select.on_value_change(_on_timeframe_change)

    async def set_symbol(symbol: str, contract) -> None:
        state["symbol"] = symbol
        state["contract"] = contract
        await _refresh()

    return set_symbol
