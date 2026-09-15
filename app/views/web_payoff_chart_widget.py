"""
NiceGUI 版到期損益圖，取代 `app/views/payoff_chart_widget.py`
(`PayoffChartWidget(QWidget)` + pyqtgraph)。

畫圖用 `ui.plotly`(NiceGUI 內建元件，純 dict figure，不需要額外安裝
`plotly` 這個 pip 套件，跟 `web_technical_analysis_panel.py` 同一套做
法)。legs 組裝/損益數學完全重用 `app/services/payoff.py`(本來就是
Qt-free)，不重寫一份——`position_legs()`/`pending_legs()` 這兩支原本是
Qt 版檔案裡的私有函式，這次順手搬到 `payoff.py`，Qt 版改成 import 那邊
的版本，兩邊不會再各自維護一份、之後改公式只要改一個地方。

跟 Qt 版同樣兩條線：「目前部位」(實線)、「含下單匣」(虛線，委託簿裡任
何還沒到終態(`STATUS_STAGED`/`STATUS_LIVE`)的委託都算進去)，外加兩平
點虛線標註、最大獲利/最大虧損文字。

*** 下單面板/委託簿共用同一個 build()，畫的都是「目前部位+目前委託簿」
***(使用者要求「這個頁面」「委託簿也要」各自都做一份損益圖)：不是下單
面板裡「還沒送出的草稿」——使用者按「暫存到委託簿」的當下，草稿才會真
的變成委託簿裡的一筆(`web_order_entry_widget.py::_on_stage_*()`)，而暫
存完會自動關閉下單面板、跳去委託簿(這次一起做的第 3 點需求)，使用者本
來就會在委託簿看到含這筆新單的損益圖，不需要在下單面板另外做一套「草
稿即時預覽」的邏輯，那是目前沒人要求的額外複雜度。
"""
import numpy as np
from nicegui import ui

from app.models.order_book import OrderBookManager
from app.models.positions import PositionManager
from app.services import theme
from app.services.payoff import (
    combined_payoff, find_breakevens, payoff_extremes, pending_legs, position_legs, price_axis_range,
)

_POSITION_COLOR = "#1a5fb4"
_PENDING_COLOR = "#e5a50a"
_BREAKEVEN_COLOR = "#c0392b"
SAMPLE_POINTS = 400


def _palette() -> dict:
    """跟 web_technical_analysis_panel.py::_palette() 同一套慣例：深色/
    淺色模式各用一組低透明度的格線/文字顏色，底色維持透明(跟外層卡片同
    色)。"""
    dark = theme.load_theme() == "dark"
    return {
        "grid": "rgba(255,255,255,0.08)" if dark else "rgba(0,0,0,0.10)",
        "line": "rgba(255,255,255,0.30)" if dark else "rgba(0,0,0,0.30)",
        "font": "rgba(255,255,255,0.70)" if dark else "rgba(0,0,0,0.70)",
        "zero": "rgba(255,255,255,0.35)" if dark else "rgba(0,0,0,0.35)",
    }


def _empty_figure() -> dict:
    p = _palette()
    axis_common = {"gridcolor": p["grid"], "linecolor": p["line"], "tickfont": {"color": p["font"]}}
    return {
        "data": [
            {
                "type": "scatter", "mode": "lines", "x": [], "y": [], "name": "目前部位",
                "line": {"color": _POSITION_COLOR, "width": 2},
            },
            {
                "type": "scatter", "mode": "lines", "x": [], "y": [], "name": "含下單匣",
                "line": {"color": _PENDING_COLOR, "width": 2, "dash": "dash"},
            },
        ],
        "layout": {
            "margin": {"l": 55, "r": 10, "t": 10, "b": 35},
            # 底色維持透明，跟外層卡片同色——不設的話 Plotly 預設白底，
            # 深色模式下會變成一塊刺眼的白色方塊(跟
            # web_technical_analysis_panel.py::_empty_figure() 同一個處
            # 理方式)。
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "showlegend": True,
            "legend": {"font": {"color": p["font"]}, "orientation": "h", "y": 1.15},
            "xaxis": {**axis_common, "title": "標的價格", "fixedrange": True},
            "yaxis": {
                **axis_common, "title": "到期損益 (USD)",
                "zeroline": True, "zerolinecolor": p["zero"], "zerolinewidth": 1, "fixedrange": True,
            },
            "shapes": [],
            "annotations": [],
            # 這是隨部位/下單匣即時變化的儀表板，不是給使用者手動探索的
            # 圖表——鎖死縮放/拖曳(跟 xaxis/yaxis 的 fixedrange 一起)，範
            # 圍完全由 price_axis_range() 決定，不會被使用者不小心拖走。
            "dragmode": False,
        },
        "config": {"displayModeBar": False, "staticPlot": True},
    }


def build(position_manager: PositionManager, order_book_manager: OrderBookManager) -> ui.column:
    with ui.column().classes("w-full gap-1 min-w-[480px]") as container:
        with ui.row().classes("items-center gap-4"):
            profit_label = ui.label("").classes("text-sm text-positive")
            loss_label = ui.label("").classes("text-sm text-negative")
        breakeven_label = ui.label("").classes("text-sm text-grey")
        chart = ui.plotly(_empty_figure()).classes("w-full h-64")

    def _refresh() -> None:
        pos_legs = position_legs(position_manager)
        pend_legs = pending_legs(order_book_manager)
        all_legs = pos_legs + pend_legs
        effective_legs = all_legs if pend_legs else pos_legs

        fig = _empty_figure()
        if not all_legs:
            chart.update_figure(fig)
            profit_label.text = ""
            loss_label.text = ""
            breakeven_label.text = "目前沒有部位或委託，無法畫損益圖"
            return

        low, high = price_axis_range(all_legs)
        prices = np.linspace(low, high, SAMPLE_POINTS)

        position_pnl = combined_payoff(pos_legs, prices) if pos_legs else np.zeros_like(prices)
        fig["data"][0]["x"] = prices.tolist()
        fig["data"][0]["y"] = position_pnl.tolist()

        if pend_legs:
            combined_pnl = combined_payoff(all_legs, prices)
            fig["data"][1]["x"] = prices.tolist()
            fig["data"][1]["y"] = combined_pnl.tolist()

        breakevens = find_breakevens(effective_legs, price_floor=low, price_ceiling=high) if effective_legs else []
        shapes = []
        annotations = []
        for price in breakevens:
            shapes.append({
                "type": "line", "xref": "x", "yref": "paper", "x0": price, "x1": price, "y0": 0, "y1": 1,
                "line": {"color": _BREAKEVEN_COLOR, "width": 1, "dash": "dash"},
            })
            annotations.append({
                "x": price, "y": 1, "xref": "x", "yref": "paper", "yanchor": "bottom", "showarrow": False,
                "text": f"{price:.0f}", "font": {"color": _BREAKEVEN_COLOR, "size": 10},
            })
        fig["layout"]["shapes"] = shapes
        fig["layout"]["annotations"] = annotations
        chart.update_figure(fig)

        extremes = payoff_extremes(effective_legs)
        profit_text = "無上限" if not extremes.max_profit_bounded else f"{extremes.max_profit:,.0f}"
        loss_text = "無下限" if not extremes.max_loss_bounded else f"{extremes.max_loss:,.0f}"
        profit_label.text = f"最大獲利：{profit_text}"
        loss_label.text = f"最大虧損：{loss_text}"
        breakeven_label.text = "損平點：" + ("、".join(f"{p:,.0f}" for p in sorted(breakevens)) if breakevens else "無")

    position_manager.positions_changed.connect(_refresh)
    order_book_manager.records_changed.connect(_refresh)
    _refresh()
    return container
