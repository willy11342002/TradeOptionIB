from typing import Callable, Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QDoubleSpinBox,
    QSpinBox, QLabel, QPushButton, QTabWidget, QMessageBox,
)

from app.models.ib_quote_client import IBQuoteClient
from app.models.option_utils import vertical_spread_legs
from app.models.order_book import OrderBookManager

_TIF_CHOICES = ["DAY", "GTC", "IOC", "FOK"]
_SPREAD_POINT_CHOICES = ["1", "2.5", "5", "10", "25", "50"]

NO_CONTEXT_TEXT = "尚未選擇履約價 (雙擊報價格任一買價/賣價開始下單)"

# *** 跟舊版(群益)的重要差異，一次講清楚 ***
# 1. 沒有「新倉/平倉」欄位：SKCOM 的 AUTO_POSITION(sNewClose=2「自動」)
#    是群益/TAIFEX 特有的欄位語意，IB 沒有對應概念——IB 的部位本來就是
#    淨額(netting)，送一張跟既有部位反向的委託，交易所自然會沖銷，不需
#    要額外告知「這是平倉」。
# 2. 沒有「連續IOC自動重送」/「≦≧觸發條件」：那套引擎是為了繞過SKCOM
#    組合單只能用IOC、不能掛單的限制。IB 的 BAG combo 可以直接掛
#    DAY/GTC 跡在單子上等成交，不需要監看報價重送——這裡的「委託條件」
#    下拉就是單純的 TIF，沒有額外的觸發邏輯。
# 3. 沒有「預估保證金」：那套公式(app/services/margin.py)是 TAIFEX 官方
#    發布的 TXO 專用公式，對美股選擇權沒有意義；IB 自己在下單時
#    (whatIfOrder)會計算真正的保證金影響，這裡不再自己估算一個數字。


class OrderEntryWidget(QWidget):
    """雙擊報價格任一 Call/Put 價格格時要用的下單面板，兩個分頁：裸買賣
    (單一合約) 跟 價差單 (IB BAG 複式單)。

    這是常駐的 dock widget 內容，不是彈出式 QDialog：UI 只建一次，雙擊
    不同的履約價時呼叫 set_context() 換掉目前鎖定的商品，不會重新開窗。

    按「送出」不會直接打到交易所，只是把這筆委託送進下單匣 (staged)，
    真正送出/改價/刪除都在下單匣視窗做。
    """

    # 目前這個下單面板算價格實際會用到的「symbol_key(conId字串)+要看買
    # 價還是賣價」清單(裸買賣一筆、價差單兩筆)，main_window.py 接這個訊
    # 號在報價表格對應的儲存格畫框線。list 內容是 (symbol_key, "bid"|"ask") tuple。
    active_legs_changed = pyqtSignal(list)

    def __init__(
        self, order_book_manager: OrderBookManager, quote_client: IBQuoteClient,
        get_contract: Callable[[float, bool], Optional[object]], parent=None,
    ):
        super().__init__(parent)
        self._manager = order_book_manager
        self._quote_client = quote_client
        # (strike, is_call) -> Option 合約(已 qualify 過)，由 main_window.py
        # 提供——它已經為了畫報價表格 qualify 過目前到期日的所有履約價，
        # 這裡直接借用那份快取，不用自己再打一次 IB 查合約。
        self._get_contract = get_contract

        self._call_contract = None
        self._put_contract = None
        self._is_call = None
        self._strike = None
        self._market_bid = None
        self._market_ask = None
        self._strike_step = 1.0

        self.title_label = QLabel(NO_CONTEXT_TEXT)
        self.title_label.setWordWrap(True)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_outright_tab(), "裸買賣")
        self.tabs.addTab(self._build_duplex_tab(), "價差單")
        self.tabs.setEnabled(False)  # 還沒雙擊選過履約價之前不能下單
        self.tabs.currentChanged.connect(self._emit_active_legs)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.tabs)
        layout.addWidget(self.status_label)
        layout.addStretch()

    # ------------------------------------------------------------ 換商品
    def set_context(
        self, call_contract, put_contract, is_call: bool,
        call_bid: float, call_ask: float, put_bid: float, put_ask: float,
        strike_step: float,
    ) -> None:
        self._call_contract = call_contract
        self._put_contract = put_contract
        self._is_call = is_call
        self._strike = call_contract.strike
        self._strike_step = strike_step
        self._market_bid = call_bid if is_call else put_bid
        self._market_ask = call_ask if is_call else put_ask

        leg = call_contract if is_call else put_contract
        self.title_label.setText(f"{leg.localSymbol or leg.symbol} ({'Call' if is_call else 'Put'} {self._strike:g})")
        self.symbol_label.setText(leg.localSymbol or leg.symbol)
        self.status_label.setText("")
        self.tabs.setEnabled(True)

        self._on_outright_side_changed(self.out_side_combo.currentIndex())

        self.spread_cp_combo.setCurrentText("買權" if is_call else "賣權")
        default_points = str(strike_step) if str(strike_step) in _SPREAD_POINT_CHOICES else _SPREAD_POINT_CHOICES[0]
        self.spread_points_combo.setCurrentText(default_points)
        # setCurrentText 在新值跟舊值相同時不會發訊號，這裡明確補呼叫一
        # 次，確保換履約價後價差單的現價/框線一定會重算。
        self._on_duplex_leg_selectors_changed()

    def _has_context(self) -> bool:
        return self._call_contract is not None

    @staticmethod
    def _build_price_row(price_spin: QDoubleSpinBox) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(price_spin)
        return row

    # ---------------------------------------------------------------- 裸買賣
    def _build_outright_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        self.symbol_label = QLabel("")
        form.addRow("商品代碼", self.symbol_label)

        self.out_side_combo = QComboBox()
        self.out_side_combo.addItems(["買進", "賣出"])
        self.out_side_combo.currentIndexChanged.connect(self._on_outright_side_changed)

        self.out_price_spin = QDoubleSpinBox()
        self.out_price_spin.setRange(0.01, 99999)
        self.out_price_spin.setDecimals(2)
        self.out_price_spin.setSingleStep(0.05)

        self.out_qty_spin = QSpinBox()
        self.out_qty_spin.setRange(1, 999)
        self.out_qty_spin.setValue(1)

        self.out_tif_combo = QComboBox()
        self.out_tif_combo.addItems(_TIF_CHOICES)

        send_btn = QPushButton("送進下單匣")
        send_btn.clicked.connect(self._on_stage_outright)

        form.addRow("買賣別", self.out_side_combo)
        form.addRow("委託價格", self._build_price_row(self.out_price_spin))
        form.addRow("口數", self.out_qty_spin)
        form.addRow("委託條件", self.out_tif_combo)
        form.addRow(send_btn)
        return tab

    def _on_outright_side_changed(self, _index: int):
        if not self._has_context():
            return
        buying = self.out_side_combo.currentText() == "買進"
        price = self._market_ask if buying else self._market_bid
        self.out_price_spin.setValue(price or self.out_price_spin.value() or 1.0)
        self._emit_active_legs()

    def _on_stage_outright(self):
        if not self._has_context():
            QMessageBox.warning(self, "提醒", "請先雙擊報價選一個履約價")
            return
        contract = self._call_contract if self._is_call else self._put_contract
        buy = self.out_side_combo.currentText() == "買進"
        price = self.out_price_spin.value()
        qty = self.out_qty_spin.value()
        tif = self.out_tif_combo.currentText()
        self._manager.stage_outright(contract, buy, price, qty, tif)
        self.status_label.setText(f"已送進下單匣：{contract.localSymbol or contract.symbol}")

    # ---------------------------------------------------------------- 價差單
    def _build_duplex_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        self.spread_cp_combo = QComboBox()
        self.spread_cp_combo.addItems(["買權", "賣權"])
        self.spread_cp_combo.currentIndexChanged.connect(self._on_duplex_leg_selectors_changed)

        self.spread_side_combo = QComboBox()
        self.spread_side_combo.addItems(["買方", "賣方"])
        self.spread_side_combo.currentIndexChanged.connect(self._on_duplex_leg_selectors_changed)

        self.spread_points_combo = QComboBox()
        self.spread_points_combo.addItems(_SPREAD_POINT_CHOICES)
        self.spread_points_combo.currentIndexChanged.connect(self._on_duplex_leg_selectors_changed)

        self.spread_price_spin = QDoubleSpinBox()
        self.spread_price_spin.setRange(0.01, 99999)
        self.spread_price_spin.setDecimals(2)
        self.spread_price_spin.setSingleStep(0.05)
        self.spread_price_spin.setValue(1.0)

        self.spread_qty_spin = QSpinBox()
        self.spread_qty_spin.setRange(1, 999)
        self.spread_qty_spin.setValue(1)

        self.spread_tif_combo = QComboBox()
        self.spread_tif_combo.addItems(_TIF_CHOICES)

        self.spread_send_btn = QPushButton("送進下單匣")
        self.spread_send_btn.clicked.connect(self._on_stage_duplex)

        form.addRow("買權/賣權", self.spread_cp_combo)
        form.addRow("買方/賣方", self.spread_side_combo)
        form.addRow("價差寬度", self.spread_points_combo)
        form.addRow("委託價格(淨權利金)", self._build_price_row(self.spread_price_spin))
        form.addRow("口數", self.spread_qty_spin)
        form.addRow("委託條件", self.spread_tif_combo)
        form.addRow(self.spread_send_btn)
        return tab

    def _compute_duplex_legs(self):
        """算出目前價差單分頁選的兩腳(合約+買賣方向)，跟送單/畫框線/算
        現價共用同一套邏輯，不要各自重算一份。垂直價差買賣方向規則見
        app/models/option_utils.py::vertical_spread_legs()，這裡只負責
        把 GUI 目前選的值轉成那個函式要的參數、再用 main_window.py 提供
        的合約快取把履約價換成實際的 Option 物件。第二腳查不到合約(不在
        目前報價表格涵蓋的履約價範圍內)回傳 None。"""
        if not self._has_context():
            return None
        is_call = self.spread_cp_combo.currentText() == "買權"
        buy_spread = self.spread_side_combo.currentText() == "買方"
        width = float(self.spread_points_combo.currentText())
        right = "C" if is_call else "P"

        (low_strike, low_action), (high_strike, high_action) = vertical_spread_legs(
            self._strike, width, right, buy_spread,
        )
        low_contract = self._get_contract(low_strike, is_call)
        high_contract = self._get_contract(high_strike, is_call)
        if low_contract is None or high_contract is None:
            return None
        return low_contract, low_action == "BUY", high_contract, high_action == "BUY", buy_spread

    def _on_stage_duplex(self):
        legs = self._compute_duplex_legs()
        if legs is None:
            if not self._has_context():
                QMessageBox.warning(self, "提醒", "請先雙擊報價選一個履約價")
            else:
                QMessageBox.warning(self, "提醒", "第二腳履約價不在目前的報價範圍內，請確認價差寬度或先擴大查詢範圍")
            return
        contract1, buy1, contract2, buy2, buy_spread = legs

        price = self.spread_price_spin.value()
        qty = self.spread_qty_spin.value()
        tif = self.spread_tif_combo.currentText()

        self._manager.stage_duplex(contract1, buy1, contract2, buy2, price, qty, tif=tif, net_buyer=buy_spread)
        self.status_label.setText(
            f"已送進下單匣：{contract1.localSymbol or contract1.symbol} / {contract2.localSymbol or contract2.symbol}",
        )

    # ------------------------------------------------- 現價自動計算/表格框線
    def _on_duplex_leg_selectors_changed(self, *_args):
        self._update_duplex_price_from_quotes()
        self._emit_active_legs()

    @staticmethod
    def _leg_cost(quote: Optional[dict], buy: bool) -> Optional[float]:
        if not quote:
            return None
        bid, ask = quote.get("bid"), quote.get("ask")
        if not bid or not ask or bid <= 0 or ask <= 0:
            return None
        return ask if buy else -bid

    def _update_duplex_price_from_quotes(self):
        """用兩腳目前的即時買賣價自動算一個現價填進委託價欄位：買方那腳
        用賣價、賣方那腳用買價、加總，取絕對值就是「現在要做這組價差要
        付出/收到多少權利金」。任何一腳目前查不到報價就不動舊值，不要用
        0 蓋掉使用者已經打好的價格。"""
        legs = self._compute_duplex_legs()
        if legs is None:
            return
        contract1, buy1, contract2, buy2, _ = legs
        cost1 = self._leg_cost(self._quote_client.get_cached(str(contract1.conId)), buy1)
        cost2 = self._leg_cost(self._quote_client.get_cached(str(contract2.conId)), buy2)
        if cost1 is None or cost2 is None:
            return
        self.spread_price_spin.setValue(max(round(abs(cost1 + cost2), 2), 0.01))

    def _active_legs_duplex(self) -> list:
        legs = self._compute_duplex_legs()
        if legs is None:
            return []
        contract1, buy1, contract2, buy2, _ = legs
        return [
            (str(contract1.conId), "ask" if buy1 else "bid"),
            (str(contract2.conId), "ask" if buy2 else "bid"),
        ]

    def _active_legs_outright(self) -> list:
        if not self._has_context():
            return []
        contract = self._call_contract if self._is_call else self._put_contract
        buy = self.out_side_combo.currentText() == "買進"
        return [(str(contract.conId), "ask" if buy else "bid")]

    def _emit_active_legs(self, *_args):
        if not self._has_context():
            self.active_legs_changed.emit([])
            return
        if self.tabs.currentIndex() == 0:
            self.active_legs_changed.emit(self._active_legs_outright())
        else:
            self.active_legs_changed.emit(self._active_legs_duplex())
