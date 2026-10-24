import os
import textwrap
import time
import traceback
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QStackedWidget,
    QTreeWidget, QTreeWidgetItem, QProgressBar, QAbstractItemView, QSizePolicy,
    QGraphicsOpacityEffect,
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QIcon, QBrush, QColor

import shiboken6
from ..icons import FIF
from ..ui_parts import IndeterminateProgressRing

from ..constants import DARK, hex_to_rgb, load_settings, save_settings
from ..widgets_qt import (
    HOTSDialog, HOTSButton, HOTSSwitch, RoundCheckDelegate, scrollbar_fix_qss, attach_fluent_table_tip,
    attach_fluent_tip, colored_svg_icon,
)
from ..i18n import T, errors_summary
from ..bg_tasks import start_bg_thread, is_shutting_down
from ..app_icons import app_icon_large, to_square_pixmap
from .. import core_uninstall as cu
from .. import core_appx as ca
from .. import core_monitor as cm


class _ElidedLabel(QLabel):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def set_full_text(self, text: str):
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._apply_elide()

    def _apply_elide(self):
        self.ensurePolished()
        width = self.contentsRect().width()
        if width > 20:
            shown = self.fontMetrics().elidedText(self._full_text, Qt.ElideMiddle, width)
        else:
            shown = self._full_text
        if shown != self.text():
            super().setText(shown)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()


_SCAN_STABLE_GAP = 3.0
_SCAN_MAX_ROUNDS = 7
_SCAN_STABLE_ROUNDS = 2


def _scan_key(items) -> frozenset:
    return frozenset((it.kind, (it.path or "").lower()) for it in items)


def _leftover_tooltip(item) -> str:
    if not item.uncertain:
        return f"{item.path}\n\n" + textwrap.fill(T("leftover_auto_note"), width=60, break_long_words=False)
    note_key = "leftover_userdata_note" if item.user_data else "leftover_uncertain_note"
    note = textwrap.fill(T(note_key), width=60, break_long_words=False)
    return f"{item.path}\n\n{note}"


class _WizardSignals(QObject):
    restore_point_done = Signal(bool, str)
    uninstall_finished = Signal()
    uninstall_failed = Signal(str)
    leftovers_scanned = Signal(list)
    leftover_progress = Signal(str, int, int)
    leftovers_deleted = Signal(int, int, int, int, list)
    summary_ready = Signal(int, int, int, int, bool)


class _BaseUninstallWizardDialog(HOTSDialog):

    def __init__(self, parent, display_name: str, size_display: str, icon: QIcon, warning_text: str = "", warning_severity: str = "high"):
        super().__init__(parent, T("uninstall_wizard_title"), min_width=520, min_height=440)
        self._display_name = display_name
        self._size_display = size_display
        self._icon = icon
        self._warning_text = warning_text
        self._warning_severity = warning_severity
        self._leftover_items: list = []
        self._removal_log: list = []
        self._deferred_uncertain: list = []
        self._carry = (0, 0, 0)
        self._abort_wait = False
        self._uninstall_exit_code = None
        self._uninstall_step_done = False
        self._uninstall_proc = None
        self._manual_timer = None
        self._manual_started = 0.0

        self._signals = _WizardSignals(self)
        self._signals.restore_point_done.connect(self._on_restore_point_done)
        self._signals.uninstall_finished.connect(self._on_uninstall_finished)
        self._signals.uninstall_failed.connect(self._on_uninstall_failed)
        self._signals.leftovers_scanned.connect(self._on_leftovers_scanned)
        self._signals.leftover_progress.connect(self._on_leftover_progress)
        self._signals.leftovers_deleted.connect(self._on_leftovers_deleted)
        self._signals.summary_ready.connect(self._show_summary)
        self._summary_started = False

        self._build()
        self.adjustSize()
        self.resize(520, 440)
        self._fit_height_to_content()
        self.center_on_parent()

    def _fit_height_to_content(self):
        need = self.layout().heightForWidth(self.width())
        if need > self.height():
            self.resize(self.width(), need)

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_height_to_content()

    def _start_uninstall(self):
        # Must start the actual uninstall (preferably in a background thread)
        # and, when done, emit self._signals.uninstall_finished (success) or
        # self._signals.uninstall_failed(message) (error), always after
        # checking self._is_alive(). Overridden in subclasses.
        raise NotImplementedError

    def _scan_leftovers(self) -> list:
        raise NotImplementedError

    def _is_still_installed(self) -> bool:
        return False

    def _app_freed_kb(self) -> int:
        return 0

    def _build(self):
        cl = self.body_layout
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        self.stack = QStackedWidget()
        cl.addWidget(self.stack, 1)

        self._build_confirm_page()
        self._build_progress_page()
        self._build_leftovers_page()
        self._build_summary_page()

        self.stack.setCurrentWidget(self.confirm_page)

    def reject(self):
        # Set BEFORE closing so the background threads (waiting for the
        # uninstaller, scanning/removing leftovers) stop touching this window
        # after it is closed.
        self._abort_wait = True
        super().reject()

    def _is_alive(self) -> bool:
        return (not self._abort_wait) and (not is_shutting_down()) and shiboken6.isValid(self)

    def _card_style(self, scope: str = "") -> str:
        body = (f"background: {DARK['panel_bg']}; "
                f"border: 1px solid {DARK['border_faint']}; border-radius: 6px;")
        return f"QWidget#{scope} {{ {body} }}" if scope else body

    def _save_wizard_checkbox(self, key: str, checked: bool):
        fresh = load_settings()
        fresh[key] = bool(checked)
        save_settings(fresh)

    def _build_confirm_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(12)
        lay.addStretch(1)

        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setPixmap(to_square_pixmap(self._icon, 48, trim_padding=True))
        lay.addWidget(icon_lbl, 0, Qt.AlignCenter)

        name_lbl = QLabel(self._display_name)
        name_lbl.setAlignment(Qt.AlignCenter)
        name_lbl.setWordWrap(True)
        name_lbl.setStyleSheet(f"color: {DARK['fg']}; font-size: 12pt; font-weight: 600; background: transparent;")
        lay.addWidget(name_lbl)

        if self._size_display:
            size_lbl = QLabel(self._size_display)
            size_lbl.setAlignment(Qt.AlignCenter)
            size_lbl.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent;")
            lay.addWidget(size_lbl)

        lay.addSpacing(8)

        if self._warning_text:
            warn_lbl = QLabel(f"⚠ {self._warning_text}")
            warn_lbl.setAlignment(Qt.AlignCenter)
            warn_lbl.setWordWrap(True)
            if self._warning_severity == "high":
                r, g, b = hex_to_rgb(DARK["red"])
                warn_lbl.setStyleSheet(
                    f"color: {DARK['fg']}; font-size: 8.5pt; background: rgba({r},{g},{b},0.12); "
                    f"border: 1px solid rgba({r},{g},{b},0.35); border-radius: 6px; padding: 8px 12px;"
                )
            else:
                warn_lbl.setStyleSheet(
                    f"color: {DARK['fg2']}; font-size: 8.5pt; background: {DARK['border_soft']}; "
                    f"border: 1px solid {DARK['border_soft2']}; border-radius: 6px; padding: 8px 12px;"
                )
            lay.addWidget(warn_lbl)
            lay.addSpacing(4)

        question_lbl = QLabel(T("uninstall_wizard_confirm_question"))
        question_lbl.setAlignment(Qt.AlignCenter)
        question_lbl.setStyleSheet(f"color: {DARK['fg']}; font-size: 10pt; background: transparent;")
        lay.addWidget(question_lbl)

        lay.addStretch(1)

        _wizard_settings = load_settings()
        restore_point_default = str(_wizard_settings.get("uninstall_wizard_restore_point", "1")).strip().lower() in ("1", "true", "yes")

        self.chk_restore_point = HOTSSwitch(T("uninstall_wizard_chk_restore_point"), font_pt=8.5, color=DARK['fg2'], spacing=8)
        self.chk_restore_point.setChecked(restore_point_default)
        self.chk_restore_point.setFocusPolicy(Qt.NoFocus)
        self.chk_restore_point.toggled.connect(
            lambda checked: self._save_wizard_checkbox("uninstall_wizard_restore_point", checked))
        lay.addWidget(self.chk_restore_point, 0, Qt.AlignCenter)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        start_btn = HOTSButton(FIF.DELETE, DARK["red"], T("menu_uninstall"), accent=True)
        start_btn.fit_to_content(min_width=110)
        start_btn.clicked.connect(self._on_start_clicked)
        btn_row.addWidget(start_btn)

        cancel_btn = HOTSButton(FIF.CLOSE, DARK["fg2"], T("btn_cancel"))
        cancel_btn.fit_to_content(min_width=110)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        rp = QWidget()
        rp_lay = QVBoxLayout(rp)
        rp_lay.setContentsMargins(28, 24, 28, 20)
        rp_lay.setSpacing(10)
        rp_lay.addStretch(1)
        self._rp_icon = QLabel()
        self._rp_icon.setAlignment(Qt.AlignCenter)
        self._set_rp_icon(DARK["accent"])
        rp_lay.addWidget(self._rp_icon, 0, Qt.AlignCenter)
        rp_lay.addSpacing(12)
        self._rp_ring = None
        if IndeterminateProgressRing is not None:
            self._rp_ring = IndeterminateProgressRing()
            self._rp_ring.setFixedSize(32, 32)
            rp_lay.addWidget(self._rp_ring, 0, Qt.AlignCenter)
            rp_lay.addSpacing(12)
        self._rp_label = QLabel(T("uninstall_wizard_creating_restore_point"))
        self._rp_label.setAlignment(Qt.AlignCenter)
        self._rp_label.setWordWrap(True)
        self._rp_label.setStyleSheet(f"color: {DARK['fg']}; font-size: 9pt; background: transparent;")
        rp_lay.addWidget(self._rp_label)

        self._rp_note_lbl = QLabel(T("uninstall_wizard_restore_point_failed"))
        self._rp_note_lbl.setAlignment(Qt.AlignCenter)
        self._rp_note_lbl.setWordWrap(True)
        self._rp_note_lbl.setStyleSheet(
            f"color: {DARK['fg2']}; font-size: 8.5pt; background: {DARK['border_soft']}; "
            f"border: 1px solid {DARK['border_soft2']}; border-radius: 6px; padding: 8px 12px;"
        )
        self._rp_note_lbl.hide()
        rp_lay.addWidget(self._rp_note_lbl)
        rp_lay.addStretch(1)

        self._rp_btn_row = QWidget()
        rp_btn_lay = QHBoxLayout(self._rp_btn_row)
        rp_btn_lay.setContentsMargins(0, 0, 0, 0)
        rp_btn_lay.addStretch(1)
        rp_continue_btn = HOTSButton(FIF.CHEVRON_RIGHT, DARK["accent"], T("uninstall_wizard_continue_uninstall"), accent=True)
        rp_continue_btn.fit_to_content(min_width=110)
        rp_continue_btn.clicked.connect(self._start_uninstall_step)
        rp_btn_lay.addWidget(rp_continue_btn)
        rp_cancel_btn = HOTSButton(FIF.CLOSE, DARK["fg2"], T("btn_cancel"))
        rp_cancel_btn.fit_to_content(min_width=110)
        rp_cancel_btn.clicked.connect(self.reject)
        rp_btn_lay.addWidget(rp_cancel_btn)
        rp_btn_lay.addStretch(1)
        self._rp_btn_row.hide()
        rp_lay.addWidget(self._rp_btn_row)

        self._restore_page = rp
        self.stack.addWidget(rp)

        self.confirm_page = page
        self.stack.addWidget(page)

    def _set_rp_icon(self, color: str):
        icon = FIF.HISTORY if hasattr(FIF, "HISTORY") else FIF.SAVE
        self._rp_icon.setPixmap(to_square_pixmap(colored_svg_icon(icon, color, sizes=(48,)), 48))

    def _show_restore_overlay(self, show: bool):
        if show:
            self._set_rp_icon(DARK["accent"])
            if self._rp_ring is not None:
                self._rp_ring.show()
            self._rp_label.show()
            self._rp_note_lbl.hide()
            self._rp_btn_row.hide()
        self.stack.setCurrentWidget(self._restore_page if show else self.confirm_page)

    def _build_progress_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(12)
        lay.addStretch(1)

        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setPixmap(to_square_pixmap(self._icon, 40, trim_padding=True))
        lay.addWidget(icon_lbl, 0, Qt.AlignCenter)

        if IndeterminateProgressRing is not None:
            ring = IndeterminateProgressRing()
            ring.setFixedSize(56, 56)
            lay.addWidget(ring, 0, Qt.AlignCenter)

        self._progress_label = QLabel(T("uninstall_wizard_uninstalling", name=self._display_name))
        self._progress_label.setAlignment(Qt.AlignCenter)
        self._progress_label.setWordWrap(True)
        self._progress_label.setStyleSheet(f"color: {DARK['fg']}; font-size: 10pt; background: transparent;")
        lay.addWidget(self._progress_label)

        hint_lbl = QLabel(T("uninstall_wizard_uninstalling_hint"))
        hint_lbl.setAlignment(Qt.AlignCenter)
        hint_lbl.setWordWrap(True)
        hint_lbl.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8pt; background: transparent;")
        lay.addWidget(hint_lbl)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._manual_continue_btn = HOTSButton(FIF.CHEVRON_RIGHT, DARK["accent"], T("uninstall_wizard_continue_anyway"))
        self._manual_continue_btn.fit_to_content(min_width=110)
        self._manual_continue_btn.clicked.connect(self._on_manual_continue)
        self._manual_continue_btn.hide()
        btn_row.addWidget(self._manual_continue_btn)

        cancel_btn = HOTSButton(FIF.CLOSE, DARK["fg2"], T("btn_close"))
        cancel_btn.fit_to_content(min_width=110)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self.progress_page = page
        self.stack.addWidget(page)

    _IGNORED_EXIT_CODES = (None, 0, 1223, 1602, 1641, 3010)
    _MANUAL_CONTINUE_FALLBACK_S = 180

    def _uninstall_failed_with_code(self) -> bool:
        return self._uninstall_exit_code not in self._IGNORED_EXIT_CODES

    def _set_manual_continue_enabled(self, enabled: bool):
        btn = self._manual_continue_btn
        btn.setEnabled(enabled)
        btn.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)
        if enabled:
            btn.setGraphicsEffect(None)
        else:
            effect = QGraphicsOpacityEffect(btn)
            effect.setOpacity(0.4)
            btn.setGraphicsEffect(effect)

    def _stop_manual_continue_watch(self):
        if self._manual_timer is not None:
            self._manual_timer.stop()

    def _refresh_manual_continue(self):
        if self._uninstall_step_done or not self._is_alive():
            self._stop_manual_continue_watch()
            return
        proc = self._uninstall_proc
        exited = proc is not None and not cu.uninstaller_still_running(proc, self._app)
        waited_long = time.monotonic() - self._manual_started > self._MANUAL_CONTINUE_FALLBACK_S
        if exited or waited_long or not self._is_still_installed():
            self._set_manual_continue_enabled(True)
            self._stop_manual_continue_watch()

    def _on_manual_continue(self):
        if self._is_still_installed():
            if not HOTSDialog.ask(self, T("app_title"), T("uninstall_wizard_still_installed_ask")):
                return
        self._on_uninstall_finished(manual=True)

    def _build_leftovers_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        self._leftover_title = QLabel(T("uninstall_wizard_scanning_leftovers"))
        self._leftover_title.setWordWrap(True)
        self._leftover_title.setStyleSheet(f"color: {DARK['fg']}; font-size: 10pt; background: transparent;")
        lay.addWidget(self._leftover_title)

        self._leftover_rescan_hint = QLabel(T("uninstall_wizard_rescan_hint"))
        self._leftover_rescan_hint.setAlignment(Qt.AlignCenter)
        self._leftover_rescan_hint.setWordWrap(True)
        self._leftover_rescan_hint.setStyleSheet(
            f"color: {DARK['fg2']}; font-size: 8pt; background: {DARK['border_soft']}; "
            f"border: 1px solid {DARK['border_soft2']}; border-radius: 6px; padding: 6px 12px;"
        )
        self._leftover_rescan_hint.hide()
        lay.addWidget(self._leftover_rescan_hint)

        self._leftover_size_summary = QLabel("")
        self._leftover_size_summary.setWordWrap(True)
        self._leftover_size_summary.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8pt; background: transparent;")
        self._leftover_size_summary.hide()
        lay.addWidget(self._leftover_size_summary)

        tree_card = QWidget()
        tree_card.setObjectName("wizardTreeCard")
        tree_card.setStyleSheet(self._card_style("wizardTreeCard"))
        tree_card_lay = QVBoxLayout(tree_card)
        tree_card_lay.setContentsMargins(4, 4, 4, 4)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.setStyleSheet(
            f"QTreeWidget {{ background: transparent; color: {DARK['fg']}; border: none; }}"
            f"QTreeWidget::item {{ padding: 3px 0; }}"
            + scrollbar_fix_qss("QTreeWidget")
        )
        self.tree.setItemDelegateForColumn(0, RoundCheckDelegate(self.tree))
        attach_fluent_table_tip(self.tree)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        tree_card_lay.addWidget(self.tree)
        lay.addWidget(tree_card, 1)

        self._leftover_progress_bar = QProgressBar()
        self._leftover_progress_bar.setTextVisible(False)
        self._leftover_progress_bar.setFixedHeight(6)
        self._leftover_progress_bar.setStyleSheet(
            f"QProgressBar {{ background: {DARK['panel_bg_alt']}; border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {DARK['accent']}; border-radius: 3px; }}"
        )
        self._leftover_progress_bar.hide()
        lay.addWidget(self._leftover_progress_bar)

        self._leftover_status = QLabel("")
        self._leftover_status.setWordWrap(True)
        self._leftover_status.setStyleSheet(f"color: {DARK['fg2']}; font-size: 7.5pt; background: transparent; border: none;")
        lay.addWidget(self._leftover_status)

        btn_row = QHBoxLayout()
        self._leftover_toggle_btn = HOTSButton(FIF.ACCEPT, DARK["fg2"], "")
        self._leftover_toggle_btn.setFixedSize(42, 42)
        self._leftover_toggle_btn.setEnabled(False)
        self._leftover_toggle_btn.clicked.connect(self._on_leftovers_toggle_all)
        attach_fluent_tip(self._leftover_toggle_btn, T("btn_toggle_select_all"))
        btn_row.addWidget(self._leftover_toggle_btn)
        btn_row.addStretch(1)
        self._leftover_delete_btn = HOTSButton(FIF.DELETE, DARK["red"], T("btn_delete_selected"), accent=True)
        self._leftover_delete_btn.fit_to_content(min_width=110)
        self._leftover_delete_btn.clicked.connect(self._on_leftovers_delete_clicked)
        self._leftover_delete_btn.setEnabled(False)
        btn_row.addWidget(self._leftover_delete_btn)

        self._leftover_skip_btn = HOTSButton(FIF.CLOSE, DARK["fg2"], T("btn_skip"))
        self._leftover_skip_btn.fit_to_content(min_width=110)
        self._leftover_skip_btn.clicked.connect(self._on_leftovers_skip)
        btn_row.addWidget(self._leftover_skip_btn)
        btn_row.addStretch(1)

        self._leftover_rescan_btn = HOTSButton(FIF.SYNC, "#60c8ff", "")
        self._leftover_rescan_btn.setFixedSize(42, 42)
        self._leftover_rescan_btn.clicked.connect(self._on_leftovers_rescan)
        self._leftover_rescan_btn.setEnabled(False)
        attach_fluent_tip(self._leftover_rescan_btn, T("btn_rescan"))
        btn_row.addWidget(self._leftover_rescan_btn)
        lay.addLayout(btn_row)

        self.leftovers_page = page
        self.stack.addWidget(page)

    def _build_summary_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 28, 28, 20)
        lay.setSpacing(12)
        lay.addStretch(1)

        check_lbl = QLabel("✓")
        check_lbl.setAlignment(Qt.AlignCenter)
        check_lbl.setFixedSize(72, 72)
        self._summary_check = check_lbl
        self._set_summary_check_color(DARK['green'])
        lay.addWidget(check_lbl, 0, Qt.AlignCenter)

        self._summary_label = QLabel("")
        self._summary_label.setAlignment(Qt.AlignCenter)
        self._summary_label.setWordWrap(True)
        self._summary_label.setStyleSheet(f"color: {DARK['fg']}; font-size: 10pt; background: transparent;")
        lay.addWidget(self._summary_label)

        self._summary_name_label = _ElidedLabel()
        self._summary_name_label.setAlignment(Qt.AlignCenter)
        self._summary_name_label.setStyleSheet(
            f"color: {DARK['fg']}; font-size: 10pt; font-weight: 600; background: transparent;")
        lay.addWidget(self._summary_name_label)

        self._summary_details_lbl = QLabel(T("uninstall_wizard_summary_details"))
        self._summary_details_lbl.setAlignment(Qt.AlignCenter)
        self._summary_details_lbl.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8.5pt; background: transparent;")
        lay.addWidget(self._summary_details_lbl)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        cards_row.addStretch(1)
        self._card_registry = self._make_stat_card()
        cards_row.addWidget(self._card_registry)
        self._card_files = self._make_stat_card()
        cards_row.addWidget(self._card_files)
        self._card_freed = self._make_stat_card()
        self._card_freed.hide()
        cards_row.addWidget(self._card_freed)
        cards_row.addStretch(1)
        lay.addLayout(cards_row)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        ok_btn = HOTSButton(FIF.ACCEPT, DARK["accent"], T("btn_ok"), accent=True)
        ok_btn.fit_to_content(min_width=110)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self.summary_page = page
        self.stack.addWidget(page)

    def _set_summary_check_color(self, color: str):
        self._summary_check.setStyleSheet(
            f"color: {color}; font-size: 28pt; font-weight: 600; background: transparent; "
            f"border: 3px solid {color}; border-radius: 36px;"
        )

    def _make_stat_card(self) -> QWidget:
        card = QWidget()
        card.setFixedWidth(150)
        card.setStyleSheet(self._card_style())
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(2)
        value_lbl = QLabel("0")
        value_lbl.setAlignment(Qt.AlignCenter)
        value_lbl.setStyleSheet(f"color: {DARK['accent']}; font-size: 15pt; font-weight: 600; background: transparent; border: none;")
        title_lbl = QLabel("")
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8pt; background: transparent; border: none;")
        v.addWidget(value_lbl)
        v.addWidget(title_lbl)
        card._value_lbl = value_lbl
        card._title_lbl = title_lbl
        return card

    def _on_start_clicked(self):
        if self.chk_restore_point.isChecked():
            self._show_restore_overlay(True)
            name = self._display_name
            signals = self._signals

            def worker():
                ok, err = cu.create_restore_point(T("uninstall_wizard_restore_point_desc", name=name))
                if self._is_alive():
                    signals.restore_point_done.emit(ok, err)

            start_bg_thread(worker)
        else:
            self._start_uninstall_step()

    def _on_restore_point_done(self, ok: bool, err: str):
        if not self._is_alive():
            return
        if ok:
            self._start_uninstall_step()
            return
        if self._rp_ring is not None:
            self._rp_ring.hide()
        self._set_rp_icon(DARK["fg2"])
        self._rp_label.hide()
        self._rp_note_lbl.show()
        self._rp_btn_row.show()

    def _start_uninstall_step(self):
        self.stack.setCurrentWidget(self.progress_page)
        self._manual_continue_btn.hide()
        try:
            cu.remember_app_executables(getattr(self, "_app", None))
        except Exception:
            pass
        self._start_uninstall()

    def _on_uninstall_failed(self, message: str):
        if not self._is_alive():
            return
        self._stop_manual_continue_watch()
        HOTSDialog.error(self, T("app_title"), message)
        self.reject()

    def _on_uninstall_finished(self, manual: bool = False):
        if not self._is_alive():
            return
        if self._uninstall_step_done:
            return
        self._uninstall_step_done = True
        self._stop_manual_continue_watch()
        self._manual_continue_btn.hide()
        if not manual and self._uninstall_failed_with_code() and self._is_still_installed():
            self._finish_with_summary(0, 0, 0)
            return

        self.stack.setCurrentWidget(self.leftovers_page)
        self._leftover_title.setText(T("uninstall_wizard_scanning_leftovers"))
        self._leftover_progress_bar.show()
        self._leftover_progress_bar.setRange(0, 0)
        self._start_leftover_scan(stabilize=True)

    _STABILIZE_SCAN = True

    def _start_leftover_scan(self, stabilize: bool = False):
        signals = self._signals
        stabilize = stabilize and self._STABILIZE_SCAN

        def worker():
            try:
                items = self._scan_leftovers()
            except Exception:
                items = []
            if stabilize and items:
                stable_count = 0
                for _ in range(_SCAN_MAX_ROUNDS - 1):
                    for _ in range(int(_SCAN_STABLE_GAP / 0.25)):
                        if not self._is_alive():
                            return
                        time.sleep(0.25)
                    try:
                        again = self._scan_leftovers()
                    except Exception:
                        break
                    stable_count = stable_count + 1 if _scan_key(again) == _scan_key(items) else 0
                    items = again
                    if stable_count >= _SCAN_STABLE_ROUNDS or not items:
                        break
            if self._is_alive():
                signals.leftovers_scanned.emit(items)

        start_bg_thread(worker)

    def _on_leftovers_rescan(self):
        self._leftover_rescan_btn.setEnabled(False)
        self._leftover_toggle_btn.setEnabled(False)
        self._leftover_rescan_hint.hide()
        self._leftover_delete_btn.setEnabled(False)
        self.tree.clear()
        self._leftover_size_summary.hide()
        self._leftover_title.setText(T("uninstall_wizard_scanning_leftovers"))
        self._leftover_progress_bar.show()
        self._leftover_progress_bar.setRange(0, 0)
        self._start_leftover_scan()

    def _on_leftovers_scanned(self, items: list):
        if not self._is_alive():
            return
        self._leftover_items = items
        self._leftover_progress_bar.hide()

        if not items:
            self._finish_with_summary(0, 0, 0)
            return

        self._show_leftover_list(items)

    def _show_leftover_list(self, items: list):
        self._leftover_items = items
        self._leftover_progress_bar.hide()
        self._leftover_status.setText("")
        self.tree.setEnabled(True)
        self._leftover_skip_btn.setEnabled(True)
        self._populate_leftover_tree(items)
        self._leftover_title.setText(T("uninstall_wizard_choose_leftovers"))
        self._leftover_delete_btn.setEnabled(True)
        self._leftover_toggle_btn.setEnabled(True)
        self._leftover_rescan_btn.setEnabled(True)
        self._leftover_rescan_hint.show()
        QTimer.singleShot(0, self._fit_height_to_content)

    def _populate_leftover_tree(self, items: list):
        self.tree.blockSignals(True)
        self.tree.clear()

        found_kb = sum(max(0, it.size_kb) for it in items)
        if found_kb > 0:
            self._leftover_size_summary.setText(T(
                "uninstall_wizard_leftovers_size_summary",
                found=cu.format_size_kb(found_kb),
            ))
            self._leftover_size_summary.show()
        else:
            self._leftover_size_summary.hide()

        certain = [i for i in items if not i.uncertain]
        uncertain_items = [i for i in items if i.uncertain and not i.user_data]
        user_data_items = [i for i in items if i.user_data]
        registry_items = [i for i in certain if i.kind in ("registry", "registry_value")]
        file_items = [i for i in certain if i.kind in ("file", "folder")]

        self._select_all_groups = []

        def add_group(label_key: str, group_items: list, select_all: bool = True):
            if not group_items:
                return
            group = QTreeWidgetItem([T(label_key, count=len(group_items))])
            group.setFlags(group.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            state = Qt.Unchecked
            if select_all:
                self._select_all_groups.append(group)
            children = []
            for it in group_items:
                child = QTreeWidgetItem([it.path])
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setData(0, Qt.UserRole, it)
                child.setToolTip(0, _leftover_tooltip(it))
                child.setCheckState(0, state)
                children.append(child)
            group.addChildren(children)
            group.setCheckState(0, state)
            self.tree.addTopLevelItem(group)
            group.setExpanded(False)
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            group.setForeground(0, QBrush(QColor(DARK["accent"])))

        self.tree.setUpdatesEnabled(False)
        add_group("uninstall_wizard_group_registry", registry_items)
        add_group("uninstall_wizard_group_files", file_items)
        add_group("uninstall_wizard_group_uncertain", uncertain_items, select_all=False)
        add_group("uninstall_wizard_group_userdata", user_data_items, select_all=False)
        self.tree.setUpdatesEnabled(True)
        self.tree.blockSignals(False)

    def _on_leftovers_toggle_all(self):
        groups = [g for g in self._select_all_groups if g.childCount()]
        all_checked = bool(groups) and all(
            g.child(j).checkState(0) == Qt.Checked for g in groups for j in range(g.childCount()))
        state = Qt.Unchecked if all_checked else Qt.Checked
        self.tree.blockSignals(True)
        for g in groups:
            for j in range(g.childCount()):
                g.child(j).setCheckState(0, state)
            g.setCheckState(0, state)
        self.tree.blockSignals(False)
        self.tree.viewport().update()

    def _on_leftovers_skip(self):
        self._finish_with_summary(0, 0, 0)

    def _on_leftovers_delete_clicked(self):
        selected = []
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            for j in range(group.childCount()):
                child = group.child(j)
                if child.checkState(0) == Qt.Checked:
                    item = child.data(0, Qt.UserRole)
                    if item is not None:
                        selected.append(item)

        if not selected:
            HOTSDialog.info(self, T("app_title"), T("no_selection_msg"))
            return

        key = "leftover_confirm_installed_msg" if self._is_still_installed() else "leftover_confirm_msg"
        message = T(key, count=len(selected))
        n_user = sum(1 for i in selected if i.user_data)
        n_uncertain = sum(1 for i in selected if i.uncertain and not i.user_data)
        if n_uncertain:
            message += "\n\n" + T("leftover_confirm_uncertain_note", n=n_uncertain)
        if n_user:
            message += "\n\n" + T("leftover_confirm_userdata_note", n=n_user)
        if not HOTSDialog.ask(self, T("app_title"), message):
            return

        self._begin_deletion(selected)

    def _begin_deletion(self, items: list):
        self._leftover_title.setText(T("uninstall_wizard_deleting_leftovers"))
        self.tree.setEnabled(False)
        self._leftover_skip_btn.setEnabled(False)
        self._leftover_delete_btn.setEnabled(False)
        self._leftover_rescan_btn.setEnabled(False)
        self._leftover_rescan_hint.hide()
        self._leftover_progress_bar.show()
        self._leftover_progress_bar.setRange(0, 0)

        signals = self._signals

        def worker():
            # Whatever happens, the wizard must leave the "please wait" page.
            try:
                run()
            except Exception:
                traceback.print_exc()
                if self._is_alive():
                    signals.leftovers_deleted.emit(0, 0, 0, 0, [T("leftover_errors_msg")])

        def run():
            registry_ok = 0
            files_ok = 0
            files_kb = 0
            pending_reboot = 0
            errors = []

            # One shared registry backup for the WHOLE operation (not a
            # separate file per key); see backup_registry_keys_for_items() in
            # core_uninstall.py. Without it the loop below, which removes
            # leftovers ONE BY ONE to report progress step by step, would
            # create a separate .reg file every time even though logically it
            # is a single uninstall.
            _, backup_err = cu.backup_registry_keys_for_items(items, app_name=self._app.display_name)
            if backup_err:
                errors.append(T("err_registry_backup_failed_general", error=backup_err))

            assoc_plan = cu.plan_association_restores(items)

            ordered = sorted(items, key=cu.deletion_order_key)
            for idx, item in enumerate(ordered):
                if not self._is_alive():
                    break
                signals.leftover_progress.emit(item.path, idx + 1, len(ordered))
                ok_count, single_errors, single_pending = cu.delete_leftovers(
                    [item], app_name=self._app.display_name, skip_registry_backup=True)
                if ok_count == 1:
                    kept = cu.pop_kept_details(item.path)
                    contents = cu.pop_key_contents(item.path)
                    if kept:
                        self._removal_log.append(("partial", item.kind, item.path,
                                                  "not removed, other data remains: " + "; ".join(kept)))
                    else:
                        self._removal_log.append(("ok", item.kind, item.path, contents))
                    if item.kind in ("registry", "registry_value"):
                        registry_ok += 1
                    else:
                        files_ok += 1
                        files_kb += item.size_kb
                elif single_pending == 1:
                    scheduled = cu.pop_reboot_details(item.path)
                    detail = ""
                    if scheduled:
                        sample = ", ".join(os.path.basename(x) for x in scheduled[:3])
                        detail = (f"{len(scheduled)} locked item(s) scheduled for permanent deletion "
                                  f"at restart, e.g. {sample}")
                    self._removal_log.append(("reboot", item.kind, item.path, detail))
                    files_ok += 1
                    files_kb += item.size_kb
                    pending_reboot += 1
                else:
                    skipped = cu.pop_protected_skip(item.path)
                    if skipped:
                        self._removal_log.append(("skipped", item.kind, item.path, skipped))
                        continue
                    self._removal_log.append(("error" if single_errors else "gone", item.kind,
                                              item.path, "; ".join(single_errors)))
                    errors.extend(single_errors)
            errors.extend(cu.apply_association_restores(assoc_plan))
            if self._is_alive():
                signals.leftovers_deleted.emit(registry_ok, files_ok, files_kb, pending_reboot, errors)

        start_bg_thread(worker)

    def _on_leftover_progress(self, path: str, done: int, total: int):
        if not self._is_alive():
            return
        if self._leftover_progress_bar.maximum() != total:
            self._leftover_progress_bar.setRange(0, total)
        self._leftover_progress_bar.setValue(done)
        self._leftover_status.setText(path)

    def _on_leftovers_deleted(self, registry_count: int, files_count: int, files_kb: int,
                               pending_reboot: int, errors: list):
        if not self._is_alive():
            return
        parts = []
        if errors:
            parts.append(errors_summary(errors))
        if pending_reboot:
            parts.append(T("leftover_pending_reboot_msg"))
        if parts:
            show = HOTSDialog.error if errors else HOTSDialog.info
            show(self, T("app_title"), "\n\n".join(parts))
        deferred, self._deferred_uncertain = self._deferred_uncertain, []
        if deferred:
            self._carry = (registry_count, files_count, files_kb)
            self._show_leftover_list(deferred)
            return
        self._finish_with_summary(registry_count, files_count, files_kb)

    def _finish_with_summary(self, registry_count: int, files_count: int, files_kb: int):
        if self._summary_started:
            return
        self._summary_started = True
        carry_reg, carry_files, carry_kb = self._carry
        self._carry = (0, 0, 0)
        registry_count += carry_reg
        files_count += carry_files
        files_kb += carry_kb
        for btn in (self._leftover_skip_btn, self._leftover_delete_btn, self._leftover_rescan_btn):
            btn.setEnabled(False)
        signals = self._signals

        def worker():
            total_kb = max(0, files_kb)
            unconfirmed = False
            try:
                unconfirmed = self._is_still_installed()
                total_kb = 0 if unconfirmed else self._app_freed_kb() + max(0, files_kb)
                log_file = ""
                try:
                    log_file = cu.write_uninstall_log(self._display_name, total_kb,
                                                      list(self._removal_log), confirmed=not unconfirmed)
                except Exception:
                    traceback.print_exc()
                try:
                    cu.record_uninstall_history(self._display_name, freed_kb=total_kb, log_file=log_file)
                except Exception:
                    traceback.print_exc()
                if not unconfirmed:
                    try:
                        cm.delete_monitor_logs_for_app(self._display_name,
                                                       getattr(self._app, "key_name", "") or "")
                    except Exception:
                        pass
            except Exception:
                traceback.print_exc()
            if self._is_alive():
                signals.summary_ready.emit(registry_count, files_count, files_kb, total_kb, unconfirmed)

        start_bg_thread(worker)

    def _show_summary(self, registry_count: int, files_count: int, files_kb: int,
                      total_kb: int, unconfirmed: bool):
        if not self._is_alive():
            return
        summary_key = "uninstall_wizard_summary_unconfirmed_msg" if unconfirmed else "uninstall_wizard_summary_msg"
        code = self._uninstall_exit_code
        error_shown = unconfirmed and self._uninstall_failed_with_code()
        self._summary_details_lbl.setVisible(not error_shown)
        if error_shown:
            self._summary_label.setText(T("uninstall_wizard_summary_error_code_msg",
                                          code=f"0x{code:08X}" if code > 0xFFFF else str(code)))
        else:
            self._summary_label.setText(T(summary_key))
        self._summary_name_label.set_full_text(self._display_name)
        self._set_summary_check_color(DARK['fg2'] if unconfirmed else DARK['green'])

        self._card_registry._value_lbl.setText(str(registry_count))
        self._card_registry._title_lbl.setText(T("uninstall_wizard_card_registry"))

        if files_count == 0:
            size_text = "0 KB"
        elif files_kb >= 1024:
            size_text = f"{files_kb / 1024.0:.1f} MB"
        elif files_kb > 0:
            size_text = f"{files_kb} KB"
        else:
            size_text = T("uninstall_wizard_less_than_1kb")
        self._card_files._value_lbl.setText(size_text)
        self._card_files._title_lbl.setText(T("uninstall_wizard_card_files_count", count=files_count))

        show_freed = total_kb > 0
        if show_freed:
            self._card_freed._value_lbl.setText(cu.format_size_kb(total_kb))
            self._card_freed._title_lbl.setText(T("uninstall_wizard_card_freed"))
        self._card_freed.setVisible(show_freed)
        card_w = 140 if show_freed else 150
        for card in (self._card_registry, self._card_files, self._card_freed):
            card.setFixedWidth(card_w)

        self.stack.setCurrentWidget(self.summary_page)


class UninstallWizardDialog(_BaseUninstallWizardDialog):

    def __init__(self, parent, app: "cu.InstalledApp"):
        self._app = app
        size_display = app.size_display if app.size_display else None
        if app.is_chained_msi_subpackage:
            warning_text, warning_severity = T("win32_chained_subpackage_warning"), "low"
        else:
            warning_text, warning_severity = "", "high"
        super().__init__(parent, app.display_name, size_display, app_icon_large(app),
                          warning_text=warning_text, warning_severity=warning_severity)

    def _is_still_installed(self) -> bool:
        try:
            return cu._uninstall_key_exists(self._app)
        except Exception:
            return False

    def _app_freed_kb(self) -> int:
        try:
            if cu._uninstall_key_exists(self._app):
                return 0
        except Exception:
            return 0
        return max(0, self._app.estimated_size_kb)

    def _start_uninstall(self):
        self._manual_continue_btn.show()
        self._set_manual_continue_enabled(False)
        self._manual_started = time.monotonic()
        self._manual_timer = QTimer(self)
        self._manual_timer.setInterval(1000)
        self._manual_timer.timeout.connect(self._refresh_manual_continue)
        self._manual_timer.start()
        signals = self._signals

        def worker():
            proc, err = cu.run_uninstaller_process(self._app, silent=False)
            self._uninstall_proc = proc
            if not self._is_alive():
                return
            if proc is None:
                label = T("err_no_uninstall_string") if err == "no_uninstall_string" else T("err_uninstall_failed", error=err)
                signals.uninstall_failed.emit(label)
                return
            cu.wait_for_uninstall_completion(proc, self._app, should_abort=lambda: not self._is_alive())
            try:
                self._uninstall_exit_code = proc.poll()
            except Exception:
                pass
            if self._is_alive():
                signals.uninstall_finished.emit()

        start_bg_thread(worker)

    def _scan_leftovers(self) -> list:
        return cu.scan_leftovers(self._app)


class AppxUninstallWizardDialog(_BaseUninstallWizardDialog):

    _STABILIZE_SCAN = False

    def __init__(self, parent, app: "ca.AppxApp", icon: QIcon):
        self._app = app
        if app.is_risky_core:
            warning_text, warning_severity = T("appx_risky_core_warning"), "high"
        elif app.is_uncertain_microsoft:
            warning_text, warning_severity = T("appx_uncertain_microsoft_warning"), "low"
        elif app.is_side_extension or app.is_background_component:
            warning_text, warning_severity = T("appx_side_extension_warning"), "low"
        else:
            warning_text, warning_severity = "", "high"
        size_display = app.size_display if app.size_display else None
        super().__init__(parent, app.display_name, size_display, icon, warning_text=warning_text, warning_severity=warning_severity)

    def _start_uninstall(self):
        app = self._app
        signals = self._signals

        def worker():
            ok, err = ca.uninstall_appx_app(app)
            if not self._is_alive():
                return
            if ok:
                signals.uninstall_finished.emit()
            else:
                signals.uninstall_failed.emit(T("err_uninstall_failed", error=err))

        start_bg_thread(worker)

    def _app_freed_kb(self) -> int:
        return max(0, self._app.size_kb)

    def _scan_leftovers(self) -> list:
        return ca.scan_appx_leftovers(self._app)
