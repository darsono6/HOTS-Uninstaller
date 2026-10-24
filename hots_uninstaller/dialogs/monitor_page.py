import contextlib
import ctypes
import faulthandler
import os
import re
import subprocess
import time
import sys
from ctypes import wintypes

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QFileDialog, QLineEdit, QStackedWidget, QSizePolicy,
)
from PySide6.QtCore import (
    Qt, QObject, QPoint, QPointF, QRectF, QSize, QTimer, Signal, QAbstractNativeEventFilter,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QApplication

from ..icons import FIF
from ..constants import DARK, accent_rgba, css_color, load_settings, save_settings
from ..i18n import T
from ..widgets_qt import (
    HOTSPage, HOTSDialog, HOTSButton, attach_fluent_table_tip, attach_fluent_tip,
)
from ..bg_tasks import start_bg_thread, is_shutting_down
from ..ui_parts import PhaseProgressBar
from ..proc_utils import system_exe
from .. import core_monitor as cm
from .. import core_uninstall as cu


@contextlib.contextmanager
def _faulthandler_paused():
    # The native Windows file dialog raises and handles RPC_E_WRONG_THREAD
    # internally; faulthandler would log it as a fatal exception.
    was_enabled = faulthandler.is_enabled()
    if was_enabled:
        faulthandler.disable()
    try:
        yield
    finally:
        if was_enabled:
            try:
                faulthandler.enable(file=sys.stderr, all_threads=True)
            except Exception:
                pass


def _fmt_size(size_kb: int) -> str:
    if size_kb <= 0:
        return ""
    mb = size_kb / 1024.0
    if mb >= 1024:
        return f"{mb / 1024.0:.2f} GB"
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{size_kb} KB"


_TYPE_LABELS = {
    "folder": "leftover_type_folder",
    "file": "leftover_type_file",
    "registry": "leftover_type_registry",
    "registry_value": "leftover_type_registry_value",
}

_GROUP_ORDER = [
    (("registry", "registry_value"), "monitor_group_registry"),
    (("folder", "file"), "monitor_group_files"),
]


def _group_index(kind: str) -> int:
    for gi, (kinds, _label_key) in enumerate(_GROUP_ORDER):
        if kind in kinds:
            return gi
    return len(_GROUP_ORDER)


def _fmt_count(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _guess_app_name(installer_path: str) -> str:
    base = os.path.splitext(os.path.basename(installer_path))[0]
    suffix_re = re.compile(
        r"(?i)[\s_.-]+(setup|install(?:er)?|x64|x86|win64|win32|win(?:dows)?|\d+(?:\.\d+)+)$")
    while True:
        new_base = suffix_re.sub("", base)
        if new_base == base:
            break
        base = new_base
    base = re.sub(r"[_.\-]+", " ", base).strip()
    return base.title() if base else ""


class _SnapshotSignals(QObject):
    done = Signal(object)
    progress = Signal(int)


class _CompareSignals(QObject):
    done = Signal(object)
    progress = Signal(int)


class _InstallWaitSignals(QObject):
    # Carries the run token. The slot is a QObject method (not a lambda): only
    # then does Qt guarantee execution in the GUI thread. With a lambda, a
    # signal emitted from a worker thread could run _finish_monitoring outside
    # the GUI thread (widget changes from a foreign thread), which ended in
    # silent unresponsiveness.
    done = Signal(int)
    failed = Signal(int)
    settling = Signal(int)


class _DropZone(QWidget):
    clicked = Signal()
    dropped = Signal(list)

    _HEIGHT = 64

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_active = False
        self._hover = False
        self.setFixedHeight(self._HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setAcceptDrops(True)
        attach_fluent_tip(self, T("monitor_drop_hint"))

    def sizeHint(self):
        return QSize(400, self._HEIGHT)

    def set_drag_active(self, on: bool):
        if self._drag_active != on:
            self._drag_active = on
            self.update()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.set_drag_active(True)
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self.set_drag_active(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self.set_drag_active(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not paths:
            e.ignore()
            return
        e.acceptProposedAction()
        QTimer.singleShot(0, lambda p=paths: self.dropped.emit(p))

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        accent = QColor(DARK["accent"])
        lit = self._drag_active or self._hover

        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        fill = QColor(accent)
        fill.setAlphaF(0.18 if self._drag_active else (0.10 if self._hover else 0.04))
        border = QColor(accent)
        border.setAlphaF(1.0 if self._drag_active else (0.75 if self._hover else 0.45))
        pen = QPen(border, 2.0)
        pen.setStyle(Qt.SolidLine if self._drag_active else Qt.DashLine)
        p.setPen(pen)
        p.setBrush(fill)
        p.drawRoundedRect(rect, 12, 12)

        title_font = QFont(self.font())
        title_font.setPointSizeF(10.5)
        title_font.setBold(True)
        hint_font = QFont(self.font())
        hint_font.setPointSizeF(8.5)
        title = T("monitor_drop_release") if self._drag_active else T("monitor_drop_title")
        hint = T("monitor_drop_hint")
        title_w = QFontMetrics(title_font).horizontalAdvance(title)
        hint_w = QFontMetrics(hint_font).horizontalAdvance(hint)

        icon_w, gap = 34.0, 14.0
        text_w = float(max(title_w, hint_w))
        left = rect.center().x() - (icon_w + gap + text_w) / 2
        left = max(left, rect.left() + 12)
        cy = rect.center().y()

        drop = 3 if self._drag_active else 0
        icon_pen = QPen(accent if lit else css_color(DARK["fg2"]), 2.6)
        icon_pen.setCapStyle(Qt.RoundCap)
        icon_pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(icon_pen)
        p.setBrush(Qt.NoBrush)
        ix = left + icon_w / 2
        top = cy - 15
        p.drawLine(QPointF(ix, top + drop), QPointF(ix, top + 18 + drop))
        p.drawLine(QPointF(ix - 7, top + 11 + drop), QPointF(ix, top + 18 + drop))
        p.drawLine(QPointF(ix + 7, top + 11 + drop), QPointF(ix, top + 18 + drop))
        p.drawPolyline([QPointF(ix - 15, top + 18), QPointF(ix - 15, top + 29),
                        QPointF(ix + 15, top + 29), QPointF(ix + 15, top + 18)])

        tx = left + icon_w + gap
        p.setFont(title_font)
        p.setPen(css_color(DARK["fg"]))
        p.drawText(QRectF(tx, cy - 20, text_w + 4, 22), Qt.AlignLeft | Qt.AlignVCenter, title)
        p.setFont(hint_font)
        p.setPen(css_color(DARK["fg2"]))
        p.drawText(QRectF(tx, cy + 2, text_w + 4, 18), Qt.AlignLeft | Qt.AlignVCenter, hint)
        p.end()


_WM_DROPFILES = 0x0233


def _log(msg: str) -> None:
    try:
        print(f"[drop] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _enable_native_file_drop(hwnd: int, on: bool) -> None:
    try:
        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        if on and shell32.IsUserAnAdmin():
            fn = user32.ChangeWindowMessageFilterEx
            fn.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p]
            fn.restype = wintypes.BOOL
            results = [bool(fn(hwnd, msg, 1, None)) for msg in (0x0233, 0x004A, 0x0049)]
            if not all(results):
                _log(f"UIPI message filter: {results}")
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        shell32.DragAcceptFiles(hwnd, bool(on))
        if on:
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            accepts = bool(user32.GetWindowLongW(hwnd, -20) & 0x10)
            if not accepts:
                _log(f"window {hwnd}: DragAcceptFiles not active")
    except Exception as e:
        _log(f"failed to set up native file drop: {e}")


_DRAGDROP_E_NOTREGISTERED = 0x80040100


def _revoke_ole_drop_target(hwnd: int) -> int:
    # Revokes the OLE drop target that Qt registers on the window as soon as ANY
    # widget accepts drops (QLineEdit does by default). While the window has an
    # OLE target, Explorer tries ONLY OLE; that path is blocked across privilege
    # levels, so neither the drop event nor WM_DROPFILES reaches the window.
    # Without an OLE target, Explorer falls back to WM_DROPFILES (the window has
    # DragAcceptFiles). This is done only for an elevated process, where OLE
    # doesn't work anyway. Returns an HRESULT (0 = revoked).
    ole32 = ctypes.windll.ole32
    ole32.RevokeDragDrop.argtypes = [wintypes.HWND]
    ole32.RevokeDragDrop.restype = ctypes.c_long
    return int(ole32.RevokeDragDrop(hwnd)) & 0xFFFFFFFF


def _read_hdrop(hdrop: int) -> tuple[list[str], tuple[int, int]]:
    shell32 = ctypes.windll.shell32
    shell32.DragQueryPoint.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.POINT)]
    shell32.DragQueryPoint.restype = wintypes.BOOL
    point = wintypes.POINT()
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragFinish.argtypes = [ctypes.c_void_p]
    paths: list[str] = []
    try:
        shell32.DragQueryPoint(hdrop, ctypes.byref(point))
        count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        for i in range(min(count, 64)):
            n = shell32.DragQueryFileW(hdrop, i, None, 0)
            buf = ctypes.create_unicode_buffer(n + 1)
            shell32.DragQueryFileW(hdrop, i, buf, n + 1)
            paths.append(buf.value)
    finally:
        shell32.DragFinish(hdrop)
    return paths, (point.x, point.y)


class _NativeDropFilter(QAbstractNativeEventFilter):
    def __init__(self, page):
        super().__init__()
        self._page = page

    def nativeEventFilter(self, event_type, message):
        try:
            if bytes(event_type) != b"windows_generic_MSG":
                return False, 0
            msg = wintypes.MSG.from_address(int(message))
            if msg.message != _WM_DROPFILES:
                return False, 0
            page = self._page
            target = int(msg.hWnd or 0)
            try:
                ours = int(page.window().winId())
            except Exception:
                ours = page._uipi_hwnd
            if not page.isVisible() or target != ours:
                _log(f"WM_DROPFILES skipped: visible={page.isVisible()} "
                     f"target_window={target} main_window={ours}")
                return False, 0
            paths, (px, py) = _read_hdrop(msg.wParam)
            if not page._point_in_drop_zone(px, py):
                _log("WM_DROPFILES outside the drop zone - skipped")
                return True, 0
            _log(f"WM_DROPFILES: {len(paths)} file(s)")
            QTimer.singleShot(0, lambda p=paths: page._handle_dropped_paths(p))
            return True, 0
        except Exception as e:
            _log(f"native filter error: {e}")
            return False, 0


class MonitorPage(HOTSPage):
    STATE_IDLE = "idle"
    STATE_SNAPSHOTTING = "snapshotting"
    STATE_MONITORING = "monitoring"
    STATE_COMPARING = "comparing"
    STATE_DONE = "done"

    def __init__(self, parent=None):
        super().__init__("monitorInterface", FIF.SEARCH, T("monitor_title"), parent)
        self._before_snapshot = None
        self._results: list = []
        self._state = self.STATE_IDLE
        self._pending_installer_path = ""
        self._installer_path = ""
        self._monitor_token = 0
        self._auto_watching = False
        self._settle_started = 0.0
        self._settle_timer = QTimer(self)
        self._settle_timer.setInterval(300)
        self._settle_timer.timeout.connect(self._tick_settle)
        try:
            settings = load_settings()
            self._snapshot_estimate = max(0, int(settings.get("monitor_snapshot_items", 0)))
            self._settle_estimate = max(0.0, float(settings.get("monitor_settle_seconds", 0)))
        except (TypeError, ValueError):
            self._snapshot_estimate = 0
            self._settle_estimate = 0.0
        self._saved_app_name = ""
        self._saved_key_name = ""
        self._unsaved_log = None
        self._uipi_hwnd = 0
        self._native_filter = None
        try:
            cm.prune_stale_monitor_logs()
        except Exception:
            pass
        self._build()
        self._update_state_ui()

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
        cl_body.addWidget(self._build_control_panel())

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels([
            T("leftover_col_type"), T("leftover_col_path"), T("leftover_col_size"),
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(2, 90)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setStyleSheet(
            f"QTableWidget {{ background-color: {DARK['table_bg']}; alternate-background-color: {DARK['table_alt_bg']}; "
            f"color: {DARK['fg']}; border: 1px solid {DARK['border_soft']}; border-radius: 6px; }}"
            f"QTableWidget::item {{ background-color: transparent; border-bottom: 1px solid {DARK['border_faint']}; padding: 2px 6px; }}"
            f"QTableWidget::item:selected {{ background-color: {accent_rgba(0.10)}; color: {DARK['fg']}; }}"
            f"QHeaderView::section {{ background-color: {DARK['header_bg']}; color: {DARK['fg2']}; "
            f"border: none; border-right: 1px solid {DARK['border_strong']}; border-bottom: 1px solid {DARK['border_soft']}; padding: 4px 6px; }}"
        )
        attach_fluent_table_tip(self.table)
        cl_body.addWidget(self.table, 1)

    def _build_top_card(self) -> QWidget:
        frame = QWidget()
        frame.setStyleSheet(self._card_style())
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(14, 12, 12, 12)
        lay.setSpacing(12)

        desc = QLabel(T("monitor_subheader"))
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent; border: none;")
        lay.addWidget(desc, 1)

        self._export_btn = HOTSButton(FIF.SAVE if hasattr(FIF, "SAVE") else FIF.DOCUMENT, DARK["fg2"], T("btn_export_list"))
        self._export_btn.fit_to_content()
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._export_list)
        lay.addWidget(self._export_btn, 0, Qt.AlignTop)

        self._save_log_btn = HOTSButton(FIF.SAVE if hasattr(FIF, "SAVE") else FIF.DOCUMENT, DARK["fg2"], T("btn_save_monitor_log"))
        self._save_log_btn.fit_to_content()
        self._save_log_btn.setEnabled(False)
        self._save_log_btn.clicked.connect(self._save_unsaved_log)
        lay.addWidget(self._save_log_btn, 0, Qt.AlignTop)

        return frame

    def _build_control_panel(self) -> QWidget:
        frame = QWidget()
        frame.setStyleSheet(self._card_style())
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        self._stage = QStackedWidget()
        self._stage.setObjectName("monStage")
        self._stage.setStyleSheet("QStackedWidget#monStage { background: transparent; border: none; }")
        self._stage.setFixedHeight(_DropZone._HEIGHT)

        self.drop_zone = _DropZone()
        self.drop_zone.clicked.connect(self._pick_and_run_installer)
        self.drop_zone.dropped.connect(self._handle_dropped_paths)
        self._stage.addWidget(self.drop_zone)

        self._busy_panel = QWidget()
        self._busy_panel.setObjectName("monBusy")
        self._busy_panel.setStyleSheet("QWidget#monBusy { background: transparent; border: none; }")
        busy_lay = QVBoxLayout(self._busy_panel)
        busy_lay.setContentsMargins(4, 0, 4, 0)
        busy_lay.setSpacing(4)
        busy_lay.addStretch(1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {DARK['fg']}; font-size: 9.5pt; background: transparent; border: none;")
        busy_lay.addWidget(self.status_label)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color: {DARK['fg2']}; font-size: 8.5pt; background: transparent; border: none;")
        self.progress_label.setVisible(False)
        busy_lay.addWidget(self.progress_label)

        self._busy_bar = PhaseProgressBar()
        self._busy_bar.setVisible(False)
        busy_lay.addWidget(self._busy_bar)
        busy_lay.addStretch(1)
        self._stage.addWidget(self._busy_panel)
        lay.addWidget(self._stage)

        name_wrap = QWidget()
        name_wrap.setObjectName("monNameRow")
        name_wrap.setStyleSheet("QWidget#monNameRow { background: transparent; border: none; }")
        name_wrap.setFixedHeight(42)
        name_row = QHBoxLayout(name_wrap)
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(8)
        name_label = QLabel(T("monitor_app_name_label"))
        name_label.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent; border: none;")
        name_row.addWidget(name_label, 0, Qt.AlignVCenter)
        self.app_name_edit = QLineEdit()
        self.app_name_edit.setReadOnly(True)
        self.app_name_edit.setFixedWidth(260)
        name_row.addWidget(self.app_name_edit, 0, Qt.AlignVCenter)

        self.done_label = QLabel("")
        self.done_label.setStyleSheet(f"color: {DARK['fg2']}; font-size: 9pt; background: transparent; border: none;")
        self.done_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        name_row.addWidget(self.done_label, 1, Qt.AlignVCenter)

        self.finish_btn = HOTSButton(FIF.ACCEPT, DARK["green"], T("btn_finish_monitor"), accent=True)
        self.finish_btn.fit_to_content()
        self.finish_btn.clicked.connect(self._finish_monitoring)
        name_row.addWidget(self.finish_btn, 0, Qt.AlignVCenter)

        self.cancel_btn = HOTSButton(FIF.CLOSE, DARK["fg2"], T("btn_cancel_monitor"))
        self.cancel_btn.fit_to_content()
        self.cancel_btn.clicked.connect(self._cancel_monitoring)
        name_row.addWidget(self.cancel_btn, 0, Qt.AlignVCenter)

        lay.addWidget(name_wrap)
        return frame

    def _set_state(self, state: str):
        self._state = state
        self._update_state_ui()

    def _update_state_ui(self):
        s = self._state
        busy = s in (self.STATE_SNAPSHOTTING, self.STATE_MONITORING, self.STATE_COMPARING)
        self._stage.setCurrentWidget(self._busy_panel if busy else self.drop_zone)
        self.finish_btn.setVisible(s == self.STATE_MONITORING)
        self.cancel_btn.setVisible(s in (self.STATE_SNAPSHOTTING, self.STATE_MONITORING))

        status_map = {
            self.STATE_IDLE: T("monitor_status_idle"),
            self.STATE_SNAPSHOTTING: T("monitor_status_snapshotting"),
            self.STATE_MONITORING: (T("monitor_status_waiting_install") if self._auto_watching
                                     else T("monitor_status_monitoring")),
            self.STATE_COMPARING: T("monitor_status_comparing"),
            self.STATE_DONE: T("monitor_status_done"),
        }
        self.status_label.setText(status_map.get(s, "") if busy else "")
        self._busy_bar.setVisible(busy)
        phase = {self.STATE_SNAPSHOTTING: 0, self.STATE_MONITORING: 2 if self._settle_started else 1,
                 self.STATE_COMPARING: 3}.get(s)
        if phase is not None:
            self._busy_bar.set_phase(phase)
        if s != self.STATE_MONITORING:
            self._settle_timer.stop()
            self._settle_started = 0.0
        self.done_label.setText(T("monitor_status_done") if s == self.STATE_DONE else "")

        if s not in (self.STATE_SNAPSHOTTING, self.STATE_COMPARING):
            self.progress_label.setVisible(False)
            self.progress_label.setText("")

    def refresh_content(self):
        pass

    def _current_app_name(self) -> str:
        return self.app_name_edit.text().strip() or T("monitor_title")

    def _launch_installer_path(self, path: str):
        try:
            folder = os.path.dirname(path) or None
            if path.lower().endswith(".msi"):
                return subprocess.Popen([system_exe("msiexec.exe"), "/i", path], cwd=folder)
            return subprocess.Popen([path], cwd=folder)
        except Exception as e:
            cm._log_monitor_error("launch installer", e)
            HOTSDialog.error(self, T("app_title"), T("monitor_err_launch_failed"))
            return None

    def _pick_and_run_installer(self):
        with _faulthandler_paused():
            path, _ = QFileDialog.getOpenFileName(
                self, T("btn_run_installer"), "", "Installers (*.exe *.msi)"
            )
        if not path:
            return
        self._begin_installer_run(path)

    def showEvent(self, e):
        super().showEvent(e)
        try:
            hwnd = int(self.window().winId())
        except Exception:
            return
        if not hwnd:
            return
        if self._native_filter is None:
            self._native_filter = _NativeDropFilter(self)
            QApplication.instance().installNativeEventFilter(self._native_filter)
        self._uipi_hwnd = hwnd
        _enable_native_file_drop(hwnd, True)
        try:
            elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            elevated = False
        if elevated:
            for delay in (0, 300, 1500):
                QTimer.singleShot(delay, lambda h=hwnd, d=delay: self._revoke_ole(h, d))

    def _revoke_ole(self, hwnd: int, delay: int):
        if not self.isVisible():
            return
        try:
            hr = _revoke_ole_drop_target(hwnd)
            if hr not in (0, _DRAGDROP_E_NOTREGISTERED):
                _log(f"RevokeDragDrop(after {delay} ms): 0x{hr:08X}")
        except Exception as e:
            _log(f"RevokeDragDrop failed: {e}")

    def hideEvent(self, e):
        if self._uipi_hwnd:
            _enable_native_file_drop(self._uipi_hwnd, False)
        super().hideEvent(e)

    def _can_accept_drop(self) -> bool:
        return self._state in (self.STATE_IDLE, self.STATE_DONE)

    def _point_in_drop_zone(self, px: int, py: int) -> bool:
        zone = self.drop_zone
        if not zone.isVisible():
            return False
        win = self.window()
        dpr = win.devicePixelRatioF()
        return zone.rect().contains(zone.mapFrom(win, QPoint(int(px / dpr), int(py / dpr))))

    def _handle_dropped_paths(self, paths: list):
        if not paths or not self._can_accept_drop():
            return
        if len(paths) > 1:
            HOTSDialog.info(self, T("app_title"), T("monitor_drop_err_multi"))
            return
        self._begin_installer_run(paths[0])

    def _begin_installer_run(self, path: str):
        ok, err_key = cm.validate_installer_path(path)
        if not ok:
            HOTSDialog.error(self, T("app_title"), T(err_key))
            return
        path = os.path.normpath(path)
        self.app_name_edit.setText(_guess_app_name(path))

        self._pending_installer_path = path
        self.table.setRowCount(0)
        self._results = []
        self._unsaved_log = None
        self._save_log_btn.setEnabled(False)
        self._export_btn.setEnabled(False)
        self._set_state(self.STATE_SNAPSHOTTING)
        self.begin_busy()

        signals = _SnapshotSignals(self)
        signals.done.connect(self._on_snapshot_taken_then_launch)
        signals.progress.connect(self._on_scan_progress)
        self._snapshot_signals = signals
        token = self._monitor_token

        def worker():
            pin = cm.file_sha256(path)
            try:
                snap = cm.take_snapshot(
                    progress_cb=lambda n: signals.progress.emit(n),
                    should_abort=lambda: is_shutting_down() or self._monitor_token != token,
                )
            except Exception:
                snap = None
            unchanged = snap is not None and pin is not None and cm.file_sha256(path) == pin
            if not is_shutting_down():
                signals.done.emit((token, snap, unchanged))

        start_bg_thread(worker)

    def _on_scan_progress(self, count: int):
        self.progress_label.setVisible(True)
        self.progress_label.setText(T("monitor_scan_progress", count=_fmt_count(count)))
        if self._state == self.STATE_SNAPSHOTTING:
            est = self._snapshot_estimate
            self._busy_bar.set_fraction(min(0.97, count / est) if est > 0 else None)

    def _on_snapshot_taken_then_launch(self, payload):
        token, snap, unchanged = payload
        self.end_busy()
        if is_shutting_down() or token != self._monitor_token:
            return
        path = self._pending_installer_path
        self._pending_installer_path = ""
        if snap is None:
            HOTSDialog.error(self, T("app_title"), T("monitor_err_snapshot_failed"))
            self._set_state(self.STATE_IDLE)
            return
        self._snapshot_estimate = snap.item_count
        try:
            settings = load_settings()
            settings["monitor_snapshot_items"] = snap.item_count
            save_settings(settings)
        except Exception:
            pass

        if not unchanged or not cm.validate_installer_path(path)[0]:
            HOTSDialog.error(self, T("app_title"), T("monitor_err_installer_changed"))
            self._set_state(self.STATE_IDLE)
            return

        proc = self._launch_installer_path(path)
        if proc is None:
            self._set_state(self.STATE_IDLE)
            return

        self._installer_path = path
        self._before_snapshot = snap
        self._set_state(self.STATE_MONITORING)
        self._start_install_watch(proc.pid, os.path.basename(path), snap)

    def _start_install_watch(self, root_pid: int, installer_basename: str, before_snap):
        self._auto_watching = True
        self._update_state_ui()
        token = self._monitor_token

        signals = _InstallWaitSignals(self)
        signals.done.connect(self._on_install_wait_done)
        signals.failed.connect(self._on_install_wait_failed)
        signals.settling.connect(self._on_install_settling)
        self._install_wait_signals = signals

        def worker():
            try:
                result = cm.wait_for_install_completion(
                    root_pid, before_snap, installer_basename,
                    should_abort=lambda: is_shutting_down() or self._monitor_token != token,
                    stage_cb=lambda: signals.settling.emit(token),
                )
            except Exception as e:
                cm._log_monitor_error("wait_for_install_completion", e)
                if not is_shutting_down():
                    signals.failed.emit(token)
                return
            if not is_shutting_down():
                self._prefetched = (token, result)
                signals.done.emit(token)

        start_bg_thread(worker)

    def _on_install_settling(self, token: int):
        if is_shutting_down() or token != self._monitor_token or self._state != self.STATE_MONITORING:
            return
        self._settle_started = time.monotonic()
        self._busy_bar.set_phase(2)
        self._busy_bar.set_fraction(0.0 if self._settle_estimate > 0 else None)
        if self._settle_estimate > 0:
            self._settle_timer.start()

    def _tick_settle(self):
        if not self._settle_started or self._settle_estimate <= 0:
            self._settle_timer.stop()
            return
        elapsed = time.monotonic() - self._settle_started
        self._busy_bar.set_fraction(min(0.97, elapsed / self._settle_estimate))

    def _remember_settle_duration(self):
        if not self._settle_started:
            return
        elapsed = time.monotonic() - self._settle_started
        self._settle_estimate = elapsed if self._settle_estimate <= 0 else (self._settle_estimate + elapsed) / 2
        try:
            settings = load_settings()
            settings["monitor_settle_seconds"] = round(self._settle_estimate, 1)
            save_settings(settings)
        except Exception:
            pass

    def _on_install_wait_done(self, token: int):
        if is_shutting_down():
            return
        if token != self._monitor_token or self._state != self.STATE_MONITORING:
            return
        self._remember_settle_duration()
        pre = getattr(self, "_prefetched", None)
        self._prefetched = None
        self._prefetched_items = pre[1] if pre and pre[0] == token else None
        self._finish_monitoring()

    def _on_install_wait_failed(self, token: int):
        if is_shutting_down():
            return
        if token != self._monitor_token or self._state != self.STATE_MONITORING:
            return
        self._auto_watching = False
        self._update_state_ui()

    def _cancel_monitoring(self):
        self._monitor_token += 1
        self._prefetched = None
        self._prefetched_items = None
        self._auto_watching = False
        self._before_snapshot = None
        self._pending_installer_path = ""
        self._set_state(self.STATE_IDLE)

    def _finish_monitoring(self):
        if self._before_snapshot is None:
            return
        self._monitor_token += 1
        self._auto_watching = False
        self._set_state(self.STATE_COMPARING)
        self.begin_busy()

        signals = _CompareSignals(self)
        signals.done.connect(self._on_compared)
        signals.progress.connect(self._on_scan_progress)
        self._compare_signals = signals
        before = self._before_snapshot
        installer, self._installer_path = self._installer_path, ""
        hints = (os.path.splitext(os.path.basename(installer))[0], self.app_name_edit.text())
        fallback_name = self._current_app_name()
        prefetched, self._prefetched_items = getattr(self, "_prefetched_items", None), None

        def worker():
            try:
                if prefetched is not None:
                    items = list(prefetched)
                    try:
                        cm.fill_missing_sizes(items)
                    except Exception:
                        pass
                else:
                    items = cm.compare_snapshot(before, progress_cb=lambda n: signals.progress.emit(n))
            except Exception:
                items = []
            resolved_name, key_name = None, ""
            if items and installer:
                installer_key = os.path.normcase(os.path.normpath(installer))
                items = [it for it in items
                         if not (it.kind == "file"
                                 and os.path.normcase(os.path.normpath(it.path)) == installer_key)]
            if items:
                try:
                    items, resolved_name, key_name = cm.attribute_install(items, hints)
                except Exception as e:
                    cm._log_monitor_error("attribute install", e)
                try:
                    cm.classify_items(items, resolved_name or fallback_name, key_name)
                except Exception:
                    for it in items:
                        it.uncertain = True
            if not is_shutting_down():
                signals.done.emit((items, resolved_name, key_name))

        start_bg_thread(worker)

    def _on_compared(self, result):
        self.end_busy()
        if is_shutting_down():
            return
        self._before_snapshot = None
        self._set_state(self.STATE_DONE)

        items, resolved_name, key_name = result
        app_name = resolved_name or self._current_app_name()
        self._populate_table(items)

        if self._results:
            if resolved_name and resolved_name != self.app_name_edit.text().strip():
                self.app_name_edit.setText(resolved_name)
            if key_name:
                self._save_log(app_name, key_name)
            else:
                self._unsaved_log = (app_name, key_name)
                self._save_log_btn.setEnabled(True)
                HOTSDialog.info(self, T("app_title"), T("monitor_log_not_saved"))

    def _save_log(self, app_name: str, key_name: str):
        log_path = cm.save_monitor_log(app_name, self._results, key_name)
        self._saved_app_name = app_name
        self._saved_key_name = key_name
        if log_path:
            HOTSDialog.info(self, T("app_title"), T("monitor_log_saved", name=app_name))
        else:
            HOTSDialog.error(self, T("app_title"), T("monitor_log_save_failed"))

    def _save_unsaved_log(self):
        if not self._unsaved_log or not self._results:
            return
        app_name, key_name = self._unsaved_log
        self._unsaved_log = None
        self._save_log_btn.setEnabled(False)
        self._save_log(self._current_app_name() or app_name, key_name)

    def _populate_table(self, items: list):
        sorted_items = sorted(items, key=lambda it: (_group_index(it.kind), it.kind, it.path.lower()))
        self._results = sorted_items

        self._export_btn.setEnabled(bool(sorted_items))
        if not sorted_items:
            self.done_label.setText(T("monitor_none_found"))

        counts: dict = {}
        for it in sorted_items:
            gi = _group_index(it.kind)
            counts[gi] = counts.get(gi, 0) + 1

        self.table.setRowCount(0)
        row = 0
        current_group = None
        for it in sorted_items:
            gi = _group_index(it.kind)
            if gi != current_group:
                current_group = gi
                self.table.insertRow(row)
                label_key = _GROUP_ORDER[gi][1] if gi < len(_GROUP_ORDER) else None
                label_text = T(label_key, count=counts.get(gi, 0)) if label_key else ""
                header_item = QTableWidgetItem(label_text)
                header_item.setFlags(Qt.ItemIsEnabled)
                font = header_item.font()
                font.setBold(True)
                header_item.setFont(font)
                self.table.setItem(row, 0, header_item)
                self.table.setSpan(row, 0, 1, 3)
                row += 1

            self.table.insertRow(row)
            type_label = T("leftover_type_service" if cu.service_name_for_item(it) else _TYPE_LABELS.get(it.kind, it.kind))
            if it.uncertain:
                type_label += f" ({T('leftover_userdata_tag' if it.user_data else 'leftover_uncertain_tag')})"
            type_item = QTableWidgetItem(type_label)
            if it.uncertain:
                type_item.setToolTip(T("leftover_userdata_tip" if it.user_data else "leftover_uncertain_tip"))
            self.table.setItem(row, 0, type_item)

            path_item = QTableWidgetItem(it.path)
            path_item.setFlags(path_item.flags() & ~Qt.ItemIsEditable)
            path_item.setToolTip(it.path)
            self.table.setItem(row, 1, path_item)

            size_item = QTableWidgetItem(_fmt_size(it.size_kb))
            size_item.setFlags(size_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 2, size_item)
            row += 1

    def _export_list(self):
        if not self._results:
            HOTSDialog.info(self, T("app_title"), T("monitor_none_found"))
            return
        with _faulthandler_paused():
            path, _ = QFileDialog.getSaveFileName(self, T("btn_export_list"), "install_log.txt", "Text (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(cm.format_export(self._results, self._current_app_name()))
            HOTSDialog.info(self, T("app_title"), T("monitor_export_success"))
        except Exception as e:
            cm._log_monitor_error("export list", e)
            HOTSDialog.error(self, T("app_title"), T("monitor_err_export_failed"))
