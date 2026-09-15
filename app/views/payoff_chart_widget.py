"""
到期損益圖：目前未平倉部位(+可選下單匣未成交委託)的到期損益曲線、兩平
點。用 pyqtgraph 畫(跟 candlestick_chart.py 同一套 pg 元件)，這裡只需要
單一座標軸，不需要多面板連動，所以不重用 PriceChartWidget，另外開一個
widget。

*** 刻意採用的簡化(沒有在畫面上顯示，只寫在這裡跟 payoff.py 開頭) ***
每一腳都用自己履約價的到期內含價值計算，不同到期日的部位加總在同一條
X 軸(標的價格)上時，忽略各腳實際到期日不同、忽略時間價值/隱含波動率。
見 app/services/payoff.py 開頭的說明。

*** IB 的部位方向永遠明確 ***：不像舊版群益那樣「買賣別欄位無法判讀」
要整筆跳過(見 app/models/positions.py 的說明)，這裡不用再防那個情況。

*** 複式單(TM合併列)：兩腳的方向/淨權利金怎麼分配，交給
app/models/positions.py 的 Position.payoff_legs() 統一決定(目前的結論
是「一買一賣的價差組合」，不是兩腳同方向——這個結論被使用者現場糾正過一
次，細節跟核對過程見那支檔案開頭的說明)，這裡不重複寫一份，只負責把
payoff_legs() 給的 (Contract, buy, premium) 攤平成 PayoffLeg 加總。
"""
import numpy as np
import pyqtgraph as pg
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QLabel

# 圖表跟上面那兩行文字資訊的垂直空間比例(圖表佔七成)，用 QVBoxLayout 的
# stretch 權重控制，不是寫死高度(dock 大小使用者可以自己拖動調整)。
INFO_AREA_STRETCH = 30
CHART_AREA_STRETCH = 70

from app.models.order_book import OrderBookManager
from app.models.positions import PositionManager
from app.services.payoff import (
    combined_payoff, find_breakevens, payoff_extremes, price_axis_range,
)
from app.services.payoff import pending_legs as get_pending_legs
from app.services.payoff import position_legs as get_position_legs

POSITION_CURVE_COLOR = "#1a5fb4"
PENDING_CURVE_COLOR = "#e5a50a"
ZERO_LINE_COLOR = "#888888"
BREAKEVEN_COLOR = "#c0392b"
UNDERLYING_PRICE_COLOR = "#9141ac"

# 跟 main_window.py 同一組顏色常數(避免循環 import，數值保持同步即可，見
# position_widgets.py 開頭同樣的做法)。*** 美股慣例：獲利=綠、虧損=紅，
# 跟台股相反，這是這次改動故意翻過來的地方。***
COLOR_PROFIT = "#3ecf6e"
COLOR_LOSS = "#e05050"

SAMPLE_POINTS = 400


class PayoffChartWidget(QWidget):
    def __init__(self, position_manager: PositionManager, order_book_manager: OrderBookManager, parent=None):
        super().__init__(parent)
        self._position_manager = position_manager
        self._order_book_manager = order_book_manager
        self._underlying_price = None
        self._effective_legs = []  # 上次 _redraw 算出來的那組 legs，set_underlying_price 每次跳價都要重算保證金，不能只靠 positions_changed/records_changed 觸發

        layout = QVBoxLayout(self)

        info_area = QWidget()
        info_layout = QVBoxLayout(info_area)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_line1 = QHBoxLayout()
        self.max_profit_label = QLabel("")
        self.max_loss_label = QLabel("")
        info_line1.addWidget(self.max_profit_label)
        info_line1.addWidget(self.max_loss_label)
        info_line1.addStretch(1)
        info_layout.addLayout(info_line1)
        self.breakeven_label = QLabel("")
        info_layout.addWidget(self.breakeven_label)
        layout.addWidget(info_area, INFO_AREA_STRETCH)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel("bottom", "標的價格")
        self.plot_widget.setLabel("left", "到期損益 (USD)")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        # 這張圖是「隨部位/下單匣即時變化的儀表板」，不是給使用者手動探索
        # 的圖表——鎖死縮放/拖曳，X/Y 範圍永遠自動貼齊目前資料範圍(見文件
        # `enableAutoRange`)，不會被使用者不小心滾動/拖走之後卡在舊範圍。
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.hideButtons()
        self.plot_widget.enableAutoRange()
        layout.addWidget(self.plot_widget, CHART_AREA_STRETCH)

        self._position_curve = self.plot_widget.plot([], [], pen=pg.mkPen(POSITION_CURVE_COLOR, width=2), name="目前部位")
        self._pending_curve = self.plot_widget.plot([], [], pen=pg.mkPen(PENDING_CURVE_COLOR, width=2, style=pg.QtCore.Qt.DashLine), name="含下單匣")
        self._zero_line = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(ZERO_LINE_COLOR, width=1))
        self.plot_widget.addItem(self._zero_line)
        self._underlying_line = pg.InfiniteLine(angle=90, pen=pg.mkPen(UNDERLYING_PRICE_COLOR, width=1, style=pg.QtCore.Qt.DotLine))
        self._underlying_line.setVisible(False)
        self.plot_widget.addItem(self._underlying_line)
        self._breakeven_lines = []

        self._position_manager.positions_changed.connect(self._redraw)
        self._order_book_manager.records_changed.connect(self._redraw)
        self._redraw()

    def set_underlying_price(self, price: float) -> None:
        self._underlying_price = price
        self._underlying_line.setPos(price)
        self._underlying_line.setVisible(True)

    def _clear_breakeven_lines(self) -> None:
        for line in self._breakeven_lines:
            self.plot_widget.removeItem(line)
        self._breakeven_lines = []

    def _update_info_labels(self, effective_legs, breakevens) -> None:
        """最大獲利/最大虧損/損平點，都是照「目前顯示中那條最完整的曲
        線」算(有下單匣就含下單匣，跟畫兩平點虛線用同一組 legs，不要兩邊
        算的東西對不起來)。"""
        extremes = payoff_extremes(effective_legs)
        if extremes.max_profit is None:
            self.max_profit_label.setText("")
            self.max_loss_label.setText("")
        else:
            profit_text = "無上限" if not extremes.max_profit_bounded else f"{extremes.max_profit:,.0f}"
            loss_text = "無下限" if not extremes.max_loss_bounded else f"{extremes.max_loss:,.0f}"
            self.max_profit_label.setText(
                f"<b>最大獲利：<span style='color:{COLOR_PROFIT}'>{profit_text}</span></b>&nbsp;&nbsp;&nbsp;"
            )
            self.max_loss_label.setText(
                f"<b>最大虧損：<span style='color:{COLOR_LOSS}'>{loss_text}</span></b>"
            )

        if not breakevens:
            self.breakeven_label.setText("損平點：無" if effective_legs else "")
        else:
            points = "、".join(f"{p:,.0f}" for p in sorted(breakevens))
            self.breakeven_label.setText(f"損平點：{points}")

    def _redraw(self) -> None:
        position_legs = get_position_legs(self._position_manager)
        pending_legs = get_pending_legs(self._order_book_manager)
        all_legs = position_legs + pending_legs
        effective_legs = all_legs if pending_legs else position_legs

        self._clear_breakeven_lines()
        if not all_legs:
            self._effective_legs = []
            self._position_curve.setData([], [])
            self._pending_curve.setData([], [])
            self._update_info_labels([], [])
            return

        low, high = price_axis_range(all_legs)
        prices = np.linspace(low, high, SAMPLE_POINTS)

        position_pnl = combined_payoff(position_legs, prices) if position_legs else np.zeros_like(prices)
        self._position_curve.setData(prices, position_pnl)

        if pending_legs:
            combined_pnl = combined_payoff(all_legs, prices)
            self._pending_curve.setData(prices, combined_pnl)
        else:
            self._pending_curve.setData([], [])

        breakevens = find_breakevens(effective_legs, price_floor=low, price_ceiling=high) if effective_legs else []
        self._update_info_labels(effective_legs, breakevens)
        self._effective_legs = effective_legs

        for price in breakevens:
            line = pg.InfiniteLine(
                pos=price, angle=90, pen=pg.mkPen(BREAKEVEN_COLOR, width=1, style=pg.QtCore.Qt.DashLine),
                label=f"{price:.0f}", labelOpts={"position": 0.05, "color": QColor(BREAKEVEN_COLOR)},
            )
            self.plot_widget.addItem(line)
            self._breakeven_lines.append(line)
