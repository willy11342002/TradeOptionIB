"""
選擇權報價視窗裡的「技術分析」頁籤——顯示目前查詢中標的的K線圖(蠟燭圖
+下方成交量)，可切換 1分K/5分K/30分K/日線/週線/月線，並支援使用者畫
線。「畫斜線」按鈕切到 Plotly 內建的 drawline 工具，使用者自己在圖表上
拖曳畫一條任意角度的直線；「畫橫線」按鈕不用 drawline，直接在目前可視
範圍高低價的中間新增一條貫穿整個可視寬度的水平線(價格文字貼著 Y 軸顯
示)，畫完後用兩個自訂圓點把手拖曳調整位置——故意不做「按鈕武裝→點圖
表上哪個位置」這種靠 `plotly_click` 事件的設計，因為 Plotly 在
`dragmode="zoom"` 底下單純點擊(沒有拖曳位移)常常不會觸發
`plotly_click`，實測驗證過連繞開 NiceGUI、直接掛在 Plotly 原生事件上的
監聽器都收不到，這是 Plotly 本身「點擊 vs 框選縮放」手勢判定的行為限
制，不能依賴。畫面/操作行為對照舊版 Qt `pyqtgraph` 圖表
(`candlestick_chart.py`，隨群益 API 一起刪除，已經不在專案裡)的設計：
Y軸不能用滑鼠/滾輪縮放，可視範圍內的最高最低價由程式自動算好、動態塞
滿Y軸，使用者只能縮放/拖曳X軸。

*** 拖曳＝平移(`dragmode="pan"`)，不是框選縮放 ***(使用者要求)：滑鼠
拖曳直接把目前可視範圍整段往左右移動(同寬度)，不會跳出選取框；要縮放
改用滑鼠滾輪(`config.scrollZoom`，跟 dragmode 無關，兩者互不衝突)。「畫
斜線」按鈕暫時切到 `dragmode="drawline"`，畫完/取消畫線一律切回 "pan"
(不是舊版的 "zoom")，見 `_on_relayout()`/`_on_clear_clicked()`。

*** K棒視窗化 + 捲動到邊緣自動載入更多 ***：本機快取(`historical_bars_
store`)存的是查到的「全部」K棒，但一次把幾千~幾萬根K棒全部塞進 Plotly
畫面會卡(使用者原始回報)，所以實際畫上 Plotly 的只有最近 `_WINDOW_SIZE`
根(`state["bars"]`，`state["all_bars"]` 才是本機快取的完整內容)。使用者
拖曳(平移)到目前顯示視窗最舊那一端附近時，`_maybe_load_more()` 會自動
接上更舊的資料：本機快取裡還有(`window_start > 0`)就直接從
`state["all_bars"]` 往前切，不用打任何 API；本機快取已經頂到底了才進一
步用 IB `reqHistoricalDataAsync` 往更早查一段、併入本機快取存檔。這段自
動載入用 `chart.figure` 局部改資料+明釘 `xaxis.range` 的方式重繪(`_push_
chart(reset_view=False)`)，不整個重建 figure，才不會把使用者正在平移
的畫面打斷跳走。

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
關掉)，靠的是 Plotly 內建的 shape editing 機制(`shape.editable=True`)。

橫線刻意**不**用這套內建機制——查過 Plotly.js 3.1.1 官方原始碼
(`src/components/shapes/draw.js`)跟社群討論才確認：(1) 在使用者拖曳
shape 的過程中呼叫 `Plotly.relayout()`，那個 shape 就不會再更新位置，
這是 Plotly.js 已知的架構限制，不是我們哪裡沒設對；(2) Plotly 沒有內建
「只能整條移動、不能個別調整端點」的選項——這正是橫線需要的行為(拖曳
任一端點都要讓兩邊同步、維持水平)，硬要在「畫完後端糾正回去再推送」這
條路上做，等於每次拖曳都要呼叫 relayout，會跟使用者正在進行的拖曳互相
打架(使用者實測回報：一拖就彈回去)。

改成完全繞開 Plotly 的 shape editing：橫線的 `editable` 維持
False(鎖死，不讓 Plotly 接手)，改由下面這段注入的 JS(`_HLINE_JS`)在橫
線兩端疊兩個自己畫的圓點把手(純 HTML `<div>`，絕對定位疊在
`.js-plotly-plot` 容器上，用 `gd._fullLayout.yaxis.p2l()`/`l2p()` 自己
算價格↔像素的對應)，拖曳這兩個把手時純前端处理(mousemove 只更新視覺，
不送到後端)，放開滑鼠那一刻才呼叫一次 `Plotly.relayout()` 把最終的
y0/y1(兩端同一個值)、`label.text` 一起送出去讓後端存檔——`_on_relayout`
的 `js_handler` 會在 `window.__hlineDragging` 為真的期間直接吞掉事件，
不要在拖曳過程中對後端灌一堆 relayout(效能考量，也避免中途觸發任何後
端邏輯)。

「畫斜線」按鈕維持用 Plotly 內建的 `drawline` 工具(切 dragmode，使用者
自己拖曳)，兩個端點本來就該各自獨立，不需要跟橫線一樣的同步邏輯，拖曳
調整端點時 `plotly_relayout` 事件帶的是部分欄位(例如 `"shapes[2].x0"`
這種點記法路徑，可能巢狀到 `"shapes[2].label.text"`)，`_apply_shape_edit()`
負責把這種路徑正確寫回 shape 字典對應的巢狀位置。
"""
import datetime
import re
from typing import Optional

from nicegui import ui

from app.models.ib_client import IBClient
from app.models import yfinance_history_client
from app.services import chart_drawing_store, historical_bars_store

# (key, 顯示名稱, IB barSizeSetting, 初次回補 durationStr, 增量更新
# durationStr)——查過 IB 官方文件(historical_limitations.html)確認：
# barSize >= "1 min" 的單次請求 duration 硬性上限已經取消，只剩「一次請
# 求盡量只回傳幾千根K棒」的軟性建議，跟 pacing 限制(同一 contract 2秒內
# 不能發6次以上請求、15秒內不能重複同樣的請求)。
#
# 初次回補(本機 `historical_bars_store` 還沒有快取)用下面這個大幅放寬、
# 但仍落在「幾千根K棒」guideline 內的 duration 抓一次；之後每次查到的K
# 棒都併入本機快取(`historical_bars_store.merge()`)，增量更新只需要抓
# 「快取之後新增的這一小段」。分K/日K的最小合法 duration 是 "1 D"(等於
# 照使用者要求「都抓一天」)；週K/月K不能配 "1 D"——IB 的 duration/
# barSize 對應規則裡，"1 D" 只支援到 "1 day" 這個粒度的 barSize，硬配會
# 直接查不到資料，只能退回官方表裡對應 barSize 允許的最小 duration
# ("1 W"/"1 M")，不是真的每次都抓一整週/一整月的資料，只是 IB 不允許比
# 這更短的組合。
_TIMEFRAMES = [
    ("1min", "1分K", "1 min", "10 D", "1 D"),
    ("5mins", "5分K", "5 mins", "3 M", "1 D"),
    ("30mins", "30分K", "30 mins", "1 Y", "1 D"),
    ("1day", "日線", "1 day", "30 Y", "1 D"),
    ("1week", "週線", "1 week", "30 Y", "1 W"),
    ("1month", "月線", "1 month", "50 Y", "1 M"),
]
_TIMEFRAME_BY_KEY = {
    key: (label, bar_size, backfill_duration, incremental_duration)
    for key, label, bar_size, backfill_duration, incremental_duration in _TIMEFRAMES
}
_DEFAULT_TIMEFRAME = "1day"
_INTRADAY_TIMEFRAMES = {"1min", "5mins", "30mins"}

# Y軸「動態縮放」的上下留白比例——可視範圍內的最高/最低價(或成交量)抓出
# 來之後，各加一點留白，圖形才不會頂到子圖邊緣，跟舊版 pyqtgraph 圖表的
# Y_PADDING_RATIO 是同一個概念(數字不同沒關係，這裡是全新實作)。
_Y_PADDING_RATIO = 0.1

# 一次畫上 Plotly 的K棒數上限(不是本機快取上限，快取繼續存全部歷史，見
# module docstring「K棒視窗化」那段)——資料量一多，瀏覽器端 Plotly 要處
# 理的蠟燭圖/hover/spike 一起變多，畫面就會卡，這裡先用一個固定值換取簡
# 單，之後真的還是太卡/太保守再依實測調整。
_WINDOW_SIZE = 1500
# 使用者平移到距離目前顯示視窗最舊那一端還剩幾根K棒，就觸發自動載入更多
# ——太小(例如 1)使用者會先看到明顯的「畫面空一截」才補資料，太大則會
# 常常還沒真的滑到底就提早觸發，50 是憑經驗抓的緩衝值。
_EDGE_LOAD_MARGIN = 50

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

# 橫線兩端的自訂拖曳把手——完全不用 Plotly 內建的 shape editing，理由見
# module docstring。`gdOf()` 找的是 `ui.plotly` 這個 NiceGUI 元件的 DOM
# 容器(id 是 "c"+element.id，見 nicegui.js::getElement())，這個容器本
# 身在 Plotly.newPlot() 之後就是 `.js-plotly-plot`(不是子節點)，兩種情
# 況都處理一下比較保險。拖曳中(`window.__hlineDragging`)只做純前端視覺
# 更新(呼叫 Plotly.relayout 讓橫線跟著把手移動，但不送到後端)，放開滑
# 鼠那一刻才補送一次最終狀態——`chart.on("plotly_relayout", ...,
# js_handler=...)` 那邊的 js_handler 靠這個全域旗標判斷要不要把事件轉
# 發給後端，見 build() 內的說明。
_HLINE_JS = """
<script>
(function () {
  function gdOf(wrapperId) {
    const el = document.getElementById(wrapperId);
    if (!el) return null;
    return el.classList.contains('js-plotly-plot') ? el : el.querySelector('.js-plotly-plot');
  }

  window.__hlineSync = function (wrapperId) {
    const gd = gdOf(wrapperId);
    if (!gd || !gd._fullLayout || !gd.layout) return;
    [...gd.querySelectorAll(':scope > .hline-handle')].forEach((el) => el.remove());
    if (getComputedStyle(gd).position === 'static') gd.style.position = 'relative';
    const ya = gd._fullLayout.yaxis;
    const size = gd._fullLayout._size;
    (gd.layout.shapes || []).forEach((shape, idx) => {
      if (shape.xref !== 'paper' || shape.yref !== 'y') return;  // 只有橫線才加把手，斜線用 Plotly 內建的端點編輯
      const pxY = ya.l2p(shape.y0) + size.t;
      [0.15, 0.85].forEach((frac) => {
        const handle = document.createElement('div');
        handle.className = 'hline-handle';
        handle.style.cssText = 'position:absolute;width:12px;height:12px;border-radius:50%;'
          + 'background:#f5a623;border:2px solid white;cursor:ns-resize;z-index:20;'
          + 'transform:translate(-50%,-50%);box-shadow:0 0 2px rgba(0,0,0,.6);';
        handle.style.left = (size.l + size.w * frac) + 'px';
        handle.style.top = pxY + 'px';
        handle.addEventListener('mousedown', (ev) => startDrag(ev, wrapperId, idx));
        gd.appendChild(handle);
      });
    });
  };

  let dragState = null;

  function startDrag(ev, wrapperId, idx) {
    ev.preventDefault();
    ev.stopPropagation();
    dragState = {wrapperId, idx};
    window.__hlineDragging = true;
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }

  function priceAt(gd, clientY) {
    const rect = gd.getBoundingClientRect();
    const size = gd._fullLayout._size;
    const ya = gd._fullLayout.yaxis;
    return Math.round(ya.p2l(clientY - rect.top - size.t) * 100) / 100;
  }

  function onMove(ev) {
    if (!dragState) return;
    const gd = gdOf(dragState.wrapperId);
    if (!gd) return;
    const price = priceAt(gd, ev.clientY);
    const patch = {};
    patch['shapes[' + dragState.idx + '].y0'] = price;
    patch['shapes[' + dragState.idx + '].y1'] = price;
    patch['shapes[' + dragState.idx + '].label.text'] = String(price);
    Plotly.relayout(gd, patch);  // window.__hlineDragging 還是 true，js_handler 不會把這次轉發給後端
    window.__hlineSync(dragState.wrapperId);
  }

  function onUp(ev) {
    if (!dragState) return;
    const {wrapperId, idx} = dragState;
    window.__hlineDragging = false;  // 先關掉抑制旗標，接下來這次 relayout 才會真的送到後端
    const gd = gdOf(wrapperId);
    const shape = gd && gd.layout.shapes[idx];
    if (shape) {
      const patch = {};
      patch['shapes[' + idx + '].y0'] = shape.y0;
      patch['shapes[' + idx + '].y1'] = shape.y1;
      patch['shapes[' + idx + '].label.text'] = shape.label.text;
      Plotly.relayout(gd, patch);
    }
    dragState = null;
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
  }
})();
</script>
"""


def _palette() -> dict:
    """低透明度的格線/文字透明度——底色維持透明(跟外層卡片同色)，格線/
    座標字如果直接套預設的不透明白色，在深色底下會顯得又粗又刺眼(使用
    者原始回報的「格子很醜」)，改成低透明度的細線。畫面固定深色(見
    `app/services/web_theme.py`)，不需要再判斷淺色配色。"""
    return {
        "grid": "rgba(255,255,255,0.08)",
        "line": "rgba(255,255,255,0.30)",
        "font": "rgba(255,255,255,0.70)",
        "spike": "rgba(255,255,255,0.45)",
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
            # dragmode="pan"：拖曳直接平移可視範圍(同寬度左右移動)，不
            # 是框選縮放(使用者要求，見 module docstring)。Y 軸鎖死的情
            # 況下，平移實質上只會動到 X 軸；要縮放改用滑鼠滾輪
            # (config.scrollZoom)。
            "dragmode": "pan",
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


def _apply_shape_edit(shape: dict, field: str, value) -> None:
    """把 `_extract_shape_edits()` 解析出來的欄位路徑寫回 shape 字典——
    `field` 可能帶點記法巢狀路徑(例如橫線拖曳把手送出的
    `"label.text"`)，要拆開逐層寫進 `shape["label"]["text"]`，不能直接
    當成字面上一個叫 `"label.text"` 的 key 塞進去。"""
    parts = field.split(".")
    target = shape
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


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
        "symbol": None, "contract": None, "timeframe": _DEFAULT_TIMEFRAME,
        # all_bars：本機快取的完整內容(可能好幾千~好幾萬根)；bars：目前
        # 實際畫上 Plotly 的視窗(all_bars[window_start:]，見
        # `_WINDOW_SIZE` 的說明)。view_range 記目前 X 軸可視範圍(Plotly
        # relayout 事件回傳的原始值，不是算過的 timestamp)，`_maybe_
        # load_more()` 補資料重繪時要靠這個把畫面釘在使用者平移到的位
        # 置，不能重繪完畫面彈回「顯示全部」。
        "all_bars": [], "bars": [], "window_start": 0, "view_range": (None, None),
        "loading_more": False, "exhausted": False,
        "visible": False, "dirty": False, "idle_text": "",
    }

    # Plotly 畫十字線(spikeline)固定會在設定的那條線底下再疊一條
    # stroke-width 比設定值多 2px、顏色寫死不透明白色的「對比襯底」線
    # (跟 spikecolor/spikethickness 這兩個 layout 參數無關，原生 SVG 屬
    # 性硬套，沒有對應的 layout 選項可以關掉)，實測是導致十字線看起來
    # 「很粗」的真正原因——直接用 CSS 蓋掉這兩條線的寬度，比在
    # layout.xaxis/yaxis 那幾個 spike* 參數上打轉有效。
    ui.add_head_html("<style>.js-plotly-plot .spikeline { stroke-width: 1px !important; }</style>")
    ui.add_head_html(_HLINE_JS)

    with ui.column().classes("w-full gap-2"):
        with ui.row().classes("items-center gap-2"):
            timeframe_select = ui.select(
                {key: label for key, label, _, _, _ in _TIMEFRAMES},
                value=_DEFAULT_TIMEFRAME, label="週期",
            ).classes("w-32")
            ui.button("重新整理", icon="refresh", on_click=lambda: _refresh())
            # 「畫斜線」按鈕切 dragmode 成 "drawline"(跟直接點 Plotly 工
            # 具列上的鉛筆圖示是同一件事)，讓使用者自己在圖表上拖曳畫一
            # 條任意角度的線；「畫橫線」不走這條路——直接在目前K棒的高低
            # 價中間新增一條橫線，畫完後用兩個自訂把手拖曳調整位置(見
            # module docstring 開頭關於 Plotly shape editing 限制的說
            # 明)，不需要使用者自己先拖出一條線再被拉平。
            ui.button("畫斜線", icon="edit", on_click=lambda: _on_draw_line_clicked())
            ui.button("畫橫線", icon="horizontal_rule", on_click=lambda: _on_draw_hline_clicked())
            ui.button("清除畫線", icon="clear", on_click=lambda: _on_clear_clicked())
        status_label = ui.label("請先在「選擇權報價」頁籤查詢標的")
        chart = ui.plotly(_empty_figure(_DEFAULT_TIMEFRAME)).classes("w-full h-96")

    def _sync_hline_handles() -> None:
        """通知前端重新畫一次橫線的拖曳把手(位置依 shapes/yaxis.range
        算)。呼叫時機：圖表資料重載、Y軸範圍變動(_rescale_y)、shapes 陣
        列增減之後。包一層 setTimeout 是因為 `chart.update_figure()` 送
        出的新 figure 要等前端真的跑完 `Plotly.react()`/`newPlot()`，
        `gd._fullLayout` 才會是最新的，緊接著同一個 tick 呼叫會抓到舊資
        料，50ms 是憑經驗抓的保守值，不是精確算出來的。"""
        ui.run_javascript(
            f"setTimeout(() => {{ window.__hlineSync && window.__hlineSync('c{chart.id}'); }}, 50)"
        )

    def _hline_shape(price: float) -> dict:
        return {
            "type": "line",
            # x0/x1 用 xref="paper"(0~1，貫穿整個可視寬度)，不用資料座
            # 標——這樣縮放/拖曳 X 軸的時候這條橫線永遠貫穿整個畫面，不
            # 會露出線段兩端的空白。
            "xref": "paper", "x0": 0, "x1": 1,
            "yref": "y", "y0": price, "y1": price,
            "line": {"color": "#f5a623", "width": 1},
            # editable 不設(等同 False)——橫線完全不用 Plotly 內建的
            # shape editing，拖曳調整靠 `_HLINE_JS` 自己疊的把手，理由見
            # module docstring 開頭。
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
        _sync_hline_handles()  # yaxis.range 變了，把手的像素位置要跟著重算

    async def _on_relayout(e) -> None:
        args = e.args or {}
        if "shapes" in args:
            # 畫新線(「畫斜線」按鈕的 drawline 拖出一條)、用 eraseshape
            # 整條刪除、或是我們自己(畫橫線按鈕/拖曳橫線把手完成時)主動
            # 推送的完整 shapes 陣列，Plotly 都會帶出完整陣列。橫線不會
            # 從這裡「新增」(直接由 _on_draw_hline_clicked() 構造好、整
            # 批推送，不經過 drawline)，所以這裡只要處理「新增了一條線
            # (斜線)就把 dragmode 切回預設的 pan」——drawline 模式下沒
            # 辦法拖曳調整既有線段的端點，見「畫斜線」按鈕的說明。
            shapes = args["shapes"]
            prev_shapes = chart_drawing_store.load(state["symbol"])
            if len(shapes) > len(prev_shapes):
                chart.run_plot_method("relayout", {"dragmode": "pan"})
            chart_drawing_store.save(state["symbol"], shapes)
            # 同步一份到 Python 端的 chart.figure 快取——
            # `_on_clear_clicked()` 清除畫線時要靠這個快取抓到目前完整
            # 的 figure 再改，不能用過期的版本。
            chart.figure["layout"]["shapes"] = shapes
            _sync_hline_handles()  # shapes 增減了，把手也要跟著增減
            return
        shape_edits = _extract_shape_edits(args)
        if shape_edits:
            # 拖曳調整既有線段的端點/位置，帶的是部分欄位(例如
            # "shapes[2].x0"，或橫線把手送出的 "shapes[2].label.text"
            # 這種巢狀路徑)，不是完整陣列，這裡從目前存檔的版本讀出
            # 來，只更新被拖動的那幾條，再存回去。橫線的兩端同步/標籤更
            # 新已經在 `_HLINE_JS` 那邊處理好了，這裡不用再猜是哪種線、
            # 也不用把結果推回瀏覽器(瀏覽器端本來就是這次拖曳的來源，早
            # 就是最新畫面了)。
            shapes = chart_drawing_store.load(state["symbol"])
            for idx, fields in shape_edits.items():
                if 0 <= idx < len(shapes):
                    for field, value in fields.items():
                        _apply_shape_edit(shapes[idx], field, value)
            chart_drawing_store.save(state["symbol"], shapes)
            chart.figure["layout"]["shapes"] = shapes
            return
        if args.get("xaxis.autorange"):
            state["view_range"] = (None, None)
            _rescale_y(None, None)
            return
        x0 = args.get("xaxis.range[0]")
        x1 = args.get("xaxis.range[1]")
        if x0 is None and x1 is None:
            rng = args.get("xaxis.range")
            if isinstance(rng, list) and len(rng) == 2:
                x0, x1 = rng
        if x0 is not None or x1 is not None:
            state["view_range"] = (x0, x1)
            _rescale_y(x0, x1)
            await _maybe_load_more(x0)

    # 自訂把手拖曳中(`window.__hlineDragging` 為 true)的時候，
    # `_HLINE_JS::onMove()` 會一路呼叫 `Plotly.relayout()` 來即時搬動橫
    # 線，這本身會連帶觸發一堆 plotly_relayout 事件——這些都只是拖到一
    # 半的中繼狀態，不需要送回後端(真正要存檔的最終結果，`onUp()` 放開
    # 滑鼠時會再呼叫一次沒有被這個旗標擋掉的 relayout，那次才會正常送到
    # `_on_relayout()`)。用 js_handler 直接在瀏覽器端擋掉，避免拖曳過程
    # 中對後端灌爆一堆沒用的事件。
    chart.on(
        "plotly_relayout", _on_relayout,
        js_handler="(...args) => { if (window.__hlineDragging) return; emit(...args); }",
    )

    def _on_draw_line_clicked() -> None:
        """「畫斜線」按鈕——切到 drawline 工具，使用者自己在圖表上拖曳畫
        任意角度的直線/斜線(等同直接點 Plotly 工具列上的鉛筆圖示，這裡
        只是在我們自己的按鈕列上多一個明顯的入口)。"""
        if state["symbol"] is None or not state["bars"]:
            return
        chart.run_plot_method("relayout", {"dragmode": "drawline"})

    def _on_draw_hline_clicked() -> None:
        """「畫橫線」按鈕——直接在目前可視範圍的中間價位新增一條橫線，
        不經過 drawline(不需要使用者自己拖一條再被拉平)。新線的拖曳調
        整完全交給 `_HLINE_JS` 的自訂把手處理，這裡只負責建立初始位
        置、存檔、推送到畫面，並同步一次把手。"""
        if state["symbol"] is None or not state["bars"]:
            return
        visible = _visible_bars(None, None)
        price = (min(b["low"] for b in visible) + max(b["high"] for b in visible)) / 2
        shapes = chart_drawing_store.load(state["symbol"])
        shapes.append(_hline_shape(price))
        chart_drawing_store.save(state["symbol"], shapes)
        chart.figure["layout"]["shapes"] = shapes
        chart.run_plot_method("relayout", {"shapes": shapes})
        _sync_hline_handles()

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

    def _push_chart(reset_view: bool) -> None:
        """把 `state["bars"]`(目前顯示視窗)的內容畫上 Plotly。
        `reset_view=True`(全新查詢/切換週期/切換標的)整個重建 figure，
        週期一換 rangebreaks 也要跟著換，重建最單純，順便把視野歸零到
        最新一段；`reset_view=False`(`_maybe_load_more()` 補到更舊的資
        料時用)只在既有 figure 上換資料，並把 X 軸範圍明釘回
        `state["view_range"]`——使用者正在平移到視窗邊緣才會觸發這個分
        支，畫面絕對不能被這次補資料打斷跳走。"""
        timeframe = state["timeframe"]
        label = _TIMEFRAME_BY_KEY[timeframe][0]
        if reset_view:
            fig = _empty_figure(timeframe)
            fig["layout"]["shapes"] = chart_drawing_store.load(state["symbol"])
        else:
            fig = chart.figure
            x0, x1 = state["view_range"]
            if x0 is not None and x1 is not None:
                # 兩端都有值才能塞給 Plotly 的 xaxis.range(它要的是完整
                # 的 [x0, x1] 區間，缺一端沒辦法用)——只有一端有值的極端
                # 情況(理論上 Plotly relayout 事件可能只帶一半)乾脆不釘
                # 死範圍，讓它照目前既有的 range 顯示就好。
                fig["layout"]["xaxis"]["range"] = [x0, x1]
                fig["layout"]["xaxis"]["autorange"] = False
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
        chart.update_figure(fig)
        if reset_view:
            _rescale_y(None, None)  # 內部已經會呼叫 _sync_hline_handles()
            # 預設(滑鼠沒有停在圖上)顯示最新一根K棒的開高低收/成交量，
            # 滑鼠移到某根K棒上會被 _on_hover() 蓋成那一根的資料，移開
            # 再由 _on_unhover() 換回這一行。
            state["idle_text"] = _format_bar_line(state["symbol"], label, state["bars"][-1])
            status_label.text = state["idle_text"]
        else:
            _rescale_y(x0, x1)

    async def _refresh() -> None:
        contract = state["contract"]
        if contract is None:
            return
        timeframe = state["timeframe"]
        label, bar_size, backfill_duration, incremental_duration = _TIMEFRAME_BY_KEY[timeframe]
        con_id = contract.conId
        cached = historical_bars_store.load(con_id, timeframe)
        status_label.text = f"查詢 {state['symbol']} {label} 中..."

        fresh = []
        if not cached:
            # 第一次查這個標的/週期(本機還沒有快取)：日K/週K/月K優先用
            # yfinance 抓 Yahoo 存的完整歷史(period="max"，一次請求就是
            # 真正的「最長」)，查失敗/查無資料(含分K——這個函式對分K
            # timeframe 直接回傳空清單)才落到下面用 IB 的大 duration 回
            # 補，見 `yfinance_history_client.py` 的說明。
            fresh = await yfinance_history_client.fetch_max_history(state["symbol"], timeframe)

        if not fresh:
            # 已經有快取只抓「新增的這一小段」；沒有快取但 yfinance 沒查
            # 到資料，退回放寬過的大 duration 整段回補，見 `_TIMEFRAMES`
            # 開頭的說明。
            duration = incremental_duration if cached else backfill_duration
            try:
                bars = await ib_client.ib.reqHistoricalDataAsync(
                    contract, endDateTime="", durationStr=duration,
                    barSizeSetting=bar_size, whatToShow="TRADES", useRTH=True,
                )
            except Exception as exc:
                status_label.text = f"查詢 {label} 失敗：{exc}"
                return
            for b in bars:
                x = b.date.isoformat()
                fresh.append({
                    "x": x, "ts": _parse_ts(x),
                    "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
                })
        merged = historical_bars_store.merge(cached, fresh)
        if not merged:
            state["all_bars"] = []
            state["bars"] = []
            status_label.text = f"{state['symbol']} 查無 {label} 資料"
            return
        if fresh:
            historical_bars_store.save(con_id, timeframe, merged)

        state["all_bars"] = merged
        state["window_start"] = max(0, len(merged) - _WINDOW_SIZE)
        state["bars"] = merged[state["window_start"]:]
        state["exhausted"] = False
        state["loading_more"] = False
        state["view_range"] = (None, None)
        _push_chart(reset_view=True)

    async def _load_more_from_ib() -> None:
        """本機快取(`state["all_bars"]`)已經頂到目前顯示視窗的最前面，
        往 IB 再查一段更早的資料——`endDateTime` 設成本機快取目前最舊那
        根K棒，`durationStr` 沿用該週期的初次回補值(`_TIMEFRAMES`)往回
        查一段同樣寬度。查不到/查到的資料併入後數量沒有增加(代表 IB 這
        次回傳的都跟本機重複)，就當作真的到 IB 資料盡頭，記
        `exhausted` 旗標，之後同一個標的/週期不用再白費力氣重試。"""
        contract = state["contract"]
        all_bars = state["all_bars"]
        if contract is None or not all_bars:
            return
        timeframe = state["timeframe"]
        _, bar_size, backfill_duration, _ = _TIMEFRAME_BY_KEY[timeframe]
        con_id = contract.conId
        try:
            end = datetime.datetime.fromisoformat(all_bars[0]["x"])
        except ValueError:
            state["exhausted"] = True
            return
        try:
            bars = await ib_client.ib.reqHistoricalDataAsync(
                contract, endDateTime=end, durationStr=backfill_duration,
                barSizeSetting=bar_size, whatToShow="TRADES", useRTH=True,
            )
        except Exception:
            state["exhausted"] = True
            return
        fresh = []
        for b in bars:
            x = b.date.isoformat()
            fresh.append({
                "x": x, "ts": _parse_ts(x),
                "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
            })
        merged = historical_bars_store.merge(all_bars, fresh)
        if len(merged) <= len(all_bars):
            state["exhausted"] = True
            return
        historical_bars_store.save(con_id, timeframe, merged)
        state["all_bars"] = merged
        state["window_start"] = 0
        state["bars"] = merged

    async def _maybe_load_more(x0) -> None:
        """使用者平移到接近目前顯示視窗最舊那一端時，自動把更舊的K棒接
        上去，見 module docstring「K棒視窗化」那段的說明。`loading_more`
        擋掉同一時間重複觸發(使用者平移一次可能連續收到好幾個
        relayout 事件)，`exhausted` 擋掉已經確認打到 IB 資料盡頭之後的
        無謂重試。"""
        if state["loading_more"] or state["exhausted"] or not state["bars"]:
            return
        ts0 = _parse_ts(x0)
        if ts0 is None:
            return
        margin_idx = min(_EDGE_LOAD_MARGIN, len(state["bars"]) - 1)
        edge_ts = state["bars"][margin_idx]["ts"]
        if edge_ts is not None and ts0 > edge_ts:
            return  # 離目前視窗最舊那端還有距離，不用載入
        state["loading_more"] = True
        try:
            if state["window_start"] > 0:
                # 本機快取裡還有更舊的資料還沒塞進畫面，直接從
                # state["all_bars"] 往前切，不用打任何 API。
                new_start = max(0, state["window_start"] - _WINDOW_SIZE)
                state["window_start"] = new_start
                state["bars"] = state["all_bars"][new_start:]
            else:
                await _load_more_from_ib()
            _push_chart(reset_view=False)
        finally:
            state["loading_more"] = False

    async def _on_timeframe_change(e) -> None:
        state["timeframe"] = e.value
        await _refresh()

    def _on_clear_clicked() -> None:
        if state["symbol"] is None:
            return
        chart_drawing_store.save(state["symbol"], [])
        fig = chart.figure
        fig["layout"]["shapes"] = []
        fig["layout"]["dragmode"] = "pan"
        chart.update_figure(fig)
        _sync_hline_handles()
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
