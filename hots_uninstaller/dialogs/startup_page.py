import os
import threading
import webbrowser
from urllib.parse import quote

from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QLabel, QListView,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)
from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QRect, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter

from ..icons import FIF
from ..ui_parts import SearchLineEdit
from ..constants import DARK, css_color, load_settings, save_settings
from ..i18n import T
from ..widgets_qt import (
    HOTSPage, HOTSDialog, HOTSButton, HOTSContextMenu,
    attach_fluent_tip, colored_svg_icon, show_fluent_popup_at,
)
from ..bg_tasks import start_bg_thread, is_shutting_down
from ..app_icons import (
    cached_square_pixmap, file_source, logo_source, prepare_icon, ready_icon,
    retry_failed_icons,
)
from ..hover_help import (
    HoverHelpDelegateMixin, HoverHelpViewMixin, details_tooltip_html,
)
from .. import core_startup as cs
from .. import core_appx as ca


def _startup_icon_source(item: "cs.StartupItem"):
    if item.kind == "appx":
        src = logo_source(ca.resolve_icon_path(item))
        if src is not None:
            return src
        return file_source(ca.find_executable(item))
    path = item.path if item.kind == "folder" else cs._extract_exe_path(item.command)
    return file_source(path)


def _prepare_items(items: list) -> None:
    for it in items:
        if is_shutting_down():
            return
        prepare_icon("startup", it.uid, lambda it=it: _startup_icon_source(it))
        try:
            it.is_missing
        except Exception:
            pass


def _scope_label(item: "cs.StartupItem") -> str:
    if item.kind == "registry":
        return f"{item.scope} · {item.run_type}"
    if item.kind == "task":
        return T("startup_scope_task")
    if item.kind == "service":
        return T("startup_scope_service")
    if item.kind == "appx":
        return T("startup_scope_appx")
    return T("startup_scope_folder_user") if item.scope == "User" else T("startup_scope_folder_common")


def _scope_hint(item: "cs.StartupItem") -> str:
    if item.kind == "registry":
        if item.run_type == "Policy":
            return T("tip_policy_entry")
        if item.run_type == "RunOnce":
            return T("tip_runonce_entry")
    return ""


_GROUP_ORDER = {
    ("HKLM", "Run"): 0, ("HKCU", "Run"): 1,
    ("HKLM", "RunOnce"): 2, ("HKCU", "RunOnce"): 3,
    ("HKLM", "Policy"): 4, ("HKCU", "Policy"): 5,
}


def _group_priority(item: "cs.StartupItem") -> int:
    if item.kind == "appx":
        return -1
    if item.kind == "registry":
        return _GROUP_ORDER.get((item.scope, item.run_type), 6)
    if item.kind == "service":
        return 7
    if item.kind == "task":
        return 8
    return 10 if item.scope == "User" else 11


def _name_tooltip(item: "cs.StartupItem") -> str:
    rows = [(T("startup_tip_source"), _scope_label(item))]
    if item.kind == "appx":
        if item.install_location:
            rows.append((T("info_install_path"), item.install_location))
    elif item.command:
        rows.append((T("startup_tip_command"), item.command))

    badges = []
    if item.is_missing:
        badges.append((DARK["red"], T("ghost_badge")))
    hint = _scope_hint(item)
    if hint:
        badges.append((DARK["fg2"], hint))
    return details_tooltip_html(rows, badges)


def _group_key(item: "cs.StartupItem") -> str:
    if item.kind == "registry":
        return f"reg|{item.scope}|{item.run_type}"
    if item.kind == "folder":
        return f"folder|{item.scope}"
    return item.kind


class _ScanSignals(QObject):
    done = Signal(list)


class _ActionSignals(QObject):
    done = Signal(object, str, bool, str, object)


_LOCAL_ACTION_LOCK = threading.Lock()


class _StartupListModel(QAbstractListModel):
    ItemRole = Qt.UserRole + 1
    HeaderRole = Qt.UserRole + 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []

    def set_rows(self, rows: list):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        if role == self.HeaderRole and row[0] == "header":
            return {"key": row[1], "label": row[2], "count": row[3], "collapsed": row[4]}
        if role == self.ItemRole and row[0] == "item":
            return row[1]
        return None

    def item_changed(self, item):
        for r, row in enumerate(self._rows):
            if row[0] == "item" and row[1] is item:
                idx = self.index(r)
                self.dataChanged.emit(idx, idx)
                return


class _StartupItemDelegate(HoverHelpDelegateMixin, QStyledItemDelegate):
    ROW_HEIGHT = 42
    HEADER_HEIGHT = 30
    ICON_SIZE = 24
    ICON_X = 10
    BTN_SIZE = 30
    RIGHT_MARGIN = 8
    HELP_SIZE = 20
    GAP = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._play_icon = colored_svg_icon(FIF.PLAY, DARK["green"], sizes=(15,))
        self._pause_icon = colored_svg_icon(FIF.PAUSE, DARK["red"], sizes=(15,))
        self._delete_icon = colored_svg_icon(FIF.DELETE, DARK["red"], sizes=(15,))
        self._delete_icon_off = colored_svg_icon(FIF.DELETE, DARK["gray"], sizes=(15,))
        self.busy_uids: set = set()
        self._init_hover_help()

    def sizeHint(self, option, index):
        if index.data(_StartupListModel.HeaderRole) is not None:
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

    def hit_rects(self, row_rect: QRect, name_text: str = ""):
        r = row_rect
        remove_rect = QRect(r.right() - self.RIGHT_MARGIN - self.BTN_SIZE,
                            r.center().y() - self.BTN_SIZE // 2,
                            self.BTN_SIZE, self.BTN_SIZE)
        toggle_rect = QRect(remove_rect.left() - self.GAP - self.BTN_SIZE,
                            remove_rect.top(), self.BTN_SIZE, self.BTN_SIZE)
        icon_rect = QRect(r.left() + self.ICON_X, r.center().y() - self.ICON_SIZE // 2,
                          self.ICON_SIZE, self.ICON_SIZE)

        name_left = icon_rect.right() + self.GAP
        max_name_right = toggle_rect.left() - self.GAP - self.HELP_SIZE - self.GAP
        max_name_width = max(0, max_name_right - name_left)

        fm = QFontMetrics(self._name_font())
        elided = fm.elidedText(name_text, Qt.ElideRight, max_name_width) if name_text else ""
        text_w = fm.horizontalAdvance(elided) if elided else 0

        help_left = min(name_left + text_w + self.GAP, max_name_right)
        help_rect = QRect(help_left, r.center().y() - self.HELP_SIZE // 2,
                          self.HELP_SIZE, self.HELP_SIZE)
        name_rect = QRect(name_left, r.top(), max_name_width, r.height())
        return icon_rect, name_rect, help_rect, toggle_rect, remove_rect

    def _paint_header(self, painter: QPainter, option, header: dict):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(option.rect, css_color(DARK["panel_bg"]))
        painter.setPen(css_color(DARK["border_faint"]))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        arrow = "▸" if header["collapsed"] else "▾"
        text = f"{arrow}  {header['label'].upper()} ({header['count']})"
        font = painter.font()
        font.setPointSize(8)
        font.setBold(True)
        font.setItalic(False)
        painter.setFont(font)
        painter.setPen(QColor(DARK["fg2"]))
        text_rect = option.rect.adjusted(10, 0, -10, 0)
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        painter.restore()

    def paint(self, painter: QPainter, option, index):
        header = index.data(_StartupListModel.HeaderRole)
        if header is not None:
            self._paint_header(painter, option, header)
            return

        item = index.data(_StartupListModel.ItemRole)
        if item is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        self.paint_row_hover(painter, option.rect, index.row())
        painter.setPen(css_color(DARK["border_faint"]))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        if item.uid in self.busy_uids:
            painter.setOpacity(0.45)

        icon_rect, name_rect, help_rect, toggle_rect, remove_rect = \
            self.hit_rects(option.rect, item.display_name)

        icon = ready_icon("startup", item.uid)
        if icon is not None:
            pix = cached_square_pixmap(
                "startup", item.uid, icon, self.ICON_SIZE, trim_padding=True,
                dpr=painter.device().devicePixelRatioF(),
            )
            painter.drawPixmap(icon_rect.topLeft(), pix)

        missing = item.is_missing
        font = painter.font()
        font.setPointSize(10)
        font.setItalic(missing)
        painter.setFont(font)
        painter.setPen(QColor(DARK["red"] if missing else DARK["fg"]))
        fm = QFontMetrics(font)
        elided = fm.elidedText(item.display_name, Qt.ElideRight, name_rect.width())
        painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

        self.paint_help_icon(painter, help_rect, index.row())

        (self._play_icon if item.enabled else self._pause_icon).paint(painter, toggle_rect)
        can_remove = item.kind != "service"
        (self._delete_icon if can_remove else self._delete_icon_off).paint(painter, remove_rect)

        painter.restore()


class _StartupListView(HoverHelpViewMixin, QListView):
    _BUTTON_TIP_DELAY_MS = 400

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

        self.on_toggle = None
        self.on_remove = None
        self.on_context_menu = None
        self.on_toggle_group = None
        self._init_hover_help_view()

        self._tip_key = None
        self._tip_text = ""
        self._tip_width = None
        self._tip_pos = None
        self._tip_popup = None
        self._tip_timer = QTimer(self)
        self._tip_timer.setSingleShot(True)
        self._tip_timer.setInterval(self._BUTTON_TIP_DELAY_MS)
        self._tip_timer.timeout.connect(self._show_button_tip)
        self.verticalScrollBar().valueChanged.connect(self._close_button_tip)

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
        self._close_button_tip()
        self.itemDelegate().set_hover_row(-1, self)
        self.itemDelegate().set_name_hover(-1, self)

    def _close_button_tip(self, *_args):
        self._tip_timer.stop()
        self._tip_key = None
        if self._tip_popup is not None:
            self._tip_popup.close()
            self._tip_popup = None

    def _show_button_tip(self):
        if self._tip_text and self._tip_pos is not None:
            self._tip_popup = show_fluent_popup_at(
                self._tip_pos, self._tip_text, width=self._tip_width)

    def _update_button_tip(self, key, text: str, width, rect: QRect):
        if key == self._tip_key:
            return
        self._close_button_tip()
        if key is None or not text:
            return
        self._tip_key = key
        self._tip_text = text
        self._tip_width = width
        self._tip_pos = self.mapToGlobal(rect.bottomLeft())
        self._tip_timer.start()

    def _button_tip_for(self, item, toggle_rect: QRect, remove_rect: QRect, pos):
        if toggle_rect.contains(pos):
            text = T("menu_toggle_disable") if item.enabled else T("menu_toggle_enable")
            return ("toggle", item.uid, item.enabled), text, None, toggle_rect
        if remove_rect.contains(pos):
            if item.kind != "service" or item.is_missing:
                return ("remove", item.uid), T("menu_remove"), None, remove_rect
            return ("remove", item.uid), T("info_service_no_remove"), 260, remove_rect
        return None, "", None, QRect()

    def set_empty_text(self, text: str):
        self.empty_label.setText(text)

    def show_empty(self, show: bool):
        self.empty_label.setVisible(show)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.empty_label.setGeometry(self.viewport().rect())

    def leaveEvent(self, event):
        self._close_help_popup()
        self._close_button_tip()
        self.itemDelegate().set_hover_row(-1, self)
        self.itemDelegate().set_name_hover(-1, self)
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        index = self.indexAt(pos)
        delegate = self.itemDelegate()

        if index.isValid() and index.data(_StartupListModel.HeaderRole) is not None:
            delegate.set_hover_row(-1, self)
            delegate.set_name_hover(-1, self)
            self._close_button_tip()
            self.setCursor(Qt.PointingHandCursor)
            if self._hover_help_row != -1 and not self._popup_hit(pos):
                self._close_help_popup()
            super().mouseMoveEvent(event)
            return

        delegate.set_hover_row(index.row() if index.isValid() else -1, self)

        over_help = False
        over_button = False
        if index.isValid():
            item = index.data(_StartupListModel.ItemRole)
            name = item.display_name if item is not None else ""
            icon_rect, name_rect, help_rect, toggle_rect, remove_rect = \
                delegate.hit_rects(self.visualRect(index), name)

            over_help = self.hover_help_on_move(delegate, index.row(), item, name_rect, help_rect, pos)
            over_button = toggle_rect.contains(pos) or remove_rect.contains(pos)
            if item is not None:
                key, text, width, rect = self._button_tip_for(item, toggle_rect, remove_rect, pos)
                self._update_button_tip(key, text, width, rect)
            self.setCursor(Qt.PointingHandCursor if (over_help or over_button) else Qt.ArrowCursor)
        else:
            delegate.set_name_hover(-1, self)
            self._close_button_tip()
            self.setCursor(Qt.ArrowCursor)

        if not over_help and self._hover_help_row != -1 and not self._popup_hit(pos):
            self._close_help_popup()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        index = self.indexAt(pos)
        if index.isValid() and event.button() == Qt.LeftButton:
            header = index.data(_StartupListModel.HeaderRole)
            if header is not None:
                if self.on_toggle_group:
                    self.on_toggle_group(header["key"])
                return

            item = index.data(_StartupListModel.ItemRole)
            name = item.display_name if item is not None else ""
            icon_rect, name_rect, help_rect, toggle_rect, remove_rect = \
                self.itemDelegate().hit_rects(self.visualRect(index), name)

            if toggle_rect.contains(pos):
                self._close_help_popup()
                self._close_button_tip()
                if item is not None and self.on_toggle:
                    self.on_toggle(item)
                return
            if remove_rect.contains(pos):
                self._close_help_popup()
                self._close_button_tip()
                if item is not None and self.on_remove:
                    self.on_remove(item)
                return

            if self.hover_help_on_click(self.itemDelegate(), index.row(), item, help_rect, pos):
                return

        super().mouseReleaseEvent(event)

    def _on_context_menu_requested(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return
        item = index.data(_StartupListModel.ItemRole)
        if item is not None and self.on_context_menu:
            self._close_button_tip()
            self.on_context_menu(item, self.mapToGlobal(pos))


class StartupPage(HOTSPage):
    def __init__(self, parent=None):
        super().__init__("startupInterface", FIF.SPEED_HIGH if hasattr(FIF, "SPEED_HIGH") else FIF.SYNC,
                          T("startup_title"), parent)
        self._all_items: list = []
        self._filter_text = ""
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._apply_filter)
        self._collapsed = load_settings().get("startup_groups_collapsed", {})
        if not isinstance(self._collapsed, dict):
            self._collapsed = {}
        self._loaded_once = False
        self._load_in_flight = False
        self._full_loading = 0
        self._reload_pending = False
        self._busy_uids: set = set()
        self._action_signals = _ActionSignals(self)
        self._action_signals.done.connect(self._on_action_done)
        self._build()
        QTimer.singleShot(1500, self._ensure_loaded)

    def _ensure_loaded(self):
        if self._loaded_once or self._load_in_flight or is_shutting_down():
            return
        self._load_in_flight = True
        self._load_async()

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

        cl_body.addWidget(self._build_top_card())

        self.model = _StartupListModel(self)

        self.view = _StartupListView(self)
        self.view.setModel(self.model)
        self.delegate = _StartupItemDelegate(self.view)
        self.delegate.busy_uids = self._busy_uids
        self.view.setItemDelegate(self.delegate)
        self.view.set_empty_text(T("startup_no_results"))
        self.view.on_toggle = self._toggle_item
        self.view.on_remove = self._remove_item
        self.view.on_show_help = self._show_help_popup
        self.view.on_context_menu = self._show_context_menu
        self.view.on_toggle_group = self._toggle_group
        self.view.setStyleSheet(
            f"QListView {{ background-color: {DARK['table_bg']}; "
            f"border: 1px solid {DARK['border_soft']}; border-radius: 6px; }}"
        )

        cl_body.addWidget(self.view, 1)
        cl_body.addWidget(self._build_status_bar())

    def _build_top_card(self) -> QWidget:
        frame = QWidget()
        frame.setStyleSheet(self._card_style())
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(14, 12, 12, 12)
        lay.setSpacing(12)

        desc = QLabel(T("startup_subheader"))
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent; border: none;")
        lay.addWidget(desc, 1)
        return frame

    def _build_status_bar(self) -> QWidget:
        frame = QWidget()
        frame.setStyleSheet(self._card_style())
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8pt; background: transparent; border: none;")
        lay.addWidget(self.status_label)
        lay.addStretch()

        self.search = SearchLineEdit()
        self.search.setPlaceholderText(T("startup_search_placeholder"))
        self.search.setFixedWidth(165)
        self.search.textChanged.connect(self._on_search_changed)
        lay.addWidget(self.search)

        refresh_btn = HOTSButton(FIF.SYNC, "#60c8ff", "")
        refresh_btn.setFixedSize(42, 42)
        refresh_btn.clicked.connect(self._load_async)
        attach_fluent_tip(refresh_btn, T("btn_refresh"))
        lay.addWidget(refresh_btn)

        return frame

    def _load_async(self):
        retry_failed_icons("startup")
        self._full_loading += 1
        self.begin_busy()
        signals = _ScanSignals(self)
        signals.done.connect(self._on_loaded)
        self._scan_signals = signals

        def worker():
            try:
                items = cs.list_startup_items()
            except Exception:
                items = []
            _prepare_items(items)
            if not is_shutting_down():
                signals.done.emit(items)

        start_bg_thread(worker)

    def _on_loaded(self, items: list):
        self.end_busy()
        self._full_loading = max(0, self._full_loading - 1)
        self._loaded_once = True
        if is_shutting_down():
            return
        self._all_items = items
        self._apply_filter()
        if self._reload_pending and not self._full_loading:
            self._reload_pending = False
            self._load_async()

    def refresh_content(self):
        self._ensure_loaded()

    def _on_search_changed(self, text: str):
        self._filter_text = (text or "").strip().lower()
        self._search_debounce.start()

    def _toggle_group(self, key: str):
        self._collapsed[key] = not self._collapsed.get(key)
        fresh = load_settings()
        fresh["startup_groups_collapsed"] = self._collapsed
        save_settings(fresh)
        self._apply_filter()

    def _apply_filter(self, keep_scroll: bool = False):
        items = self._all_items
        if self._filter_text:
            items = [i for i in items
                     if self._filter_text in i.display_name.lower()
                     or self._filter_text in (i.command or "").lower()]
        items = sorted(items, key=lambda it: (_group_priority(it), _group_key(it),
                                              it.display_name.lower()))

        rows = []
        groups: dict = {}
        order: list = []
        for it in items:
            key = _group_key(it)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(it)
        for key in order:
            group_items = groups[key]
            collapsed = bool(self._collapsed.get(key))
            rows.append(("header", key, _scope_label(group_items[0]), len(group_items), collapsed))
            if not collapsed:
                rows.extend(("item", it) for it in group_items)

        sb = self.view.verticalScrollBar()
        pos = sb.value()
        self.model.set_rows(rows)
        if keep_scroll:
            self.view.doItemsLayout()
            sb.setValue(min(pos, sb.maximum()))
        self.view.show_empty(not items)
        self._update_status(len(items))

    def _update_status(self, shown: int):
        self._shown_count = shown
        total = len(self._all_items)
        active = sum(1 for i in self._all_items if i.enabled)
        key = "startup_status_count" if shown != total else "startup_status_all"
        self.status_label.setText(T(key, shown=shown, total=total, active=active))

    def _show_help_popup(self, item, global_pos):
        return show_fluent_popup_at(global_pos, _name_tooltip(item), width=420)

    def _toggle_item(self, item):
        self._run_action("toggle", item)

    def _remove_item(self, item):
        if item.kind == "service" and not item.is_missing:
            HOTSDialog.info(self, T("app_title"), T("info_service_no_remove"))
            return
        if not HOTSDialog.ask(self, T("confirm_uninstall_title"),
                               T("confirm_remove_startup_msg", name=item.display_name)):
            return
        self._run_action("remove", item)

    def _run_action(self, action: str, item):
        if self._full_loading or item.uid in self._busy_uids:
            return
        self._busy_uids.add(item.uid)
        self.view.viewport().update()
        self.begin_busy()

        signals = self._action_signals
        local = item.kind in ("registry", "folder")

        def worker():
            def run():
                if action == "toggle":
                    return cs.toggle_startup_item(item)
                return cs.remove_startup_item(item)

            local_items = None
            try:
                if local:
                    with _LOCAL_ACTION_LOCK:
                        ok, err = run()
                        if ok:
                            local_items = cs.list_local_startup_items()
                else:
                    ok, err = run()
                if local_items is not None:
                    _prepare_items(local_items)
            except Exception as e:
                ok, err, local_items = False, str(e), None
            if not is_shutting_down():
                signals.done.emit(item, action, ok, err, local_items)

        if start_bg_thread(worker) is None:
            self._busy_uids.discard(item.uid)
            self.view.viewport().update()
            self.end_busy()

    def _request_reload(self):
        if self._full_loading:
            self._reload_pending = True
        else:
            self._load_async()

    def _on_action_done(self, item, action: str, ok: bool, err: str, local_items):
        self._busy_uids.discard(item.uid)
        self.end_busy()
        if is_shutting_down():
            return

        if not ok:
            self.view.viewport().update()
            if action == "toggle":
                HOTSDialog.error(self, T("app_title"),
                                 T("err_toggle_failed", error=f"{item.display_name}: {err}"))
            else:
                HOTSDialog.error(self, T("app_title"), T("err_remove_failed", error=err))
            self._request_reload()
            return

        if self._full_loading or not any(i is item for i in self._all_items):
            self.view.viewport().update()
            self._request_reload()
            return

        if item.kind in ("registry", "folder"):
            others = [i for i in self._all_items if i.kind not in ("registry", "folder")]
            self._all_items = (local_items or []) + others
            self._apply_filter(keep_scroll=True)
        elif action == "toggle":
            item.enabled = not item.enabled
            self.model.item_changed(item)
            self.view.viewport().update()
            self._update_status(getattr(self, "_shown_count", len(self._all_items)))
        else:
            self._all_items = [i for i in self._all_items if i is not item]
            self._apply_filter(keep_scroll=True)

    def _show_context_menu(self, item, global_pos):
        toggle_label = T("menu_toggle_disable") if item.enabled else T("menu_toggle_enable")
        items = [("⏻", DARK["fg2"], toggle_label, lambda: self._toggle_item(item))]
        if item.kind not in ("service", "appx"):
            items.append(("✕", DARK["red"], T("menu_remove"), lambda: self._remove_item(item)))
        exe = cs._extract_exe_path(item.command)
        target_path = cs.resolve_target_path(item)
        path_exists = bool(target_path) and os.path.exists(target_path)
        if item.kind == "folder" or (item.kind == "appx" and path_exists) or (exe and os.path.exists(exe)):
            items.append(("📁", DARK["fg2"], T("menu_open_location"), lambda: self._open_location(item)))
        if item.kind in ("registry", "service", "appx"):
            items.append(("🔑", DARK["fg2"], T("menu_open_registry"), lambda: cs.open_registry_key(item)))
        if item.kind == "task":
            items.append(("🗓", DARK["fg2"], T("menu_open_task_scheduler"), lambda: self._open_task_scheduler(item)))
        if target_path:
            items.append(("⧉", DARK["fg2"], T("menu_copy_path"), lambda: self._copy_path(target_path)))
        items.append(("🔍", "#60c8ff", T("menu_search_google"), lambda: self._search_google(item)))
        if path_exists:
            items.append(("⚙", DARK["fg2"], T("menu_properties"), lambda: cs.show_file_properties(target_path)))

        self._ctx_menu = HOTSContextMenu(self, items)
        self._ctx_menu.popup(global_pos)

    def _open_location(self, item):
        cs.open_containing_folder(item)

    def _search_google(self, item):
        webbrowser.open(f"https://www.google.com/search?q={quote(item.display_name)}")

    def _copy_path(self, path: str):
        QApplication.clipboard().setText(path)

    def _open_task_scheduler(self, item):
        QApplication.clipboard().setText(cs.task_full_path(item))
        cs.open_task_scheduler()
