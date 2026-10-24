import os
import subprocess
import webbrowser
from urllib.parse import quote

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QGridLayout, QWidget, QLabel, QScrollArea, QFrame,
    QListView, QAbstractItemView, QStyledItemDelegate,
)
from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QPointF, QRect, QSize,
    Qt, Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen

from ..icons import FIF
from ..constants import DARK, css_color, load_settings, save_settings
from ..i18n import T
from ..proc_utils import popen_no_busy_cursor
from ..widgets_qt import (
    HOTSPage, HOTSDialog, HOTSButton, HOTSSwitch, HOTSContextMenu, h_separator,
    attach_fluent_tip, paint_round_check, show_fluent_popup_at,
)
from ..hover_help import (
    HoverHelpDelegateMixin, HoverHelpViewMixin, details_tooltip_html,
)
from .summary_dialog import SummaryDialog, ConfirmDialog
from ..bg_tasks import start_bg_thread, is_shutting_down
from .. import core_junk as cj
from .. import core_startup as cs


def _fmt_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "0 B"
    kb = num_bytes / 1024.0
    if kb < 1024:
        return f"{kb:.0f} KB"
    mb = kb / 1024.0
    if mb < 1024:
        return f"{mb:.1f} MB"
    gb = mb / 1024.0
    return f"{gb:.2f} GB"


class _ScanSignals(QObject):
    done = Signal(list)


class _DeleteSignals(QObject):
    done = Signal(int, object, list, object)


class _JunkListModel(QAbstractListModel):
    HeaderRole = Qt.UserRole + 1
    ItemRole = Qt.UserRole + 2
    IndexRole = Qt.UserRole + 3
    CheckRole = Qt.UserRole + 4
    TextRole = Qt.UserRole + 5
    SizeTextRole = Qt.UserRole + 6

    selection_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list = []
        self._checked: list = []
        self._texts: list = []
        self._by_cat: dict = {}
        self._order: list = []
        self._counts: dict = {}
        self._headers: dict = {}
        self._collapsed: dict = {}
        self._rows: list = []

    def clear(self):
        self.set_items([], lambda _i: True, {})

    def set_items(self, items: list, is_checked_fn, collapsed: dict):
        self.beginResetModel()
        self._items = items
        self._checked = [bool(is_checked_fn(i)) for i in items]
        self._texts = []
        for item in items:
            text = item.path
            if item.kind == "special_recycle_bin" and item.item_count > 0:
                text += "  ·  " + T("junk_recycle_items", count=item.item_count)
            elif item.kind == "special_restore_points" and item.item_count > 0:
                text += "  ·  " + T("junk_restore_points_to_delete", count=item.item_count)
            self._texts.append(text)

        self._by_cat = {}
        for idx, item in enumerate(items):
            self._by_cat.setdefault(item.category_key, []).append(idx)
        self._order = [c.key for c in cj.CATEGORIES if c.key in self._by_cat]
        self._counts = {k: sum(1 for i in idxs if self._checked[i])
                        for k, idxs in self._by_cat.items()}
        self._headers = {}
        for key in self._order:
            cat = next(c for c in cj.CATEGORIES if c.key == key)
            idxs = self._by_cat[key]
            total = sum(items[i].size_bytes for i in idxs)
            self._headers[key] = {
                "key": key,
                "label": T(cat.label_key),
                "stats": T("junk_group_header", count=len(idxs), size=_fmt_size(total)),
            }
        self._collapsed = collapsed
        self._rows = self._build_rows()
        self.endResetModel()
        self.selection_changed.emit()

    def rebuild(self, collapsed: dict):
        self.beginResetModel()
        self._collapsed = collapsed
        self._rows = self._build_rows()
        self.endResetModel()

    def _build_rows(self) -> list:
        rows = []
        for key in self._order:
            rows.append(("header", key))
            if not self._collapsed.get(key, True):
                rows.extend(("item", i) for i in self._by_cat[key])
        return rows

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        kind, ref = self._rows[index.row()]
        if kind == "header":
            if role == self.HeaderRole:
                h = dict(self._headers[ref])
                h["collapsed"] = bool(self._collapsed.get(ref, True))
                h["state"] = self.group_state(ref)
                return h
            return None
        if role == self.ItemRole:
            return self._items[ref]
        if role == self.IndexRole:
            return ref
        if role == self.CheckRole:
            return self._checked[ref]
        if role == self.TextRole:
            return self._texts[ref]
        if role == self.SizeTextRole:
            return _fmt_size(self._items[ref].size_bytes)
        return None

    def group_state(self, key: str):
        total = len(self._by_cat.get(key, ()))
        checked = self._counts.get(key, 0)
        if checked == 0:
            return Qt.Unchecked
        return Qt.Checked if checked == total else Qt.PartiallyChecked

    def _emit_all_changed(self):
        if self._rows:
            self.dataChanged.emit(self.index(0), self.index(len(self._rows) - 1))
        self.selection_changed.emit()

    def toggle_item(self, idx: int):
        self._checked[idx] = not self._checked[idx]
        key = self._items[idx].category_key
        self._counts[key] += 1 if self._checked[idx] else -1
        self._emit_all_changed()

    def set_group_checked(self, key: str, checked: bool):
        for i in self._by_cat.get(key, ()):
            self._checked[i] = checked
        self._counts[key] = len(self._by_cat.get(key, ())) if checked else 0
        self._emit_all_changed()

    def toggle_group(self, key: str):
        self.set_group_checked(key, self.group_state(key) != Qt.Checked)

    def set_all_checked(self, checked: bool):
        self._checked = [checked] * len(self._items)
        self._counts = {k: (len(v) if checked else 0) for k, v in self._by_cat.items()}
        self._emit_all_changed()

    def checked_indices(self) -> list:
        return [i for i, c in enumerate(self._checked) if c]

    def checked_bytes(self) -> int:
        return sum(self._items[i].size_bytes for i, c in enumerate(self._checked) if c)

    def all_checked(self) -> bool:
        return bool(self._items) and all(self._checked)

    def item_count(self) -> int:
        return len(self._items)


class _JunkItemDelegate(HoverHelpDelegateMixin, QStyledItemDelegate):
    ROW_HEIGHT = 40
    HEADER_HEIGHT = 32
    CHECK_SIZE = 16
    CHECK_X = 12
    CHECK_ZONE = 40
    ITEM_INDENT = 18
    ARROW_ZONE = 22
    RIGHT_MARGIN = 14
    SIZE_W = 84
    HELP_SIZE = 20
    GAP = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._check_hover_row = -1
        self._init_hover_help()

    def set_check_hover(self, row: int, view):
        if row == self._check_hover_row:
            return
        self._check_hover_row = row
        view.viewport().update()

    def sizeHint(self, option, index):
        if index.data(_JunkListModel.HeaderRole) is not None:
            return QSize(1, self.HEADER_HEIGHT)
        return QSize(1, self.ROW_HEIGHT)

    _NAME_FONT = None

    @classmethod
    def _name_font(cls) -> QFont:
        if cls._NAME_FONT is None:
            f = QFont()
            f.setPointSize(10)
            cls._NAME_FONT = f
        return cls._NAME_FONT

    def check_rect(self, row_rect: QRect, indent: int = 0) -> QRect:
        return QRect(row_rect.left() + indent + self.CHECK_X,
                     row_rect.center().y() - self.CHECK_SIZE // 2,
                     self.CHECK_SIZE, self.CHECK_SIZE)

    def check_zone(self, row_rect: QRect, indent: int = 0) -> QRect:
        return QRect(row_rect.left(), row_rect.top(), self.CHECK_ZONE + indent, row_rect.height())

    def hit_rects(self, row_rect: QRect, name_text: str = ""):
        r = row_rect
        check_rect = self.check_rect(r, self.ITEM_INDENT)
        size_rect = QRect(r.right() - self.RIGHT_MARGIN - self.SIZE_W, r.top(),
                          self.SIZE_W, r.height())
        name_left = self.CHECK_ZONE + self.ITEM_INDENT
        max_name_right = size_rect.left() - self.GAP - self.HELP_SIZE - self.GAP
        max_name_width = max(0, max_name_right - name_left)

        fm = QFontMetrics(self._name_font())
        text_w = min(fm.horizontalAdvance(name_text), max_name_width) if name_text else 0
        help_left = min(name_left + text_w + self.GAP, max_name_right)
        help_rect = QRect(help_left, r.center().y() - self.HELP_SIZE // 2,
                          self.HELP_SIZE, self.HELP_SIZE)
        name_rect = QRect(name_left, r.top(), max_name_width, r.height())
        return check_rect, name_rect, help_rect, size_rect

    def _paint_check(self, painter: QPainter, rect: QRect, state, hover: bool = False):
        paint_round_check(painter, rect, state, hover)

    def _paint_header(self, painter: QPainter, option, header: dict, index_row: int = -1):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(option.rect, css_color(DARK["panel_bg"]))
        painter.setPen(css_color(DARK["border_faint"]))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        self._paint_check(painter, self.check_rect(option.rect), header["state"],
                          self._check_hover_row == index_row)

        font = painter.font()
        font.setPointSize(8)
        font.setBold(True)
        font.setItalic(False)
        painter.setFont(font)
        painter.setPen(QColor(DARK["fg2"]))

        stats_rect = option.rect.adjusted(0, 0, -self.RIGHT_MARGIN, 0)
        painter.drawText(stats_rect, Qt.AlignVCenter | Qt.AlignRight, header["stats"])
        stats_w = QFontMetrics(font).horizontalAdvance(header["stats"])

        acx = option.rect.left() + self.CHECK_ZONE + 9
        acy = option.rect.center().y()
        pen = QPen(QColor(DARK["fg2"]), 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        if header["collapsed"]:
            painter.drawPolyline([QPointF(acx - 2.5, acy - 5), QPointF(acx + 3, acy), QPointF(acx - 2.5, acy + 5)])
        else:
            painter.drawPolyline([QPointF(acx - 5, acy - 2.5), QPointF(acx, acy + 3), QPointF(acx + 5, acy - 2.5)])
        label_font = painter.font()
        label_font.setPointSize(9)
        painter.setFont(label_font)
        painter.setPen(QColor(DARK["accent"]))

        label = header['label']
        label_left = self.CHECK_ZONE + self.ARROW_ZONE
        label_w = max(0, option.rect.width() - label_left - self.RIGHT_MARGIN - stats_w - self.GAP)
        label = QFontMetrics(label_font).elidedText(label, Qt.ElideRight, label_w)
        painter.drawText(QRect(option.rect.left() + label_left, option.rect.top(),
                               label_w, option.rect.height()),
                         Qt.AlignVCenter | Qt.AlignLeft, label)
        painter.restore()

    @staticmethod
    def _split_path(text: str, special: bool):
        if special:
            return "", text
        stripped = text.rstrip("\\/")
        head, tail = os.path.split(stripped)
        if not tail:
            return "", text
        return head + os.sep if head else "", tail

    def paint(self, painter: QPainter, option, index):
        header = index.data(_JunkListModel.HeaderRole)
        if header is not None:
            self._paint_header(painter, option, header, index.row())
            return

        item = index.data(_JunkListModel.ItemRole)
        if item is None:
            super().paint(painter, option, index)
            return

        text = index.data(_JunkListModel.TextRole)
        checked = index.data(_JunkListModel.CheckRole)
        size_text = index.data(_JunkListModel.SizeTextRole)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        self.paint_row_hover(painter, option.rect, index.row())
        painter.setPen(css_color(DARK["border_faint"]))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        check_rect, name_rect, help_rect, size_rect = self.hit_rects(option.rect, text)
        self._paint_check(painter, check_rect, Qt.Checked if checked else Qt.Unchecked,
                          self._check_hover_row == index.row())

        font = painter.font()
        font.setPointSize(10)
        font.setItalic(False)
        font.setBold(False)
        painter.setFont(font)
        fm = QFontMetrics(font)

        special = item.kind in ("special_recycle_bin", "special_restore_points")
        dir_part, base = self._split_path(text, special)
        avail = name_rect.width()
        base_w = fm.horizontalAdvance(base)
        dir_w = fm.horizontalAdvance(dir_part)
        if not dir_part:
            painter.setPen(QColor(DARK["fg"]))
            painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft,
                             fm.elidedText(base, Qt.ElideMiddle, avail))
        else:
            if dir_w + base_w > avail:
                dir_part = fm.elidedText(dir_part, Qt.ElideMiddle, max(0, avail - base_w))
                dir_w = fm.horizontalAdvance(dir_part)
            if base_w >= avail:
                painter.setPen(QColor(DARK["fg"]))
                painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft,
                                 fm.elidedText(base, Qt.ElideMiddle, avail))
            else:
                painter.setPen(QColor(DARK["fg2"]))
                painter.drawText(QRect(name_rect.left(), name_rect.top(), dir_w, name_rect.height()),
                                 Qt.AlignVCenter | Qt.AlignLeft, dir_part)
                painter.setPen(QColor(DARK["fg"]))
                painter.drawText(QRect(name_rect.left() + dir_w, name_rect.top(),
                                       avail - dir_w, name_rect.height()),
                                 Qt.AlignVCenter | Qt.AlignLeft, base)

        self.paint_help_icon(painter, help_rect, index.row())

        size_font = painter.font()
        size_font.setPointSize(8)
        painter.setFont(size_font)
        painter.setPen(QColor(DARK["fg2"]))
        painter.drawText(size_rect, Qt.AlignVCenter | Qt.AlignRight, size_text)

        painter.restore()


class _JunkListView(HoverHelpViewMixin, QListView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WA_Hover, True)
        self.setUniformItemSizes(False)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setFrameShape(QListView.NoFrame)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setCursor(Qt.ArrowCursor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.on_toggle_item = None
        self.on_toggle_group_check = None
        self.on_toggle_group = None
        self.on_context_menu = None
        self._init_hover_help_view()

        self.empty_label = QLabel("", self.viewport())
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet(
            f"color: {DARK['fg2']}; font-size: 9pt; background: transparent; padding: 24px;"
        )
        self.empty_label.setVisible(False)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu_requested)

    def setModel(self, model):
        super().setModel(model)
        model.modelAboutToBeReset.connect(self._on_model_about_to_reset)

    def _on_model_about_to_reset(self):
        self._close_help_popup()
        self.itemDelegate().set_hover_row(-1, self)
        self.itemDelegate().set_name_hover(-1, self)
        self.itemDelegate().set_check_hover(-1, self)

    def set_empty_text(self, text: str):
        self.empty_label.setText(text)

    def show_empty(self, show: bool):
        self.empty_label.setVisible(show)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.empty_label.setGeometry(self.viewport().rect())

    def leaveEvent(self, event):
        self._close_help_popup()
        self.itemDelegate().set_hover_row(-1, self)
        self.itemDelegate().set_name_hover(-1, self)
        self.itemDelegate().set_check_hover(-1, self)
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        index = self.indexAt(pos)
        delegate = self.itemDelegate()

        if index.isValid() and index.data(_JunkListModel.HeaderRole) is not None:
            delegate.set_hover_row(-1, self)
            delegate.set_name_hover(-1, self)
            delegate.set_check_hover(
                index.row() if delegate.check_zone(self.visualRect(index)).contains(pos) else -1, self)
            self.setCursor(Qt.PointingHandCursor)
            if self._hover_help_row != -1 and not self._popup_hit(pos):
                self._close_help_popup()
            super().mouseMoveEvent(event)
            return

        delegate.set_hover_row(index.row() if index.isValid() else -1, self)

        over_help = False
        over_check = False
        if index.isValid():
            item = index.data(_JunkListModel.ItemRole)
            text = index.data(_JunkListModel.TextRole) or ""
            rect = self.visualRect(index)
            check_rect, name_rect, help_rect, size_rect = delegate.hit_rects(rect, text)
            over_help = self.hover_help_on_move(delegate, index.row(), item, name_rect, help_rect, pos)
            over_check = delegate.check_zone(rect, delegate.ITEM_INDENT).contains(pos)
            delegate.set_check_hover(index.row() if over_check else -1, self)
            self.setCursor(Qt.PointingHandCursor if (over_help or over_check) else Qt.ArrowCursor)
        else:
            delegate.set_name_hover(-1, self)
            delegate.set_check_hover(-1, self)
            self.setCursor(Qt.ArrowCursor)

        if not over_help and self._hover_help_row != -1 and not self._popup_hit(pos):
            self._close_help_popup()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        index = self.indexAt(pos)
        if index.isValid() and event.button() == Qt.LeftButton:
            delegate = self.itemDelegate()
            rect = self.visualRect(index)
            header = index.data(_JunkListModel.HeaderRole)
            if header is not None:
                if delegate.check_zone(rect).contains(pos):
                    if self.on_toggle_group_check:
                        self.on_toggle_group_check(header["key"])
                elif self.on_toggle_group:
                    self.on_toggle_group(header["key"])
                return

            item = index.data(_JunkListModel.ItemRole)
            text = index.data(_JunkListModel.TextRole) or ""
            check_rect, name_rect, help_rect, size_rect = delegate.hit_rects(rect, text)
            if delegate.check_zone(rect, delegate.ITEM_INDENT).contains(pos):
                self._close_help_popup()
                if self.on_toggle_item:
                    self.on_toggle_item(index.data(_JunkListModel.IndexRole))
                return
            if self.hover_help_on_click(delegate, index.row(), item, help_rect, pos):
                return

        super().mouseReleaseEvent(event)

    def _on_context_menu_requested(self, pos):
        index = self.indexAt(pos)
        idx = None
        if index.isValid() and index.data(_JunkListModel.HeaderRole) is None:
            idx = index.data(_JunkListModel.IndexRole)
        if self.on_context_menu:
            self.on_context_menu(idx, self.mapToGlobal(pos))


def _details_tooltip(item: "cj.JunkItem") -> str:
    cat = next((c for c in cj.CATEGORIES if c.key == item.category_key), None)
    rows = [(T("junk_col_path"), item.path)]
    if cat is not None:
        rows.append((T("junk_col_category"), T(cat.label_key)))
    rows.append((T("junk_col_size"), _fmt_size(item.size_bytes)))
    badges = []
    if cj.is_large_log(item):
        badges.append((DARK["fg2"], T("junk_tip_large_log")))
    return details_tooltip_html(rows, badges)


class JunkPage(HOTSPage):
    def __init__(self, parent=None):
        super().__init__("junkInterface", FIF.BROOM if hasattr(FIF, "BROOM") else FIF.DELETE,
                          T("junk_title"), parent)
        self._items: list = []
        self._category_checks: dict = {}
        self._settings_open = False
        self._collapsed: dict = {}
        self._last_delete_recycle = False
        self._last_delete_had_restore_points = False
        self._last_delete_only_restore_points = False
        self._build()

    def _card_style(self) -> str:
        return (f"background: {DARK['panel_bg']}; "
                f"border: 1px solid {DARK['border_faint']}; border-radius: 6px;")

    def _build(self):
        cl = self.content_layout

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        cl_body = QVBoxLayout(body)
        cl_body.setContentsMargins(0, 0, 0, 0)
        cl_body.setSpacing(8)
        cl.addWidget(body, 1)

        self._categories_panel = self._build_categories_panel()

        cl_body.addWidget(self._build_top_card())
        summary_bar = self._build_summary_bar()
        cl_body.addWidget(summary_bar)

        self.model = _JunkListModel(self)
        self.model.selection_changed.connect(self._on_selection_changed)

        self.view = _JunkListView(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(_JunkItemDelegate(self.view))
        self.view.set_empty_text(T("junk_none_found"))
        self.view.on_toggle_item = self.model.toggle_item
        self.view.on_toggle_group_check = self.model.toggle_group
        self.view.on_toggle_group = self._toggle_group
        self.view.on_show_help = self._show_help_popup
        self.view.on_context_menu = self._show_context_menu
        self.view.setStyleSheet(
            f"QListView {{ background-color: {DARK['table_bg']}; "
            f"border: 1px solid {DARK['border_soft']}; border-radius: 6px; }}"
        )
        self._summary_bar = summary_bar
        self._bottom_bar = self._build_bottom_bar()
        cl_body.addWidget(self.view, 1)
        cl_body.addWidget(self._bottom_bar)

        self._settings_view = self._build_settings_view()
        cl_body.addWidget(self._settings_view, 1)

    def _build_settings_view(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setStyleSheet("background: transparent;")

        card = QWidget()
        card.setObjectName("junkSettingsCard")
        card.setStyleSheet(
            f"QWidget#junkSettingsCard {{ background: {DARK['panel_bg']}; "
            f"border: 1px solid {DARK['border_faint']}; border-radius: 6px; }}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        self._categories_panel.setParent(card)
        lay.addWidget(self._categories_panel)
        lay.addStretch()
        scroll.setWidget(card)
        scroll.hide()
        return scroll

    def _build_categories_panel(self) -> QWidget:
        frame = QWidget()
        frame.setStyleSheet("background: transparent;")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(6)
        saved = load_settings().get("junk_categories", {})
        if not isinstance(saved, dict):
            saved = {}
        for i, cat in enumerate(cj.CATEGORIES):
            cb = HOTSSwitch(T(cat.label_key))
            saved_state = saved.get(cat.key)
            cb.setChecked(saved_state if (cat.persist and isinstance(saved_state, bool)) else cat.default_on)
            cb.stateChanged.connect(self._on_category_toggled)
            self._category_checks[cat.key] = cb
            grid.addWidget(cb, i, 0)
        outer.addLayout(grid)

        drives = cj.list_fixed_drives()
        self._drive_checks: dict = {}
        if len(drives) > 1:
            outer.addWidget(h_separator())
            header = QLabel(T("junk_settings_drives_header"))
            header.setStyleSheet(
                f"color: {DARK['fg2']}; font-size: 8pt; font-weight: 600; background: transparent;"
            )
            outer.addWidget(header)

            selected = cj.get_selected_drives()
            if selected is not None and not any(d in selected for d in drives):
                selected = None
                cj.set_selected_drives(None)
            drive_grid = QGridLayout()
            drive_grid.setHorizontalSpacing(20)
            drive_grid.setVerticalSpacing(4)
            for i, letter in enumerate(drives):
                cb = HOTSSwitch(f"{letter}:")
                cb.setChecked(selected is None or letter in selected)
                cb.stateChanged.connect(self._on_drive_toggled)
                self._drive_checks[letter] = cb
                drive_grid.addWidget(cb, i, 0)
            outer.addLayout(drive_grid)

        outer.addWidget(h_separator())
        mode_header = QLabel(T("junk_opt_delete_header"))
        mode_header.setStyleSheet(
            f"color: {DARK['fg2']}; font-size: 8pt; font-weight: 600; background: transparent;"
        )
        outer.addWidget(mode_header)
        self._recycle_check = HOTSSwitch(T("junk_opt_recycle"))
        self._recycle_check.setChecked(load_settings().get("junk_delete_to_recycle") is True)
        self._recycle_check.stateChanged.connect(self._on_delete_mode_toggled)
        outer.addWidget(self._recycle_check)
        mode_hint = QLabel(T("junk_opt_recycle_hint"))
        mode_hint.setWordWrap(True)
        mode_hint.setMaximumWidth(520)
        mode_hint.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8pt; background: transparent;")
        outer.addWidget(mode_hint)

        self._sync_settings_state()
        return frame

    def _sync_settings_state(self):
        rb = self._category_checks.get("recycle_bin")
        bin_on = rb is not None and rb.isChecked()

        self._recycle_check.blockSignals(True)
        if bin_on:
            self._recycle_check.setChecked(False)
        else:
            self._recycle_check.setChecked(load_settings().get("junk_delete_to_recycle") is True)
        self._recycle_check.setEnabled(not bin_on)
        self._recycle_check.blockSignals(False)

        for cb in self._drive_checks.values():
            cb.setEnabled(bin_on)

    def _on_delete_mode_toggled(self, _state=None):
        fresh = load_settings()
        fresh["junk_delete_to_recycle"] = self._recycle_check.isChecked()
        save_settings(fresh)

    def _on_category_toggled(self, _state=None):
        self._sync_settings_state()
        fresh = load_settings()
        fresh["junk_categories"] = {
            cat.key: self._category_checks[cat.key].isChecked()
            for cat in cj.CATEGORIES
            if cat.persist and cat.key in self._category_checks
        }
        save_settings(fresh)

    def _on_drive_toggled(self, _state=None):
        checked = [letter for letter in self._drive_checks if self._drive_checks[letter].isChecked()]
        if not checked:
            sender = self.sender()
            if sender is not None:
                sender.blockSignals(True)
                sender.setChecked(True)
                sender.blockSignals(False)
            return
        if checked == list(self._drive_checks.keys()):
            cj.set_selected_drives(None)
        else:
            cj.set_selected_drives(checked)

    def _build_top_card(self) -> QWidget:
        frame = QWidget()
        frame.setStyleSheet(self._card_style())
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(14, 12, 12, 12)
        lay.setSpacing(12)

        self._desc_label = QLabel(T("junk_subheader"))
        self._desc_label.setWordWrap(True)
        self._desc_label.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent; border: none;")
        lay.addWidget(self._desc_label, 1)

        scan_btn = HOTSButton(FIF.SEARCH, "#60c8ff", T("btn_scan"), accent=True)
        scan_btn.fit_to_content()
        scan_btn.clicked.connect(self._scan_async)
        lay.addWidget(scan_btn, 0, Qt.AlignTop)

        settings_btn = HOTSButton(FIF.SETTING, DARK["fg2"], "")
        settings_btn.setFixedWidth(44)
        settings_btn.clicked.connect(self._toggle_settings)
        attach_fluent_tip(settings_btn, T("btn_junk_settings"))
        lay.addWidget(settings_btn, 0, Qt.AlignTop)
        self._settings_btn = settings_btn

        return frame

    def _build_summary_bar(self) -> QWidget:
        frame = QWidget()
        frame.setStyleSheet(self._card_style())
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(10, 6, 10, 6)

        self.status_label = QLabel(T("junk_scan_first_msg"))
        self.status_label.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8pt; background: transparent; border: none;")
        lay.addWidget(self.status_label)

        lay.addStretch()

        self.found_label = QLabel("")
        self.found_label.setStyleSheet(f"color: {DARK['accent']}; font-size: 8pt; background: transparent; border: none;")
        lay.addWidget(self.found_label)

        return frame

    def _build_bottom_bar(self) -> QWidget:
        frame = QWidget()
        frame.setStyleSheet(self._card_style())
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        toggle_btn = HOTSButton(FIF.ACCEPT, DARK["fg2"], T("btn_select_all"))
        toggle_btn.fit_to_content()
        toggle_btn.clicked.connect(self._on_toggle_select_all)
        lay.addWidget(toggle_btn)
        self._toggle_btn = toggle_btn

        delete_btn = HOTSButton(FIF.DELETE, DARK["red"], T("btn_delete_selected"), accent=True)
        delete_btn.fit_to_content()
        delete_btn.clicked.connect(self._delete_selected)
        lay.addWidget(delete_btn)
        self._delete_btn = delete_btn

        lay.addStretch()

        self.selected_label = QLabel("")
        self.selected_label.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8pt; background: transparent; border: none;")
        lay.addWidget(self.selected_label)

        self._update_select_toggle()

        return frame

    def _toggle_settings(self):
        self._set_settings_open(not self._settings_open)

    def _set_settings_open(self, opened: bool):
        if opened == self._settings_open:
            return
        self._settings_open = opened
        self._settings_btn.set_accent(opened)
        self._desc_label.setText(T("junk_settings_subheader") if opened else T("junk_subheader"))
        self._settings_view.setVisible(opened)
        for w in (self._summary_bar, self.view, self._bottom_bar):
            w.setVisible(not opened)

    def _selected_categories(self) -> list:
        return [key for key, cb in self._category_checks.items() if cb.isChecked()]

    def _scan_async(self):
        # begin_busy() is only a counter + spinner and doesn't block the buttons.
        # Without this guard a second "Scan" click started a parallel scan, and
        # "Delete selected" during a long deletion (e.g. Windows.old) started a
        # second thread deleting the same items: doubled summaries, doubly
        # counted recovered space, and two threads stopping/starting the same
        # services at once.
        if self.is_busy():
            return
        self._set_settings_open(False)
        categories = self._selected_categories()
        if not categories:
            HOTSDialog.info(self, T("app_title"), T("junk_no_categories_msg"))
            return

        self.begin_busy()
        self.model.clear()
        self.view.show_empty(False)
        self.status_label.setText(T("junk_scanning_msg"))
        self.found_label.setText("")
        self._delete_btn.setEnabled(False)

        signals = _ScanSignals(self)
        signals.done.connect(self._on_scanned)
        self._scan_signals = signals

        def worker():
            try:
                items = cj.scan_junk(categories)
            except Exception:
                items = []
                cj.mark_scan_failed(list(categories))
            if not is_shutting_down():
                signals.done.emit(items)

        start_bg_thread(worker)

    def _on_scanned(self, items: list):
        self.end_busy()
        if is_shutting_down():
            return
        self._items = items
        self._delete_btn.setEnabled(bool(items))

        self._collapsed = {}
        self.model.set_items(items, cj.is_checked_by_default, self._collapsed)
        self.view.show_empty(not items)

        total_found = sum(i.size_bytes for i in items)
        self.found_label.setText(T("junk_found_summary", count=len(items), size=_fmt_size(total_found)))

        parts = []
        failed = cj.get_failed_category_count()
        if not items and not failed:
            parts.append(T("junk_none_found"))
        if failed:
            parts.append(T("junk_note_scan_failed", count=failed))
        if "stray_truncated" in cj.get_scan_notes():
            parts.append(T("junk_note_scan_truncated"))
        skipped = cj.get_skipped_in_use_count()
        if skipped:
            parts.append(T("junk_note_in_use_skipped", count=skipped))
        running = sorted({i.browser for i in items if i.browser})
        if running:
            parts.append(T("junk_note_browser_running", names=", ".join(running)))
        self.status_label.setText("  ".join(parts))
        self._update_total_label()
        self._update_select_toggle()

    def _toggle_group(self, key: str):
        self._collapsed[key] = not self._collapsed.get(key, True)
        sb = self.view.verticalScrollBar()
        pos = sb.value()
        self.model.rebuild(self._collapsed)
        self.view.doItemsLayout()
        sb.setValue(min(pos, sb.maximum()))

    def _on_selection_changed(self):
        self._update_total_label()
        self._update_select_toggle()

    def _show_help_popup(self, item, global_pos):
        return show_fluent_popup_at(global_pos, _details_tooltip(item), width=420)

    def refresh_content(self):
        pass

    def _checked_rows(self) -> list:
        return self.model.checked_indices()

    def _update_total_label(self):
        count = self.model.item_count()
        if not count:
            self.selected_label.setText("")
            return
        rows = self.model.checked_indices()
        self.selected_label.setText(
            T("junk_selected_label", size=_fmt_size(self.model.checked_bytes()))
            + f"  ·  {len(rows)}/{count}")

    def _set_all_checked(self, checked: bool):
        self.model.set_all_checked(checked)

    def _on_toggle_select_all(self):
        self._set_all_checked(not self.model.all_checked())

    def _update_select_toggle(self):
        if not hasattr(self, "_toggle_btn"):
            return
        all_checked = self.model.all_checked()
        self._toggle_btn.set_accent(all_checked)
        if all_checked:
            self._toggle_btn.set_icon(FIF.CLOSE, DARK["fg2"])
            self._toggle_btn.set_label(T("btn_clear_selection"))
        else:
            self._toggle_btn.set_icon(FIF.ACCEPT, DARK["fg2"])
            self._toggle_btn.set_label(T("btn_select_all"))
        self._toggle_btn.fit_to_content()
        self._toggle_btn.setEnabled(bool(self.model.item_count()))

    def _delete_selected(self):
        if self.is_busy():
            return
        rows = self._checked_rows()
        if not rows:
            HOTSDialog.info(self, T("app_title"), T("junk_no_items_selected_msg"))
            return

        selected = [self._items[r] for r in rows]
        recycle = self._recycle_check.isChecked()
        mode = cj.DELETE_RECYCLE if recycle else cj.DELETE_PERMANENT
        self._last_delete_recycle = recycle
        self._last_delete_had_restore_points = any(i.category_key == "restore_points" for i in selected)
        self._last_delete_only_restore_points = all(i.category_key == "restore_points" for i in selected)
        total_size = sum(max(0, int(i.size_bytes or 0)) for i in selected)
        msg = T("junk_confirm_recycle_msg" if recycle else "junk_confirm_msg",
                count=len(selected), size=_fmt_size(total_size))
        extra_notes = [T("junk_confirm_auto_note")]
        if any(i.kind == "special_recycle_bin" for i in selected):
            extra_notes.append(T("junk_confirm_recycle_note"))
        if any(i.category_key == "windows_old" for i in selected):
            extra_notes.append(T("junk_confirm_windows_old_note"))
            if recycle:
                extra_notes.append(T("junk_confirm_windows_old_permanent_note"))
        if any(i.category_key == "restore_points" for i in selected):
            extra_notes.append(T("junk_confirm_restore_points_note"))
        if recycle and any(i.category_key in cj.REGENERABLE_CACHE_CATEGORIES for i in selected):
            extra_notes.append(T("junk_confirm_cache_permanent_note"))
        if any(i.category_key == "stray_temp_files" for i in selected):
            extra_notes.append(T("junk_confirm_stray_note"))
        if any(i.category_key == "privacy_history" for i in selected):
            extra_notes.append(T("junk_confirm_privacy_note"))
        running_browsers = tuple(sorted({i.browser for i in selected if i.browser}))
        if running_browsers:
            extra_notes.append(" " + T("junk_note_browser_running", names=", ".join(running_browsers)))
        note = "".join(extra_notes).strip() or None
        if not ConfirmDialog.ask(self, T("junk_title"), msg, note=note, permanent=not recycle):
            return

        self.begin_busy()
        signals = _DeleteSignals(self)
        signals.done.connect(self._on_delete_done)
        self._delete_signals = signals

        def worker():
            try:
                to_delete = selected
                pre_errors: list[str] = []
                if running_browsers:
                    still_running = cj.close_browsers(running_browsers)
                    fresh = cj.rescan_browser_history(running_browsers)
                    to_delete = [i for i in selected if i.browser not in running_browsers]
                    for item in fresh:
                        if item.browser in still_running:
                            pre_errors.append(item.path)
                        else:
                            to_delete.append(item)
                ok_count, freed, errors, recycled = cj.delete_junk_items(to_delete, mode)
                errors = pre_errors + errors
            except Exception as e:
                ok_count, freed, errors, recycled = 0, 0, [str(e)], 0
            if not is_shutting_down():
                signals.done.emit(ok_count, freed, errors, recycled)

        start_bg_thread(worker)

    def _on_delete_done(self, ok_count: int, freed_bytes: int, errors: list, recycled_bytes: int = 0):
        self.end_busy()
        if is_shutting_down():
            return

        if ok_count or errors:
            notes = []
            if errors:
                if self._last_delete_had_restore_points and self._last_delete_only_restore_points:
                    notes.append(T("junk_errors_restore_points_msg", count=len(errors)))
                else:
                    errors_key = "junk_errors_recycle_msg" if self._last_delete_recycle else "junk_errors_msg"
                    notes.append(T(errors_key, count=len(errors)))
            if self._last_delete_had_restore_points:
                notes.append(T("junk_note_restore_points_size_unknown"))
            note = "\n".join(notes) if notes else None
            if recycled_bytes > 0:
                message = T("junk_done_recycle_msg")
                if freed_bytes > 0:
                    message += " " + T("junk_done_recycle_also_freed", size=_fmt_size(freed_bytes))
                cards = [
                    (str(ok_count), T("junk_summary_card_items")),
                    (_fmt_size(recycled_bytes), T("junk_summary_card_recycled")),
                ]
            else:
                message = T("junk_done_msg")
                cards = [
                    (str(ok_count), T("junk_summary_card_items")),
                    (_fmt_size(freed_bytes), T("junk_summary_card_size")),
                ]
            SummaryDialog(
                self,
                title=T("junk_summary_title"),
                message=message,
                cards=cards,
                note=note,
            ).exec()

        self._scan_async()

    def _show_context_menu(self, item_idx, global_pos):
        item = self._items[item_idx] if item_idx is not None else None

        menu_items = []
        if item is not None and item.kind not in ("special_recycle_bin", "special_restore_points"):
            path = item.path
            menu_items.append(("🔍", "#60c8ff", T("menu_search_google"), lambda: self._search_google(path)))
            menu_items.append(("📁", DARK["fg2"], T("menu_open_location"), lambda: self._open_location(item)))
            menu_items.append(("⚙", DARK["fg2"], T("menu_properties"), lambda: cs.show_file_properties(path)))
            menu_items.append(None)

        menu_items.append(("☑", DARK["fg2"], T("btn_select_all"), lambda: self._set_all_checked(True)))
        menu_items.append(("☐", DARK["fg2"], T("btn_clear_selection"), lambda: self._set_all_checked(False)))

        self._ctx_menu = HOTSContextMenu(self, menu_items)
        self._ctx_menu.popup(global_pos)

    def _search_google(self, path: str):
        webbrowser.open(f"https://www.google.com/search?q={quote(os.path.basename(path))}")

    def _open_location(self, item):
        try:
            if item.kind == "folder":
                popen_no_busy_cursor(["explorer.exe", item.path])
            else:
                popen_no_busy_cursor(["explorer.exe", "/select,", item.path])
        except Exception:
            pass
