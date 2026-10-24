from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QListView, QAbstractItemView,
    QStyledItemDelegate,
)
from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QRect, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter

from ..icons import FIF
from ..ui_parts import SearchLineEdit
from ..constants import DARK, css_color
from ..i18n import T, T_count
from ..widgets_qt import (
    HOTSPage, HOTSDialog, HOTSButton, HOTSContextMenu, attach_fluent_tip, colored_svg_icon,
    make_folder_button, show_fluent_popup_at,
)
from ..hover_help import (
    HoverHelpDelegateMixin, HoverHelpViewMixin, details_tooltip_html,
)
from ..bg_tasks import start_bg_thread, is_shutting_down
from .. import core_uninstall as cu


def _fmt_date(raw: str) -> str:
    return raw.replace("T", "  ") if raw else ""


def _details_tooltip(item: "cu.RegistryBackupEntry") -> str:
    rows = [
        (T("backups_col_program"), item.app_name),
        (T("backups_col_hive"), item.hive_name),
        (T("backups_col_key"), item.subkey),
    ]
    if item.restored_at:
        rows.append((T("backups_col_restored"), _fmt_date(item.restored_at)))
    badges = []
    if not item.exists:
        badges.append((DARK["red"], T("backups_file_missing")))
    return details_tooltip_html(rows, badges)


class _ScanSignals(QObject):
    done = Signal(list)


class _RestoreSignals(QObject):
    done = Signal(list)


class _BackupsListModel(QAbstractListModel):
    ItemRole = Qt.UserRole + 1
    IndexRole = Qt.UserRole + 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list = []

    def set_items(self, items: list):
        self.beginResetModel()
        self._items = items
        self.endResetModel()

    def items(self) -> list:
        return self._items

    def item_at(self, idx: int):
        return self._items[idx] if 0 <= idx < len(self._items) else None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        if role == self.ItemRole:
            return self._items[index.row()]
        if role == self.IndexRole:
            return index.row()
        return None

    def item_count(self) -> int:
        return len(self._items)


class _BackupItemDelegate(HoverHelpDelegateMixin, QStyledItemDelegate):
    ROW_HEIGHT = 42
    LEFT_MARGIN = 14
    RIGHT_MARGIN = 14
    BTN_SIZE = 30
    RESTORED_W = 18
    DATE_W = 152
    SIZE_W = 70
    HELP_SIZE = 20
    GAP = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._restore_icon = colored_svg_icon(FIF.HISTORY, DARK["green"], sizes=(15,))
        self._restore_icon_off = colored_svg_icon(FIF.HISTORY, DARK["gray"], sizes=(15,))
        self._delete_icon = colored_svg_icon(FIF.DELETE, DARK["red"], sizes=(15,))
        self._show_icon = colored_svg_icon(FIF.VIEW, "#60c8ff", sizes=(15,))
        self._show_icon_off = colored_svg_icon(FIF.VIEW, DARK["gray"], sizes=(15,))
        self._show_icon_portable = colored_svg_icon(FIF.VIEW, "#f0a030", sizes=(15,))
        self._init_hover_help()

    def sizeHint(self, option, index):
        return QSize(1, self.ROW_HEIGHT)

    _NAME_FONT = None

    @classmethod
    def _name_font(cls) -> QFont:
        if cls._NAME_FONT is None:
            f = QFont()
            f.setPointSize(10)
            cls._NAME_FONT = f
        return cls._NAME_FONT

    def hit_rects(self, row_rect: QRect):
        r = row_rect
        delete_rect = QRect(r.right() - self.RIGHT_MARGIN - self.BTN_SIZE,
                            r.center().y() - self.BTN_SIZE // 2,
                            self.BTN_SIZE, self.BTN_SIZE)
        restore_rect = QRect(delete_rect.left() - self.GAP - self.BTN_SIZE,
                             delete_rect.top(), self.BTN_SIZE, self.BTN_SIZE)
        restored_rect = QRect(restore_rect.left() - self.GAP - self.RESTORED_W,
                              r.top(), self.RESTORED_W, r.height())
        date_rect = QRect(restored_rect.left() - self.GAP - self.DATE_W, r.top(),
                          self.DATE_W, r.height())
        size_rect = QRect(date_rect.left() - self.GAP - self.SIZE_W, r.top(),
                          self.SIZE_W, r.height())

        name_left = self.LEFT_MARGIN
        max_name_right = size_rect.left() - self.GAP - self.HELP_SIZE - self.GAP
        max_name_width = max(0, max_name_right - name_left)
        name_rect = QRect(name_left, r.top(), max_name_width, r.height())
        return name_rect, size_rect, date_rect, restored_rect, restore_rect, delete_rect

    def _help_rect_after(self, name_rect: QRect, text_w: int) -> QRect:
        r = name_rect
        help_left = min(r.left() + text_w + self.GAP, r.right())
        return QRect(help_left, r.center().y() - self.HELP_SIZE // 2,
                     self.HELP_SIZE, self.HELP_SIZE)

    def full_hit_rects(self, row_rect: QRect, item):
        name_rect, size_rect, date_rect, restored_rect, restore_rect, delete_rect = \
            self.hit_rects(row_rect)
        text_w = 0
        if item is not None:
            fm = QFontMetrics(self._name_font())
            text_w = min(fm.horizontalAdvance(item.app_name), name_rect.width())
        help_rect = self._help_rect_after(name_rect, text_w)
        return name_rect, help_rect, size_rect, date_rect, restored_rect, restore_rect, delete_rect

    def history_rects(self, row_rect: QRect):
        r = row_rect
        delete_rect = QRect(r.right() - self.RIGHT_MARGIN - self.BTN_SIZE,
                            r.center().y() - self.BTN_SIZE // 2,
                            self.BTN_SIZE, self.BTN_SIZE)
        show_rect = QRect(delete_rect.left() - self.GAP - self.BTN_SIZE,
                          delete_rect.top(), self.BTN_SIZE, self.BTN_SIZE)
        date_rect = QRect(show_rect.left() - self.GAP - self.DATE_W, r.top(),
                          self.DATE_W, r.height())
        size_rect = QRect(date_rect.left() - self.GAP - self.SIZE_W, r.top(),
                          self.SIZE_W, r.height())
        name_rect = QRect(self.LEFT_MARGIN, r.top(),
                          max(0, size_rect.left() - self.GAP - self.LEFT_MARGIN), r.height())
        return name_rect, size_rect, date_rect, show_rect, delete_rect

    def _paint_history_row(self, painter: QPainter, option, item):
        name_rect, size_rect, date_rect, show_rect, delete_rect = self.history_rects(option.rect)

        font = self._name_font()
        font.setItalic(False)
        painter.setFont(font)
        fm = QFontMetrics(font)

        painter.setPen(QColor(DARK["fg"]))
        painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft,
                         fm.elidedText(item.app_name, Qt.ElideRight, name_rect.width()))

        painter.setPen(QColor(DARK["fg2"]))
        size_text = cu.format_size_kb(item.freed_kb)
        painter.drawText(size_rect, Qt.AlignVCenter | Qt.AlignRight, size_text)
        painter.drawText(date_rect, Qt.AlignVCenter | Qt.AlignRight, _fmt_date(item.uninstalled_at))

        if not item.log_exists:
            icon = self._show_icon_off
        elif item.portable:
            icon = self._show_icon_portable
        else:
            icon = self._show_icon
        icon.paint(painter, show_rect)
        self._delete_icon.paint(painter, delete_rect)

    def paint(self, painter: QPainter, option, index):
        item = index.data(_BackupsListModel.ItemRole)
        if item is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        self.paint_row_hover(painter, option.rect, index.row())
        painter.setPen(css_color(DARK["border_faint"]))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        if isinstance(item, cu.UninstallHistoryEntry):
            self._paint_history_row(painter, option, item)
            painter.restore()
            return

        name_rect, help_rect, size_rect, date_rect, restored_rect, restore_rect, delete_rect = \
            self.full_hit_rects(option.rect, item)

        font = self._name_font()
        missing = not item.exists
        font.setItalic(missing)
        painter.setFont(font)
        fm = QFontMetrics(font)

        painter.setPen(QColor(DARK["red"] if missing else DARK["fg"]))
        painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft,
                         fm.elidedText(item.app_name, Qt.ElideRight, name_rect.width()))
        font.setItalic(False)

        self.paint_help_icon(painter, help_rect, index.row())

        painter.setFont(font)
        painter.setPen(QColor(DARK["fg2"]))
        painter.drawText(size_rect, Qt.AlignVCenter | Qt.AlignRight,
                         T("backups_size_kb", size=item.size_kb))
        painter.drawText(date_rect, Qt.AlignVCenter | Qt.AlignRight, _fmt_date(item.deleted_at))

        if item.restored_at:
            painter.setPen(QColor(DARK["green"]))
            painter.drawText(restored_rect, Qt.AlignVCenter | Qt.AlignCenter, "✓")

        (self._restore_icon if item.exists else self._restore_icon_off).paint(painter, restore_rect)
        self._delete_icon.paint(painter, delete_rect)

        painter.restore()


class _BackupsListView(HoverHelpViewMixin, QListView):
    _BUTTON_TIP_DELAY_MS = 400

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WA_Hover, True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setFrameShape(QListView.NoFrame)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setCursor(Qt.ArrowCursor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.on_restore_item = None
        self.on_delete_item = None
        self.on_show_log = None
        self.on_delete_history = None
        self.on_context_menu = None
        self._init_hover_help_view()

        self._tip_key = None
        self._tip_text = ""
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
            self._tip_popup = show_fluent_popup_at(self._tip_pos, self._tip_text)

    def _update_button_tip(self, key, text: str, rect: QRect):
        if key == self._tip_key:
            return
        self._close_button_tip()
        if key is None or not text:
            return
        self._tip_key = key
        self._tip_text = text
        self._tip_pos = self.mapToGlobal(rect.bottomLeft())
        self._tip_timer.start()

    def _button_tip_for(self, item, restore_rect: QRect, delete_rect: QRect, pos):
        if restore_rect.contains(pos):
            text = T("backups_btn_restore") if item.exists else T("backups_file_missing")
            return ("restore", id(item)), text, restore_rect
        if delete_rect.contains(pos):
            return ("delete", id(item)), T("backups_btn_delete"), delete_rect
        return None, "", QRect()

    def _history_tip_for(self, item, show_rect: QRect, delete_rect: QRect, pos):
        if show_rect.contains(pos):
            if not item.log_exists:
                text = T("backups_history_no_log")
            elif item.portable:
                text = T("backups_history_btn_show_portable")
            else:
                text = T("backups_history_btn_show")
            return ("show", id(item)), text, show_rect
        if delete_rect.contains(pos):
            return ("delete", id(item)), T("backups_history_btn_delete"), delete_rect
        return None, "", QRect()

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

        delegate.set_hover_row(index.row() if index.isValid() else -1, self)

        over_help = False
        over_button = False
        if index.isValid():
            item = index.data(_BackupsListModel.ItemRole)
            if isinstance(item, cu.UninstallHistoryEntry):
                delegate.set_name_hover(-1, self)
                self._close_help_popup()
                _n, _s, _d, show_rect, delete_rect = delegate.history_rects(self.visualRect(index))
                key, text, tip_rect = self._history_tip_for(item, show_rect, delete_rect, pos)
                self._update_button_tip(key, text, tip_rect)
                over = show_rect.contains(pos) or delete_rect.contains(pos)
                self.setCursor(Qt.PointingHandCursor if over else Qt.ArrowCursor)
                super().mouseMoveEvent(event)
                return
            rect = self.visualRect(index)
            name_rect, help_rect, size_rect, date_rect, restored_rect, restore_rect, delete_rect = \
                delegate.full_hit_rects(rect, item)
            over_help = self.hover_help_on_move(delegate, index.row(), item, name_rect, help_rect, pos)
            over_button = restore_rect.contains(pos) or delete_rect.contains(pos)
            if item is not None:
                key, text, tip_rect = self._button_tip_for(item, restore_rect, delete_rect, pos)
                self._update_button_tip(key, text, tip_rect)
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
            delegate = self.itemDelegate()
            item = index.data(_BackupsListModel.ItemRole)
            if isinstance(item, cu.UninstallHistoryEntry):
                _n, _s, _d, show_rect, delete_rect = delegate.history_rects(self.visualRect(index))
                if show_rect.contains(pos):
                    self._close_button_tip()
                    if item.log_exists and self.on_show_log:
                        self.on_show_log(item)
                    return
                if delete_rect.contains(pos):
                    self._close_button_tip()
                    if self.on_delete_history:
                        self.on_delete_history(item)
                    return
                super().mouseReleaseEvent(event)
                return
            rect = self.visualRect(index)
            name_rect, help_rect, size_rect, date_rect, restored_rect, restore_rect, delete_rect = \
                delegate.full_hit_rects(rect, item)

            if restore_rect.contains(pos):
                self._close_help_popup()
                self._close_button_tip()
                if item is not None and item.exists and self.on_restore_item:
                    self.on_restore_item(item)
                return
            if delete_rect.contains(pos):
                self._close_help_popup()
                self._close_button_tip()
                if item is not None and self.on_delete_item:
                    self.on_delete_item(item)
                return
            if self.hover_help_on_click(delegate, index.row(), item, help_rect, pos):
                return

        super().mouseReleaseEvent(event)

    def _on_context_menu_requested(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return
        item = index.data(_BackupsListModel.ItemRole)
        if isinstance(item, cu.UninstallHistoryEntry):
            return
        idx = index.data(_BackupsListModel.IndexRole)
        if self.on_context_menu:
            self.on_context_menu(idx, self.mapToGlobal(pos))


class BackupsPage(HOTSPage):
    def __init__(self, parent=None):
        icon = FIF.HISTORY if hasattr(FIF, "HISTORY") else (FIF.SAVE if hasattr(FIF, "SAVE") else FIF.SYNC)
        super().__init__("backupsInterface", icon, T("backups_title"), parent)
        self._mode = "backups"
        self._all_items: list = []
        self._filter_text = ""
        self._restoring = False
        self._build()
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

        self.model = _BackupsListModel(self)

        self.view = _BackupsListView(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(_BackupItemDelegate(self.view))
        self.view.set_empty_text(T("backups_no_results"))
        self.view.on_restore_item = self._restore_one
        self.view.on_delete_item = self._delete_one
        self.view.on_show_log = self._show_log
        self.view.on_delete_history = self._delete_history_one
        self.view.on_show_help = self._show_help_popup
        self.view.on_context_menu = self._show_context_menu
        self.view.setStyleSheet(
            f"QListView {{ background-color: {DARK['table_bg']}; "
            f"border: 1px solid {DARK['border_soft']}; border-radius: 6px; }}"
        )
        cl_body.addWidget(self.view, 1)
        cl_body.addWidget(self._build_bottom_bar())

    def _build_top_card(self) -> QWidget:
        frame = QWidget()
        frame.setStyleSheet(self._card_style())
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(14, 12, 12, 12)
        lay.setSpacing(12)

        self._desc_label = QLabel(T("backups_subheader"))
        self._desc_label.setWordWrap(True)
        self._desc_label.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent; border: none;")
        lay.addWidget(self._desc_label, 1)

        history_btn = HOTSButton(FIF.HISTORY, DARK["fg2"], T("backups_history_toggle_btn"))
        history_btn.fit_to_content()
        history_btn.clicked.connect(self._toggle_mode)
        lay.addWidget(history_btn, 0, Qt.AlignTop)
        self._history_btn = history_btn

        self._clear_btn = HOTSButton(FIF.DELETE, DARK["red"], "")
        self._clear_btn.setFixedSize(42, 42)
        self._clear_btn.clicked.connect(self._clear_current)
        attach_fluent_tip(self._clear_btn, T("backups_clear_all_btn"))
        lay.addWidget(self._clear_btn, 0, Qt.AlignTop)

        return frame

    def _build_bottom_bar(self) -> QWidget:
        frame = QWidget()
        frame.setStyleSheet(self._card_style())
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        self._folder_btn = make_folder_button(cu.backup_folder_path(), cu.open_backup_folder)
        lay.addWidget(self._folder_btn)

        self._history_folder_btn = make_folder_button(
            cu.uninstall_logs_folder_path(), cu.open_uninstall_logs_folder)
        self._history_folder_btn.setVisible(False)
        lay.addWidget(self._history_folder_btn)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8pt; background: transparent; border: none;")
        lay.addWidget(self.status_label)
        attach_fluent_tip(self.status_label, "")
        lay.addStretch()

        self.search = SearchLineEdit()
        self.search.setPlaceholderText(T("backups_search_placeholder"))
        self.search.setFixedWidth(165)
        self.search.textChanged.connect(self._on_search_changed)
        lay.addWidget(self.search)

        refresh_btn = HOTSButton(FIF.SYNC, "#60c8ff", "")
        refresh_btn.setFixedSize(42, 42)
        refresh_btn.clicked.connect(self._load_async)
        attach_fluent_tip(refresh_btn, T("btn_refresh"))
        lay.addWidget(refresh_btn)

        return frame

    def _toggle_mode(self):
        self._mode = "history" if self._mode == "backups" else "backups"
        history = self._mode == "history"

        self._history_btn.set_accent(history)
        self._history_btn.set_label(T("backups_history_back_btn") if history else T("backups_history_toggle_btn"))
        self._history_btn.fit_to_content()

        self._desc_label.setText(T("backups_history_subheader") if history else T("backups_subheader"))
        self.view.set_empty_text(T("backups_history_no_results") if history else T("backups_no_results"))
        self._folder_btn.setVisible(not history)
        self._history_folder_btn.setVisible(history)
        attach_fluent_tip(self._clear_btn,
                          T("backups_clear_history_btn") if history else T("backups_clear_all_btn"))

        self.search.clear()
        self._filter_text = ""
        self._load_async()

    def _show_help_popup(self, item, global_pos):
        return show_fluent_popup_at(global_pos, _details_tooltip(item), width=420)

    def _load_async(self):
        self.begin_busy()
        signals = _ScanSignals(self)
        signals.done.connect(self._on_loaded)
        self._scan_signals = signals
        mode = self._mode

        def worker():
            try:
                items = cu.list_uninstall_history() if mode == "history" else cu.list_registry_backups()
            except Exception:
                items = []
            if not is_shutting_down():
                signals.done.emit(items)

        start_bg_thread(worker)

    def _on_loaded(self, items: list):
        self.end_busy()
        if is_shutting_down():
            return
        self._all_items = items
        self._apply_filter()

    def refresh_content(self):
        self._load_async()

    def _on_search_changed(self, text: str):
        self._filter_text = (text or "").strip().lower()
        self._apply_filter()

    def _apply_filter(self):
        items = self._all_items
        if self._filter_text:
            if self._mode == "history":
                items = [i for i in items if self._filter_text in i.app_name.lower()]
            else:
                items = [i for i in items
                         if self._filter_text in i.app_name.lower()
                         or self._filter_text in i.subkey.lower()]
        self.model.set_items(items)
        self.view.show_empty(not items)
        self._update_status(len(items))

    def _update_status(self, shown: int):
        total = len(self._all_items)
        history = self._mode == "history"
        if shown == total:
            key = "backups_history_status_all" if history else "backups_status_all"
            self.status_label.setText(T_count(key, shown))
        else:
            key = "backups_history_status_count" if history else "backups_status_count"
            self.status_label.setText(T(key, shown=shown, total=total))
        at_limit = (not history) and total >= cu.MAX_REGISTRY_BACKUPS
        attach_fluent_tip(self.status_label,
                          T("backups_limit_note", limit=cu.MAX_REGISTRY_BACKUPS) if at_limit else "")

    def _restore_one(self, item):
        if not HOTSDialog.ask(self, T("backups_title"), T_count("backups_confirm_restore_msg", 1)):
            return
        self._do_restore([item])

    def _do_restore(self, items: list):
        if self._restoring:
            return
        self._restoring = True
        self.begin_busy()
        signals = _RestoreSignals(self)
        signals.done.connect(self._on_restore_done)
        self._restore_signals = signals

        def worker():
            results = []
            for item in items:
                ok, err = cu.restore_registry_backup(item.path)
                results.append((item.subkey, ok, err))
            if not is_shutting_down():
                signals.done.emit(results)

        start_bg_thread(worker)

    def _on_restore_done(self, results: list):
        self._restoring = False
        self.end_busy()
        if is_shutting_down():
            return
        errors = [f"{subkey}: {err}" for subkey, ok, err in results if not ok]
        if errors:
            detail = HOTSDialog.format_error_details(errors)
            HOTSDialog.error(self, T("app_title"), T("leftover_errors_msg") + "\n\n" + detail)
        if any(ok for _subkey, ok, _err in results):
            HOTSDialog.info(self, T("app_title"), T("backups_restore_done_msg"))
        self._load_async()

    def _delete_one(self, item):
        if not HOTSDialog.ask(self, T("backups_title"), T_count("backups_confirm_delete_msg", 1)):
            return
        self._do_delete([item])

    def _do_delete(self, items: list):
        errors = []
        for item in items:
            ok, err = cu.delete_registry_backup(item.path)
            if not ok:
                errors.append(f"{item.subkey}: {err}")

        if errors:
            detail = HOTSDialog.format_error_details(errors)
            HOTSDialog.error(self, T("app_title"), T("leftover_errors_msg") + "\n\n" + detail)
        self._load_async()

    def _clear_current(self):
        if self._mode == "history":
            self._clear_history()
        else:
            self._clear_backups()

    def _clear_backups(self):
        count = len(cu.list_registry_backups())
        if not count:
            return
        if not HOTSDialog.ask(self, T("backups_title"), T("backups_confirm_clear_all_msg", count=count)):
            return
        errors = cu.delete_all_registry_backups()
        if errors:
            detail = HOTSDialog.format_error_details(errors)
            HOTSDialog.error(self, T("app_title"), T("leftover_errors_msg") + "\n\n" + detail)
        self._load_async()

    def _clear_history(self):
        count = len(cu.list_uninstall_history())
        if not count:
            return
        if not HOTSDialog.ask(self, T("backups_title"), T("backups_confirm_clear_history_msg", count=count)):
            return
        errors = cu.clear_uninstall_history()
        if errors:
            detail = HOTSDialog.format_error_details(errors)
            HOTSDialog.error(self, T("app_title"), T("leftover_errors_msg") + "\n\n" + detail)
        self._load_async()

    def _show_log(self, item):
        if not cu.open_uninstall_log(item.log_file):
            HOTSDialog.info(self, T("backups_title"), T("backups_history_no_log"))
            self._load_async()

    def _delete_history_one(self, item):
        if not HOTSDialog.ask(self, T("backups_title"), T("backups_confirm_delete_history_one_msg")):
            return
        ok, err = cu.delete_uninstall_history_entry(item)
        if not ok:
            detail = HOTSDialog.format_error_details([f"{item.app_name}: {err}"])
            HOTSDialog.error(self, T("app_title"), T("leftover_errors_msg") + "\n\n" + detail)
        self._load_async()

    def _show_context_menu(self, idx, global_pos):
        item = self.model.item_at(idx) if idx is not None else None
        if item is None or isinstance(item, cu.UninstallHistoryEntry) or not item.exists:
            return

        menu_items = [
            ("📝", DARK["fg2"], T("menu_open_notepad"),
             lambda: cu.open_backup_in_notepad(item.path)),
            ("📁", DARK["fg2"], T("menu_open_location"),
             lambda: cu.reveal_backup_in_explorer(item.path)),
        ]

        self._ctx_menu = HOTSContextMenu(self, menu_items)
        self._ctx_menu.popup(global_pos)
