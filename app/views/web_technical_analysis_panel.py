"""
選擇權報價視窗裡的「技術分析」頁籤——顯示目前查詢中標的的K線圖(蠟燭圖
+下方成交量)，可切換 1分K/5分K/30分K/日線/週線/月線，並支援使用者用
「畫斜線」/「畫橫線」兩個按鈕在圖表上拖曳畫線(兩者都是切到 Plotly 的
drawline 工具，「畫橫線」多一步：拖曳完成後自動把那條線拉平成貫穿整個
可視寬度的水平線，用拖曳起點的價格，價格文字貼著 Y 軸顯示)。故意不做
成「按鈕武裝→點圖表上哪個位置」這種靠 `plotly_click` 事件的設計——
Plotly 在 `dragmode="zoom"` 底下單純點擊(沒有拖曳位移)常常不會觸發
`plotly_click`，實測驗證過連繞開 NiceGUI、直接掛在 Plotly 原生事件上的
監聽器都收不到，這是 Plotly 本身「點擊 vs 框選縮放」手勢判定的行為限
制，不能依賴；改成靠「畫完一條線」這個完成事件(`plotly_relayout` 帶完
整 shapes 陣列)，這個事件很可靠。畫面/操作行為對照舊版 Qt `pyqtgraph`
圖表
(`candlestick_chart.py`，隨群益 API 一起刪除，已經不在專案裡)的設計：
Y軸不能用滑鼠/滾輪縮放，可視範圍內的最高最低價由程式自動算好、動態塞
滿Y軸，使用者只能縮放/拖曳X軸。

畫圖用 `ui.plotly`(NiceGUI 內建元件，直接傳一個 dict figure 就能動，不
需要額外安裝 `plotly` 這個 pip 套件——`ui.plotly` 只有在收到
`plotly.graph_objects.Figure` 物件時才會用到那個套件，純 dict 走另一條
路徑，見 `nicegui/elements/plotly/plotly.py::_get_figure_json()`)取代
Qt 版的 pyqtgraph：K線用 Plotly 內建的 `candlestick` trace 型別；畫線工
具用 Plotly 內建的 `drawline`/`eraseshape` modebar 按鈕(拖曳畫線 Plotly
原生就有，不用像 pyqtgraph 版那樣自己接滑鼠事件重刻一遍)。

價格/成交量是兩個手動疊起來的子圖(各自的 xaxis/yaxis、用 domain 分配上
下各自的高度，不是用 Plotly 的 `make_subplots`——那是 `plotly.py` 套件
的功能，這裡刻意只用 dict figure 不裝套件，見上一段)，兩個子圖的 X 軸
用 `xaxis2.matches = "x"` 綁在一起，縮放/拖曳其中一個另一個會跟著動。

畫線持久化用 `app/services/chart_drawing_store.py`，用標的代碼(不分時
間週期)存一份 Plotly shape 清單——故意不分時間週期各存一份，因為使用
者畫線通常是標記價位/趨勢線，跨週期還是同一組參考線比較有意義，也符合
需求「不需要太複雜」。

畫完的直線/斜線可以再拖曳端點調整(Plotly 對 shape 的預設行為，沒有另外
關掉)，只有「畫橫線」按鈕產生的水平參考線刻意鎖死不可拖曳
(`editable=False`)，因為那條線的 `x0`/`x1` 用 `xref="paper"`(0~1)貫穿
整個可視寬度，一旦允許拖曳端點，很容易不小心把「貫穿全寬」的線拖成一
截不貫穿的線段，價位不對的話直接清除重畫/用 `eraseshape` 刪除更省事。

拖曳調整既有 shape 端點/位置時，`plotly_relayout` 事件帶的是部分欄位
(例如 `"shapes[2].x0"`)，不是完整的新 `shapes` 陣列(只有「畫新線」跟
「用 eraseshape 刪除」這兩種操作才會帶完整陣列，實測驗證過)——
`_on_relayout()` 裡 `_extract_shape_edits()` 專門解析這種
`"shapes[N].欄位"` 格式的 key，併回目前存檔的版本再存回去，兩種情況分
開處理，不要混在一起判斷。
"""
import datetime
import re
from typing import Optional

from nicegui import ui

from app.models.ib_client import IBClient
from app.services import chart_drawing_store, theme

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
_INTRADAY_TIMEFRAMES = {"1min", "5mins", "30mins"}

# Y軸「動態縮放」的上下留白比例——可視範圍內的最高/最低價(或成交量)抓出
# 來之後，各加一點留白，圖形才不會頂到子圖邊緣，跟舊版 pyqtgraph 圖表的
# Y_PADDING_RATIO 是同一個概念(數字不同沒關係，這裡是全新實作)。
_Y_PADDING_RATIO = 0.1

# 漲跌配色跟 `app/services/web_theme.py` 的 _POSITIVE/_NEGATIVE 用同一組
# 顏色(綠漲紅跌，跟 T 字報價表格一致)——那兩個是那支模組裡的私有前綴變
# 數，這裡直接抄數值，不 import。
_POSITIVE = "#22c55e"
_NEGATIVE = "#ef4444"
_VOLUME_UP = "rgba(34, 197, 94, 0.55)"
_VOLUME_DOWN = "rgba(239, 68, 68, 0.55)"

# 價格/成交量兩個子圖的 Y 軸 domain，中間留一點空隙分開兩塊。
_PRICE_DOMAIN = [0.28, 1.0]
_VOLUME_DOMAIN = [0.0, 0.20]


def _palette() -> dict:
    """深色/淺色模式各用一組格線/文字透明度——底色維持透明(跟外層卡片同
    色)，格線/座標字如果直接套預設的不透明白/黑色，在深色底下會顯得又
    粗又刺眼(使用者原始回報的「格子很醜」)，改成低透明度的細線。"""
    dark = theme.load_theme() == "dark"
    return {
        "grid": "rgba(255,255,255,0.08)" if dark else "rgba(0,0,0,0.10)",
        "line": "rgba(255,255,255,0.30)" if dark else "rgba(0,0,0,0.30)",
        "font": "rgba(255,255,255,0.70)" if dark else "rgba(0,0,0,0.70)",
        "spike": "rgba(255,255,255,0.45)" if dark else "rgba(0,0,0,0.45)",
    }


def _rangebreaks(timeframe_key: str) -> list:
    """把座標軸上「本來就不會有資料」的區間跳過，圖上才不會出現整段平白
    的空檔(使用者原始回報的「中間還有空的」)：週末一律跳過；分K再加跳過
    非美股正規盤時段(9:30-16:00，近似值，用 IB 回傳的時區，不逐一處理
    國定假日——`不需要太複雜`)。"""
    breaks = [{"bounds": ["sat", "mon"]}]
    if timeframe_key in _INTRADAY_TIMEFRAMES:
        breaks.append({"pattern": "hour", "bounds": [16, 9.5]})
    return breaks


def _empty_figure(timeframe_key: str) -> dict:
    p = _palette()
    axis_common = {
        "gridcolor": p["grid"], "zeroline": False, "linecolor": p["line"],
        "tickfont": {"color": p["font"]},
    }
    crosshair = {
        # spikesnap 三個合法值是 "cursor"/"data"/"hovered data"，不是
        # "cursor" vs "data" 兩選一——"cursor" 十字線跟著滑鼠原始座標走
        # (對不準收盤價)；"data" 聽起來像「對齊資料點」但實測會卡在固
        # 定位置不跟著滑鼠移動(踩過的坑，不是預期行為)；真正「跟著滑鼠
        # 目前 hover 到的那個資料點走」的是 "hovered data"(Plotly 的預
        # 設值)，垂直線對到那天、水平線對到那天的收盤價。
        "showspikes": True, "spikemode": "across", "spikesnap": "hovered data",
        "spikethickness": 1, "spikedash": "dot", "spikecolor": p["spike"],
    }
    breaks = _rangebreaks(timeframe_key)
    return {
        "data": [
            {
                "type": "candlestick", "xaxis": "x", "yaxis": "y",
                "x": [], "open": [], "high": [], "low": [], "close": [],
                "increasing": {"line": {"color": _POSITIVE}, "fillcolor": _POSITIVE},
                "decreasing": {"line": {"color": _NEGATIVE}, "fillcolor": _NEGATIVE},
            },
            {
                "type": "bar", "xaxis": "x2", "yaxis": "y2",
                "x": [], "y": [], "marker": {"color": []},
            },
        ],
        "layout": {
            "margin": {"l": 50, "r": 10, "t": 10, "b": 30},
            "xaxis": {
                **axis_common, **crosshair, "type": "date", "anchor": "y",
                "rangeslider": {"visible": False}, "showticklabels": False, "rangebreaks": breaks,
            },
            "xaxis2": {
                **axis_common, **crosshair, "type": "date", "anchor": "y2",
                "matches": "x", "rangebreaks": breaks,
            },
            # fixedrange=True：滑鼠/滾輪縮放、拖曳一律對 Y 軸沒有作用，Y
            # 軸範圍只能靠程式呼叫 relayout 改，對照 `_rescale_y()`。價
            # 格/成交量兩條 Y 軸都鎖，跟舊版 pyqtgraph 版兩個子圖都鎖 Y
            # 軸的行為一致。
            "yaxis": {
                **axis_common, **crosshair, "fixedrange": True, "autorange": False,
                "domain": _PRICE_DOMAIN, "anchor": "x",
            },
            "yaxis2": {
                **axis_common, "fixedrange": True, "autorange": False,
                "domain": _VOLUME_DOMAIN, "anchor": "x2", "showticklabels": False, "showspikes": False,
            },
            "dragmode": "zoom",  # Y 軸鎖死的情況下，框選縮放實質上只會動到 X 軸
            # hovermode 用 "x" 不要用 "closest"：後者要滑鼠夠靠近某根K
            # 棒本體(蠟燭圖的實際繪製範圍)才會觸發，游標停在空白處(例如
            # 遠高於當天最高價的地方)完全沒反應；"x" 是「先找最近的日
            # 期，不管Y座標在哪」，同一個X座標範圍內整條垂直空間都能觸
            # 發，垂直/水平十字線一樣都有(實測驗證過，之前這裡的判斷是
            # 錯的)。
            "hovermode": "x",
            "newshape": {"line": {"color": "#f5a623", "width": 2}},
            "shapes": [],
            "showlegend": False,
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "font": {"color": p["font"]},
        },
        "config": {
            "scrollZoom": True,
            "displaylogo": False,
            # 完全不顯示 Plotly 內建的工具列——「畫斜線」/「畫橫線」/
            # 「清除畫線」都已經是我們自己按鈕列上的按鈕(切 dragmode 或
            # 直接改 shapes)，不需要靠工具列上的鉛筆/橡皮擦圖示，關掉
            # 工具列不會少功能。*** 使用者明確要求不要看到這個工具列
            # ***：之前誤以為「工具列隱藏」是要它「一直顯示」，改成
            # displayModeBar=True 之後才發現理解反了——原本的意思是工
            # 具列會冒出來卡畫面，要的是徹底關掉，不是常駐顯示。
            "displayModeBar": False,
        },
    }


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


_SHAPE_EDIT_KEY = re.compile(r"^shapes\[(\d+)\]\.(.+)$")


def _extract_shape_edits(args: dict) -> dict[int, dict]:
    """從 `plotly_relayout` 事件的 args 裡挑出 `"shapes[N].欄位"` 這種
    key(使用者拖曳調整既有 shape 端點/位置時才會出現)，按 shape 的索引
    分組，回傳 `{index: {欄位: 新值, ...}, ...}`——沒有這種 key 就回傳空
    dict，呼叫端用這個判斷「這次 relayout 是不是一次拖曳編輯」。"""
    edits: dict[int, dict] = {}
    for key, value in args.items():
        match = _SHAPE_EDIT_KEY.match(key)
        if match:
            idx, field = int(match.group(1)), match.group(2)
            edits.setdefault(idx, {})[field] = value
    return edits


def _format_bar_line(symbol: str, timeframe_label: str, bar: dict) -> str:
    """狀態列文字——顯示這根K棒的開高低收/成交量，取代原本「共幾根K棒」
    這種對使用者沒什麼意義的統計數字(使用者原始要求)。`bar["x"]` 是
    `.isoformat()` 字串，日K/週K/月K只有日期(沒有 "T")，分K會帶時間，
    這裡統一把 "T" 換成空白，兩種都好讀。"""
    date_text = bar["x"].replace("T", " ")
    return (
        f"{symbol}｜{timeframe_label}｜{date_text}　"
        f"開 {bar['open']:g}　高 {bar['high']:g}　低 {bar['low']:g}　收 {bar['close']:g}　"
        f"量 {int(bar['volume']):,}"
    )


def build(ib_client: IBClient):
    """建立「技術分析」頁籤內容，回傳 `(set_symbol, set_visible)` 給
    `web_quote_board_page.py`：
    - `set_symbol(symbol, contract)`——查詢到新標的時呼叫，`contract` 是
      標的股票的 Contract(不是選擇權合約)，這個頁籤畫的是標的走勢，不
      是個別選擇權合約的價格序列。
    - `set_visible(is_visible)`——切換到/離開這個頁籤時呼叫。

    *** 一定要等頁籤真的切換過來、容器有實際寬高之後才第一次把K線資料畫
    上 Plotly ***：`ui.tab_panel` 沒被選到時是用 CSS 隱藏(display:none)
    但還留在 DOM 裡，如果趁隱藏的時候呼叫 `Plotly.react()`/`relayout()`
    塞資料，Plotly 量到的容器寬高是 0，算出來的座標軸範圍會整個跑掉(蠟
    燭圖擠成一條細線、Y軸卡在 Plotly 量不到資料時的預設範圍)，之後就算
    切過去顯示也不會自動修正。所以查到新標的當下如果使用者還停在「T字
    報價」頁籤，只記一個 `dirty` 旗標，真正的資料查詢/畫圖延到使用者切
    到「技術分析」頁籤那一刻(`set_visible(True)`)才做。"""
    state = {
        "symbol": None, "contract": None, "timeframe": _DEFAULT_TIMEFRAME, "bars": [],
        "visible": False, "dirty": False, "idle_text": "", "hline_armed": False,
    }

    # Plotly 畫十字線(spikeline)固定會在設定的那條線底下再疊一條
    # stroke-width 比設定值多 2px、顏色寫死不透明白色的「對比襯底」線
    # (跟 spikecolor/spikethickness 這兩個 layout 參數無關，原生 SVG 屬
    # 性硬套，沒有對應的 layout 選項可以關掉)，實測是導致十字線看起來
    # 「很粗」的真正原因——直接用 CSS 蓋掉這兩條線的寬度，比在
    # layout.xaxis/yaxis 那幾個 spike* 參數上打轉有效。
    ui.add_head_html("<style>.js-plotly-plot .spikeline { stroke-width: 1px !important; }</style>")

    with ui.column().classes("w-full gap-2"):
        with ui.row().classes("items-center gap-2"):
            timeframe_select = ui.select(
                {key: label for key, label, _, _ in _TIMEFRAMES},
                value=_DEFAULT_TIMEFRAME, label="週期",
            ).classes("w-32")
            ui.button("重新整理", icon="refresh", on_click=lambda: _refresh())
            # 「畫斜線」/「畫橫線」兩個按鈕都是把 dragmode 切成
            # "drawline"(跟直接點 Plotly 工具列上的鉛筆圖示是同一件
            # 事)，讓使用者自己在圖表上拖曳畫一條線——不要做成「按鈕武
            # 裝→點圖表上哪個位置」，Plotly 在 dragmode="zoom" 底下滑鼠
            # 沒有明顯拖曳位移的單純點擊常常不會觸發 `plotly_click` 事
            # 件(跟框選縮放手勢的判定會混淆，這是 Plotly 本身的行為限
            # 制，不是程式邏輯或 NiceGUI 轉發的問題——直接掛一個不透過
            # NiceGUI、純 Plotly 原生的 `gd.on('plotly_click', ...)` 監
            # 聽器實測驗證過，一樣收不到)。「畫橫線」按鈕多做一件事：拖
            # 曳完成後，把畫出來的線強制拉平成水平線(用拖曳起點的價格)
            # ，讓使用者可以「畫在自己想要的位置」，又不用真的画得多精
            # 準水平——這個「畫完一條線」的完成事件(`plotly_relayout`
            # 帶完整 shapes 陣列)是可靠的，跟不可靠的 `plotly_click` 是
            # 兩回事，見 `_on_relayout()` 的說明。
            ui.button("畫斜線", icon="edit", on_click=lambda: _on_draw_line_clicked())
            ui.button("畫橫線", icon="horizontal_rule", on_click=lambda: _on_draw_hline_clicked())
            ui.button("清除畫線", icon="clear", on_click=lambda: _on_clear_clicked())
        status_label = ui.label("請先在「選擇權報價」頁籤查詢標的")
        chart = ui.plotly(_empty_figure(_DEFAULT_TIMEFRAME)).classes("w-full h-96")

    def _hline_shape(price: float) -> dict:
        return {
            "type": "line",
            # x0/x1 用 xref="paper"(0~1，貫穿整個可視寬度)，不用資料座
            # 標——這樣縮放/拖曳 X 軸的時候這條橫線永遠貫穿整個畫面，不
            # 會露出線段兩端的空白。
            "xref": "paper", "x0": 0, "x1": 1,
            "yref": "y", "y0": price, "y1": price,
            "line": {"color": "#f5a623", "width": 1},
            # *** shape.editable 的 schema 預設值是 False，不是 True
            # ***：只有使用者用 drawline 工具「手畫」出來的新 shape，
            # Plotly 才會自動幫它加上 editable:true，我們這裡是程式碼自
            # 己組出來的 shape dict，不明確寫 True 就會是鎖死的(踩過的
            # 坑，一開始以為「不寫 editable」等於「用預設可編輯」，其實
            # 剛好相反)——使用者要求橫線能上下拖曳調整價位，這裡就要明
            # 講。抓著兩端點拖曳理論上可以把 x0/x1 拖離 0/1(不再貫穿全
            # 寬)，但那是使用者自己要挑端點拖才會發生，抓線本身拖曳只
            # 會整條平移(y0/y1 一起變、x0/x1 不變)，真的拖壞了就刪掉重
            # 畫，不特別處理這個邊角案例。
            "editable": True,
            # label.xanchor="left" 讓價格文字貼齊畫布最左邊(paper x=0
            # 那一端，正好是 Y 軸的位置)，看起來就像 Y 軸多長出一個自訂
            # 刻度，符合「Y軸要標註這條線畫在哪個價格」的需求。
            "label": {"text": f"{price:g}", "xanchor": "left", "font": {"color": "#f5a623", "size": 11}},
        }

    def _visible_bars(ts0: Optional[float], ts1: Optional[float]) -> list:
        bars = state["bars"]
        visible = [
            b for b in bars
            if (ts0 is None or b["ts"] is None or b["ts"] >= ts0)
            and (ts1 is None or b["ts"] is None or b["ts"] <= ts1)
        ]
        return visible or bars

    def _rescale_y(x0=None, x1=None) -> None:
        if not state["bars"]:
            return
        visible = _visible_bars(_parse_ts(x0), _parse_ts(x1))
        lo, hi = min(b["low"] for b in visible), max(b["high"] for b in visible)
        span = hi - lo or max(abs(hi), 1.0) * 0.02
        pad = span * _Y_PADDING_RATIO
        vol_hi = max((b["volume"] for b in visible), default=0.0)
        chart.run_plot_method("relayout", {
            "yaxis.range": [lo - pad, hi + pad], "yaxis.autorange": False,
            "yaxis2.range": [0, vol_hi * 1.1 if vol_hi > 0 else 1], "yaxis2.autorange": False,
        })

    def _on_relayout(e) -> None:
        args = e.args or {}
        if "shapes" in args:
            # 畫新線(drawline 拖出一條)、或用 eraseshape 整條刪除，
            # Plotly 都會帶出完整的新 shapes 陣列——這個「畫完一條線」
            # 的完成事件很可靠(不像 plotly_click)，「畫橫線」按鈕就是
            # 靠這個：使用者拖曳畫完隨便一條線之後，如果目前是
            # hline_armed 狀態，把剛畫好的那條(陣列最後一個，drawline
            # 一律是 append 到最後面)強制改寫成貫穿全寬的水平線，用拖曳
            # 起點(y0)當價格——這樣使用者可以「拖到畫面上想要的位置」，
            # 又不用真的畫得多水平。
            shapes = args["shapes"]
            prev_shapes = chart_drawing_store.load(state["symbol"])
            added_new = len(shapes) > len(prev_shapes)
            if added_new and state["hline_armed"]:
                price = shapes[-1].get("y0")
                shapes[-1] = _hline_shape(price)
                state["hline_armed"] = False
                status_label.text = state["idle_text"]
                chart.run_plot_method("relayout", {"shapes": shapes, "dragmode": "zoom"})
            elif added_new:
                # *** 畫完一條線(不管是「畫斜線」按鈕還是上面 hline
                # 分支)一定要自動把 dragmode 切回 "zoom" ***：Plotly 在
                # dragmode="drawline" 底下，任何一次點擊/拖曳都會被當成
                # 「要開始畫新的一條」，既有的 shape 完全沒辦法拖曳調
                # 整——這是使用者實測回報的：畫完線不會自動變回拖曳模
                # 式、既有線段(不管橫線斜線)都改不動，兩個症狀其實是同
                # 一個根因。停在 hline_armed 分支的那一路已經有自己的
                # relayout 呼叫(順便帶 dragmode)，這裡只處理其他情況
                # (一般 drawline 畫的斜線/直線)。
                chart.run_plot_method("relayout", {"dragmode": "zoom"})
            chart_drawing_store.save(state["symbol"], shapes)
            # 同步一份到 Python 端的 chart.figure 快取——
            # `_on_clear_clicked()` 清除畫線時要靠這個快取抓到目前完整
            # 的 figure 再改，不能用過期的版本。
            chart.figure["layout"]["shapes"] = shapes
            return
        shape_edits = _extract_shape_edits(args)
        if shape_edits:
            # 拖曳調整既有線段的端點/位置，帶的是部分欄位(例如
            # "shapes[2].x0")，不是完整陣列，這裡從目前存檔的版本讀出
            # 來，只更新被拖動的那幾條，再存回去。
            shapes = chart_drawing_store.load(state["symbol"])
            needs_push_back = False
            for idx, fields in shape_edits.items():
                if not (0 <= idx < len(shapes)):
                    continue
                shape = shapes[idx]
                shape.update(fields)
                if shape.get("xref") == "paper":
                    # *** 橫線一定要在這裡自己把兩個端點拉回同一個價
                    # 位、更新標籤文字 ***：Plotly 的 shape 端點拖曳本
                    # 來就是各自獨立的(y0/y1 可以拖成不同值)，這對橫線
                    # 是不對的——橫線的定義就是 y0==y1，只拖一個端點會
                    # 讓線歪掉，不再是橫線。用「這次哪個欄位被拖動」取
                    # 新價位(y1 優先、沒有才用 y0；整條平移時兩者會是
                    # 同一個值，取哪個都一樣)，兩邊都設成這個值，
                    # label.text 也跟著換成新的價位文字。
                    price = fields.get("y1", fields.get("y0", shape.get("y0")))
                    shape["y0"] = price
                    shape["y1"] = price
                    shape.setdefault("label", {})["text"] = f"{price:g}"
                    needs_push_back = True
                shapes[idx] = shape
            chart_drawing_store.save(state["symbol"], shapes)
            chart.figure["layout"]["shapes"] = shapes
            if needs_push_back:
                # 橫線被拖歪的那個瞬間，瀏覽器畫面已經先顯示了不對稱的
                # 版本，這裡把糾正好的完整 shapes 推回去，畫面才會立刻
                # 跳回真正水平的樣子——這個推送本身還會再觸發一次
                # plotly_relayout，但那次事件的 shapes 陣列長度沒變(不
                # 是新增/刪除)，會落回最上面的 "shapes" in args 分支，
                # 該分支只會重存一次同樣內容、不會再推送，不會無窮迴圈。
                chart.run_plot_method("relayout", {"shapes": shapes})
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

    def _on_draw_line_clicked() -> None:
        """「畫斜線」按鈕——切到 drawline 工具，使用者自己在圖表上拖曳畫
        任意角度的直線/斜線(等同直接點 Plotly 工具列上的鉛筆圖示，這裡
        只是在我們自己的按鈕列上多一個明顯的入口)。"""
        if state["symbol"] is None or not state["bars"]:
            return
        state["hline_armed"] = False
        chart.run_plot_method("relayout", {"dragmode": "drawline"})

    def _on_draw_hline_clicked() -> None:
        """「畫橫線」按鈕——一樣切到 drawline 工具讓使用者拖曳，但多記一
        個 `hline_armed` 旗標，畫完那條線由 `_on_relayout()` 攔下來強制
        拉平成水平線(用拖曳起點的價格)，理由見按鈕旁邊的註解。"""
        if state["symbol"] is None or not state["bars"]:
            return
        state["hline_armed"] = True
        status_label.text = "在圖表上拖曳一下(角度不拘)，會自動拉平成橫線"
        chart.run_plot_method("relayout", {"dragmode": "drawline"})

    def _on_hover(e) -> None:
        # 蠟燭圖(curveNumber 0)的 hover point 直接帶 open/high/low/close
        # (Plotly 對 candlestick trace 的內建行為，實測驗證過)，用
        # pointIndex 對回 state["bars"]——這個 trace 的 x/open/high/
        # low/close 陣列本來就是照 state["bars"] 的順序組出來的
        # (`_refresh()`)，索引一定對得上，不用另外找日期比對。
        points = (e.args or {}).get("points") or []
        price_point = next((p for p in points if p.get("curveNumber") == 0), None)
        if price_point is None:
            return
        idx = price_point.get("pointIndex")
        if idx is None or not (0 <= idx < len(state["bars"])):
            return
        timeframe_label = _TIMEFRAME_BY_KEY[state["timeframe"]][0]
        status_label.text = _format_bar_line(state["symbol"], timeframe_label, state["bars"][idx])

    def _on_unhover(_e) -> None:
        status_label.text = state["idle_text"]

    # plotly_hover 用自訂 js_handler 只挑需要的欄位送出去，不要用 `.on()`
    # 預設的 `(...args) => emit(...args)` 整包原始事件物件轉發——Plotly
    # 原始 hover 事件裡帶一份 native 滑鼠事件的參照，裡面有 DOM 節點的循
    # 環參照，直接整包送有序列化失敗的風險(relayout 事件沒有這個問題，
    # payload 只有改動的 layout 欄位，可以直接用預設轉發)。這裡自己重組
    # 一份只留 open/high/low/close/pointIndex/curveNumber 的乾淨物件，
    # 保證送到後端的東西一定可以序列化。
    chart.on(
        "plotly_hover", _on_hover,
        js_handler="""(payload) => emit({
            points: (payload.points || []).map(p => ({
                curveNumber: p.curveNumber, pointIndex: p.pointIndex,
                open: p.open, high: p.high, low: p.low, close: p.close,
            })),
        })""",
    )
    chart.on("plotly_unhover", _on_unhover, js_handler="() => emit({})")

    async def _refresh() -> None:
        contract = state["contract"]
        if contract is None:
            return
        timeframe = state["timeframe"]
        label, bar_size, duration = _TIMEFRAME_BY_KEY[timeframe]
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
            state["bars"].append({
                "x": x, "ts": _parse_ts(x),
                "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
            })

        fig = _empty_figure(timeframe)  # 換週期時間隔(rangebreaks)也要跟著換，整張圖重建最單純
        fig["data"][0].update({
            "x": [b["x"] for b in state["bars"]],
            "open": [b["open"] for b in state["bars"]],
            "high": [b["high"] for b in state["bars"]],
            "low": [b["low"] for b in state["bars"]],
            "close": [b["close"] for b in state["bars"]],
        })
        fig["data"][1].update({
            "x": [b["x"] for b in state["bars"]],
            "y": [b["volume"] for b in state["bars"]],
            "marker": {"color": [_VOLUME_UP if b["close"] >= b["open"] else _VOLUME_DOWN for b in state["bars"]]},
        })
        fig["layout"]["shapes"] = chart_drawing_store.load(state["symbol"])
        chart.update_figure(fig)
        _rescale_y(None, None)
        # 預設(滑鼠沒有停在圖上)顯示最新一根K棒的開高低收/成交量，滑鼠移
        # 到某根K棒上會被 _on_hover() 蓋成那一根的資料，移開再由
        # _on_unhover() 換回這一行。
        state["idle_text"] = _format_bar_line(state["symbol"], label, state["bars"][-1])
        status_label.text = state["idle_text"]
        state["hline_armed"] = False  # 換標的/換週期，之前武裝中的「畫橫線」狀態沒意義了

    async def _on_timeframe_change(e) -> None:
        state["timeframe"] = e.value
        await _refresh()

    def _on_clear_clicked() -> None:
        if state["symbol"] is None:
            return
        chart_drawing_store.save(state["symbol"], [])
        fig = chart.figure
        fig["layout"]["shapes"] = []
        fig["layout"]["dragmode"] = "zoom"
        chart.update_figure(fig)
        state["hline_armed"] = False  # 清畫線的同時取消任何還沒完成的「畫橫線」武裝狀態
        status_label.text = state["idle_text"]

    timeframe_select.on_value_change(_on_timeframe_change)

    async def set_symbol(symbol: str, contract) -> None:
        state["symbol"] = symbol
        state["contract"] = contract
        if state["visible"]:
            await _refresh()
        else:
            state["dirty"] = True  # 頁籤還沒切過來，資料查詢延到 set_visible(True) 才做

    async def set_visible(is_visible: bool) -> None:
        state["visible"] = is_visible
        if is_visible and state["dirty"]:
            state["dirty"] = False
            await _refresh()

    return set_symbol, set_visible
