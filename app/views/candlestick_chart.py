"""
「開倉」頁面主畫面的價格圖表：加權指數(TAIEX)K棒 + 台指期收盤價線圖疊加、
下方成交量附圖、目前選定分析的壓力/支撐水平線、滑鼠十字線、縮放時動態
補抓更多歷史。用 pyqtgraph 畫，K棒是照官方 customGraphicsItem.py 範例
改的自訂 GraphicsObject (pyqtgraph 沒有內建K棒元件)。

滑鼠滾輪/拖曳只能操作「時間」(X 軸)：兩個子圖的 Y 軸都關閉滑鼠互動
(setMouseEnabled(y=False))，Y 軸範圍完全由程式依目前可見的資料範圍自動
算好、讓K棒佔滿畫面中間 2/3，不會出現「滾一下Y軸也跟著縮、指數擠成一
條線」的狀況。

x 軸刻意不用真實日期座標，而是用「第幾個交易日」的整數索引，再用自訂
AxisItem 動態把索引換成日期字串——如果直接用日期當座標，週末/假日會在
圖上留下難看的空白區段，這是畫日K線圖的標準做法。
"""
import datetime

import pyqtgraph as pg
from PyQt5.QtCore import Qt, QRectF, pyqtSignal
from PyQt5.QtGui import QPainter, QPicture, QColor
from PyQt5.QtWidgets import QVBoxLayout, QWidget, QLabel

# 沿用這支 app 既有的漲跌顏色慣例 (main_window.py)：台股習慣紅漲綠跌，
# 跟歐美常見的紅跌綠漲相反。
UP_COLOR = "#cc0000"
DOWN_COLOR = "#008000"
FUTURES_LINE_COLOR = "#1a5fb4"
RESISTANCE_COLOR = "#e5a50a"
SUPPORT_COLOR = "#9141ac"
CROSSHAIR_COLOR = "#888888"
DAY_BOUNDARY_COLOR = "#808080"  # 換日線：分/5分/30分這種盤中週期才需要，日/週/月線本身每根就是一個完整週期
# 換日線用早上8點當分界，不是午夜——夜盤大約15:00~次日05:00，跨過午夜還
# 在繼續走，用午夜切的話同一個連續的夜盤會被切成兩天看；8點這個時間點日
# 盤(TX00 08:45/TSEA 09:00)都還沒開盤、夜盤也早就收了(05:00收)，落在這個
# 空檔切最不會誤傷任何一段連續的交易時段。
DAY_BOUNDARY_HOUR = 8

# 視圖左邊界離目前已載入資料的開頭小於這麼多根K棒，就觸發補抓更多歷史。
EDGE_LOAD_THRESHOLD = 5
# 資料來源(finmind_client)已經是本機 CSV 增量快取，補抓幾乎瞬間完成，
# 不用像打即時 API 那樣保守設上限——這裡設一個大到能涵蓋全部歷史
# (TAIEX 回溯到 2000 年、台指期回溯到 1998 年開始交易) 的天數當保險，
# 純粹避免使用者一直拖時無限倍增下去。
MAX_CHART_DAYS = 11000
# 可見資料的高低點只佔畫面高度的 2/3，等同上下各留資料高度 25% 的邊界。
Y_PADDING_RATIO = 0.25
# 「即時跟隨」模式(set_live_follow)第一次套用、還沒有既有縮放程度可以沿
# 用時，預設一次顯示幾根K棒(最新那根落在正中間，左右各半)。
DEFAULT_LIVE_FOLLOW_BARS = 60


class _DateAxisItem(pg.AxisItem):
    """把 X 軸的數字座標(第幾根K棒)動態換成日期字串顯示。刻度密度完全交給
    pyqtgraph 自己依目前縮放範圍/可用寬度決定要放幾個刻度，我們只負責
    把它選中的位置轉成對應的日期字串，縮放/平移的時候標籤密度才會自動
    跟著調整，不會一直是同一批標籤擠在一起或留一堆空白。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dates: list[str] = []

    def tickStrings(self, values, scale, spacing):
        labels = []
        for v in values:
            i = round(v)
            labels.append(self.dates[i] if 0 <= i < len(self.dates) else "")
        return labels


class _PriceAxisItem(pg.AxisItem):
    """加權指數的 Y 軸。除了正常的自動刻度，把目前選定分析的壓力/支撐
    價位也當成刻度直接顯示在軸上 (只顯示數字，不顯示「壓力/支撐」文字，
    顏色跟圖上的水平線一致)，取代原本浮在圖表中間的文字標籤，才不會擠
    在一起看不清楚。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.extra_levels: list[float] = []
        self._level_colors: dict[str, str] = {}  # 格式化後的文字 -> 顏色

    def set_extra_levels(self, level_colors: dict[float, str]):
        """level_colors 是 {價位: 顏色字串}，色碼跟圖上那條水平線一致。"""
        self.extra_levels = list(level_colors.keys())
        self._level_colors = {f"{level:g}": color for level, color in level_colors.items()}
        self.picture = None
        self.update()

    def tickValues(self, minVal, maxVal, size):
        ticks = super().tickValues(minVal, maxVal, size)
        extra_values = [level for level in self.extra_levels if minVal <= level <= maxVal]
        if not extra_values:
            return ticks
        # 刻意不去重——就算跟自動刻度剛好同一個數字，把我們的顏色版本疊
        # 在最後面畫，同一個位置疊上去正好蓋掉原本沒有顏色的那個。
        if ticks:
            spacing, values = ticks[0]
            return [(spacing, list(values) + extra_values)] + list(ticks[1:])
        return [(1, extra_values)]

    def tickStrings(self, values, scale, spacing):
        return [f"{v:g}" for v in values]

    def drawPicture(self, p, axisSpec, tickSpecs, textSpecs):
        """跟父類別幾乎一樣，差別只在畫刻度文字時，如果這個文字剛好是
        壓力/支撐價位就換成對應顏色的筆，其餘刻度維持預設顏色。"""
        p.setRenderHint(p.RenderHint.Antialiasing, False)
        p.setRenderHint(p.RenderHint.TextAntialiasing, True)

        pen, p1, p2 = axisSpec
        p.setPen(pen)
        p.drawLine(p1, p2)

        for pen, p1, p2 in tickSpecs:
            p.setPen(pen)
            p.drawLine(p1, p2)

        if self.style["tickFont"] is not None:
            p.setFont(self.style["tickFont"])
        bounding = self.boundingRect().toAlignedRect()
        p.setClipRect(bounding)
        default_pen = self.textPen()
        for rect, flags, text in textSpecs:
            color = self._level_colors.get(text)
            p.setPen(pg.mkPen(color) if color else default_pen)
            p.drawText(rect, int(flags), text)


class _CandlestickItem(pg.GraphicsObject):
    """data 是 [(index, open, high, low, close), ...]，仿 pyqtgraph 官方
    examples/customGraphicsItem.py 的做法，先畫進 QPicture 快取起來，
    paint() 只需要重播，不用每次都重新算形狀。"""

    def __init__(self, data: list[tuple]):
        super().__init__()
        self.data = data
        self._picture = QPicture()
        self._generate_picture()

    def _generate_picture(self):
        painter = QPainter(self._picture)
        width = 0.3
        for index, open_, high, low, close in self.data:
            color = QColor(UP_COLOR if close >= open_ else DOWN_COLOR)
            painter.setPen(pg.mkPen(color))
            painter.drawLine(pg.QtCore.QPointF(index, low), pg.QtCore.QPointF(index, high))
            painter.setBrush(pg.mkBrush(color))
            painter.drawRect(QRectF(index - width, open_, width * 2, close - open_))
        painter.end()

    def paint(self, painter, *args):
        painter.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        return QRectF(self._picture.boundingRect())


class PriceChartWidget(QWidget):
    # 縮放/平移到快超出目前已載入的歷史範圍時發出，帶要求的新天數，
    # 由外部(opening_tab.py)決定怎麼重新抓資料，圖表本身不碰網路。
    request_more_history = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self._dates: list[str] = []
        self._candles_by_index: dict[int, dict] = {}
        self._futures_by_index: dict[int, float] = {}
        self._level_lines: list[pg.InfiniteLine] = []
        self._day_boundary_price_lines: list[pg.InfiniteLine] = []
        self._day_boundary_volume_lines: list[pg.InfiniteLine] = []
        self._candlestick_item: _CandlestickItem | None = None
        self._futures_curve = None
        self._volume_item = None
        self._loading_more = False
        self._updating_y_range = False
        self._current_days = 0
        self._live_follow = False
        self._user_navigated = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.hover_label = QLabel(" ")
        layout.addWidget(self.hover_label)

        self._graphics_layout = pg.GraphicsLayoutWidget()
        layout.addWidget(self._graphics_layout)

        self._price_axis = _PriceAxisItem(orientation="left")
        self.price_plot = self._graphics_layout.addPlot(row=0, col=0, axisItems={"left": self._price_axis})
        self.price_plot.showGrid(x=True, y=True, alpha=0.2)
        self.price_plot.showAxis("bottom", False)
        self.price_plot.getViewBox().setMouseEnabled(x=True, y=False)

        self._date_axis = _DateAxisItem(orientation="bottom")
        self.volume_plot = self._graphics_layout.addPlot(row=1, col=0, axisItems={"bottom": self._date_axis})
        self.volume_plot.showGrid(x=True, y=True, alpha=0.2)
        self.volume_plot.getViewBox().setMouseEnabled(x=True, y=False)
        self.volume_plot.setXLink(self.price_plot)

        self._graphics_layout.ci.layout.setRowStretchFactor(0, 3)
        self._graphics_layout.ci.layout.setRowStretchFactor(1, 1)

        self._v_line_price = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(CROSSHAIR_COLOR, style=Qt.DashLine))
        self._h_line_price = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(CROSSHAIR_COLOR, style=Qt.DashLine))
        self._v_line_volume = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(CROSSHAIR_COLOR, style=Qt.DashLine))
        for line in (self._v_line_price, self._h_line_price):
            line.hide()
            self.price_plot.addItem(line, ignoreBounds=True)
        self._v_line_volume.hide()
        self.volume_plot.addItem(self._v_line_volume, ignoreBounds=True)

        self._graphics_layout.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.price_plot.getViewBox().sigRangeChanged.connect(self._on_range_changed)
        # sigRangeChanged 任何範圍變動(含我們自己呼叫 setXRange)都會發，
        # 拿來做 Y 軸自動縮放沒問題；但「靠近邊界要補歷史」只能用
        # sigRangeChangedManually——這個訊號只有滑鼠滾輪/拖曳這種「使用者
        # 真的動手操作」才會發，用它就不用再猜測「現在是不是使用者主動
        # 縮小範圍」，初次載入自動 fit 全部資料也不會誤觸發。
        self.price_plot.getViewBox().sigRangeChangedManually.connect(self._on_range_changed_manually)

    def set_live_follow(self, enabled: bool):
        """盤中有正在即時組的那根K棒(1分/5分/30分/日線)時呼叫這個開啟「最新
        那根K棒永遠待在畫面正中間」模式，跟原本「盡量保留使用者上次看的
        範圍」互斥——不然新K棒一直長出來，右邊會不斷超出視野，盤中盯著看
        反而要一直手動往右拉。切換週期/盤別時呼叫這個，等於重新開始置中
        (使用者上次手動拉走的狀態也一併重置)。"""
        self._live_follow = enabled
        self._user_navigated = False

    def _apply_live_follow_range(self):
        if not self._dates:
            return
        last_index = len(self._dates) - 1
        (x_min, x_max), _ = self.price_plot.getViewBox().viewRange()
        span = x_max - x_min
        if span <= 1:
            span = min(DEFAULT_LIVE_FOLLOW_BARS, last_index + 1) or 1
        half = span / 2
        self.price_plot.setXRange(last_index - half, last_index + half, padding=0)

    def set_price_data(self, taiex_history: list[dict], futures_history: list[dict], days: int):
        self._current_days = days

        # 補抓更多歷史後重畫，盡量讓使用者原本看的日期範圍留在畫面上，
        # 不要因為資料變多、索引跟著往後挪就整個跳走。
        preserved_range = self._current_view_dates()

        if self._candlestick_item is not None:
            self.price_plot.removeItem(self._candlestick_item)
            self._candlestick_item = None
        if self._futures_curve is not None:
            self.price_plot.removeItem(self._futures_curve)
            self._futures_curve = None
        if self._volume_item is not None:
            self.volume_plot.removeItem(self._volume_item)
            self._volume_item = None

        # x軸的日期清單要用兩條序列「聯集」，不能只用加權指數(taiex_history)
        # 自己的日期——加權指數沒有夜盤，全盤模式下台指期(futures_history)
        # 在15:00~次日05:00這段會有一堆加權指數完全沒有對應時間點的K棒，
        # 如果只用加權指數的日期建索引，這些台指期夜盤資料會因為在
        # date_to_index裡查無此日期而被整批捨棄(之前就是這樣被吃掉的)。
        # 聯集後排序，字串是"YYYY-MM-DD"或"YYYY-MM-DD HH:MM"這種左補零、
        # 定長的格式，字典序排序等於時間先後排序。加權指數在夜盤那些索引
        # 上本來就沒有K棒，蠟燭圖那段自然留空，台指期的線會照樣畫過去。
        self._dates = sorted({row["date"] for row in taiex_history} | {row["date"] for row in futures_history})
        self._date_axis.dates = self._dates
        date_to_index = {date: i for i, date in enumerate(self._dates)}
        self._rebuild_day_boundaries()

        self._candles_by_index = {}
        candles = []
        volume_x, volume_height, volume_brushes = [], [], []
        for row in taiex_history:
            o, h, l, c = row.get("open"), row.get("high"), row.get("low"), row.get("close")
            if None in (o, h, l, c):
                continue
            i = date_to_index[row["date"]]
            candles.append((i, o, h, l, c))
            self._candles_by_index[i] = row

            volume = row.get("volume")
            if volume is not None:
                volume_x.append(i)
                volume_height.append(volume)
                volume_brushes.append(pg.mkBrush(QColor(UP_COLOR if c >= o else DOWN_COLOR)))

        self._candlestick_item = _CandlestickItem(candles)
        self.price_plot.addItem(self._candlestick_item)

        if volume_x:
            self._volume_item = pg.BarGraphItem(
                x=volume_x, height=volume_height, width=0.6, brushes=volume_brushes,
            )
            self.volume_plot.addItem(self._volume_item)

        self._futures_by_index = {}
        futures_x, futures_y = [], []
        for row in futures_history:
            index = date_to_index.get(row["date"])
            if index is not None and row.get("close") is not None:
                futures_x.append(index)
                futures_y.append(row["close"])
                self._futures_by_index[index] = row["close"]
        self._futures_curve = self.price_plot.plot(
            futures_x, futures_y, pen=pg.mkPen(FUTURES_LINE_COLOR, width=2),
        )

        if self._live_follow and not self._user_navigated:
            self._apply_live_follow_range()
        elif preserved_range:
            start_date, end_date, overshoot_right, overshoot_left = preserved_range
            new_i0 = date_to_index.get(start_date, 0)
            new_i1 = date_to_index.get(end_date, len(self._dates) - 1)
            self.price_plot.setXRange(new_i0 - overshoot_left, new_i1 + overshoot_right, padding=0)
        else:
            self.price_plot.enableAutoRange(x=True)

        self._loading_more = False
        self._update_y_range()

    def _current_view_dates(self):
        """回傳目前視野對應的(起點日期, 終點日期, 右邊超出資料範圍多少,
        左邊超出資料範圍多少)。「即時跟隨」模式(_apply_live_follow_range)
        會故意讓右邊超出目前最後一根K棒、留白給接下來要長出來的新K棒——
        如果這裡把超出範圍的部分直接 clamp 掉、只記錄「有資料的最後一根」
        的日期，使用者手動拖曳離開跟隨模式後，下一次重畫(不管是即時跳動
        還是補歷史觸發的)換算回新資料時就會把這段留白弄丟，畫面看起來像
        突然「縮小、K棒變大」——所以超出範圍的量要另外記，換算新視野時
        要加回去，不能只留日期字串本身。"""
        if not self._dates:
            return None
        last_valid_index = len(self._dates) - 1
        (x_min, x_max), _ = self.price_plot.getViewBox().viewRange()
        i0 = max(0, min(last_valid_index, round(x_min)))
        i1 = max(0, min(last_valid_index, round(x_max)))
        overshoot_right = max(0.0, x_max - last_valid_index)
        overshoot_left = max(0.0, -x_min)
        return self._dates[i0], self._dates[i1], overshoot_right, overshoot_left

    def _update_y_range(self):
        """讓可見範圍內的 K 棒 (連同疊加的台指期線) 佔畫面中間 2/3，不要
        整條線被壓在畫面邊緣，也不要因為使用者滾輪/拖曳而跟著亂縮。"""
        if not self._dates or self._updating_y_range:
            return

        (x_min, x_max), _ = self.price_plot.getViewBox().viewRange()
        i0 = max(0, int(x_min))
        i1 = min(len(self._dates) - 1, int(x_max) + 1)

        values = []
        for i in range(i0, i1 + 1):
            candle = self._candles_by_index.get(i)
            if candle:
                values.append(candle["high"])
                values.append(candle["low"])
            futures_close = self._futures_by_index.get(i)
            if futures_close is not None:
                values.append(futures_close)

        volume_values = [
            self._candles_by_index[i]["volume"]
            for i in range(i0, i1 + 1)
            if self._candles_by_index.get(i) and self._candles_by_index[i].get("volume") is not None
        ]

        self._updating_y_range = True
        try:
            if values:
                low, high = min(values), max(values)
                span = high - low or max(abs(high), 1) * 0.02
                padding = span * Y_PADDING_RATIO
                self.price_plot.setYRange(low - padding, high + padding, padding=0)
            if volume_values:
                self.volume_plot.setYRange(0, max(volume_values) * 1.1, padding=0)
        finally:
            self._updating_y_range = False

    def set_levels(self, resistance_levels: list[float], support_levels: list[float]):
        for line in self._level_lines:
            self.price_plot.removeItem(line)
        self._level_lines.clear()

        for level in resistance_levels or []:
            self._add_level_line(level, RESISTANCE_COLOR)
        for level in support_levels or []:
            self._add_level_line(level, SUPPORT_COLOR)

        # 數字改顯示在 Y 軸上，不在圖裡疊文字標籤，才不會擠在一起看不清楚，
        # 顏色跟對應的水平線一致 (壓力橘色、支撐紫色)。
        level_colors = {level: RESISTANCE_COLOR for level in (resistance_levels or [])}
        level_colors.update({level: SUPPORT_COLOR for level in (support_levels or [])})
        self._price_axis.set_extra_levels(level_colors)

    def _add_level_line(self, level: float, color: str):
        line = pg.InfiniteLine(
            pos=level, angle=0, movable=False,
            pen=pg.mkPen(color, width=1.5, style=Qt.DashLine),
        )
        self.price_plot.addItem(line)
        self._level_lines.append(line)

    def clear_levels(self):
        self.set_levels([], [])

    def _rebuild_day_boundaries(self):
        """盤中週期(分/5分/30分)的x軸標籤是"YYYY-MM-DD HH:MM"，K棒是連續排
        列的索引，光看軸上偶爾出現的日期刻度很難看出「一天佔了幾根、現在
        這根是當天第幾根」，所以在每次換到新的一天那個位置畫一條直的換日
        虛線，貫穿價格圖跟成交量圖。日/週/月線的標籤沒有時間部分(沒有空
        白)，代表每一根本身就是一個完整週期，不需要換日線。"""
        for line in self._day_boundary_price_lines:
            self.price_plot.removeItem(line)
        for line in self._day_boundary_volume_lines:
            self.volume_plot.removeItem(line)
        self._day_boundary_price_lines.clear()
        self._day_boundary_volume_lines.clear()

        prev_trading_day = None
        for index, label in enumerate(self._dates):
            if " " not in label:
                return  # 日/週/月線，不需要換日線
            trading_day = self._trading_day_for_label(label)
            if prev_trading_day is not None and trading_day != prev_trading_day:
                self._add_day_boundary_line(index - 0.5)
            prev_trading_day = trading_day

    @staticmethod
    def _trading_day_for_label(label: str) -> datetime.date:
        """凌晨0點~8點這段算前一天的交易日(夜盤延續)，8點以後才算新的一
        天——不能直接拿calendar_date比較，不然午夜會把同一個連續的夜盤切
        成兩天。"""
        date_part, time_part = label.split(" ", 1)
        calendar_date = datetime.date.fromisoformat(date_part)
        hour = int(time_part.split(":", 1)[0])
        if hour < DAY_BOUNDARY_HOUR:
            return calendar_date - datetime.timedelta(days=1)
        return calendar_date

    def _add_day_boundary_line(self, pos: float):
        pen = pg.mkPen(DAY_BOUNDARY_COLOR, width=1, style=Qt.DashLine)
        price_line = pg.InfiniteLine(pos=pos, angle=90, movable=False, pen=pen)
        volume_line = pg.InfiniteLine(pos=pos, angle=90, movable=False, pen=pen)
        self.price_plot.addItem(price_line, ignoreBounds=True)
        self.volume_plot.addItem(volume_line, ignoreBounds=True)
        self._day_boundary_price_lines.append(price_line)
        self._day_boundary_volume_lines.append(volume_line)

    def _on_mouse_moved(self, scene_pos):
        in_price = self.price_plot.sceneBoundingRect().contains(scene_pos)
        in_volume = self.volume_plot.sceneBoundingRect().contains(scene_pos)
        if not (in_price or in_volume):
            self._hide_crosshair()
            return

        view_box = self.price_plot.getViewBox()
        point = view_box.mapSceneToView(scene_pos)
        index = round(point.x())
        if not (0 <= index < len(self._dates)):
            self._hide_crosshair()
            self.hover_label.setText(" ")
            return

        # 原本只看 self._candles_by_index 有沒有這個index，全盤(含夜盤)模式
        # 下加權指數沒有夜盤資料，那段索引在_candles_by_index裡本來就是空
        # 的，導致滑鼠移到夜盤那段(只有台指期的線、沒有蠟燭)時十字線跟資訊
        # 整個不顯示——只要蠟燭或台指期線任一邊在這個索引有資料就該顯示，
        # 不能只認蠟燭圖。
        row = self._candles_by_index.get(index)
        futures_close = self._futures_by_index.get(index)
        if row is None and futures_close is None:
            self._hide_crosshair()
            self.hover_label.setText(" ")
            return

        self._v_line_price.setPos(index)
        self._v_line_volume.setPos(index)
        self._v_line_price.show()
        self._v_line_volume.show()
        if in_price:
            self._h_line_price.setPos(point.y())
            self._h_line_price.show()
        else:
            self._h_line_price.hide()

        date_label = self._dates[index]
        if row is not None:
            text = f"{date_label}　開:{row['open']:g}　高:{row['high']:g}　低:{row['low']:g}　收:{row['close']:g}"
        else:
            text = f"{date_label}　(加權指數無資料，僅台指期夜盤)"
        if futures_close is not None:
            text += f"　台指期收:{futures_close:g}"
        self.hover_label.setText(text)

    def _hide_crosshair(self):
        self._v_line_price.hide()
        self._h_line_price.hide()
        self._v_line_volume.hide()

    def _on_range_changed(self, view_box, view_range, changed=None):
        """任何範圍變動都會觸發 (包含我們自己呼叫 setXRange/enableAutoRange)，
        只負責讓 Y 軸自動貼齊目前可見範圍，不做「補歷史」判斷——如果在這裡
        判斷，剛載入資料時 pyqtgraph 自動 fit 全部資料那次也會算進來，
        容易誤判成「使用者要看更早的資料」。"""
        self._update_y_range()

    def _on_range_changed_manually(self, mask):
        """只有滑鼠滾輪縮放/拖曳這種使用者真的動手操作才會觸發 (跟上面
        sigRangeChanged 不同，程式自己呼叫 setXRange 不會觸發這個)，
        用這個訊號判斷「要不要補更多歷史」就不用再猜測目前是不是使用者
        主動縮小範圍——不管是拖曳平移還是滾輪縮放，只要靠近目前已載入
        資料的左邊界就該補。使用者只要自己動手拉/縮過一次，就代表他現在
        想自己控制看哪一段，「即時跟隨最新K棒置中」模式(set_live_follow)
        就先讓路，不要每次新K棒進來又把畫面搶回去。"""
        self._user_navigated = True
        if self._loading_more or not self._dates or self._current_days >= MAX_CHART_DAYS:
            return
        (x_min, _x_max), _ = self.price_plot.getViewBox().viewRange()
        if x_min < EDGE_LOAD_THRESHOLD:
            self._loading_more = True
            self.request_more_history.emit(min(self._current_days * 2, MAX_CHART_DAYS))
