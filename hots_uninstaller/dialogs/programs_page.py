import os
import subprocess
import webbrowser
from datetime import date, datetime
from urllib.parse import quote

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QApplication,
    QListView, QAbstractItemView, QStyledItemDelegate,
)
from PySide6.QtCore import (
    Qt, QObject, Signal, QSize, QTimer, QRect, QAbstractListModel, QModelIndex,
)
from PySide6.QtGui import QColor, QPainter, QFontMetrics, QFont

from ..icons import FIF
from ..ui_parts import SearchLineEdit
from ..constants import DARK, css_color, load_settings, save_settings
from ..i18n import T
from ..proc_utils import popen_no_busy_cursor
from ..widgets_qt import (
    HOTSPage, HOTSDialog, HOTSButton, HOTSContextMenu,
    attach_fluent_tip, colored_svg_icon, show_fluent_popup_at,
)
from ..hover_help import (
    HoverHelpDelegateMixin, HoverHelpViewMixin, details_tooltip_html,
)
from ..bg_tasks import start_bg_thread, is_shutting_down
from ..app_icons import (
    IconPrefetcher, app_icon_if_ready, app_icon_key, app_icon_source,
    cached_square_pixmap, retry_failed_icons,
)
from .. import core_uninstall as cu
from .. import core_monitor as cm
from .leftover_dialog import LeftoverDialog
from .uninstall_wizard import UninstallWizardDialog


_NEW_PROGRAM_DAYS = 14


def _install_date_obj(app: "cu.InstalledApp"):
    d = (app.install_date or "").strip()
    if len(d) == 8 and d.isdigit():
        try:
            return datetime.strptime(d, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def _effective_install_date(app: "cu.InstalledApp", monitored_dates: dict):
    d = _install_date_obj(app)
    m = monitored_dates.get(app.uid)
    if m is not None and (d is None or m > d):
        return m
    return d


def _is_new_program(app: "cu.InstalledApp", monitored_dates: dict) -> bool:
    d = _effective_install_date(app, monitored_dates)
    if d is None:
        return False
    return (date.today() - d).days <= _NEW_PROGRAM_DAYS


def _details_tooltip(app: "cu.InstalledApp", monitored: bool = False) -> str:
    rows = [
        (T("col_version"), app.display_version or "—"),
        (T("col_publisher"), app.publisher or T("publisher_unknown")),
    ]
    if app.install_date_display:
        date_value = app.install_date_display
        if app.install_date_estimated:
            date_value = f"{date_value} {T('install_date_estimated_suffix')}"
        rows.append((T("col_install_date"), date_value))
    if app.install_location:
        rows.append((T("info_install_path"), app.install_location))

    badges = []
    if app.is_ghost:
        badges.append((DARK["red"], T("ghost_badge")))
        missing = app.missing_uninstaller_path
        if missing:
            badges.append((DARK["red"], T("ghost_missing_file", path=missing)))
            if app.install_location and os.path.isdir(app.install_location):
                badges.append((DARK["fg2"], T("ghost_folder_exists")))
        else:
            badges.append((DARK["red"], T("ghost_no_command")))
    if app.no_remove:
        badges.append((DARK["fg2"], T("no_remove_badge")))
    if app.system_component:
        badges.append((DARK["fg2"], T("system_component_badge")))
    if app.is_update:
        badges.append((DARK["fg2"], T("update_badge")))
    elif app.has_parent:
        badges.append((DARK["fg2"], T("child_component_badge")))
    if monitored:
        badges.append((DARK["fg2"], T("prog_monitored_badge")))

    return details_tooltip_html(rows, badges)


class _ScanSignals(QObject):
    done = Signal(list)


class _ProgramsListModel(QAbstractListModel):
    AppRole = Qt.UserRole + 1
    HeaderRole = Qt.UserRole + 2
    MonitoredRole = Qt.UserRole + 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []
        self._monitored: set = set()

    def set_monitored(self, uids: set):
        self._monitored = uids

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
        if role == self.AppRole:
            return row[1] if row[0] == "app" else None
        if role == self.MonitoredRole:
            return row[0] == "app" and row[1].uid in self._monitored
        if role == self.HeaderRole:
            if row[0] == "header":
                _, key, label, count, collapsed = row
                return {"key": key, "label": label, "count": count, "collapsed": collapsed}
            return None
        return None


class _ProgramItemDelegate(HoverHelpDelegateMixin, QStyledItemDelegate):
    ROW_HEIGHT = 42
    HEADER_HEIGHT = 30
    ICON_SIZE = 24
    ICON_X = 10
    DELETE_SIZE = 30
    RIGHT_MARGIN = 8
    SIZE_W = 64
    HELP_SIZE = 20
    GAP = 10
    MONITOR_DOT = 6
    MONITOR_GAP = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._delete_icon = colored_svg_icon(FIF.DELETE, DARK["red"], sizes=(15,))
        self._init_hover_help()

    def sizeHint(self, option, index):
        if index.data(_ProgramsListModel.HeaderRole) is not None:
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
        delete_rect = QRect(r.right() - self.RIGHT_MARGIN - self.DELETE_SIZE,
                             r.center().y() - self.DELETE_SIZE // 2,
                             self.DELETE_SIZE, self.DELETE_SIZE)
        size_rect = QRect(delete_rect.left() - self.GAP - self.SIZE_W,
                           r.top(), self.SIZE_W, r.height())
        icon_rect = QRect(r.left() + self.ICON_X, r.center().y() - self.ICON_SIZE // 2,
                           self.ICON_SIZE, self.ICON_SIZE)

        name_left = icon_rect.right() + self.GAP
        max_name_right = size_rect.left() - self.GAP - self.HELP_SIZE - self.GAP
        max_name_width = max(0, max_name_right - name_left)

        fm = QFontMetrics(self._name_font())
        elided = fm.elidedText(name_text, Qt.ElideRight, max_name_width) if name_text else ""
        text_w = fm.horizontalAdvance(elided) if elided else 0

        help_left = min(name_left + text_w + self.GAP, max_name_right)
        help_rect = QRect(help_left, r.center().y() - self.HELP_SIZE // 2,
                           self.HELP_SIZE, self.HELP_SIZE)

        name_rect = QRect(name_left, r.top(), max_name_width, r.height())
        return icon_rect, name_rect, help_rect, size_rect, delete_rect

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
        header = index.data(_ProgramsListModel.HeaderRole)
        if header is not None:
            self._paint_header(painter, option, header)
            return

        app = index.data(_ProgramsListModel.AppRole)
        if app is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        self.paint_row_hover(painter, option.rect, index.row())
        painter.setPen(css_color(DARK["border_faint"]))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        icon_rect, name_rect, help_rect, size_rect, delete_rect = self.hit_rects(option.rect, app.display_name)

        icon = app_icon_if_ready(app)
        if icon is not None:
            pix = cached_square_pixmap(
                "win32", app.uid, icon, self.ICON_SIZE, trim_padding=True,
                dpr=painter.device().devicePixelRatioF(),
            )
            painter.drawPixmap(icon_rect.topLeft(), pix)

        font = painter.font()
        font.setPointSize(10)
        font.setItalic(app.is_ghost)
        painter.setFont(font)
        if app.is_ghost:
            name_color = DARK["red"]
        elif app.is_component:
            name_color = DARK["fg2"]
        else:
            name_color = DARK["fg"]
        painter.setPen(QColor(name_color))
        fm = QFontMetrics(font)
        elided = fm.elidedText(app.display_name, Qt.ElideRight, name_rect.width())
        painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

        self.paint_help_icon(painter, help_rect, index.row())

        size_text_w = 0
        if app.size_display:
            size_font = painter.font()
            size_font.setPointSize(8)
            size_font.setItalic(False)
            painter.setFont(size_font)
            painter.setPen(QColor(DARK["fg2"]))
            painter.drawText(size_rect, Qt.AlignVCenter | Qt.AlignRight, app.size_display)
            size_text_w = QFontMetrics(size_font).horizontalAdvance(app.size_display)

        if index.data(_ProgramsListModel.MonitoredRole):
            mon_x = size_rect.right() + 1 - size_text_w - self.MONITOR_GAP - self.MONITOR_DOT
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(DARK["accent"]))
            painter.drawEllipse(QRect(mon_x, option.rect.center().y() - self.MONITOR_DOT // 2,
                                      self.MONITOR_DOT, self.MONITOR_DOT))

        self._delete_icon.paint(painter, delete_rect)

        painter.restore()


class _ProgramsListView(HoverHelpViewMixin, QListView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WA_Hover, True)
        # Must be False: group header rows have a DIFFERENT height than program
        # rows (HEADER_HEIGHT vs ROW_HEIGHT). With True (which assumes all rows
        # are the same size) Qt would compute row positions wrongly, giving
        # overlapping/misaligned rows while scrolling.
        self.setUniformItemSizes(False)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setFrameShape(QListView.NoFrame)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setCursor(Qt.ArrowCursor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.on_uninstall = None
        self.on_context_menu = None
        self.on_toggle_group = None
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
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        index = self.indexAt(pos)
        delegate = self.itemDelegate()

        if index.isValid() and index.data(_ProgramsListModel.HeaderRole) is not None:
            delegate.set_hover_row(-1, self)
            delegate.set_name_hover(-1, self)
            self.setCursor(Qt.PointingHandCursor)
            if self._hover_help_row != -1 and not self._popup_hit(pos):
                self._close_help_popup()
            super().mouseMoveEvent(event)
            return

        delegate.set_hover_row(index.row() if index.isValid() else -1, self)

        over_help = False
        if index.isValid():
            app = index.data(_ProgramsListModel.AppRole)
            name = app.display_name if app is not None else ""
            icon_rect, name_rect, help_rect, size_rect, delete_rect = \
                delegate.hit_rects(self.visualRect(index), name)

            over_help = self.hover_help_on_move(delegate, index.row(), app, name_rect, help_rect, pos)
            if over_help:
                self.setCursor(Qt.PointingHandCursor)
            elif delete_rect.contains(pos):
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
        else:
            delegate.set_name_hover(-1, self)
            self.setCursor(Qt.ArrowCursor)

        if not over_help and self._hover_help_row != -1 and not self._popup_hit(pos):
            self._close_help_popup()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        index = self.indexAt(pos)
        if index.isValid() and event.button() == Qt.LeftButton:
            header = index.data(_ProgramsListModel.HeaderRole)
            if header is not None:
                if self.on_toggle_group:
                    self.on_toggle_group(header["key"])
                return

            app = index.data(_ProgramsListModel.AppRole)
            name = app.display_name if app is not None else ""
            icon_rect, name_rect, help_rect, size_rect, delete_rect = \
                self.itemDelegate().hit_rects(self.visualRect(index), name)

            if delete_rect.contains(pos):
                self._close_help_popup()
                if app is not None and self.on_uninstall:
                    self.on_uninstall(app)
                return

            if self.hover_help_on_click(self.itemDelegate(), index.row(), app, help_rect, pos):
                return

        super().mouseReleaseEvent(event)

    def _on_context_menu_requested(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return
        app = index.data(_ProgramsListModel.AppRole)
        if app is not None and self.on_context_menu:
            self.on_context_menu(app, self.mapToGlobal(pos))


class ProgramsPage(HOTSPage):
    def __init__(self, parent=None):
        super().__init__("programsInterface", FIF.DESKTOP, T("prog_title"), parent)
        self._all_apps: list = []
        self._filter_text = ""
        self._show_system = False
        self._monitored_uids: set = set()
        self._monitored_dates: dict = {}
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._apply_filter)
        self._collapsed = load_settings().get("programs_groups_collapsed", {})
        if not isinstance(self._collapsed, dict):
            self._collapsed = {}
        self._build()
        self._icon_prefetcher = IconPrefetcher(self)
        self._icon_prefetcher.progress.connect(self.view.viewport().update)
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

        self.model = _ProgramsListModel(self)

        self.view = _ProgramsListView(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(_ProgramItemDelegate(self.view))
        self.view.set_empty_text(T("prog_no_results"))
        self.view.on_uninstall = self._uninstall_one
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

        desc = QLabel(T("prog_subheader"))
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent; border: none;")
        lay.addWidget(desc, 1)

        self._show_system_btn = HOTSButton(FIF.VIEW, DARK["accent"], "")
        self._show_system_btn.setFixedWidth(44)
        self._show_system_btn.clicked.connect(self._toggle_show_system)
        attach_fluent_tip(self._show_system_btn, T("opt_show_system"))
        lay.addWidget(self._show_system_btn, 0, Qt.AlignTop)

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
        self.search.setPlaceholderText(T("prog_search_placeholder"))
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
        retry_failed_icons("win32")
        self.begin_busy()
        signals = _ScanSignals(self)
        signals.done.connect(self._on_loaded)
        self._scan_signals = signals

        def worker():
            try:
                apps = cu.list_installed_apps(include_system=True)
            except Exception:
                apps = []
            try:
                self._monitored_dates = cm.monitored_app_dates(apps)
                self._monitored_uids = set(self._monitored_dates)
            except Exception:
                self._monitored_dates = {}
                self._monitored_uids = set()
            if not is_shutting_down():
                signals.done.emit(apps)

        start_bg_thread(worker)

    def _on_loaded(self, apps: list):
        self.end_busy()
        if is_shutting_down():
            return
        self._all_apps = apps
        self.model.set_monitored(self._monitored_uids)
        self._apply_filter()
        self._prefetch_icons(apps)

    def _prefetch_icons(self, apps: list):
        shown = [row[1] for row in self.model._rows if row[0] == "app"]
        seen = {a.uid for a in shown}
        ordered = shown + [a for a in apps if a.uid not in seen]
        self._icon_prefetcher.start([
            ("win32", app_icon_key(a), lambda a=a: app_icon_source(a))
            for a in ordered
        ])

    def refresh_content(self):
        pass

    def _on_search_changed(self, text: str):
        self._filter_text = (text or "").strip().lower()
        self._search_debounce.start()

    def _toggle_show_system(self):
        self._show_system = not self._show_system
        self._show_system_btn.set_accent(self._show_system)
        self._apply_filter()

    def _toggle_group(self, key: str):
        self._collapsed[key] = not self._collapsed.get(key)
        fresh = load_settings()
        fresh["programs_groups_collapsed"] = self._collapsed
        save_settings(fresh)
        self._apply_filter()

    def _apply_filter(self):
        apps = self._all_apps
        if not self._show_system:
            apps = [a for a in apps if not a.is_component]
        if self._filter_text:
            apps = [a for a in apps
                    if self._filter_text in (a.display_name or "").lower()
                    or self._filter_text in (a.publisher or "").lower()]

        dates = self._monitored_dates
        new_apps = sorted(
            (a for a in apps if _is_new_program(a, dates)),
            key=lambda a: _effective_install_date(a, dates) or date.min, reverse=True,
        )
        rest_apps = sorted(
            (a for a in apps if not _is_new_program(a, dates)),
            key=lambda a: (a.display_name or "").lower(),
        )

        rows = []
        for key, label, group_apps in (
            ("new", T("prog_group_new"), new_apps),
            ("all", T("prog_group_all"), rest_apps),
        ):
            if not group_apps:
                continue
            collapsed = bool(self._collapsed.get(key))
            rows.append(("header", key, label, len(group_apps), collapsed))
            if not collapsed:
                rows.extend(("app", a) for a in group_apps)

        self.model.set_rows(rows)
        self.view.show_empty(not apps)
        self._update_status(len(apps))

    def _update_status(self, shown: int):
        total = len(self._all_apps)
        self.status_label.setText(T("status_count_simple", shown=shown, total=total))

    def _show_help_popup(self, app, global_pos):
        return show_fluent_popup_at(global_pos, _details_tooltip(app, app.uid in self._monitored_uids), width=420)

    def _show_context_menu(self, app, global_pos):
        items = [("✕", DARK["red"], T("menu_uninstall"), lambda: self._uninstall_one(app))]
        if app.is_ghost:
            items.append(("⚠", DARK["fg2"], T("menu_force_remove"), lambda: self._force_remove_one(app)))
        items.append(("🔍", "#60c8ff", T("menu_scan_leftovers"), lambda: self._scan_leftovers_one(app)))
        if app.install_location and os.path.isdir(app.install_location):
            items.append(("📁", DARK["fg2"], T("menu_open_location"), lambda: self._open_location(app)))
        items.append(("🔑", DARK["fg2"], T("menu_open_registry"), lambda: cu.open_registry_key(app)))
        items.append(("🌐", "#60c8ff", T("menu_search_google"), lambda: self._search_google(app)))
        items.append(("⧉", DARK["fg2"], T("menu_copy_name"), lambda: self._copy_name(app)))

        self._ctx_menu = HOTSContextMenu(self, items)
        self._ctx_menu.popup(global_pos)

    def _uninstall_one(self, app):
        if app.is_ghost:
            self._remove_ghost(app)
            return
        dlg = UninstallWizardDialog(self, app)
        dlg.exec()
        self._load_async()

    def _remove_ghost(self, app):
        self.begin_busy()
        signals = _ScanSignals(self)
        signals.done.connect(lambda items: self._on_ghost_scanned(app, items))
        self._ghost_signals = signals

        def worker():
            try:
                items = cu.scan_leftovers(app)
            except Exception:
                items = []
            if not is_shutting_down():
                signals.done.emit(items)

        start_bg_thread(worker)

    def _on_ghost_scanned(self, app, items: list):
        self.end_busy()
        if is_shutting_down():
            return
        if not items:
            msg = T("confirm_ghost_remove_msg", name=app.display_name)
            if HOTSDialog.ask(self, T("confirm_ghost_remove_title"), msg):
                self._force_remove_one(app, confirm=False)
            return
        LeftoverDialog(self, app, items=items, remove_entry=True).exec()
        self._load_async()

    def _force_remove_one(self, app, confirm: bool = True):
        if confirm:
            msg = T("confirm_force_msg", name=app.display_name)
            if not HOTSDialog.ask(self, T("confirm_force_title"), msg):
                return
        ok, err, backup_path = cu.force_remove_registry_entry(app)
        if ok:
            if backup_path:
                msg = T("info_force_removed_backup_msg", tab=T("nav_backups"))
            else:
                msg = T("info_force_removed_msg")
            HOTSDialog.info(self, T("app_title"), msg)
        else:
            HOTSDialog.error(self, T("app_title"), T("err_force_remove_failed", error=err))
        self._load_async()

    def _scan_leftovers_one(self, app):
        dlg = LeftoverDialog(self, app)
        dlg.exec()
        self._load_async()

    def _open_location(self, app):
        path = os.path.normpath(app.install_location) if app.install_location else ""
        if not path or not os.path.isdir(path):
            return
        try:
            popen_no_busy_cursor(["explorer.exe", path])
        except Exception:
            pass

    def _copy_name(self, app):
        QApplication.clipboard().setText(app.display_name)

    def _search_google(self, app):
        webbrowser.open(f"https://www.google.com/search?q={quote(app.display_name)}")
