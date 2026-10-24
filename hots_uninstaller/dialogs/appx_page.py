import os
import webbrowser
import subprocess
from urllib.parse import quote

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QApplication,
    QListView, QAbstractItemView, QStyledItemDelegate, QToolTip,
)
from PySide6.QtCore import (
    Qt, QObject, Signal, QSize, QTimer, QRect, QAbstractListModel, QModelIndex,
)
from PySide6.QtGui import QIcon, QColor, QPainter, QFontMetrics, QFont

from ..icons import FIF
from ..ui_parts import SearchLineEdit
from ..constants import DARK, css_color, load_settings, save_settings
from ..i18n import T
from ..proc_utils import popen_no_busy_cursor
from ..widgets_qt import (
    HOTSPage, HOTSButton, HOTSContextMenu,
    attach_fluent_tip, colored_svg_icon, show_fluent_popup_at,
)
from ..hover_help import (
    HoverHelpDelegateMixin, HoverHelpViewMixin, details_tooltip_html,
)
from ..bg_tasks import start_bg_thread, is_shutting_down
from ..app_icons import (
    IconPrefetcher, cached_square_pixmap, file_source, get_icon, logo_source,
    ready_icon, retry_failed_icons,
)
from .. import core_appx as ca
from .uninstall_wizard import AppxUninstallWizardDialog


def _appx_icon_source(app: "ca.AppxApp"):
    src = logo_source(ca.resolve_icon_path(app))
    if src is not None:
        return src
    if app.is_background_component:
        return None
    return file_source(ca.find_executable(app))


def _appx_icon(app: "ca.AppxApp") -> QIcon:
    return get_icon("appx", app.uid, lambda: _appx_icon_source(app))


def _appx_details_tooltip(app: "ca.AppxApp") -> str:
    rows = [
        (T("col_version"), app.version or "—"),
        (T("col_publisher"), app.publisher_display or T("publisher_unknown")),
    ]
    if app.install_location:
        rows.append((T("info_install_path"), app.install_location))

    badges = []
    if app.is_removal_blocked:
        badges.append((DARK["fg2"], T("appx_non_removable")))
    if app.is_side_extension:
        badges.append((DARK["fg2"], T("appx_side_extension_badge")))
    if app.is_background_component:
        badges.append((DARK["fg2"], T("appx_background_badge")))

    return details_tooltip_html(rows, badges)


class _ScanSignals(QObject):
    done = Signal(list)


class _AppxListModel(QAbstractListModel):
    AppRole = Qt.UserRole + 1
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
        if role == self.AppRole:
            return row[1] if row[0] == "app" else None
        if role == self.HeaderRole:
            if row[0] == "header":
                _, key, label, count, collapsed = row
                return {"key": key, "label": label, "count": count, "collapsed": collapsed}
            return None
        return None


class _AppxItemDelegate(HoverHelpDelegateMixin, QStyledItemDelegate):
    ROW_HEIGHT = 42
    HEADER_HEIGHT = 30
    ICON_SIZE = 24
    ICON_X = 10
    HELP_SIZE = 20
    DELETE_SIZE = 30
    RIGHT_MARGIN = 8
    SIZE_W = 64
    GAP = 10
    ICON_GAP = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._delete_icon = colored_svg_icon(FIF.DELETE, DARK["red"], sizes=(15,))
        self._delete_icon_disabled = colored_svg_icon(FIF.DELETE, DARK["gray"], sizes=(15,))
        self._foreign_icon = colored_svg_icon(FIF.INFO, DARK["accent"], sizes=(13,))
        self._init_hover_help()

    def sizeHint(self, option, index):
        if index.data(_AppxListModel.HeaderRole) is not None:
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

    def hit_rects(self, row_rect: QRect, app: "ca.AppxApp"):
        r = row_rect
        delete_rect = QRect(r.right() - self.RIGHT_MARGIN - self.DELETE_SIZE,
                             r.center().y() - self.DELETE_SIZE // 2,
                             self.DELETE_SIZE, self.DELETE_SIZE)
        size_rect = QRect(delete_rect.left() - self.GAP - self.SIZE_W,
                           r.top(), self.SIZE_W, r.height())
        icon_rect = QRect(r.left() + self.ICON_X, r.center().y() - self.ICON_SIZE // 2,
                           self.ICON_SIZE, self.ICON_SIZE)

        has_foreign = bool(app.foreign_account_display)
        name_left = icon_rect.right() + self.GAP
        max_name_right = size_rect.left() - self.GAP - self.HELP_SIZE - self.GAP
        if has_foreign:
            max_name_right -= self.HELP_SIZE + self.ICON_GAP
        max_name_width = max(0, max_name_right - name_left)

        fm = QFontMetrics(self._name_font())
        elided = fm.elidedText(app.display_name, Qt.ElideRight, max_name_width)
        text_w = fm.horizontalAdvance(elided) if elided else 0

        help_left = min(name_left + text_w + self.GAP, max_name_right)
        help_rect = QRect(help_left, r.center().y() - self.HELP_SIZE // 2,
                           self.HELP_SIZE, self.HELP_SIZE)
        if has_foreign:
            foreign_rect = QRect(help_left + self.HELP_SIZE + self.ICON_GAP,
                                  help_rect.top(), self.HELP_SIZE, self.HELP_SIZE)
        else:
            foreign_rect = QRect()

        name_rect = QRect(name_left, r.top(), max_name_width, r.height())
        return icon_rect, name_rect, help_rect, foreign_rect, size_rect, delete_rect

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
        painter.setFont(font)
        painter.setPen(QColor(DARK["fg2"]))
        text_rect = option.rect.adjusted(10, 0, -10, 0)
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        painter.restore()

    def paint(self, painter: QPainter, option, index):
        header = index.data(_AppxListModel.HeaderRole)
        if header is not None:
            self._paint_header(painter, option, header)
            return

        app = index.data(_AppxListModel.AppRole)
        if app is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        self.paint_row_hover(painter, option.rect, index.row())
        painter.setPen(css_color(DARK["border_faint"]))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        icon_rect, name_rect, help_rect, foreign_rect, size_rect, delete_rect = \
            self.hit_rects(option.rect, app)

        icon = ready_icon("appx", app.uid)
        if icon is not None:
            pix = cached_square_pixmap(
                "appx", app.uid, icon, self.ICON_SIZE, trim_padding=True,
                dpr=painter.device().devicePixelRatioF(),
            )
            painter.drawPixmap(icon_rect.topLeft(), pix)

        name_color = DARK["fg2"] if app.is_component else DARK["fg"]
        font = self._name_font()
        painter.setFont(font)
        painter.setPen(QColor(name_color))
        fm = QFontMetrics(font)
        elided = fm.elidedText(app.display_name, Qt.ElideRight, name_rect.width())
        painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

        self.paint_help_icon(painter, help_rect, index.row())
        if foreign_rect.isValid():
            self._foreign_icon.paint(painter, foreign_rect)

        size_font = painter.font()
        size_font.setPointSize(8)
        painter.setFont(size_font)
        painter.setPen(QColor(DARK["fg2"]))
        painter.drawText(size_rect, Qt.AlignVCenter | Qt.AlignRight, app.size_display)

        can_remove = not app.is_removal_blocked
        (self._delete_icon if can_remove else self._delete_icon_disabled).paint(painter, delete_rect)

        painter.restore()


class _AppxListView(HoverHelpViewMixin, QListView):
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

        self.on_uninstall = None
        self.on_show_info = None
        self.on_context_menu = None
        self.on_toggle_group = None

        self._native_tip_row = -1
        self._init_hover_help_view()
        self.verticalScrollBar().valueChanged.connect(self._hide_native_tip)

        self.empty_label = QLabel("", self.viewport())
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet(
            f"color: {DARK['fg2']}; font-size: 9pt; background: transparent; padding: 24px; border: none;"
        )
        self.empty_label.setVisible(False)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu_requested)

    def setModel(self, model):
        super().setModel(model)
        model.modelAboutToBeReset.connect(self._on_model_about_to_reset)

    def _on_model_about_to_reset(self):
        self._close_help_popup()
        self._hide_native_tip()
        self.itemDelegate().set_hover_row(-1, self)
        self.itemDelegate().set_name_hover(-1, self)

    def set_empty_text(self, text: str):
        self.empty_label.setText(text)

    def show_empty(self, show: bool):
        self.empty_label.setVisible(show)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.empty_label.setGeometry(self.viewport().rect())

    def _hide_native_tip(self, *_args):
        self._native_tip_row = -1
        QToolTip.hideText()

    def leaveEvent(self, event):
        self._close_help_popup()
        self._hide_native_tip()
        self.itemDelegate().set_hover_row(-1, self)
        self.itemDelegate().set_name_hover(-1, self)
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        index = self.indexAt(pos)
        delegate = self.itemDelegate()

        if index.isValid() and index.data(_AppxListModel.HeaderRole) is not None:
            delegate.set_hover_row(-1, self)
            delegate.set_name_hover(-1, self)
            self.setCursor(Qt.PointingHandCursor)
            self._hide_native_tip()
            if self._hover_help_row != -1 and not self._popup_hit(pos):
                self._close_help_popup()
            super().mouseMoveEvent(event)
            return

        delegate.set_hover_row(index.row() if index.isValid() else -1, self)

        over_help = False
        if index.isValid():
            app = index.data(_AppxListModel.AppRole)
            _icon_rect, name_rect, help_rect, foreign_rect, _size_rect, delete_rect = \
                delegate.hit_rects(self.visualRect(index), app)

            over_help = self.hover_help_on_move(delegate, index.row(), app, name_rect, help_rect, pos)
            if over_help or (foreign_rect.isValid() and foreign_rect.contains(pos)) or \
                    delete_rect.contains(pos):
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)

            if delete_rect.contains(pos) and app.is_removal_blocked:
                if self._native_tip_row != index.row():
                    self._native_tip_row = index.row()
                    QToolTip.showText(self.mapToGlobal(pos), T("appx_non_removable"), self)
            elif self._native_tip_row != -1:
                self._hide_native_tip()
        else:
            delegate.set_name_hover(-1, self)
            self._hide_native_tip()
            self.setCursor(Qt.ArrowCursor)

        if not over_help and self._hover_help_row != -1 and not self._popup_hit(pos):
            self._close_help_popup()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        index = self.indexAt(pos)
        if index.isValid() and event.button() == Qt.LeftButton:
            header = index.data(_AppxListModel.HeaderRole)
            if header is not None:
                if self.on_toggle_group:
                    self.on_toggle_group(header["key"])
                return

            app = index.data(_AppxListModel.AppRole)
            delegate = self.itemDelegate()
            _icon_rect, _name_rect, help_rect, foreign_rect, _size_rect, delete_rect = \
                delegate.hit_rects(self.visualRect(index), app)

            if delete_rect.contains(pos):
                self._close_help_popup()
                if not app.is_removal_blocked and self.on_uninstall:
                    self.on_uninstall(app)
                return

            if self.hover_help_on_click(delegate, index.row(), app, help_rect, pos):
                return
            if foreign_rect.isValid() and foreign_rect.contains(pos):
                self._show_info_popup(
                    T("appx_foreign_account", account=app.foreign_account_display),
                    self.mapToGlobal(foreign_rect.bottomLeft()), width=320,
                )
                return

        super().mouseReleaseEvent(event)

    def _show_info_popup(self, text: str, global_pos, width: int):
        self._close_help_popup()
        if self.on_show_info:
            self._help_popup = self.on_show_info(text, global_pos, width)

    def _on_context_menu_requested(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return
        app = index.data(_AppxListModel.AppRole)
        if app is not None and self.on_context_menu:
            self.on_context_menu(app, self.mapToGlobal(pos))


class AppxPage(HOTSPage):
    def __init__(self, parent=None):
        super().__init__("appxInterface", FIF.STORE_MICROSOFT, T("appx_title"), parent)
        self._all_apps: list = []
        self._filter_text = ""
        self._show_system = False
        self._show_other_users = False
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._apply_filter)
        self._collapsed = load_settings().get("appx_groups_collapsed", {})
        if not isinstance(self._collapsed, dict):
            self._collapsed = {}
        self._loaded_once = False
        self._load_in_flight = False
        self._build()
        self._icon_prefetcher = IconPrefetcher(self)
        self._icon_prefetcher.progress.connect(self.view.viewport().update)
        QTimer.singleShot(900, self._ensure_loaded)

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

        self.model = _AppxListModel(self)

        self.view = _AppxListView(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(_AppxItemDelegate(self.view))
        self.view.set_empty_text(T("appx_no_results"))
        self.view.on_uninstall = self._uninstall_one
        self.view.on_show_help = self._show_help_popup
        self.view.on_show_info = self._show_info_popup
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

        desc = QLabel(T("appx_subheader"))
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent; border: none;")
        lay.addWidget(desc, 1)

        self._show_system_btn = HOTSButton(FIF.VIEW, DARK["accent"], "")
        self._show_system_btn.setFixedWidth(44)
        self._show_system_btn.clicked.connect(self._toggle_show_system)
        attach_fluent_tip(self._show_system_btn, T("opt_show_system_appx"))
        lay.addWidget(self._show_system_btn, 0, Qt.AlignTop)

        self._show_other_users_btn = HOTSButton(getattr(FIF, "PEOPLE", FIF.VIEW), DARK["accent"], "")
        self._show_other_users_btn.setFixedWidth(44)
        self._show_other_users_btn.clicked.connect(self._toggle_show_other_users)
        attach_fluent_tip(self._show_other_users_btn, T("opt_show_other_users"))
        lay.addWidget(self._show_other_users_btn, 0, Qt.AlignTop)

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
        retry_failed_icons("appx")
        self.begin_busy()
        signals = _ScanSignals(self)
        signals.done.connect(self._on_loaded)
        self._scan_signals = signals

        def worker():
            try:
                apps = ca.list_appx_apps(all_users=self._show_other_users)
            except Exception:
                apps = []
            if not is_shutting_down():
                signals.done.emit(apps)

        start_bg_thread(worker)

    def _on_loaded(self, apps: list):
        self.end_busy()
        self._loaded_once = True
        self._load_in_flight = False
        if is_shutting_down():
            return
        self._all_apps = apps
        self._apply_filter()
        self._prefetch_icons(apps)

    def _prefetch_icons(self, apps: list):
        shown = [row[1] for row in self.model._rows if row[0] == "app"]
        seen = {a.uid for a in shown}
        ordered = shown + [a for a in apps if a.uid not in seen]
        self._icon_prefetcher.start([
            ("appx", a.uid, lambda a=a: _appx_icon_source(a)) for a in ordered
        ])

    def refresh_content(self):
        self._ensure_loaded()

    def _on_search_changed(self, text: str):
        self._filter_text = (text or "").strip().lower()
        self._search_debounce.start()

    def _toggle_show_system(self):
        self._show_system = not self._show_system
        self._show_system_btn.set_accent(self._show_system)
        self._apply_filter()

    def _toggle_show_other_users(self):
        self._show_other_users = not self._show_other_users
        self._show_other_users_btn.set_accent(self._show_other_users)
        self._load_async()

    def _apply_filter(self):
        apps = self._all_apps
        if not self._show_system:
            apps = [a for a in apps if not a.is_component]
        if self._filter_text:
            apps = [a for a in apps
                    if self._filter_text in a.display_name.lower()
                    or self._filter_text in (a.publisher_display or "").lower()]

        rows = []
        for key, label, group_apps in (
            ("other", T("appx_group_other"), [a for a in apps if not a.is_microsoft]),
            ("microsoft", T("appx_group_microsoft"), [a for a in apps if a.is_microsoft]),
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

    def _toggle_group(self, key: str):
        self._collapsed[key] = not self._collapsed.get(key)
        fresh = load_settings()
        fresh["appx_groups_collapsed"] = self._collapsed
        save_settings(fresh)
        self._apply_filter()

    def _show_help_popup(self, app, global_pos):
        return show_fluent_popup_at(global_pos, _appx_details_tooltip(app), width=420)

    def _show_info_popup(self, text: str, global_pos, width: int):
        return show_fluent_popup_at(global_pos, text, width=width)

    def _show_context_menu(self, app, global_pos):
        items = []
        # Protected packages (NonRemovable, or registered only for the system
        # account; see is_removal_blocked) have the trash icon disabled in the
        # list, so the context menu must not be a back door to the same doomed
        # removal.
        if not app.is_removal_blocked:
            items.append(("✕", DARK["red"], T("menu_uninstall"), lambda: self._uninstall_one(app)))
        if app.install_location and os.path.isdir(app.install_location):
            items.append(("📁", DARK["fg2"], T("menu_open_location"), lambda: self._open_location(app)))
        if app.install_location:
            items.append(("⧉", DARK["fg2"], T("menu_copy_path"), lambda: self._copy_path(app.install_location)))
        if app.package_family_name:
            items.append(("🛍", DARK["fg2"], T("menu_open_store"), lambda: self._open_store(app)))
        if ca.find_registry_key(app):
            items.append(("🔑", DARK["fg2"], T("menu_open_registry"), lambda: ca.open_registry_key(app)))
        items.append(("🔍", "#60c8ff", T("menu_search_google"), lambda: self._search_google(app)))

        self._ctx_menu = HOTSContextMenu(self, items)
        self._ctx_menu.popup(global_pos)

    def _uninstall_one(self, app):
        if app.is_removal_blocked:
            return
        dlg = AppxUninstallWizardDialog(self, app, _appx_icon(app))
        dlg.exec()
        self._load_async()

    def _open_location(self, app):
        try:
            popen_no_busy_cursor(["explorer.exe", app.install_location])
        except Exception:
            pass

    def _copy_path(self, path: str):
        QApplication.clipboard().setText(path)

    def _open_store(self, app):
        try:
            os.startfile(ca.store_uri(app))
        except Exception:
            pass

    def _search_google(self, app):
        webbrowser.open(f"https://www.google.com/search?q={quote(app.display_name)}")
