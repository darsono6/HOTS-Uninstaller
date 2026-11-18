import os
import re
import ctypes
import ctypes.wintypes
import math
import shiboken6

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTimer, QRect, QProcess, QObject, Signal
from PySide6.QtGui import QIcon, QPixmap, QGuiApplication, QCursor

from .constants import load_settings, save_settings, set_live_glow, set_live_transparency, updates_on_startup
from .i18n import T, set_lang, current_lang, pick_lang
from .widgets_qt import HOTSDialog
from .icons import FIF
from .ui_parts import InfoBanner
from .nav_rail import NavRail
from .frameless import FramelessWindow
from .bg_tasks import start_bg_thread, is_shutting_down, register_wakeup, register_qthread
from .dialogs import (
    ProgramsPage, AppxPage, StartupPage, JunkPage, MonitorPage, BackupsPage, AboutPage, SupportPage, OptionsPage,
)

# The name must match _ACTIVATE_EVENT_NAME in the launcher. The program runs elevated and UIPI blocks a non-elevated
# second launch from focusing its window, so that launch only signals this event.
_EXTERNAL_ACTIVATE_EVENT_NAME = "Local\\HOTSUninstaller_ActivateEvent"

_k32 = ctypes.windll.kernel32
_k32.CreateEventW.restype = ctypes.wintypes.HANDLE
_k32.CreateEventW.argtypes = [
    ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR,
]
_k32.WaitForMultipleObjects.restype = ctypes.wintypes.DWORD
_k32.WaitForMultipleObjects.argtypes = [
    ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.HANDLE),
    ctypes.wintypes.BOOL, ctypes.wintypes.DWORD,
]
_k32.CloseHandle.restype = ctypes.wintypes.BOOL
_k32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
_k32.SetEvent.restype = ctypes.wintypes.BOOL
_k32.SetEvent.argtypes = [ctypes.wintypes.HANDLE]
_k32.LocalFree.argtypes = [ctypes.c_void_p]

_advapi32 = ctypes.windll.advapi32
_advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.wintypes.BOOL
_advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.wintypes.ULONG),
]


def _activation_event_security_attributes():
    try:
        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", ctypes.c_ulong),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", ctypes.c_int),
            ]

        # SY and BA get full access; the logged-in user (IU) may only wait on and set the event and read its descriptor,
        # so the launcher can verify the owner. No low-integrity label, so sandboxed processes cannot set it.
        sddl = "D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00120002;;;IU)"
        p_sd = ctypes.c_void_p()
        ok = _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(p_sd), None
        )
        if not ok or not p_sd:
            return None, None

        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = p_sd
        sa.bInheritHandle = False
        return sa, p_sd
    except Exception:
        return None, None


class _ExternalActivateSignals(QObject):
    activate = Signal()

_ACCENT = None


def _parse_saved_geometry(geo_str: str):
    m = re.match(r"^(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?$", geo_str if isinstance(geo_str, str) else "")
    if not m:
        return 900, 650, None, None
    w, h = int(m.group(1)), int(m.group(2))
    if m.group(3) is None:
        return w, h, None, None
    return w, h, int(m.group(3)), int(m.group(4))


def _geometry_fits_on_screen(x: int, y: int, w: int, h: int) -> bool:
    win_rect = QRect(x, y, w, h)
    return any(win_rect.intersects(screen.availableGeometry())
               for screen in QApplication.screens())


def _centered_position(w: int, h: int):
    screen = QGuiApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
    if screen is None:
        return None, None
    geo = screen.availableGeometry()
    x = geo.x() + (geo.width() - w) // 2
    y = geo.y() + (geo.height() - h) // 2
    return x, y


class UninstallerWindow(FramelessWindow):
    MIN_WINDOW_SIZE = (900, 650)
    def __init__(self, on_before_show=None):
        super().__init__()

        self._settings = load_settings()
        set_lang(pick_lang(self._settings))

        self.setWindowTitle(T("app_title"))

        w, h, x, y = _parse_saved_geometry(self._settings.get("geometry", ""))
        self.resize(w, h)
        if x is not None and y is not None and _geometry_fits_on_screen(x, y, w, h):
            self.move(x, y)
        else:
            cx, cy = _centered_position(w, h)
            if cx is not None and cy is not None:
                self.move(cx, cy)
        self.setMinimumSize(*self.MIN_WINDOW_SIZE)

        _ico = self._find_asset("graphic/logo.ico")
        if _ico:
            self.setWindowIcon(QIcon(_ico))
        self._logo_top_path = self._find_asset("graphic/logoS.png")

        self.programsPage = ProgramsPage(self)
        self.appxPage = AppxPage(self)
        self.startupPage = StartupPage(self)
        self.junkPage = JunkPage(self)
        self.monitorPage = MonitorPage(self)
        self.backupsPage = BackupsPage(self)
        self.aboutPage = AboutPage(self)
        self.supportPage = SupportPage(self)
        self.optionsPage = OptionsPage(
            self,
            on_about=lambda: self._go_to(self.aboutPage, "options"),
            on_support=lambda: self._go_to(self.supportPage, "options"),
            on_language=self._change_language,
            on_appearance=lambda values: self._save_settings_merged(**values),
            on_live_glow=self._apply_live_glow,
            on_live_transparency=self._apply_live_transparency,
            on_restart=self._restart_app,
        )

        self._build_navigation()

        try:
            if self._logo_top_path:
                self._setup_top_logo(self._logo_top_path)
        except Exception as e:
            print(f"Titlebar customization warning: {e}")

        self.closeEvent = self._on_close_event
        self._start_external_activation_listener()

        if on_before_show is not None:
            try:
                on_before_show()
            except Exception as e:
                print(f"on_before_show hook warning: {e}")

        self.show()

        try:
            import sys
            from . import core_portable as cp
            startup_path = cp.parse_remove_arg(sys.argv[1:])
        except Exception:
            startup_path = ""
        QTimer.singleShot(
            400, lambda: self.process_remove_requests([startup_path] if startup_path else None))

        if updates_on_startup(self._settings):
            QTimer.singleShot(2500, self._check_updates_silently)

    def _check_updates_silently(self):
        from .dialogs._about_shared import _UpdateCheckWorker
        worker = _UpdateCheckWorker(self)
        self._startup_update_worker = worker
        worker.finished_ok.connect(self._on_startup_update_check_ok)
        worker.failed.connect(lambda err: None)
        worker.finished.connect(worker.deleteLater)
        register_qthread(worker)
        worker.start()

    def _on_startup_update_check_ok(self, tag: str, url: str, asset):
        if is_shutting_down() or not shiboken6.isValid(self):
            return
        from .dialogs._about_shared import _parse_version, APP_VERSION
        if _parse_version(tag) > _parse_version(APP_VERSION):
            self._show_update_available_hud(tag)
            options_page = getattr(self, "optionsPage", None)
            if options_page is not None and shiboken6.isValid(options_page):
                options_page.apply_known_update(tag, url, asset)

    def _show_update_available_hud(self, tag: str):
        self._dismiss_update_hud()
        banner = InfoBanner.info(
            title=T("update_hud_title"),
            content=T("update_hud_msg", version=tag),
            parent=self,
            closable=True,
            duration=-1,
        )
        banner.set_clickable()
        banner.clicked.connect(self._open_options_from_update_hud)
        self._update_infobar = banner

    def _open_options_from_update_hud(self):
        self._dismiss_update_hud()
        self._go_to(self.optionsPage, "options")

    def _dismiss_update_hud(self):
        ib = getattr(self, "_update_infobar", None)
        if ib is not None and shiboken6.isValid(ib):
            ib.close()
        self._update_infobar = None

    def _start_external_activation_listener(self):
        self._ext_activate_signals = _ExternalActivateSignals(self)
        self._ext_activate_signals.activate.connect(self._on_external_activate)

        shutdown_handle = _k32.CreateEventW(None, True, False, None)

        def _listen():
            handle = None
            p_sd = None
            try:
                sa, p_sd = _activation_event_security_attributes()
                sa_arg = ctypes.byref(sa) if sa is not None else None
                handle = _k32.CreateEventW(
                    sa_arg, False, False, _EXTERNAL_ACTIVATE_EVENT_NAME
                )

                if p_sd:
                    try:
                        _k32.LocalFree(p_sd)
                    except Exception:
                        pass
                    p_sd = None
                if not handle:
                    return
                INFINITE = 0xFFFFFFFF
                WAIT_OBJECT_0 = 0x0
                handles = (ctypes.wintypes.HANDLE * 2)(handle, shutdown_handle)
                while True:
                    result = _k32.WaitForMultipleObjects(2, handles, False, INFINITE)
                    if result != WAIT_OBJECT_0:
                        break
                    if is_shutting_down():
                        break
                    self._ext_activate_signals.activate.emit()
            except Exception as e:
                print(f"External activation listener warning: {e}")
            finally:
                if handle:
                    try:
                        _k32.CloseHandle(handle)
                    except Exception:
                        pass
                if shutdown_handle:
                    try:
                        _k32.CloseHandle(shutdown_handle)
                    except Exception:
                        pass

        self._ext_activate_thread = start_bg_thread(_listen)
        register_wakeup(lambda: _k32.SetEvent(shutdown_handle))

    def _on_external_activate(self):
        self._restore_and_activate()
        QTimer.singleShot(0, self.process_remove_requests)

    def _restore_and_activate(self):
        try:
            state = self.windowState()
            if state & Qt.WindowMinimized:
                self.setWindowState((state & ~Qt.WindowMinimized) | Qt.WindowActive)
            if self.isFullscreenMode() or (state & (Qt.WindowMaximized | Qt.WindowFullScreen)):
                self.show()
            else:
                self.showNormal()
            self.raise_()
            self.activateWindow()
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception as e:
            print(f"Restore/activate warning: {e}")

    _remove_busy = False
    _pending_remove = None

    def process_remove_requests(self, extra_paths=None):
        if is_shutting_down() or not shiboken6.isValid(self):
            return
        from . import core_portable as cp
        if self._pending_remove is None:
            self._pending_remove = []
        for p in list(extra_paths or []) + cp.pop_pending_requests():
            if p and p not in self._pending_remove:
                self._pending_remove.append(p)
        if self._remove_busy:
            return
        self._remove_busy = True
        try:
            while self._pending_remove and not is_shutting_down():
                self._open_portable_removal(self._pending_remove.pop(0))
        finally:
            self._remove_busy = False

    def _open_portable_removal(self, path: str):
        from . import core_portable as cp
        from .dialogs.leftover_dialog import LeftoverDialog

        err = cp.validate_portable_exe(path)
        if err:
            self._restore_and_activate()
            HOTSDialog.error(self, T("app_title"), T(err))
            return

        self._restore_and_activate()
        while True:
            running = cp.running_processes(path)
            if not running:
                break
            names = sorted({os.path.basename(r) for r in running}, key=str.lower)
            shown = "\n".join(names[:8]) + ("\n…" if len(names) > 8 else "")
            if not HOTSDialog.ask(self, T("app_title"), T("portable_running_msg", list=shown)):
                return
        LeftoverDialog(
            self, cp.build_portable_app(path),
            scan_func=lambda _app: cp.scan_portable(path),
            preselect=lambda it: it.preselected,
            subtitle_key="leftover_portable_subtitle",
        ).exec()

    def _find_asset(self, name: str) -> str:
        from .resource_utils import resource_path
        p = resource_path(name)
        return p if os.path.exists(p) else ""

    def _build_navigation(self):
        self._rail = NavRail(self)
        self._rail.set_logo(self._find_asset("graphic/logo1.png"))
        self.hBoxLayout.insertWidget(0, self._rail)
        self.setTitleBarOffset(NavRail.WIDTH)

        def fif(*names):
            for name in names:
                icon = getattr(FIF, name, None)
                if icon is not None:
                    return icon
            return FIF.APPLICATION

        main_pages = [
            ("programs", self.programsPage, fif("DESKTOP"),                     T("nav_programs")),
            ("appx",     self.appxPage,     fif("STORE_MICROSOFT"),             T("nav_appx")),
            ("startup",  self.startupPage,  fif("SPEED_HIGH"),                  T("nav_startup")),
            ("junk",     self.junkPage,     fif("BROOM", "DELETE"),             T("nav_junk")),
            ("monitor",  self.monitorPage,  fif("SEARCH"),                      T("nav_monitor")),
            ("backups",  self.backupsPage,  fif("HISTORY", "SAVE", "SYNC"),     T("nav_backups")),
        ]

        self._page_rail_keys = {}
        for key, page, icon, text in main_pages:
            self.stackedWidget.addWidget(page)
            self._page_rail_keys[page] = key
            self._rail.add_tile(key, icon, text,
                                on_click=lambda p=page, k=key: self._go_to(p, k))

        for page in (self.optionsPage, self.aboutPage, self.supportPage):
            self.stackedWidget.addWidget(page)
            self._page_rail_keys[page] = "options"

        self._rail.add_tile(
            "options", fif("SETTING"), T("btn_options"),
            on_click=lambda: self._go_to(self.optionsPage, "options"), bottom=True,
        )

        self.stackedWidget.currentChanged.connect(self._sync_rail_from_stack)
        self._rail.set_current("programs")

    def _go_to(self, page, rail_key: str):
        self.switchTo(page)
        self._rail.set_current(rail_key)

    def _sync_rail_from_stack(self, index: int):
        widget = self.stackedWidget.widget(index)
        self._rail.set_current(self._page_rail_keys.get(widget))

    def _setup_top_logo(self, path: str):
        try:
            pix = QPixmap(path)
            if pix.isNull():
                return

            tb = self.titleBar
            margin = 6
            scale_factor = 0.50
            target_h = max(int((tb.height() - margin) * scale_factor), 12)

            dpr = self.devicePixelRatioF() or 1.0
            target_h_px = max(1, math.ceil(target_h * dpr))
            scaled = pix.scaledToHeight(target_h_px, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)

            tb.set_logo(scaled, left_margin=10)
        except Exception as e:
            print(f"Top logo setup warning: {e}")

    def _change_language(self, code: str):
        if code and code != current_lang():
            set_lang(code)
            self._save_settings_merged(language=code)
            HOTSDialog.info(self, T("lang_title"), T("lang_restart_msg"))

    def _restart_app(self):
        import sys
        import os

        app = QApplication.instance()
        prev_quit_on_last = app.quitOnLastWindowClosed() if app is not None else True
        if app is not None:
            app.setQuitOnLastWindowClosed(False)

        if not self.close():
            if app is not None:
                app.setQuitOnLastWindowClosed(prev_quit_on_last)
            return

        # Release the single-instance mutex explicitly before spawning, or the new instance may see it taken and exit.
        try:
            main_mod = sys.modules.get("__main__")
            release_fn = getattr(main_mod, "release_single_instance_lock", None)
            if callable(release_fn):
                release_fn()
        except Exception:
            pass

        python = sys.executable
        from . import core_portable as cp
        args = cp.strip_remove_args(sys.argv[1:])
        is_frozen = getattr(sys, "frozen", False) or "__compiled__" in globals()
        if is_frozen:
            python = sys.argv[0]

        try:
            if is_frozen:
                ok = QProcess.startDetached(python, args)
            else:
                # From source, start the launcher .pyw, not -m: it owns the single-instance mutex and the UAC elevation.
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                launcher = os.path.join(base_dir, "hots_uninstaller_launcher.pyw")
                pythonw = python
                pythonw_candidate = os.path.join(os.path.dirname(python), "pythonw.exe")
                if os.path.exists(pythonw_candidate):
                    pythonw = pythonw_candidate
                ok = QProcess.startDetached(pythonw, [launcher] + args)
        except Exception as e:
            print(f"_restart_app: QProcess.startDetached raised: {e!r}", flush=True)
            ok = False

        if not ok:
            if app is not None:
                app.setQuitOnLastWindowClosed(prev_quit_on_last)
            HOTSDialog.error(self, T("app_title"), T("app_restart_fail_msg"))
            return

        if app is not None:
            app.quit()

    def _apply_live_glow(self, value: int):
        set_live_glow(value)
        self.refresh_live_appearance()

    def _apply_live_transparency(self, value: int):
        set_live_transparency(value)
        self.refresh_live_appearance()

    def _save_settings_merged(self, **kwargs) -> bool:
        fresh = load_settings()
        changed = False
        for k, v in kwargs.items():
            if fresh.get(k) != v:
                changed = True
            fresh[k] = v
        self._settings = fresh
        save_settings(fresh)
        return changed

    def _on_close_event(self, event):
        for _ in range(3):
            QApplication.processEvents()

        geo = self.geometry()
        if self.isFullScreen() or self.isMaximized() or self.isFullscreenMode():
            normal = self.normalGeometry()
            if normal.isValid() and not normal.isNull():
                geo = normal
        self._save_settings_merged(
            geometry=f"{geo.width()}x{geo.height()}+{geo.x()}+{geo.y()}",
            language=current_lang(),
        )
        event.accept()
