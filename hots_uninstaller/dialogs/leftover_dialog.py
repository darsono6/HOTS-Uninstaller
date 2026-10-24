from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView,
)
from PySide6.QtCore import Qt, Signal, QObject

from ..icons import FIF
from ..constants import DARK, accent_rgba
from ..widgets_qt import (
    HOTSDialog, HOTSButton, RoundCheckDelegate, scrollbar_fix_qss, attach_fluent_table_tip,
)
from ..i18n import T, errors_summary
from ..bg_tasks import start_bg_thread, is_shutting_down
from .. import core_uninstall as cu
from .. import core_monitor as cm


class _ScanSignals(QObject):
    done = Signal(list)


_TYPE_LABELS = {"folder": "leftover_type_folder", "file": "leftover_type_file",
                 "registry": "leftover_type_registry", "registry_value": "leftover_type_registry_value"}


class LeftoverDialog(HOTSDialog):
    def __init__(self, parent, app: "cu.InstalledApp", items=None,
                 remove_entry: bool = False, scan_func=None, preselect=None,
                 subtitle_key: str = "leftover_subtitle"):
        height = 460 if app.is_ghost else 510
        super().__init__(parent, T("leftover_title"), min_width=680, min_height=height)
        self._app = app
        self._scan_func = scan_func or cu.scan_leftovers
        self._preselect = preselect
        self._subtitle_key = subtitle_key
        self._remove_entry = remove_entry and app.is_ghost
        self._installed = not app.is_ghost
        self._items: list[cu.LeftoverItem] = []
        self._build()
        self.adjustSize()
        self.resize(680, height)
        self.center_on_parent()
        if items is not None:
            self._on_scanned(items)
        else:
            self._scan_async()

    def _card_style(self) -> str:
        return (f"QWidget#leftoverCard {{ background: {DARK['panel_bg']}; "
                f"border: 1px solid {DARK['border_faint']}; border-radius: 6px; }}")

    def _build(self):
        cl = self.body_layout
        cl.setContentsMargins(20, 16, 20, 16)
        cl.setSpacing(10)

        self.subtitle = QLabel(T(self._subtitle_key, name=self._app.display_name))
        self.subtitle.setWordWrap(True)
        self.subtitle.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent;")
        cl.addWidget(self.subtitle)

        if self._installed:
            warn_frame = QWidget()
            warn_frame.setObjectName("leftoverWarn")
            warn_frame.setStyleSheet(
                f"QWidget#leftoverWarn {{ background: {DARK['panel_bg']}; "
                f"border: 1px solid {DARK['border_faint']}; border-radius: 6px; }}")
            warn_lay = QHBoxLayout(warn_frame)
            warn_lay.setContentsMargins(14, 10, 14, 10)
            warn = QLabel(T("leftover_installed_warning"))
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {DARK['red']}; font-size: 9pt; background: transparent; border: none;")
            warn_lay.addWidget(warn)
            cl.addWidget(warn_frame)

        self.loading_label = QLabel(T("leftover_scanning_msg"))
        self.loading_label.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent;")
        cl.addWidget(self.loading_label)

        self.uncertain_label = QLabel("")
        self.uncertain_label.setWordWrap(True)
        self.uncertain_label.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent;")
        self.uncertain_label.hide()
        cl.addWidget(self.uncertain_label)

        table_card = QWidget()
        table_card.setObjectName("leftoverCard")
        table_card.setStyleSheet(self._card_style())
        table_lay = QVBoxLayout(table_card)
        table_lay.setContentsMargins(4, 4, 4, 4)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels([
            T("leftover_col_type"), T("leftover_col_path"), T("leftover_col_size"),
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(2, 90)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setStyleSheet(
            f"QTableWidget {{ background-color: transparent; color: {DARK['fg']}; "
            f"border: none; border-radius: 6px; gridline-color: {DARK['grid_line']}; }}"
            f"QTableWidget::item {{ background-color: transparent; }}"
            f"QTableWidget::item:selected {{ background-color: {accent_rgba(0.16)}; color: {DARK['fg']}; }}"
            f"QHeaderView::section {{ background-color: {DARK['panel_bg_alt']}; color: {DARK['fg2']}; "
            f"border: none; border-bottom: 1px solid {DARK['border_soft2']}; padding: 6px; }}"
            + scrollbar_fix_qss("QTableWidget")
        )
        self.table.setItemDelegateForColumn(0, RoundCheckDelegate(self.table))
        attach_fluent_table_tip(self.table)
        self.table.itemChanged.connect(self._on_item_changed)
        table_lay.addWidget(self.table)
        cl.addWidget(table_card, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        toggle_btn = HOTSButton(FIF.ACCEPT, DARK["fg2"], T("btn_select_all"))
        toggle_btn.fit_to_content()
        toggle_btn.clicked.connect(self._on_toggle_select_all)
        btn_row.addWidget(toggle_btn)
        self._toggle_btn = toggle_btn

        delete_btn = HOTSButton(FIF.DELETE, DARK["red"], T("btn_delete_selected"), accent=True)
        delete_btn.fit_to_content()
        delete_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(delete_btn)
        self._delete_btn = delete_btn

        btn_row.addStretch()

        cl.addLayout(btn_row)

    def _scan_async(self):
        self.table.setEnabled(False)
        self._delete_btn.setEnabled(False)
        self._toggle_btn.setEnabled(False)
        signals = _ScanSignals(self)
        signals.done.connect(self._on_scanned)
        self._scan_signals = signals

        app = self._app
        scan = self._scan_func

        def worker():
            try:
                items = scan(app)
            except Exception:
                items = []
            if not is_shutting_down():
                signals.done.emit(items)

        start_bg_thread(worker)

    def _on_scanned(self, items: list):
        if is_shutting_down():
            return
        self._items = items
        self.table.setEnabled(True)
        self._delete_btn.setEnabled(bool(items))

        if not items:
            self.loading_label.setText(T("leftover_none_found"))
        else:
            self.loading_label.setText("")

        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(items))
            for row, item in enumerate(items):
                type_text = T("leftover_type_service" if cu.service_name_for_item(item) else _TYPE_LABELS.get(item.kind, item.kind))
                if item.uncertain:
                    type_text += f" ({T('leftover_userdata_tag' if item.user_data else 'leftover_uncertain_tag')})"
                type_item = QTableWidgetItem(type_text)
                if item.uncertain:
                    type_item.setToolTip(T("leftover_userdata_tip" if item.user_data else "leftover_uncertain_tip"))
                type_item.setFlags(type_item.flags() | Qt.ItemIsUserCheckable)
                if self._preselect is not None:
                    checked = bool(self._preselect(item))
                else:
                    checked = False
                type_item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                self.table.setItem(row, 0, type_item)

                path_item = QTableWidgetItem(item.path)
                path_item.setFlags(path_item.flags() & ~Qt.ItemIsEditable)
                path_item.setToolTip(item.path)
                self.table.setItem(row, 1, path_item)

                size_text = f"{item.size_kb / 1024.0:.1f} MB" if item.size_kb >= 1024 else (
                    f"{item.size_kb} KB" if item.size_kb else "")
                size_item = QTableWidgetItem(size_text)
                size_item.setFlags(size_item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, 2, size_item)
        finally:
            self.table.blockSignals(False)
        self._update_select_toggle()

    def _checked_count(self) -> int:
        n = 0
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                n += 1
        return n

    def _is_uncertain_row(self, row: int) -> bool:
        return row < len(self._items) and bool(self._items[row].uncertain)

    def _certain_rows(self) -> list:
        return [r for r in range(self.table.rowCount()) if not self._is_uncertain_row(r)]

    def _all_checked(self) -> bool:
        rows = self._certain_rows()
        if not rows:
            return False
        for row in rows:
            item = self.table.item(row, 0)
            if item is None or item.checkState() != Qt.Checked:
                return False
        return True

    def _on_toggle_select_all(self):
        self._set_all_checked(not self._all_checked())

    def _on_item_changed(self, item):
        if item.column() == 0:
            self._update_select_toggle()

    def _update_select_toggle(self):
        all_checked = self._all_checked()
        self._toggle_btn.set_accent(all_checked)
        if all_checked:
            self._toggle_btn.set_icon(FIF.CLOSE, DARK["fg2"])
            self._toggle_btn.set_label(T("btn_clear_selection"))
        else:
            self._toggle_btn.set_icon(FIF.ACCEPT, DARK["fg2"])
            self._toggle_btn.set_label(T("btn_select_all"))
        self._toggle_btn.fit_to_content()
        self._toggle_btn.setEnabled(bool(self._certain_rows()) or self._checked_count() > 0)
        self._update_uncertain_summary()

    def _update_uncertain_summary(self):
        uncertain_rows = [r for r in range(self.table.rowCount()) if self._is_uncertain_row(r)]
        if not uncertain_rows:
            self.uncertain_label.hide()
            return
        checked = sum(
            1 for r in uncertain_rows
            if self.table.item(r, 0) is not None and self.table.item(r, 0).checkState() == Qt.Checked)
        if checked:
            self.uncertain_label.setText(
                T("leftover_uncertain_summary_checked", checked=checked, count=len(uncertain_rows)))
        else:
            self.uncertain_label.setText(T("leftover_uncertain_summary", count=len(uncertain_rows)))
        self.uncertain_label.show()

    def _set_all_checked(self, checked: bool):
        self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                if checked and self._is_uncertain_row(row):
                    continue
                item = self.table.item(row, 0)
                if item is not None:
                    item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        finally:
            self.table.blockSignals(False)
        self._update_select_toggle()

    def _delete_selected(self):
        selected = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked and row < len(self._items):
                selected.append(self._items[row])

        if not selected:
            HOTSDialog.info(self, T("app_title"), T("no_selection_msg"))
            return

        selected_ids = {id(i) for i in selected}
        with_entry = self._remove_entry and all(
            id(i) in selected_ids for i in self._items if not i.uncertain)
        if with_entry:
            confirm_key = "leftover_confirm_ghost_msg"
        else:
            confirm_key = "leftover_confirm_installed_msg" if self._installed else "leftover_confirm_msg"
        message = T(confirm_key, count=len(selected))
        n_user = sum(1 for i in selected if i.user_data)
        n_uncertain = sum(1 for i in selected if i.uncertain and not i.user_data)
        if n_uncertain:
            message += "\n\n" + T("leftover_confirm_uncertain_note", n=n_uncertain)
        if n_user:
            message += "\n\n" + T("leftover_confirm_userdata_note", n=n_user)
        if not HOTSDialog.ask(self, T("leftover_title"), message):
            return

        entry_removed = False
        if with_entry:
            ok_count, errors, pending_reboot, entry_removed = cu.remove_ghost_with_leftovers(self._app, selected)
        else:
            ok_count, errors, pending_reboot = cu.delete_leftovers_logged(
                selected, app_name=self._app.display_name,
                portable=self._app.key_name.startswith("portable:"))

        if errors:
            HOTSDialog.error(self, T("app_title"), errors_summary(errors))
        if pending_reboot:
            HOTSDialog.info(self, T("app_title"), T("leftover_pending_reboot_msg"))
        if ok_count:
            HOTSDialog.info(self, T("app_title"), T("leftover_done_msg", count=ok_count))
        if (with_entry and entry_removed) or (
                not self._installed and not errors and len(selected) == len(self._items)):
            try:
                cm.delete_monitor_logs_for_app(self._app.display_name,
                                               getattr(self._app, "key_name", "") or "")
            except Exception:
                pass
        if not errors and len(selected) == len(self._items):
            self.accept()
            return

        self._scan_async()
