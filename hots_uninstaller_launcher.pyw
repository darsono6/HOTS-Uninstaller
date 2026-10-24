import sys, os, time, traceback, ctypes
from pathlib import Path


def _uninst_log(text):
    try:
        print(text, flush=True)
    except Exception:
        pass


def _validated_path(raw):
    # The uninstall modes run elevated and get file paths on the command line, so a planted
    # symlink/junction in %TEMP% must not let them touch any other file. Returns a safe target
    # path or None. Requirements: absolute path inside the temp dir, %APPDATA%\\HOTS Uninstaller
    # or the launcher folder (also after resolving links); the file itself is not a link, a
    # directory or hard-linked; no ':' in the name (NTFS alternate streams).
    try:
        if not raw or not os.path.isabs(raw):
            return None
        target = os.path.normpath(raw)
        name = os.path.basename(target)
        if not name or ":" in name or name.endswith((" ", ".")):
            return None
        import tempfile
        roots = [tempfile.gettempdir(), os.environ.get("TEMP"), os.environ.get("TMP"),
                 os.path.dirname(os.path.abspath(__file__))]
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(os.path.join(appdata, "HOTS Uninstaller"))
        real_parent = os.path.realpath(os.path.dirname(target))
        allowed = False
        for r in roots:
            if not r:
                continue
            real_root = os.path.realpath(r)
            try:
                if os.path.commonpath([os.path.normcase(real_parent),
                                       os.path.normcase(real_root)]) == os.path.normcase(real_root):
                    allowed = True
                    break
            except ValueError:
                continue
        if not allowed or not os.path.isdir(real_parent):
            return None
        real_target = os.path.join(real_parent, name)
        if os.path.lexists(real_target):
            st = os.lstat(real_target)
            if (os.path.islink(real_target) or os.path.isdir(real_target)
                    or getattr(st, "st_file_attributes", 0) & 0x400
                    or st.st_nlink > 1):
                return None
        return real_target
    except Exception:
        return None


def _write_atomically(real_target, data):
    # Write to a NEW randomly named file (O_EXCL, never follows a planted link), then os.replace:
    # that swaps the directory entry instead of overwriting what a link points to, and a reader
    # never sees a half-written file.
    tmp = os.path.join(os.path.dirname(real_target),
                       f".hots_{os.getpid()}_{os.urandom(6).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, real_target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _drop_directory_links():
    """The program's data folder lives in the user's profile, and an ELEVATED process
    writes to it. If someone replaced it (or its backup/log subfolder) with a link
    (junction/symlink), the admin-level program would write and delete files in a
    foreign place. So the link is removed BEFORE the first write (os.rmdir removes
    the link itself, not its target) and replaced with a regular folder. (A rare,
    deliberate user junction loses its pointer this way, but not data: it stays at
    the target.)
    """
    try:
        base = Path(os.environ.get("APPDATA", Path.home())) / "HOTS Uninstaller"
        get_attrs = ctypes.windll.kernel32.GetFileAttributesW
        get_attrs.restype = ctypes.c_uint32
        for d in (base, base / "RegistryBackups", base / "MonitorLogs", base / "UninstallLogs"):
            attrs = get_attrs(str(d))
            if attrs != 0xFFFFFFFF and (attrs & 0x400):
                try:
                    os.rmdir(d)
                except OSError:
                    sys.exit(1)
    except Exception:
        pass


_drop_directory_links()

log_path = Path(os.environ.get("APPDATA", Path.home())) / "HOTS Uninstaller" / "error.log"
log_path.parent.mkdir(parents=True, exist_ok=True)

_LOG_MAX_BYTES = 250 * 1024


def _prepare_log_file(path: Path, max_bytes: int = _LOG_MAX_BYTES) -> None:
    """Log rotation: above the limit the file moves to error.log.1 (one copy,
    overwritten each time). If another instance holds the file open, Windows will
    not let it be moved and rotation is silently skipped (the next start does it). A
    symlink in place of the log is removed, because the elevated process would
    append through it to a foreign file.
    """
    try:
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isfile(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, str(path) + ".1")
    except OSError:
        pass


_prepare_log_file(log_path)
# Mode "a" (not "w"): a later launch that only wakes the already running instance, or
# goes through UAC, must not wipe the log of the instance that is already running. The
# previous session stays in the file (useful after a crash); its size is kept in check
# by the rotation above.
_log = open(log_path, "a", encoding="utf-8", buffering=1)
sys.stderr = _log
sys.stdout = _log


# Uninstall, step 1 (shown by the uninstaller before anything is removed).
# Exit code: 0 = confirmed (choices saved), 3 = cancelled, 2 = refused path, 1 = error.
if len(sys.argv) >= 3 and sys.argv[1] == "--uninstall-wizard":
    def _run_uninstall_wizard() -> int:
        import json

        choices_path = _validated_path(sys.argv[2])
        if choices_path is None:
            _uninst_log(f"[wizard] refused choices path: {sys.argv[2]!r}")
            return 2

        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

            from PySide6.QtWidgets import QApplication, QDialog
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QIcon

            QApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )

            from hots_uninstaller.constants import load_settings
            from hots_uninstaller.i18n import set_lang, pick_lang
            from hots_uninstaller.widgets_qt import apply_global_style
            from hots_uninstaller.resource_utils import resource_path
            from hots_uninstaller import uninstall_flow

            settings = load_settings()
            set_lang(pick_lang(settings))

            app = QApplication.instance() or QApplication(sys.argv)
            apply_global_style(app)
            try:
                icon_path = resource_path("graphic/logo.ico")
                if os.path.exists(icon_path):
                    app.setWindowIcon(QIcon(icon_path))
            except Exception:
                pass

            counts = {"saved_items": uninstall_flow.count_saved_items()}

            from hots_uninstaller.dialogs.self_uninstall_wizard import SelfUninstallWizardDialog
            dlg = SelfUninstallWizardDialog(counts=counts)
            if dlg.exec() != QDialog.Accepted or not dlg.choices:
                return 3

            _write_atomically(choices_path, json.dumps(dlg.choices).encode("utf-8"))
            return 0
        except Exception:
            _uninst_log("[wizard] failed:\n" + traceback.format_exc())
            return 1

    sys.exit(_run_uninstall_wizard())


# Uninstall, step 2 (run by hots_uninstall_cleanup.cmd): applies the saved choices.
# Exit code: 0 = done (result written), 2 = refused result path, 3 = result could not be written.
if len(sys.argv) >= 4 and sys.argv[1] == "--uninstall-apply":
    def _run_uninstall_apply() -> int:
        import json

        choices_path = _validated_path(sys.argv[2])
        result_path = _validated_path(sys.argv[3])
        if result_path is None:
            _uninst_log(f"[apply] refused result path: {sys.argv[3]!r}")
            return 2
        report = {"errors": [], "delete_data": False}

        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from hots_uninstaller import uninstall_flow

            choices = {}
            if choices_path:
                try:
                    with open(choices_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        choices = {k: bool(v) for k, v in data.items()}
                except Exception:
                    _uninst_log("[apply] cannot read choices file:\n" + traceback.format_exc())
                    choices = {}
            else:
                _uninst_log(f"[apply] refused choices path: {sys.argv[2]!r}")

            report = uninstall_flow.apply_uninstall_choices(choices)
        except Exception as e:
            _uninst_log("[apply] failed:\n" + traceback.format_exc())
            report.setdefault("errors", []).append(f"fatal: {e}")

        try:
            _write_atomically(result_path, json.dumps(report).encode("utf-8"))
        except Exception:
            return 3

        return 0

    sys.exit(_run_uninstall_apply())


def _is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def _set_app_user_model_id():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Darsono.HOTSUninstaller.1"
        )
    except Exception:
        pass


_set_app_user_model_id()


# Names are in the "Local\" namespace (one logon session), not "Global\": a global
# object could be created in advance by a process from ANY session/user (the program
# would silently never start), and a second logged-in user could not have their own
# instance. The elevated and non-elevated processes of the same user share the session,
# so they see each other.
_MUTEX_NAME = "Local\\HOTS_Uninstaller_SingleInstance_Mutex"
_mutex_handle = None


def _acquire_single_instance_lock() -> bool:
    """True = we are the first instance. The mutex has the DEFAULT access (creator,
    SYSTEM, Administrators), not open to everyone. When an elevated instance is
    running, a non-elevated process cannot open its mutex (ERROR_ACCESS_DENIED),
    which also means "the program is already running".
    """
    global _mutex_handle
    try:
        ERROR_ALREADY_EXISTS = 183
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return False
        if k32.GetLastError() == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except Exception:
        return True


def release_single_instance_lock():
    global _mutex_handle
    if _mutex_handle:
        try:
            ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        except Exception:
            pass
        _mutex_handle = None


_ACTIVATE_EVENT_NAME = "Local\\HOTSUninstaller_ActivateEvent"


def _signal_running_instance() -> bool:
    """Signals the already running (elevated) instance that someone tried to start the
    program again, without touching its window. This process is NOT elevated at this
    point (before any UAC), and Windows (UIPI) stops lower-privilege processes from
    forcing focus/restoring the window of a higher-privilege process
    (ShowWindow/SetForegroundWindow on a minimized window may be silently ignored).
    SetEvent on a named object needs no special rights, so it works across that
    boundary.
    """
    try:
        EVENT_MODIFY_STATE = 0x0002
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenEventW(
            EVENT_MODIFY_STATE | SYNCHRONIZE, False, _ACTIVATE_EVENT_NAME
        )
        if not handle:
            return False
        try:
            return bool(ctypes.windll.kernel32.SetEvent(handle))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def _enqueue_remove_request():
    """Click on "Usun z HOTS" while the program is already running: this short-lived
    process cannot open a window in the elevated instance, so it appends the path to
    the queue file (read by the running instance after the signal). Standard library
    only: the program package is not imported here yet.
    """
    try:
        args = sys.argv[1:]
        if "--remove" not in args:
            return
        i = args.index("--remove")
        if i + 1 >= len(args):
            return
        path = args[i + 1].strip().strip('"')
        if not path:
            return
        queue = Path(os.environ.get("APPDATA", Path.home())) / "HOTS Uninstaller" / "pending_remove.txt"
        queue.parent.mkdir(parents=True, exist_ok=True)
        with open(queue, "a", encoding="utf-8") as f:
            f.write(path + "\n")
    except Exception as e:
        print(f"enqueue remove request failed: {e}", flush=True)


def _focus_existing_instance():
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.FindWindowW(None, "HOTS Uninstaller")
        if not hwnd:
            return

        SW_RESTORE = 9
        SW_SHOW = 5

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)

        fg_hwnd = user32.GetForegroundWindow()
        current_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
        target_tid = user32.GetWindowThreadProcessId(hwnd, None)

        attached_fg = False
        attached_target = False
        try:
            if fg_tid and fg_tid != current_tid:
                attached_fg = bool(user32.AttachThreadInput(current_tid, fg_tid, True))
            if target_tid and target_tid != current_tid:
                attached_target = bool(user32.AttachThreadInput(current_tid, target_tid, True))

            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached_fg:
                user32.AttachThreadInput(current_tid, fg_tid, False)
            if attached_target:
                user32.AttachThreadInput(current_tid, target_tid, False)
    except Exception:
        pass


def _relaunch_as_admin():
    is_frozen = getattr(sys, "frozen", False) or "__compiled__" in globals()
    if is_frozen:
        # NOTE: sys.executable can be unreliable in Nuitka builds (it may point to a
        # non-existent "python.exe" instead of the real renamed output exe); sys.argv[0]
        # is the actual path of the running executable.
        target = sys.argv[0]
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        script = os.path.abspath(__file__)
        target = sys.executable
        params = " ".join([f'"{script}"'] + [f'"{a}"' for a in sys.argv[1:]])
    print(f"_relaunch_as_admin: target={target!r} params={params!r}", flush=True)
    ctypes.windll.kernel32.SetLastError(0)
    ret = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        target,
        params,
        None,
        1,
    )
    last_err = ctypes.windll.kernel32.GetLastError()
    print(f"_relaunch_as_admin: ShellExecuteW returned {ret} (GetLastError={last_err})", flush=True)
    return ret > 32


def _tr(key: str, fallback: str, **kw) -> str:
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from hots_uninstaller.constants import load_settings
        from hots_uninstaller.i18n import T, set_lang, pick_lang
        set_lang(pick_lang(load_settings()))
        return T(key, **kw)
    except Exception:
        return fallback.format(**kw)


_exit_code = 0
try:
    print("START", flush=True)

    if not _acquire_single_instance_lock():
        print("Program is already running — switching to existing window.", flush=True)
        _enqueue_remove_request()
        if not _signal_running_instance():
            print("Event signal failed/unavailable — falling back to direct window focus.", flush=True)
            _focus_existing_instance()
        sys.exit(0)

    if not _is_admin():
        print("Not admin — relaunching with UAC...", flush=True)
        print(f"sys.executable={sys.executable!r} sys.argv={sys.argv!r} __file__={__file__!r} frozen={getattr(sys, 'frozen', False)} compiled={'__compiled__' in globals()}", flush=True)
        if _mutex_handle:
            try:
                ctypes.windll.kernel32.CloseHandle(_mutex_handle)
            except Exception:
                pass
            _mutex_handle = None
        ok = _relaunch_as_admin()
        if ok:
            sys.exit(0)
        else:
            print("ShellExecuteW failed or was declined by the user — aborting startup.", flush=True)
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                app = QApplication.instance() or QApplication(sys.argv)
                m = QMessageBox()
                m.setIcon(QMessageBox.Warning)
                m.setWindowTitle("HOTS Uninstaller")
                m.setText(_tr(
                    "startup_admin_msg",
                    "{app} requires administrator privileges to run correctly.\n\n"
                    "Startup was cancelled. Please launch the program again and accept "
                    "the User Account Control (UAC) prompt.",
                    app="HOTS Uninstaller",
                ))
                m.exec()
            except Exception as e2:
                print(f"messagebox failed: {e2}", flush=True)
            sys.exit(1)

    print(f"===== session {time.strftime('%Y-%m-%d %H:%M:%S')} =====", flush=True)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    import PySide6
    print(f"PySide6: {PySide6.__version__}", flush=True)

    from hots_uninstaller.__main__ import main
    main()

except SystemExit as _e:
    _exit_code = _e.code if isinstance(_e.code, int) else (0 if _e.code is None else 1)
except Exception:
    _exit_code = 1
    tb = traceback.format_exc()
    print(tb, flush=True)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        m = QMessageBox()
        m.setWindowTitle(_tr("startup_error_title", "{app} — Error", app="HOTS Uninstaller"))
        m.setText(tb[:1000])
        m.exec()
    except Exception as e2:
        print(f"messagebox failed: {e2}", flush=True)
finally:
    _log.flush()

sys.exit(_exit_code)
