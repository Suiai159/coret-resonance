"""Small QPainter scope: raw storage stays intact; only paint points are reduced."""
from bisect import bisect_left, bisect_right
from .trace import Trace
from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


def envelope(indices, values, left, right, budget=1800):
    """Preserve extrema and break gaps, including gaps inside a decimation bucket."""
    if not indices:
        return []
    ordered = all(a < b for a, b in zip(indices, indices[1:]))
    if ordered:
        a, b = max(0, bisect_left(indices, left) - 1), min(len(indices), bisect_right(indices, right) + 1)
        points = list(zip(indices[a:b], values[a:b]))
    else:
        points = [(i, v) for i, v in zip(indices, values) if left <= i <= right]
    groups, group = [], []
    for point in points:
        if group and point[0] != group[-1][0] + 1:
            groups.append(group); group = []
        group.append(point)
    if group:
        groups.append(group)
    width = max(1, (len(points) + max(1, budget // 2) - 1) // max(1, budget // 2))
    result = []
    for group in groups:
        if width == 1:
            result.append(group)
            continue
        reduced = [group[0]]
        for start in range(0, len(group), width):
            block = group[start:start + width]
            reduced.extend(sorted(set((min(block, key=lambda x:x[1]), max(block, key=lambda x:x[1])))))
        reduced.append(group[-1])
        result.append(reduced)
    return result


class Waveform(QWidget):
    cursor_readout = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(460, 290)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.trace = Trace()
        self.indices, self.values = self.trace.indices, self.trace.values
        self.rate = 100000
        self.xmin, self.xmax = 0.0, 1000.0
        self.follow = True
        self.follow_count = 1000
        self.auto_y = False
        self.cursor = None
        self.drag_origin = None
        self.setToolTip('滚轮：以鼠标为中心缩放横轴；左键拖动：平移；双击：显示完整会话。横轴由样本序号计算。')

    def area(self):
        return QRectF(70, 30, max(10, self.width() - 96), max(10, self.height() - 80))

    def set_data(self, indices, values, rate=100000):
        self.trace = Trace()
        self.trace.append(indices, values)
        self.indices, self.values, self.rate = self.trace.indices, self.trace.values, rate
        if self.follow:
            self.follow_view()
        self.update()

    @property
    def extrema(self):
        return self.trace.minimum, self.trace.maximum

    def append_data(self, offset, indices, values, rate):
        if offset == 0:
            self.set_data(indices, values, rate)
            return
        if offset != len(self.indices):
            raise ValueError('显示增量不连续')
        self.trace.append(indices, values)
        self.rate = rate
        if self.follow: self.follow_view()
        self.update()

    def render_points(self, budget):
        if self.trace.contiguous:
            return [self.trace.points(self.xmin, self.xmax, budget)]
        # Corrupt/gapped captures remain visibly split rather than interpolated.
        return envelope(self.indices, self.values, self.xmin, self.xmax, budget)

    def follow_view(self):
        if not self.indices:
            self.xmin, self.xmax = 0.0, float(self.follow_count or 1000)
        else:
            end = float(self.trace.last)
            self.xmin = float(self.trace.first) if self.follow_count is None else max(float(self.trace.first), end - self.follow_count + 1)
            self.xmax = max(self.xmin + 20, end)

    def set_span(self, count):
        self.follow_count = count
        self.follow = True
        self.follow_view(); self.update()

    def fit(self):
        self.set_span(None)

    def zoom(self, factor, anchor=0.5):
        span = self.xmax - self.xmin
        full = max(1000, self.trace.last - self.trace.first + 1) if self.indices else 1000
        new_span = min(full * 1.1, max(20, span * factor))
        center = self.xmin + anchor * span
        self.xmin, self.xmax = center - anchor * new_span, center + (1 - anchor) * new_span
        self.follow = False
        self.update()

    def y_limits(self):
        if self.auto_y and self.values:
            lo, hi = self.extrema
            margin = max(100, (hi - lo) * .12)
            return max(0, lo - margin), min(65535, hi + margin)
        return 0, 65535

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor('#132735'))
        r = self.area()
        font = QFont('DejaVu Sans Mono', 9)
        p.setFont(font)
        lo, hi = self.y_limits()
        span = max(1, self.xmax - self.xmin)
        for j in range(5):
            y = r.top() + r.height() * j / 4
            p.setPen(QPen(QColor('#34505e' if j == 2 else '#243e4c'), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor('#93acb9'))
            p.drawText(QRectF(0, y - 10, 59, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f'{hi - (hi-lo)*j/4:,.0f}')
        for j in range(6):
            x = r.left() + r.width() * j / 5
            p.setPen(QPen(QColor('#243e4c'), 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QColor('#93acb9'))
            p.drawText(QRectF(max(0, min(self.width()-96, x-48)), r.bottom() + 8, 96, 20), Qt.AlignmentFlag.AlignCenter,
                       f'{(self.xmin+span*j/5)/self.rate:.4f}')
        p.setPen(QColor('#a7bdc9'))
        p.drawText(QPointF(16, 18), 'RAW / uint16')
        p.drawText(QPointF(max(0, self.width()-92), self.height()-9), '时间 / s')
        if not self.values:
            p.setFont(QFont('Noto Sans CJK SC', 12))
            p.setPen(QColor('#93acb9'))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, '连接数据源，然后点击「开始测量」\n也可以打开已保存的会话')
            return
        p.save(); p.setClipRect(r)
        # Dense long-window envelopes must use the raster fast path. A 1.6px
        # antialiased geometric stroke can monopolize the GIL for hundreds of
        # milliseconds. This changes drawing only, never stored samples.
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        trace_pen = QPen(QColor('#63dec4'), 1)
        trace_pen.setCosmetic(True)
        p.setPen(trace_pen)
        for segment in self.render_points(max(200, int(r.width())*2)):
            path = QPainterPath()
            for n, (i, v) in enumerate(segment):
                point = QPointF(r.left()+(i-self.xmin)/span*r.width(), r.bottom()-(v-lo)/max(1,hi-lo)*r.height())
                if n == 0:
                    path.moveTo(point)
                else:
                    path.lineTo(point)
            if len(segment) == 1:
                p.drawEllipse(path.currentPosition(), 2, 2)
            else:
                p.drawPath(path)
        if self.cursor is not None:
            x = self.cursor
            p.setPen(QPen(QColor('#9bbbc7'), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        p.restore()
        p.end()

    def wheelEvent(self, event):
        r = self.area()
        if r.contains(event.position()):
            self.zoom(0.8 if event.angleDelta().y() > 0 else 1.25, (event.position().x()-r.left())/r.width())
            event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.area().contains(event.position()):
            self.drag_origin = (event.position().x(), self.xmin, self.xmax)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def nearest_sample(self, index):
        """Fast cursor lookup for validated continuous traces; preserve corrupt-file fallback."""
        if not self.indices:
            return None
        if self.trace.contiguous:
            right = bisect_left(self.indices, index)
            candidates = (max(0, right-1), min(len(self.indices)-1, right))
            return min(candidates, key=lambda n: abs(self.indices[n]-index))
        return min(range(len(self.indices)), key=lambda n: abs(self.indices[n]-index))

    def mouseMoveEvent(self, event):
        r, x = self.area(), event.position().x()
        if self.drag_origin:
            origin, left, right = self.drag_origin
            delta = (x-origin)/r.width()*(right-left)
            self.xmin, self.xmax = left-delta, right-delta
            self.follow = False
        self.cursor = x if r.contains(event.position()) else None
        if self.indices and self.cursor is not None:
            index = self.xmin + (x-r.left())/r.width()*(self.xmax-self.xmin)
            n = self.nearest_sample(index)
            self.cursor_readout.emit(f'游标  {self.indices[n]/self.rate:.6f} s   |   {self.values[n]:,} RAW')
        self.update()

    def mouseReleaseEvent(self, event):
        self.drag_origin = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, event):
        self.fit()

    def leaveEvent(self, event):
        self.cursor = None
        self.update()
