import html

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor

from .icons import FIF
from .constants import DARK
from .widgets_qt import colored_svg_icon


HELP_ICON_DELAY_MS = 500
HELP_ICON_FADE_MS = 500
ROW_BG_FADE_MS = 220
HELP_POPUP_DELAY_MS = 250
_TICK_MS = 16


def details_tooltip_html(rows, badges=()) -> str:
    cells = "".join(
        f"<tr>"
        f"<td style='color:{DARK['fg2']}; padding:1px 14px 1px 0; white-space:nowrap;'>{html.escape(label)}:</td>"
        f"<td style='color:{DARK['fg']};'>{html.escape(value)}</td>"
        f"</tr>"
        for label, value in rows
    )
    table_html = f"<table cellspacing='2'>{cells}</table>"
    warn = "".join(
        f"<div style='color:{color}; margin-bottom:6px;'>{html.escape(text)}</div>"
        for color, text in badges
    )
    return warn + table_html


class HoverHelpDelegateMixin:
    def _init_hover_help(self, hover_alpha: float = 0.10):
        self._help_icon = colored_svg_icon(FIF.HELP, DARK["fg2"], sizes=(13,))

        # NOTE: accent_rgba() returns a CSS "rgba(r,g,b,a)" string. It works in
        # stylesheets (setStyleSheet), but QColor(...) in Python does not parse that
        # format and silently creates an invalid, black color. So the QColor is built
        # here directly from the components, so that the highlight is really in the
        # accent color.
        self._hover_bg = QColor(DARK["accent"])
        self._hover_bg.setAlphaF(hover_alpha)

        self._hover_row = -1
        self._bg_progress = 0.0
        self._bg_target = 0.0

        self._help_row = -1
        self._help_pending_row = -1
        self._help_progress = 0.0
        self._help_target = 0.0
        self._help_delay_timer = QTimer(self)
        self._help_delay_timer.setSingleShot(True)
        self._help_delay_timer.setInterval(HELP_ICON_DELAY_MS)
        self._help_delay_timer.timeout.connect(self._arm_help_fade)

        self._hover_view = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(_TICK_MS)
        self._hover_timer.timeout.connect(self._tick_hover)

    def set_hover_row(self, row: int, view):
        self._hover_view = view
        self._hover_row = row
        self._bg_target = 1.0 if row >= 0 else 0.0
        if not self._hover_timer.isActive():
            self._hover_timer.start()

    def set_name_hover(self, row: int, view):
        self._hover_view = view
        if row == self._help_row:
            return
        self._help_delay_timer.stop()
        self._help_row = row
        self._help_progress = 0.0
        self._help_target = 0.0
        if row < 0:
            return
        self._help_pending_row = row
        self._help_delay_timer.start()

    def _arm_help_fade(self):
        if self._help_pending_row != self._help_row:
            return
        self._help_target = 1.0
        if not self._hover_timer.isActive():
            self._hover_timer.start()

    @staticmethod
    def _ease_out(t: float) -> float:
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        return 1.0 - (1.0 - t) * (1.0 - t)

    def _tick_hover(self):
        bg_step = _TICK_MS / ROW_BG_FADE_MS
        help_step = _TICK_MS / HELP_ICON_FADE_MS
        moving = False

        if self._bg_progress < self._bg_target:
            self._bg_progress = min(self._bg_target, self._bg_progress + bg_step)
            moving = True
        elif self._bg_progress > self._bg_target:
            self._bg_progress = max(self._bg_target, self._bg_progress - bg_step)
            moving = True

        if self._help_progress < self._help_target:
            self._help_progress = min(self._help_target, self._help_progress + help_step)
            moving = True

        if not moving:
            self._hover_timer.stop()
        if self._hover_view is not None:
            self._hover_view.viewport().update()

    def help_icon_appearing(self, row: int) -> bool:
        return self._help_row == row and self._help_progress > 0.0

    def paint_row_hover(self, painter, rect, row: int):
        if row == self._hover_row and self._bg_progress > 0.0:
            c = QColor(self._hover_bg)
            c.setAlphaF(self._hover_bg.alphaF() * self._ease_out(self._bg_progress))
            painter.fillRect(rect, c)

    def paint_help_icon(self, painter, help_rect, row: int):
        if row == self._help_row and self._help_progress > 0.0:
            t = self._ease_out(self._help_progress)
            ring_pen_color = QColor(DARK["accent"])
            ring_pen_color.setAlphaF(0.55 * t)
            ring_pen = painter.pen()
            ring_pen.setColor(ring_pen_color)
            ring_pen.setWidthF(1.2)
            painter.setPen(ring_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(help_rect.adjusted(-1, -1, 1, 1))
            painter.setOpacity(t)
            self._help_icon.paint(painter, help_rect)
            painter.setOpacity(1.0)


class HoverHelpViewMixin:
    def _init_hover_help_view(self):
        self.on_show_help = None
        self._help_popup = None
        self._hover_help_row = -1
        self._pending_help_app = None
        self._pending_help_pos = None
        self._help_timer = QTimer(self)
        self._help_timer.setSingleShot(True)
        self._help_timer.setInterval(HELP_POPUP_DELAY_MS)
        self._help_timer.timeout.connect(self._show_pending_help)
        self.verticalScrollBar().valueChanged.connect(self._close_help_popup)

    def _popup_hit(self, pos) -> bool:
        if self._help_popup is None:
            return False
        try:
            return self._help_popup.geometry().contains(self.mapToGlobal(pos))
        except Exception:
            return False

    def _close_help_popup(self, *_args):
        self._help_timer.stop()
        self._hover_help_row = -1
        self._pending_help_app = None
        if self._help_popup is not None:
            self._help_popup.close()
            self._help_popup = None

    def _show_pending_help(self):
        if self._pending_help_app is not None and self.on_show_help:
            self._help_popup = self.on_show_help(self._pending_help_app, self._pending_help_pos)

    def hover_help_on_move(self, delegate, row: int, item, name_rect, help_rect, pos) -> bool:
        delegate.set_name_hover(row if (name_rect.contains(pos) or help_rect.contains(pos)) else -1, self)

        if help_rect.contains(pos) and delegate.help_icon_appearing(row):
            if row != self._hover_help_row:
                self._hover_help_row = row
                self._help_timer.stop()
                if self._help_popup is not None:
                    self._help_popup.close()
                    self._help_popup = None
                self._pending_help_app = item
                self._pending_help_pos = self.mapToGlobal(help_rect.bottomLeft())
                self._help_timer.start()
            return True
        return False

    def hover_help_on_click(self, delegate, row: int, item, help_rect, pos) -> bool:
        if help_rect.contains(pos) and delegate.help_icon_appearing(row):
            self._help_timer.stop()
            if item is not None and self.on_show_help:
                if self._help_popup is not None:
                    self._help_popup.close()
                self._pending_help_app = item
                self._help_popup = self.on_show_help(item, self.mapToGlobal(help_rect.bottomLeft()))
            return True
        return False
