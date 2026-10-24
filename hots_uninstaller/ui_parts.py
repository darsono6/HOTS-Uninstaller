from typing import Optional, Union

from PySide6.QtCore import (
    Qt, QRectF, QSize, QPoint, QEvent, QEasingCurve, QTimer, QAbstractAnimation,
    QPropertyAnimation, QSequentialAnimationGroup, QParallelAnimationGroup,
    Property, Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QWidget, QLineEdit, QToolButton, QCheckBox, QStyle, QStyleOptionButton,
    QFrame, QLabel, QHBoxLayout, QGraphicsOpacityEffect, QApplication,
)

from .constants import DARK, IS_LIGHT_THEME
from .icons import FIF, AppIcon, make_qicon


def theme_color(variant: str = "primary") -> QColor:
    h, s, v, _ = QColor(DARK["accent"]).getHsvF()
    if h < 0:
        h = 0.0
    if not IS_LIGHT_THEME:
        s *= 0.84
        v = 1.0
        if variant == "dark1":
            v *= 0.9
        elif variant == "dark2":
            s *= 0.977
            v *= 0.82
        elif variant == "dark3":
            s *= 0.95
            v *= 0.7
        elif variant == "light1":
            s *= 0.92
        elif variant == "light2":
            s *= 0.78
        elif variant == "light3":
            s *= 0.65
    else:
        if variant == "dark1":
            v *= 0.75
        elif variant == "dark2":
            s *= 1.05
            v *= 0.5
        elif variant == "dark3":
            s *= 1.1
            v *= 0.4
        elif variant == "light1":
            v *= 1.05
        elif variant == "light2":
            s *= 0.75
            v *= 1.05
        elif variant == "light3":
            s *= 0.65
            v *= 1.05
    return QColor.fromHsvF(h, min(s, 1.0), min(v, 1.0))


def _icon_color() -> str:
    return "#656565" if IS_LIGHT_THEME else "#ffffff"


def _build_qss():
    if IS_LIGHT_THEME:
        hover, pressed = "rgba(0, 0, 0, 9)", "rgba(0, 0, 0, 6)"
        edit = f"""
QLineEdit#hotsSearch {{
    color: black;
    background-color: rgba(255, 255, 255, 0.7);
    border: 1px solid rgba(0, 0, 0, 13);
    border-bottom: 1px solid rgba(0, 0, 0, 100);
    border-radius: 5px;
    padding: 0px 10px;
    selection-background-color: {theme_color("light1").name()};
}}
QLineEdit#hotsSearch:hover {{
    background-color: rgba(249, 249, 249, 0.5);
    border: 1px solid rgba(0, 0, 0, 13);
    border-bottom: 1px solid rgba(0, 0, 0, 100);
}}
QLineEdit#hotsSearch:focus {{
    border-bottom: 1px solid rgba(0, 0, 0, 13);
    background-color: white;
}}
QLineEdit#hotsSearch:disabled {{
    color: rgba(0, 0, 0, 92);
    background-color: rgba(249, 249, 249, 0.3);
    border: 1px solid rgba(0, 0, 0, 13);
    border-bottom: 1px solid rgba(0, 0, 0, 13);
}}
"""
        check = """
QCheckBox#hotsCheck { color: black; }
QCheckBox#hotsCheck:disabled { color: rgba(0, 0, 0, 0.36); }
"""
    else:
        hover, pressed = "rgba(255, 255, 255, 9)", "rgba(255, 255, 255, 6)"
        edit = f"""
QLineEdit#hotsSearch {{
    color: white;
    background-color: rgba(255, 255, 255, 0.0605);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-bottom: 1px solid rgba(255, 255, 255, 0.5442);
    border-radius: 5px;
    padding: 0px 10px;
    selection-background-color: {theme_color("primary").name()};
    selection-color: black;
}}
QLineEdit#hotsSearch:hover {{
    background: rgba(255, 255, 255, 0.0837);
}}
QLineEdit#hotsSearch:focus {{
    background: rgba(30, 30, 30, 0.7);
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}}
QLineEdit#hotsSearch:disabled {{
    color: rgba(255, 255, 255, 92);
    background-color: rgba(255, 255, 255, 0.0419);
    border: 1px solid rgba(255, 255, 255, 0.0698);
}}
"""
        check = """
QCheckBox#hotsCheck { color: white; }
QCheckBox#hotsCheck:disabled { color: rgba(255, 255, 255, 0.36); }
"""

    tool = f"""
QToolButton#hotsToolBtn {{
    background-color: transparent;
    border: none;
    border-radius: 4px;
    margin: 0;
}}
QToolButton#hotsToolBtn:hover {{ background-color: {hover}; }}
QToolButton#hotsToolBtn:pressed {{ background-color: {pressed}; }}
QToolButton#hotsToolBtn:disabled {{ background-color: transparent; }}
"""
    check_full = f"""
QCheckBox#hotsCheck {{
    spacing: 8px;
    min-width: 28px;
    min-height: 22px;
    outline: none;
    margin-left: 1px;
}}
QCheckBox#hotsCheck::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 1px solid transparent;
    background-color: transparent;
}}
{check}
"""
    return tool, edit, check_full


TOOLBTN_QSS, SEARCH_QSS, CHECK_QSS = _build_qss()


class IconWidget(QWidget):
    def __init__(self, icon=None, parent: Optional[QWidget] = None):
        if isinstance(icon, QWidget) and parent is None:
            parent, icon = icon, None
        super().__init__(parent)
        self._icon = icon

    def setIcon(self, icon):
        self._icon = icon
        self.update()

    def icon(self):
        return self._icon

    def sizeHint(self) -> QSize:
        return QSize(16, 16)

    def paintEvent(self, _event):
        icon = self._icon
        if icon is None:
            return
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        if isinstance(icon, AppIcon):
            icon.paint(p, QRectF(self.rect()), DARK["fg"])
        elif isinstance(icon, QIcon):
            icon.paint(p, self.rect())


class TransparentToolButton(QToolButton):
    def __init__(self, icon=None, parent: Optional[QWidget] = None):
        if isinstance(icon, QWidget) and parent is None:
            parent, icon = icon, None
        super().__init__(parent)
        self.setObjectName("hotsToolBtn")
        self.setStyleSheet(TOOLBTN_QSS)
        self.setCursor(Qt.PointingHandCursor)
        if icon is not None:
            self.setIcon(icon)

    def setIcon(self, icon):
        if isinstance(icon, AppIcon):
            size = self.iconSize().width() or 16
            icon = make_qicon(icon, DARK["fg"], (size,))
        super().setIcon(icon)


class SearchLineEdit(QLineEdit):
    searchSignal = Signal(str)
    clearSignal = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("hotsSearch")
        self.setStyleSheet(SEARCH_QSS)
        self.setFixedHeight(33)
        self.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.setFocusPolicy(Qt.ClickFocus)

        self.hBoxLayout = QHBoxLayout(self)
        self.hBoxLayout.setSpacing(3)
        self.hBoxLayout.setContentsMargins(4, 4, 4, 4)
        self.hBoxLayout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.clearButton = self._make_button(FIF.CLOSE, 29, 25)
        self.clearButton.hide()
        self.searchButton = self._make_button(FIF.SEARCH, 31, 23)
        self.hBoxLayout.addWidget(self.clearButton, 0, Qt.AlignRight)
        self.hBoxLayout.addWidget(self.searchButton, 0, Qt.AlignRight)

        self.setTextMargins(0, 0, 58, 0)

        self.clearButton.clicked.connect(self.clear)
        self.clearButton.clicked.connect(self.clearSignal)
        self.searchButton.clicked.connect(self.search)
        self.textChanged.connect(self._on_text_changed)

        from .widgets_qt import attach_line_edit_context_menu
        attach_line_edit_context_menu(self)

    @staticmethod
    def _make_button(icon: AppIcon, w: int, h: int) -> TransparentToolButton:
        btn = TransparentToolButton()
        btn.setFixedSize(w, h)
        btn.setIconSize(QSize(10, 10))
        btn.setIcon(make_qicon(icon, _icon_color(), (10,)))
        return btn

    def search(self):
        text = self.text().strip()
        if text:
            self.searchSignal.emit(text)
        else:
            self.clearSignal.emit()

    def _on_text_changed(self, text: str):
        self.clearButton.setVisible(bool(text) and self.hasFocus())

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.clearButton.setVisible(bool(self.text()))
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
            app.installEventFilter(self)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.clearButton.hide()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            try:
                gpos = event.globalPosition().toPoint()
            except AttributeError:
                gpos = event.globalPos()
            target = QApplication.widgetAt(gpos)
            if target is not self and (target is None or not self.isAncestorOf(target)):
                self.clearFocus()
        return False

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self.hasFocus():
            return
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        m = self.contentsMargins()
        w, h = self.width() - m.left() - m.right(), self.height()
        path = QPainterPath()
        path.addRoundedRect(QRectF(m.left(), h - 10, w, 10), 5, 5)
        cut = QPainterPath()
        cut.addRect(m.left(), h - 10, w, 8)
        p.fillPath(path.subtracted(cut), theme_color("primary"))


class CheckBox(QCheckBox):
    def __init__(self, text: Union[str, QWidget] = "", parent: Optional[QWidget] = None):
        if isinstance(text, QWidget) and parent is None:
            parent, text = text, ""
        super().__init__(text, parent)
        self.setObjectName("hotsCheck")
        self.setStyleSheet(CHECK_QSS)
        self._is_pressed = False
        self._is_hover = False

    def mousePressEvent(self, e):
        self._is_pressed = True
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._is_pressed = False
        super().mouseReleaseEvent(e)
        self.update()

    def enterEvent(self, e):
        self._is_hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._is_hover = False
        self.update()
        super().leaveEvent(e)

    def _colors(self):
        checked = self.isChecked()
        enabled = self.isEnabled()
        pressed, hover = self._is_pressed, self._is_hover
        clear = QColor(0, 0, 0, 0)

        if not IS_LIGHT_THEME:
            if not enabled:
                return (clear, QColor(255, 255, 255, 41)) if checked else (QColor(255, 255, 255, 41), clear)
            if checked:
                c = theme_color("dark2" if pressed else "dark1" if hover else "primary")
                return c, c
            if pressed:
                return QColor(255, 255, 255, 40), QColor(255, 255, 255, 18)
            if hover:
                return QColor(255, 255, 255, 141), QColor(255, 255, 255, 11)
            return QColor(255, 255, 255, 141), QColor(0, 0, 0, 26)

        if not enabled:
            return (clear, QColor(0, 0, 0, 56)) if checked else (QColor(0, 0, 0, 56), clear)
        if checked:
            c = theme_color("light2" if pressed else "light1" if hover else "primary")
            return c, c
        if pressed:
            return QColor(0, 0, 0, 69), QColor(0, 0, 0, 31)
        if hover:
            return QColor(0, 0, 0, 143), QColor(0, 0, 0, 13)
        return QColor(0, 0, 0, 122), QColor(0, 0, 0, 6)

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing)

        opt = QStyleOptionButton()
        opt.initFrom(self)
        rect = self.style().subElementRect(QStyle.SE_CheckBoxIndicator, opt, self)

        border, bg = self._colors()
        p.setPen(border)
        p.setBrush(bg)
        p.drawRoundedRect(rect, 4.5, 4.5)

        state = self.checkState()
        if state == Qt.Unchecked:
            return
        if not self.isEnabled():
            p.setOpacity(0.8)

        mark = QColor("#ffffff") if IS_LIGHT_THEME else QColor("#000000")
        pen = QPen(mark, 1.15, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        if state == Qt.Checked:
            path = QPainterPath()
            path.moveTo(x + 0.25 * w, y + 0.52 * h)
            path.lineTo(x + 0.42 * w, y + 0.69 * h)
            path.lineTo(x + 0.77 * w, y + 0.31 * h)
            p.drawPath(path)
        else:
            p.drawLine(QPoint(int(x + 0.3 * w), int(y + 0.5 * h)),
                       QPoint(int(x + 0.7 * w), int(y + 0.5 * h)))


class IndeterminateProgressBar(QWidget):
    _SEGMENT_OFFSETS = (0.0, 0.45)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._phase = 0.0
        self._ease = QEasingCurve(QEasingCurve.InOutCubic)
        self._ani = QPropertyAnimation(self, b"phase", self)
        self._ani.setDuration(2200)
        self._ani.setStartValue(0.0)
        self._ani.setEndValue(1.0)
        self._ani.setLoopCount(-1)
        self.setFixedHeight(4)

    def _get_phase(self) -> float:
        return self._phase

    def _set_phase(self, value: float):
        self._phase = value
        self.update()

    phase = Property(float, _get_phase, _set_phase)

    def showEvent(self, e):
        super().showEvent(e)
        self._ani.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._ani.stop()

    def _segment(self, t: float) -> tuple:
        head = self._ease.valueForProgress(min(1.0, t / 0.7))
        tail = self._ease.valueForProgress(max(0.0, (t - 0.3) / 0.7))
        return tail, head

    def paintEvent(self, e):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        radius = h / 2
        track = QColor(DARK["fg"])
        track.setAlpha(28)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, w, h), radius, radius)
        color = theme_color()
        for offset in self._SEGMENT_OFFSETS:
            tail, head = self._segment((self._phase + offset) % 1.0)
            x0, x1 = tail * w, head * w
            if x1 - x0 < 1:
                continue
            p.setBrush(color)
            p.drawRoundedRect(QRectF(x0, 0, x1 - x0, h), radius, radius)
        p.end()


class PhaseProgressBar(QWidget):
    _WEIGHTS = (0.30, 0.34, 0.28, 0.08)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._index = 0
        self._fraction: Optional[float] = None
        self._phase = 0.0
        self._ease = QEasingCurve(QEasingCurve.InOutCubic)
        self._ani = QPropertyAnimation(self, b"phase", self)
        self._ani.setDuration(2200)
        self._ani.setStartValue(0.0)
        self._ani.setEndValue(1.0)
        self._ani.setLoopCount(-1)
        self.setFixedHeight(4)

    def _get_phase(self) -> float:
        return self._phase

    def _set_phase(self, value: float):
        self._phase = value
        if self._fraction is None:
            self.update()

    phase = Property(float, _get_phase, _set_phase)

    def set_phase(self, index: int):
        index = max(0, min(len(self._WEIGHTS) - 1, index))
        if index != self._index:
            self._index = index
            self._fraction = None
            self.update()

    def set_fraction(self, fraction: Optional[float]):
        if fraction is not None:
            fraction = max(0.0, min(1.0, fraction))
        if fraction != self._fraction:
            self._fraction = fraction
            self.update()

    def showEvent(self, e):
        super().showEvent(e)
        self._ani.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._ani.stop()

    def paintEvent(self, e):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        radius = h / 2
        track = QColor(DARK["fg"])
        track.setAlpha(28)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, w, h), radius, radius)
        color = theme_color()
        start = sum(self._WEIGHTS[:self._index])
        slot = self._WEIGHTS[self._index]
        p.setBrush(color)
        if self._fraction is not None:
            end = start + slot * self._fraction
            p.drawRoundedRect(QRectF(0, 0, end * w, h), radius, radius)
        else:
            if start > 0:
                p.drawRoundedRect(QRectF(0, 0, start * w, h), radius, radius)
            slot_x, slot_w = start * w, slot * w
            p.setClipRect(QRectF(slot_x, 0, slot_w, h))
            for offset in IndeterminateProgressBar._SEGMENT_OFFSETS:
                t = (self._phase + offset) % 1.0
                head = self._ease.valueForProgress(min(1.0, t / 0.7))
                tail = self._ease.valueForProgress(max(0.0, (t - 0.3) / 0.7))
                x0, x1 = slot_x + tail * slot_w, slot_x + head * slot_w
                if x1 - x0 >= 1:
                    p.drawRoundedRect(QRectF(x0, 0, x1 - x0, h), radius, radius)
        p.end()


class IndeterminateProgressRing(QWidget):
    def __init__(self, parent: Optional[QWidget] = None, start: bool = True):
        super().__init__(parent)
        self._stroke = 6
        self._start_angle = -180
        self._span_angle = 0
        self._running = start

        self._start_ani1 = QPropertyAnimation(self, b"startAngle", self)
        self._start_ani1.setDuration(1000)
        self._start_ani1.setStartValue(0)
        self._start_ani1.setEndValue(450)
        self._start_ani2 = QPropertyAnimation(self, b"startAngle", self)
        self._start_ani2.setDuration(1000)
        self._start_ani2.setStartValue(450)
        self._start_ani2.setEndValue(1080)
        start_group = QSequentialAnimationGroup(self)
        start_group.addAnimation(self._start_ani1)
        start_group.addAnimation(self._start_ani2)

        self._span_ani1 = QPropertyAnimation(self, b"spanAngle", self)
        self._span_ani1.setDuration(1000)
        self._span_ani1.setStartValue(0)
        self._span_ani1.setEndValue(180)
        self._span_ani2 = QPropertyAnimation(self, b"spanAngle", self)
        self._span_ani2.setDuration(1000)
        self._span_ani2.setStartValue(180)
        self._span_ani2.setEndValue(0)
        span_group = QSequentialAnimationGroup(self)
        span_group.addAnimation(self._span_ani1)
        span_group.addAnimation(self._span_ani2)

        self._group = QParallelAnimationGroup(self)
        self._group.addAnimation(start_group)
        self._group.addAnimation(span_group)
        self._group.setLoopCount(-1)

        self.setFixedSize(80, 80)

    def _get_start_angle(self) -> int:
        return self._start_angle

    def _set_start_angle(self, angle: int):
        self._start_angle = angle
        self.update()

    def _get_span_angle(self) -> int:
        return self._span_angle

    def _set_span_angle(self, angle: int):
        self._span_angle = angle
        self.update()

    startAngle = Property(int, _get_start_angle, _set_start_angle)
    spanAngle = Property(int, _get_span_angle, _set_span_angle)

    def setStrokeWidth(self, w: int):
        self._stroke = w
        self.update()

    def strokeWidth(self) -> int:
        return self._stroke

    def start(self):
        self._running = True
        if self.isVisible():
            self._begin()

    def stop(self):
        self._running = False
        self._end()

    def _begin(self):
        self._start_angle = 0
        self._span_angle = 0
        self._group.start()

    def _end(self):
        self._group.stop()
        self._start_angle = 0
        self._span_angle = 0

    def showEvent(self, e):
        super().showEvent(e)
        if self._running:
            self._begin()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._group.stop()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing)
        cw = self._stroke
        w = min(self.height(), self.width()) - cw
        rc = QRectF(cw / 2, self.height() / 2 - w / 2, w, w)
        pen = QPen(theme_color("primary"), cw, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        start = -self._start_angle + 180
        p.drawArc(rc, (start % 360) * 16, -self._span_angle * 16)


class _BannerIcon(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(36, 36)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        FIF.INFO_FILLED.paint(p, QRectF(10, 10, 15, 15), theme_color("primary"))


class InfoBanner(QFrame):
    closedSignal = Signal()

    MARGIN_TOP = 24
    SLIDE_PX = 16

    def __init__(self, title: str, content: str, parent: QWidget,
                 closable: bool = True, duration: int = -1):
        super().__init__(parent)
        self.setObjectName("hotsInfoBanner")
        self._duration = duration

        light = IS_LIGHT_THEME
        fg = "black" if light else "white"
        border = "rgb(229, 229, 229)" if light else "rgb(29, 29, 29)"
        bg = "rgb(244, 244, 244)" if light else "rgb(39, 39, 39)"
        self.setStyleSheet(
            f"QFrame#hotsInfoBanner {{ border: 1px solid {border}; border-radius: 6px; background-color: {bg}; }}"
            f"QLabel {{ background-color: transparent; border: none; color: {fg}; font-size: 14px; }}"
        )

        self._icon = _BannerIcon()
        self._title = QLabel(title)
        self._title.setStyleSheet("font-weight: bold;")
        self._title.setVisible(bool(title))
        self._content = QLabel(content)
        self._content.setVisible(bool(content))

        self._close_btn = TransparentToolButton()
        self._close_btn.setFixedSize(36, 36)
        self._close_btn.setIconSize(QSize(12, 12))
        self._close_btn.setIcon(make_qicon(FIF.CLOSE, "#000000" if IS_LIGHT_THEME else "#ffffff", (12,)))
        self._close_btn.setVisible(closable)
        self._close_btn.clicked.connect(self.close)

        text_lay = QHBoxLayout()
        text_lay.setSizeConstraint(QHBoxLayout.SetMinimumSize)
        text_lay.setAlignment(Qt.AlignTop)
        text_lay.setContentsMargins(1, 8, 0, 8)
        text_lay.setSpacing(5)
        text_lay.addWidget(self._title, 1, Qt.AlignTop)
        text_lay.addSpacing(7)
        text_lay.addWidget(self._content, 1, Qt.AlignTop)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSizeConstraint(QHBoxLayout.SetMinimumSize)
        lay.setSpacing(0)
        lay.addWidget(self._icon, 0, Qt.AlignTop | Qt.AlignLeft)
        lay.addLayout(text_lay)
        lay.addSpacing(12)
        lay.addWidget(self._close_btn, 0, Qt.AlignTop | Qt.AlignLeft)

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._slide = None
        self._fade = None

        parent.installEventFilter(self)

    @classmethod
    def info(cls, title: str, content: str, parent: QWidget,
             closable: bool = True, duration: int = -1) -> "InfoBanner":
        bar = cls(title, content, parent, closable, duration)
        bar.show()
        return bar

    def _target_pos(self) -> QPoint:
        p = self.parentWidget()
        return QPoint((p.width() - self.width()) // 2, self.MARGIN_TOP)

    def showEvent(self, e):
        super().showEvent(e)
        self.adjustSize()
        self.raise_()
        end = self._target_pos()
        self.move(end.x(), end.y() - self.SLIDE_PX)

        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(200)
        self._slide.setEasingCurve(QEasingCurve.OutQuad)
        self._slide.setStartValue(self.pos())
        self._slide.setEndValue(end)

        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(200)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.finished.connect(self._drop_effect)

        self._slide.start()
        self._fade.start()

        if self._duration >= 0:
            QTimer.singleShot(self._duration, self.close)

    def _drop_effect(self):
        self.setGraphicsEffect(None)
        self._effect = None

    def eventFilter(self, obj, e):
        if obj is self.parentWidget() and e.type() in (QEvent.Resize, QEvent.WindowStateChange):
            if self._slide is None or self._slide.state() != QAbstractAnimation.Running:
                self.move(self._target_pos())
        return super().eventFilter(obj, e)

    def closeEvent(self, e):
        self.closedSignal.emit()
        self.deleteLater()
        e.ignore()
