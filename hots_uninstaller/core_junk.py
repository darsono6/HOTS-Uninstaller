import ctypes
import fnmatch
import glob
import json
import os
import shutil
import stat
import string
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from .constants import load_settings, save_settings
from .core_appx import _run_powershell
from .proc_utils import system_exe
from .i18n import T


@dataclass
class JunkItem:
    category_key: str
    kind: str
    path: str
    size_bytes: int = 0
    drives: tuple = ()
    item_count: int = 0
    restore_point_seqs: tuple = ()
    browser: str = ""


@dataclass
class JunkCategory:
    key: str
    label_key: str
    default_on: bool = True
    checked_by_default: bool = True
    persist: bool = True


CATEGORIES: list[JunkCategory] = [
    JunkCategory("user_temp",             "junk_cat_user_temp"),
    JunkCategory("windows_temp",          "junk_cat_windows_temp"),
    JunkCategory("windows_update",        "junk_cat_windows_update"),
    JunkCategory("delivery_optimization", "junk_cat_delivery_optimization"),
    JunkCategory("memory_dumps",          "junk_cat_memory_dumps"),
    JunkCategory("log_files",             "junk_cat_log_files"),
    JunkCategory("stray_temp_files",      "junk_cat_stray_temp_files", default_on=False,
                 checked_by_default=False),
    JunkCategory("temp_old_items",        "junk_cat_temp_old_items", default_on=False,
                 checked_by_default=False),
    JunkCategory("browser_cache",         "junk_cat_browser_cache"),
    JunkCategory("app_cache",             "junk_cat_app_cache"),
    JunkCategory("privacy_history",       "junk_cat_privacy_history", default_on=False, persist=False),
    JunkCategory("thumbnail_cache",       "junk_cat_thumbnail_cache"),
    JunkCategory("shader_cache",          "junk_cat_shader_cache", default_on=False),
    JunkCategory("wer_reports",           "junk_cat_wer_reports"),
    JunkCategory("recent_items",          "junk_cat_recent_items"),
    JunkCategory("prefetch",              "junk_cat_prefetch", default_on=False),
    JunkCategory("recycle_bin",           "junk_cat_recycle_bin", default_on=False),
    JunkCategory("windows_old",           "junk_cat_windows_old", default_on=False, persist=False),
    JunkCategory("restore_points",        "junk_cat_restore_points", default_on=False,
                 checked_by_default=False, persist=False),
]

_CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}

LARGE_LOG_BYTES = 50 * 1024 * 1024


def is_large_log(item: "JunkItem") -> bool:
    return item.category_key == "log_files" and item.size_bytes >= LARGE_LOG_BYTES


def is_checked_by_default(item: "JunkItem") -> bool:
    cat = _CATEGORY_BY_KEY.get(item.category_key)
    if cat is not None and not cat.checked_by_default:
        return False
    return not is_large_log(item)


_LOG_EXT = (".log",)
_JUNK_TEMP_EXT = (".tmp", ".temp", ".old", ".bak", ".chk", ".dmp", ".gid", ".~mp", ".wbk")

_TEMP_FOLDER_PATTERNS = (
    "*.tmp", "*.temp", "*.old", "*.gid", "*.fts", "*.$$$", "*.---", "*.??$",
    "*.___", "*.~mp", "*._mp", "*.$db", "*.db$", "*.dmp", "*.??~", "thumbs.db",
)


def _is_temp_folder_junk_name(filename: str) -> bool:
    lower = filename.lower()
    return any(fnmatch.fnmatch(lower, pat) for pat in _TEMP_FOLDER_PATTERNS)


# Files/folders modified within the last N seconds are not touched during scanning: a
# running application may be writing them right now (a browser download in progress, an
# autosave file, etc.).
_MIN_AGE_SECONDS = 24 * 60 * 60


_scan_notes: set = set()


def get_scan_notes() -> list[str]:
    return sorted(_scan_notes)


def _is_fresh(path: str) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) < _MIN_AGE_SECONDS
    except OSError:
        return False


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003

_DIR_SIZE_MAX_FILES = 2_000_000


def _is_reparse_dir_entry(entry) -> bool:
    try:
        if entry.is_symlink():
            return True
        return getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT
    except OSError:
        return True


def _dir_size_bytes(path: str, limit_files: int = _DIR_SIZE_MAX_FILES) -> int:
    total = 0
    count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not _is_reparse_dir_entry(entry):
                                stack.append(entry.path)
                            continue
                        count += 1
                        if count > limit_files:
                            return total
                        total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


_LOG_SKIP_DIRS = frozenset({"measuredboot"})


def _iter_files_by_ext(folder: str, extensions: tuple, recursive: bool = False,
                       skip_dirs: frozenset = frozenset()):
    if not folder or not os.path.isdir(folder):
        return
    try:
        if recursive:
            for root, _dirs, files in os.walk(folder):
                _dirs[:] = [d for d in _dirs if d.lower() not in skip_dirs]
                for f in files:
                    if f.lower().endswith(extensions):
                        full = os.path.join(root, f)
                        if not _is_fresh(full):
                            yield full
        else:
            for f in os.listdir(folder):
                full = os.path.join(folder, f)
                if os.path.isfile(full) and f.lower().endswith(extensions):
                    if not _is_fresh(full):
                        yield full
    except Exception:
        return


def _scan_temp_folder(category_key: str, folder: str) -> list[JunkItem]:
    items = []
    if not folder or not os.path.isdir(folder):
        return items
    try:
        for name in os.listdir(folder):
            if not _is_temp_folder_junk_name(name):
                continue
            full = os.path.join(folder, name)
            if not os.path.isfile(full):
                continue
            if _is_fresh(full):
                continue
            items.append(JunkItem(category_key, "file", full, _safe_size(full)))
    except Exception:
        pass
    return items


def _looks_like_temp_dir(path: str) -> bool:
    parts = [p.lower() for p in os.path.normpath(path).split(os.sep) if p]
    if not parts:
        return False
    if parts[-1] in ("temp", "tmp"):
        return True
    return len(parts) >= 2 and parts[-1].isdigit() and parts[-2] in ("temp", "tmp")


def _temp_dir() -> Optional[str]:
    for var in ("TEMP", "TMP"):
        path = os.environ.get(var)
        if path and _looks_like_temp_dir(path) and os.path.isdir(path):
            return path
    return None


def _scan_user_temp() -> list[JunkItem]:
    return _scan_temp_folder("user_temp", _temp_dir())


def _scan_system_temp(folder: str) -> list[JunkItem]:
    if not folder:
        return []
    try:
        os.listdir(folder)
    except Exception:
        return []
    return _scan_temp_folder("windows_temp", folder)


def _scan_windows_temp() -> list[JunkItem]:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    items = _scan_temp_folder("windows_temp", os.path.join(windir, "Temp"))
    items += _scan_system_temp(os.path.join(windir, "SystemTemp"))
    return items


def _scan_windows_update() -> list[JunkItem]:
    items = []
    windir = os.environ.get("WINDIR", r"C:\Windows")
    su_download = os.path.join(windir, "SoftwareDistribution", "Download")
    if os.path.isdir(su_download):
        try:
            for name in os.listdir(su_download):
                full = os.path.join(su_download, name)
                if _is_fresh(full):
                    continue
                if os.path.isfile(full):
                    items.append(JunkItem("windows_update", "file", full, _safe_size(full)))
                elif os.path.isdir(full):
                    items.append(JunkItem("windows_update", "folder", full, _dir_size_bytes(full)))
        except Exception:
            pass
    return items


def _scan_delivery_optimization() -> list[JunkItem]:
    items = []
    windir = os.environ.get("WINDIR", r"C:\Windows")
    do_dir = os.path.join(windir, "SoftwareDistribution", "DeliveryOptimization")
    if os.path.isdir(do_dir):
        try:
            for name in os.listdir(do_dir):
                full = os.path.join(do_dir, name)
                if _is_fresh(full):
                    continue
                if os.path.isfile(full):
                    items.append(JunkItem("delivery_optimization", "file", full, _safe_size(full)))
                elif os.path.isdir(full):
                    items.append(JunkItem("delivery_optimization", "folder", full, _dir_size_bytes(full)))
        except Exception:
            pass
    return items


def _scan_prefetch() -> list[JunkItem]:
    items = []
    windir = os.environ.get("WINDIR", r"C:\Windows")
    prefetch_dir = os.path.join(windir, "Prefetch")
    for f in glob.glob(os.path.join(prefetch_dir, "*.pf")):
        if _is_fresh(f):
            continue
        items.append(JunkItem("prefetch", "file", f, _safe_size(f)))
    return items


def _scan_windows_old() -> list[JunkItem]:
    items = []
    system_drive = os.environ.get("SystemDrive", "C:").rstrip(":\\") + ":"
    for name in ("Windows.old", "$WINDOWS.~BT", "$WINDOWS.~WS"):
        path = os.path.join(system_drive + "\\", name)
        # A folder changed in the last 24 h most likely belongs to an ongoing or just
        # finished feature update ($WINDOWS.~BT/~WS are still needed by the installer),
        # so it is not offered for removal.
        if os.path.isdir(path) and not _is_fresh(path):
            items.append(JunkItem("windows_old", "folder", path, _dir_size_bytes(path)))
    return items


_PS_LIST_RESTORE_POINTS_CMD = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "try { $pts = @(Get-ComputerRestorePoint) } catch { $pts = @() }; "
    "$seqs = @($pts | Select-Object -ExpandProperty SequenceNumber); "
    "try { $shadow = @(Get-WmiObject -Class Win32_ShadowStorage) } catch { $shadow = @() }; "
    "$used = ($shadow | Measure-Object -Property UsedSpace -Sum).Sum; "
    "if (-not $used) { $used = 0 }; "
    "@{ Sequences = $seqs; UsedSpaceBytes = [int64]$used } | ConvertTo-Json -Compress -Depth 2"
)

_PS_CURRENT_RESTORE_POINT_SEQUENCES_CMD = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "try { $pts = @(Get-ComputerRestorePoint) } catch { $pts = @() }; "
    "@{ Sequences = @($pts | Select-Object -ExpandProperty SequenceNumber) } "
    "| ConvertTo-Json -Compress -Depth 2"
)


def _as_list(value) -> list:
    """ConvertTo-Json can serialize an array with EXACTLY one element as a bare value
    instead of a one-element array (a known PowerShell gotcha, see the
    PackageUserInformation normalization in core_appx.list_appx_apps). None/missing
    is normalized to an empty list, so callers do not have to check for it.
    """
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _parse_restore_point_sequences(json_text: str) -> Optional[set]:
    try:
        data = json.loads(json_text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return {int(s) for s in _as_list(data.get("Sequences"))}
    except (TypeError, ValueError):
        return None


def _scan_restore_points() -> list[JunkItem]:
    ok, out = _run_powershell(_PS_LIST_RESTORE_POINTS_CMD, timeout=30)
    if not ok or not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    try:
        seqs = sorted({int(s) for s in _as_list(data.get("Sequences"))}, reverse=True)
    except (TypeError, ValueError):
        return []
    if len(seqs) <= 1:
        return []

    used_bytes = data.get("UsedSpaceBytes") or 0
    try:
        used_bytes = max(int(used_bytes), 0)
    except (TypeError, ValueError):
        used_bytes = 0

    to_delete = tuple(seqs[1:])
    return [JunkItem("restore_points", "special_restore_points", "SystemRestore",
                      used_bytes, item_count=len(to_delete), restore_point_seqs=to_delete)]


_ERROR_SUCCESS = 0


def _sr_remove_restore_point(seq: int) -> int:
    try:
        dll = ctypes.WinDLL("Srclient.dll")
        fn = dll.SRRemoveRestorePoint
        fn.argtypes = [ctypes.c_uint32]
        fn.restype = ctypes.c_uint32
        return fn(ctypes.c_uint32(seq))
    except Exception:
        return -1


def _delete_restore_points(seqs: tuple) -> tuple[int, list[str]]:
    """Deletes the given restore points by SequenceNumber. Right before deleting, and
    INDEPENDENTLY of the scan, the CURRENT list of existing points is fetched (time
    may have passed between the scan and the click on "Delete"; a new point may have
    been created, e.g. by a driver install), and the current newest point is
    computed from it. Its deletion is blocked even if its number is in `seqs`.
    Points that no longer exist are skipped without an error. If the current list
    cannot be obtained (e.g. PowerShell unavailable), NOTHING is deleted: better to
    do nothing than to delete based on stale or uncertain data. Returns
    (number_actually_deleted, list_of_errors).
    """
    if not seqs:
        return 0, []

    ok, out = _run_powershell(_PS_CURRENT_RESTORE_POINT_SEQUENCES_CMD, timeout=20)
    current = _parse_restore_point_sequences(out) if ok and out else None
    if current is None:
        return 0, [T("junk_err_restore_points_list_failed")]
    if not current:
        return 0, []

    newest = max(current)
    removed = 0
    errors: list[str] = []
    for seq in seqs:
        if seq == newest or seq not in current:
            continue
        code = _sr_remove_restore_point(seq)
        if code == _ERROR_SUCCESS:
            removed += 1
        else:
            errors.append(f"SequenceNumber {seq}: 0x{code & 0xFFFFFFFF:X}")
    return removed, errors


def _registry_crash_dump_folders() -> list:
    import winreg
    folders: list = []
    base = r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as key:
            try:
                value, _ = winreg.QueryValueEx(key, "DumpFolder")
                if value:
                    folders.append(os.path.expandvars(value))
            except OSError:
                pass

            i = 0
            while True:
                try:
                    subname = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, subname) as subkey:
                        value, _ = winreg.QueryValueEx(subkey, "DumpFolder")
                        if value:
                            folders.append(os.path.expandvars(value))
                except OSError:
                    continue
    except OSError:
        pass
    return folders


def _scan_memory_dumps() -> list[JunkItem]:
    items = []
    windir = os.environ.get("WINDIR", r"C:\Windows")

    memory_dmp = os.path.join(windir, "MEMORY.DMP")
    if os.path.isfile(memory_dmp):
        items.append(JunkItem("memory_dumps", "file", memory_dmp, _safe_size(memory_dmp)))

    minidump = os.path.join(windir, "Minidump")
    for f in glob.glob(os.path.join(minidump, "*.dmp")):
        items.append(JunkItem("memory_dumps", "file", f, _safe_size(f)))

    live_kernel_reports = os.path.join(windir, "LiveKernelReports")
    for f in glob.glob(os.path.join(live_kernel_reports, "**", "*.dmp"), recursive=True):
        items.append(JunkItem("memory_dumps", "file", f, _safe_size(f)))

    service_profiles = os.path.join(windir, "ServiceProfiles")
    for account in ("LocalService", "NetworkService"):
        crash_dumps = os.path.join(service_profiles, account, "AppData", "Local", "CrashDumps")
        for f in glob.glob(os.path.join(crash_dumps, "*.dmp")):
            items.append(JunkItem("memory_dumps", "file", f, _safe_size(f)))

    system_crash_dumps = os.path.join(
        windir, "System32", "config", "systemprofile", "AppData", "Local", "CrashDumps")
    for f in glob.glob(os.path.join(system_crash_dumps, "*.dmp")):
        items.append(JunkItem("memory_dumps", "file", f, _safe_size(f)))

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        crash_dumps = os.path.join(local_appdata, "CrashDumps")
        for f in glob.glob(os.path.join(crash_dumps, "*.dmp")):
            items.append(JunkItem("memory_dumps", "file", f, _safe_size(f)))

        for browser_dir in (
            os.path.join(local_appdata, "Google", "Chrome", "User Data"),
            os.path.join(local_appdata, "Microsoft", "Edge", "User Data"),
            os.path.join(local_appdata, "BraveSoftware", "Brave-Browser", "User Data"),
        ):
            reports_dir = os.path.join(browser_dir, "Crashpad", "reports")
            for f in glob.glob(os.path.join(reports_dir, "*.dmp")):
                items.append(JunkItem("memory_dumps", "file", f, _safe_size(f)))

    for reg_folder in _registry_crash_dump_folders():
        if reg_folder and os.path.isdir(reg_folder):
            for f in glob.glob(os.path.join(reg_folder, "*.dmp")):
                items.append(JunkItem("memory_dumps", "file", f, _safe_size(f)))

    return [i for i in items if not _is_fresh(i.path)]


def _scan_log_files() -> list[JunkItem]:
    items = []
    windir = os.environ.get("WINDIR", r"C:\Windows")
    logs_dir = os.path.join(windir, "Logs")
    for f in _iter_files_by_ext(logs_dir, _LOG_EXT, recursive=True, skip_dirs=_LOG_SKIP_DIRS):
        items.append(JunkItem("log_files", "file", f, _safe_size(f)))

    temp = _temp_dir()
    for f in _iter_files_by_ext(temp, _LOG_EXT + _JUNK_TEMP_EXT, recursive=False):
        items.append(JunkItem("log_files", "file", f, _safe_size(f)))

    return items


_SWEEP_MIN_AGE_SECONDS = 24 * 60 * 60
_SWEEP_MAX_FILES = 2_000_000

_STRAY_EXCLUDE_FRAGMENTS = (
    "/sendto/",
    "/temporary internet files/",
    "/application data/aim/",
    "/norton antivirus/quarantine/",
    "/temp/incredimail/",
    "/symantec shared/virusdefs/",
    "/microsoft/money/webcache/",
    "/microsoft/office/data/",
    "/microsoft publisher/pagewiz/",
    "/application data/microsoft/",
)


# Cloud-synced folders (top level of the profile). Deleting a *.old/*.bak file there
# also removes it from the cloud and from other devices, and "online-only" files are not
# local at all, so the sweep does not touch them.
_CLOUD_SYNC_DIR_PREFIXES = ("onedrive", "dropbox", "google drive", "icloud")


def _is_stray_excluded(dirpath_norm: str) -> bool:
    return any(frag in dirpath_norm for frag in _STRAY_EXCLUDE_FRAGMENTS)


def _is_link_or_junction(path: str) -> bool:
    """True for a symlink AND a junction. Since Python 3.8 os.path.islink() returns
    False for junctions on Windows (they are treated like regular folders), so
    islink() alone is not enough to skip them in os.walk, and junctions are exactly
    the problem here (old XP aliases in the profile and user-made junctions that may
    lead outside the profile, e.g. to another drive). On a read error it is better
    to skip the folder than to enter it.
    """
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if isjunction is not None:
            return bool(isjunction(path))
        tag = getattr(os.lstat(path), "st_reparse_tag", 0)
        return tag in (_IO_REPARSE_TAG_SYMLINK, _IO_REPARSE_TAG_MOUNT_POINT)
    except OSError:
        return True


def _scan_stray_temp_files() -> list[JunkItem]:
    items = []
    userprofile = os.environ.get("USERPROFILE")
    if not userprofile or not os.path.isdir(userprofile):
        return items
    # An environment variable pointing at a drive root (a misconfiguration) would turn
    # the "profile sweep" into a sweep of the whole system drive, so we do not touch it.
    if _is_drive_root(userprofile):
        return items

    seen = 0
    now = time.time()
    try:
        for dirpath, dirs, filenames in os.walk(userprofile, topdown=True):
            dirpath_norm = dirpath.replace("\\", "/").lower() + "/"
            if _is_stray_excluded(dirpath_norm):
                dirs[:] = []
                continue

            pruned = []
            at_profile_root = os.path.normcase(dirpath) == os.path.normcase(userprofile)
            for d in dirs:
                full_d = os.path.join(dirpath, d)
                if _is_link_or_junction(full_d):
                    continue
                if at_profile_root and d.lower().startswith(_CLOUD_SYNC_DIR_PREFIXES):
                    continue
                child_norm = dirpath_norm + d.lower() + "/"
                if _is_stray_excluded(child_norm):
                    continue
                pruned.append(d)
            dirs[:] = pruned

            for name in filenames:
                seen += 1
                if seen > _SWEEP_MAX_FILES:
                    _scan_notes.add("stray_truncated")
                    return items
                if not _is_temp_folder_junk_name(name):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    if (now - os.path.getmtime(full)) < _SWEEP_MIN_AGE_SECONDS:
                        continue
                    items.append(JunkItem("stray_temp_files", "file", full, _safe_size(full)))
                except OSError:
                    continue
    except Exception:
        pass
    return items


_CHROMIUM_CACHE_NAMES = ("Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache",
                         "DawnWebGPUCache", "GrShaderCache", "ShaderCache", "Media Cache")


def _chromium_profile_dirs(user_data_dir: str) -> list[str]:
    profiles = [user_data_dir]
    try:
        for d in os.listdir(user_data_dir):
            full = os.path.join(user_data_dir, d)
            if not os.path.isdir(full) or _is_link_or_junction(full):
                continue
            if (d in ("Default", "Guest Profile") or d.startswith("Profile ")
                    or os.path.isfile(os.path.join(full, "Preferences"))):
                profiles.append(full)
    except Exception:
        pass
    return profiles


def _chromium_cache_dirs(user_data_dir: str) -> list[str]:
    dirs = []
    if not os.path.isdir(user_data_dir):
        return dirs
    for profile in _chromium_profile_dirs(user_data_dir):
        for sub in _CHROMIUM_CACHE_NAMES:
            p = os.path.join(profile, sub)
            if os.path.isdir(p) and not _is_link_or_junction(p):
                dirs.append(p)
        try:
            for name in os.listdir(profile):
                if name.lower().startswith("jumplisticons"):
                    p = os.path.join(profile, name)
                    if os.path.isdir(p):
                        dirs.append(p)
        except Exception:
            pass
    return dirs


def _is_rotated_log_name(name: str) -> bool:
    n = name.lower()
    return n in ("log.old", "sessionrestorelog.old") or n.endswith(".log.old")


def _chromium_old_log_files(user_data_dir: str) -> list[JunkItem]:
    items = []
    if not user_data_dir or not os.path.isdir(user_data_dir):
        return items
    try:
        for root, dirs, files in os.walk(user_data_dir):
            dirs[:] = [d for d in dirs if not _is_link_or_junction(os.path.join(root, d))]
            for name in files:
                if not _is_rotated_log_name(name):
                    continue
                full = os.path.join(root, name)
                if _is_fresh(full):
                    continue
                items.append(JunkItem("browser_cache", "file", full, _safe_size(full)))
    except Exception:
        pass
    return items


_CHROMIUM_BROWSER_ROOTS = (
    ("Google", "Chrome", "User Data"),
    ("Microsoft", "Edge", "User Data"),
    ("BraveSoftware", "Brave-Browser", "User Data"),
    ("Vivaldi", "User Data"),
    ("Chromium", "User Data"),
    ("Opera Software", "Opera Stable"),
    ("Opera Software", "Opera GX Stable"),
)

_FIREFOX_LIKE_ROOTS = (
    ("Mozilla", "Firefox"),
    ("librewolf",),
    ("Waterfox",),
)


def _scan_browser_cache() -> list[JunkItem]:
    items = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        for parts in _CHROMIUM_BROWSER_ROOTS:
            root = os.path.join(local_appdata, *parts)
            for d in _chromium_cache_dirs(root):
                items.append(JunkItem("browser_cache", "folder", d, _dir_size_bytes(d)))
            items += _chromium_old_log_files(root)

        copilot_dir = os.path.join(local_appdata, "Microsoft", "Copilot", "User Data")
        items += _chromium_old_log_files(copilot_dir)

        for parts in _FIREFOX_LIKE_ROOTS:
            profiles_dir = os.path.join(local_appdata, *parts, "Profiles")
            for cache_dir in glob.glob(os.path.join(profiles_dir, "*", "cache2")):
                if os.path.isdir(cache_dir) and not _is_link_or_junction(cache_dir):
                    items.append(JunkItem("browser_cache", "folder", cache_dir, _dir_size_bytes(cache_dir)))

        shared_webview_dir = os.path.join(local_appdata, "Microsoft", "Windows", "SharedWebView")
        items += _chromium_old_log_files(shared_webview_dir)

        packages_dir = os.path.join(local_appdata, "Packages")
        items += _chromium_old_log_files(packages_dir)

    return items


# Electron apps keep user data in %APPDATA%\<name> together with the Chromium cache. We
# touch ONLY folders with the exact names from _CHROMIUM_CACHE_NAMES (plus two VS Code
# caches), never the rest of the app folder (settings, sign-in, attachments, notes). The
# list is deliberately short; a missing folder simply finds nothing. Spotify is not
# included: its "Storage" also holds downloaded offline music, which is user data.
_VSCODE_EXTRA_CACHE_NAMES = ("CachedData", "CachedExtensionVSIXs")

_ELECTRON_APPS = (
    ("discord", ()), ("discordptb", ()), ("discordcanary", ()),
    ("Slack", ()),
    ("Code", _VSCODE_EXTRA_CACHE_NAMES),
    ("Code - Insiders", _VSCODE_EXTRA_CACHE_NAMES),
    ("VSCodium", _VSCODE_EXTRA_CACHE_NAMES),
    ("Notion", ()), ("obsidian", ()), ("Postman", ()), ("Signal", ()),
)


def _scan_app_cache() -> list[JunkItem]:
    global _skipped_in_use
    items: list[JunkItem] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        for app_dir, extra in _ELECTRON_APPS:
            root = os.path.join(appdata, app_dir)
            if not os.path.isdir(root) or _is_link_or_junction(root):
                continue
            found = []
            for name in _CHROMIUM_CACHE_NAMES + tuple(extra):
                p = os.path.join(root, name)
                if os.path.isdir(p) and not _is_link_or_junction(p):
                    found.append(p)
            if not found:
                continue
            if _is_file_in_use(os.path.join(root, "lockfile")):
                _skipped_in_use += 1
                continue
            for p in found:
                items.append(JunkItem("app_cache", "folder", p, _dir_size_bytes(p)))

    local = os.environ.get("LOCALAPPDATA")
    if local:
        steam_html = os.path.join(local, "Steam", "htmlcache")
        for d in _chromium_cache_dirs(steam_html):
            items.append(JunkItem("app_cache", "folder", d, _dir_size_bytes(d)))
    return items


_CHROMIUM_HISTORY_FILES = (
    "History", "History-journal", "Visited Links", "Top Sites", "Top Sites-journal",
    "Shortcuts", "Shortcuts-journal", "Network Action Predictor",
    "Network Action Predictor-journal",
)

_CHROMIUM_HISTORY_ROOTS = (
    ("Chrome", "chrome.exe", "LOCALAPPDATA", ("Google", "Chrome", "User Data")),
    ("Edge", "msedge.exe", "LOCALAPPDATA", ("Microsoft", "Edge", "User Data")),
    ("Brave", "brave.exe", "LOCALAPPDATA", ("BraveSoftware", "Brave-Browser", "User Data")),
    ("Vivaldi", "vivaldi.exe", "LOCALAPPDATA", ("Vivaldi", "User Data")),
    ("Chromium", "chrome.exe", "LOCALAPPDATA", ("Chromium", "User Data")),
    ("Opera", "opera.exe", "APPDATA", ("Opera Software", "Opera Stable")),
    ("Opera GX", "opera.exe", "APPDATA", ("Opera Software", "Opera GX Stable")),
)


def _history_root(env: str, parts: tuple) -> Optional[str]:
    base = os.environ.get(env)
    if not base:
        return None
    root = os.path.join(base, *parts)
    if not os.path.isdir(root) or _is_link_or_junction(root):
        return None
    return root


def _scan_browser_history(only: tuple = ()) -> list[JunkItem]:
    items: list[JunkItem] = []
    for name, _exe, env, parts in _CHROMIUM_HISTORY_ROOTS:
        if only and name not in only:
            continue
        root = _history_root(env, parts)
        if root is None:
            continue
        running = _is_file_in_use(os.path.join(root, "lockfile"))
        for profile in _chromium_profile_dirs(root):
            history = os.path.join(profile, "History")
            if not os.path.isfile(history):
                continue
            in_use = running or _is_file_in_use(history)
            for file_name in _CHROMIUM_HISTORY_FILES:
                path = os.path.join(profile, file_name)
                if os.path.isfile(path) and not _is_link_or_junction(path):
                    items.append(JunkItem("privacy_history", "file", path, _safe_size(path),
                                          browser=name if in_use else ""))
    return items


def _running_history_browsers(names: tuple) -> list[str]:
    running = []
    for name, _exe, env, parts in _CHROMIUM_HISTORY_ROOTS:
        if name in names:
            root = _history_root(env, parts)
            if root and _is_file_in_use(os.path.join(root, "lockfile")):
                running.append(name)
    return running


def rescan_browser_history(names: tuple) -> list[JunkItem]:
    return _scan_browser_history(only=tuple(names))


def close_browsers(names: tuple) -> list[str]:
    names = tuple(names)
    exes = sorted({exe for name, exe, _e, _p in _CHROMIUM_HISTORY_ROOTS if name in names})
    if not exes:
        return []

    def run_taskkill(force: bool):
        for exe in exes:
            args = [system_exe("taskkill.exe"), "/T", "/IM", exe] if not force else [system_exe("taskkill.exe"), "/F", "/T", "/IM", exe]
            try:
                subprocess.run(args, capture_output=True, timeout=20,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception:
                pass

    def wait_until_closed(seconds: float) -> list[str]:
        deadline = time.monotonic() + seconds
        while True:
            running = _running_history_browsers(names)
            if not running or time.monotonic() >= deadline:
                return running
            time.sleep(0.3)

    run_taskkill(False)
    if not wait_until_closed(5):
        return []
    run_taskkill(True)
    return wait_until_closed(8)


def _scan_thumbnail_cache() -> list[JunkItem]:
    items = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        explorer_dir = os.path.join(local_appdata, "Microsoft", "Windows", "Explorer")
        for f in glob.glob(os.path.join(explorer_dir, "thumbcache_*.db")):
            items.append(JunkItem("thumbnail_cache", "file", f, _safe_size(f)))
        for f in glob.glob(os.path.join(explorer_dir, "iconcache_*.db")):
            items.append(JunkItem("thumbnail_cache", "file", f, _safe_size(f)))
    return items


def _scan_children(category_key: str, folder: str) -> list[JunkItem]:
    items: list[JunkItem] = []
    if not folder or not os.path.isdir(folder):
        return items
    try:
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            if _is_fresh(full):
                continue
            if os.path.isfile(full):
                items.append(JunkItem(category_key, "file", full, _safe_size(full)))
            elif os.path.isdir(full):
                items.append(JunkItem(category_key, "folder", full, _dir_size_bytes(full)))
    except Exception:
        pass
    return items


def _scan_wer_reports() -> list[JunkItem]:
    items = []
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    wer_dirs = [os.path.join(program_data, "Microsoft", "Windows", "WER")]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        wer_dirs.append(os.path.join(local_appdata, "Microsoft", "Windows", "WER"))
    for wer_dir in wer_dirs:
        for sub in ("ReportQueue", "ReportArchive"):
            items += _scan_children("wer_reports", os.path.join(wer_dir, sub))
    return items


def _scan_recent_items() -> list[JunkItem]:
    items = []
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return items
    recent_dir = os.path.join(appdata, "Microsoft", "Windows", "Recent")
    if not os.path.isdir(recent_dir):
        return items
    try:
        for name in os.listdir(recent_dir):
            full = os.path.join(recent_dir, name)
            if os.path.isfile(full):
                if _is_fresh(full):
                    continue
                items.append(JunkItem("recent_items", "file", full, _safe_size(full)))
            elif os.path.isdir(full) and name in ("AutomaticDestinations", "CustomDestinations"):
                items += _scan_children("recent_items", full)
    except Exception:
        pass
    return items


def _local_fixed_drive_letters() -> list[str]:
    DRIVE_FIXED = 3
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        letters = [d for i, d in enumerate(string.ascii_uppercase) if mask & (1 << i)]
    except Exception:
        letters = [os.environ.get("SystemDrive", "C:").rstrip(":\\")]

    fixed = []
    for letter in letters:
        try:
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
        except Exception:
            continue
        if drive_type == DRIVE_FIXED:
            fixed.append(letter)
    return fixed


def _query_recycle_bin(drive_letter: str) -> tuple[int, int]:
    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint32),
            ("i64Size", ctypes.c_int64),
            ("i64NumItems", ctypes.c_int64),
        ]

    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    try:
        result = ctypes.windll.shell32.SHQueryRecycleBinW(f"{drive_letter}:\\", ctypes.byref(info))
    except Exception:
        return 0, 0
    if result != 0:
        return 0, 0
    return max(info.i64Size, 0), max(info.i64NumItems, 0)


def _scan_recycle_bin() -> list[JunkItem]:
    total = 0
    total_count = 0
    non_empty: list[str] = []
    available = set(_local_fixed_drive_letters())
    selected = get_selected_drives()
    drives = [d for d in selected if d in available] if selected is not None else sorted(available)
    for drive_letter in drives:
        size, count = _query_recycle_bin(drive_letter)
        if size > 0:
            total += size
            total_count += count
            non_empty.append(drive_letter)
    if total > 0:
        return [JunkItem("recycle_bin", "special_recycle_bin", "$Recycle.Bin", total,
                         drives=tuple(non_empty), item_count=total_count)]
    return []


def list_fixed_drives() -> list[str]:
    return sorted(_local_fixed_drive_letters())


def get_selected_drives() -> Optional[list[str]]:
    drives = load_settings().get("junk_selected_drives")
    if drives is None:
        return None
    if isinstance(drives, list):
        return [d for d in drives if isinstance(d, str) and len(d) == 1]
    return None


def set_selected_drives(drives: Optional[list[str]]) -> None:
    settings = load_settings()
    if drives is None:
        settings.pop("junk_selected_drives", None)
    else:
        settings["junk_selected_drives"] = list(dict.fromkeys(drives))
    save_settings(settings)


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _is_drive_root(path: str) -> bool:
    _drive, rest = os.path.splitdrive(os.path.normpath(path))
    return rest in ("", os.sep)


def _protected_paths() -> set:
    env_names = ("SYSTEMDRIVE", "WINDIR", "USERPROFILE", "PUBLIC", "ALLUSERSPROFILE",
                 "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA",
                 "APPDATA", "LOCALAPPDATA")
    paths = set()
    for name in env_names:
        value = os.environ.get(name)
        if value:
            paths.add(_norm_path(value))
    profile = os.environ.get("USERPROFILE")
    if profile:
        paths.add(_norm_path(os.path.dirname(os.path.normpath(profile))))
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for sub in ("System32", "SysWOW64", "WinSxS", "servicing", "Temp", "SystemTemp", "Logs",
                "Prefetch", "Minidump", "LiveKernelReports", "SoftwareDistribution",
                os.path.join("SoftwareDistribution", "Download")):
        paths.add(_norm_path(os.path.join(windir, sub)))
    temp = _temp_dir()
    if temp:
        paths.add(_norm_path(temp))
    return paths


def _is_protected_path(path: str, kind: str = "file") -> bool:
    if _is_drive_root(path):
        return True
    norm = _norm_path(path)
    protected = _protected_paths()
    if norm in protected:
        return True
    if kind == "folder":
        prefix = norm + os.sep
        return any(other.startswith(prefix) for other in protected)
    return False


def _delete_block_reason(item: "JunkItem") -> Optional[str]:
    """Reason why an item must NOT be deleted (or None). Checked right before deletion,
    not only at scan time, because time may have passed between the scan and the
    click on "Delete": e.g. a cache folder may have been replaced by a
    junction/symlink, and rmtree/remove on a link can reach outside the folder the
    user saw in the list.
    """
    if item.kind in ("special_recycle_bin", "special_restore_points"):
        return None
    if not os.path.isabs(item.path) or _is_protected_path(item.path, item.kind):
        return T("junk_err_blocked")
    if os.path.lexists(item.path) and _is_link_or_junction(item.path):
        return T("junk_err_blocked")
    return None


def _dedupe_and_prune(items: list[JunkItem]) -> list[JunkItem]:
    seen_paths = set()
    deduped: list[JunkItem] = []
    for it in items:
        key = _norm_path(it.path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        # Links (symlink/junction) are never "junk" to delete: the user may have moved a
        # browser cache to a RAM disk through a junction, and a program running as
        # administrator must not operate "through" a link in folders writable by other
        # users.
        if it.kind not in ("special_recycle_bin", "special_restore_points") and _is_link_or_junction(it.path):
            continue
        deduped.append(it)

    folder_prefixes = [_norm_path(it.path) + os.sep for it in deduped if it.kind == "folder"]
    if not folder_prefixes:
        return deduped

    pruned: list[JunkItem] = []
    for it in deduped:
        key_as_folder = _norm_path(it.path) + os.sep
        if any(key_as_folder.startswith(prefix) and key_as_folder != prefix for prefix in folder_prefixes):
            continue
        pruned.append(it)
    return pruned


def _scan_shader_cache() -> list[JunkItem]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    items: list[JunkItem] = []
    for folder in (
        os.path.join(local, "D3DSCache"),
        os.path.join(local, "NVIDIA", "DXCache"),
        os.path.join(local, "NVIDIA", "GLCache"),
        os.path.join(local, "AMD", "DxCache"),
    ):
        items += _scan_children("shader_cache", folder)
    return items


_TEMP_OLD_SECONDS = 30 * 24 * 3600
_ACTIVITY_SCAN_MAX_ENTRIES = 100_000
# Infrastructure folders, not junk (case-insensitive comparison). "Low" is the temp
# directory of low-integrity processes (protected mode, sandboxes) with its own
# integrity label: those processes cannot write to the parent (%TEMP%), so after it is
# deleted they cannot recreate it. Found on a real scan: "Low" was always "old" and
# empty, so it landed on the delete list.
_TEMP_OLD_SKIP_NAMES = frozenset({"low"})


def _has_recent_activity(path: str, cutoff: float) -> bool:
    try:
        if os.path.getmtime(path) >= cutoff:
            return True
    except OSError:
        return True
    seen = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    seen += 1
                    if seen > _ACTIVITY_SCAN_MAX_ENTRIES:
                        return True
                    try:
                        if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                            return True
                        if entry.is_dir(follow_symlinks=False) and not _is_reparse_dir_entry(entry):
                            stack.append(entry.path)
                    except OSError:
                        return True
        except OSError:
            return True
    return False


def _scan_temp_old_items() -> list[JunkItem]:
    items: list[JunkItem] = []
    temp = _temp_dir()
    if not temp or not os.path.isdir(temp):
        return items
    cutoff = time.time() - _TEMP_OLD_SECONDS
    try:
        names = os.listdir(temp)
    except OSError:
        return items
    for name in names:
        if name.lower() in _TEMP_OLD_SKIP_NAMES:
            continue
        full = os.path.join(temp, name)
        try:
            if _is_link_or_junction(full):
                continue
            if os.path.isdir(full):
                if not _has_recent_activity(full, cutoff):
                    items.append(JunkItem("temp_old_items", "folder", full, _dir_size_bytes(full)))
            elif os.path.isfile(full):
                if os.path.getmtime(full) < cutoff:
                    items.append(JunkItem("temp_old_items", "file", full, _safe_size(full)))
        except OSError:
            continue
    return items


_SCANNERS = {
    "user_temp": _scan_user_temp,
    "windows_temp": _scan_windows_temp,
    "windows_update": _scan_windows_update,
    "delivery_optimization": _scan_delivery_optimization,
    "memory_dumps": _scan_memory_dumps,
    "log_files": _scan_log_files,
    "stray_temp_files": _scan_stray_temp_files,
    "temp_old_items": _scan_temp_old_items,
    "browser_cache": _scan_browser_cache,
    "app_cache": _scan_app_cache,
    "privacy_history": _scan_browser_history,
    "thumbnail_cache": _scan_thumbnail_cache,
    "shader_cache": _scan_shader_cache,
    "wer_reports": _scan_wer_reports,
    "recent_items": _scan_recent_items,
    "prefetch": _scan_prefetch,
    "recycle_bin": _scan_recycle_bin,
    "windows_old": _scan_windows_old,
    "restore_points": _scan_restore_points,
}


_DELETE_ACCESS = 0x00010000
_FILE_SHARE_ALL = 0x00000007
_OPEN_EXISTING = 3
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33

_kernel32 = None


def _get_kernel32():
    global _kernel32
    if _kernel32 is None:
        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            k.CreateFileW.restype = ctypes.c_void_p
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            k.CloseHandle.restype = ctypes.c_int
            _kernel32 = k
        except Exception:
            return None
    return _kernel32


def _probe_delete_access(path: str) -> Optional[int]:
    try:
        k = _get_kernel32()
        if k is None:
            return None
        handle = k.CreateFileW(path, _DELETE_ACCESS, _FILE_SHARE_ALL, None, _OPEN_EXISTING, 0, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            return ctypes.get_last_error()
        k.CloseHandle(handle)
        return None
    except Exception:
        return None


def _is_file_in_use(path: str) -> bool:
    return _probe_delete_access(path) in (_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION)


_skipped_in_use = 0


def get_skipped_in_use_count() -> int:
    return _skipped_in_use


def _drop_files_in_use(items: list[JunkItem]) -> list[JunkItem]:
    global _skipped_in_use
    kept: list[JunkItem] = []
    for item in items:
        if item.kind == "file" and not item.browser and _is_file_in_use(item.path):
            _skipped_in_use += 1
            continue
        kept.append(item)
    return kept


_failed_categories: list[str] = []


def get_failed_category_count() -> int:
    return len(_failed_categories)


def mark_scan_failed(category_keys: list[str]) -> None:
    _failed_categories.extend(category_keys)


def scan_junk(category_keys: list[str]) -> list[JunkItem]:
    global _skipped_in_use
    _scan_notes.clear()
    _failed_categories.clear()
    _skipped_in_use = 0
    items: list[JunkItem] = []
    for key in category_keys:
        scanner = _SCANNERS.get(key)
        if scanner is None:
            continue
        try:
            items.extend(scanner())
        except Exception:
            _failed_categories.append(key)
    return _drop_files_in_use(_dedupe_and_prune(items))


_E_UNEXPECTED = -2147418113


def _empty_recycle_bin(drives: tuple = ()) -> tuple[bool, str]:
    """Empty the Recycle Bin ONLY on the given drives. SHEmptyRecycleBinW with
    pszRootPath=None empties the bins of ALL drives at once, ignoring the selection
    from Settings (the scan showed the size of the selected drives only, yet
    deletion cleared everything). An empty tuple = legacy behaviour (all drives).
    """
    SHERB_NOCONFIRMATION = 0x00000001
    SHERB_NOPROGRESSUI = 0x00000002
    SHERB_NOSOUND = 0x00000004
    flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND

    roots = [f"{d}:\\" for d in drives] or [None]
    errors: list[str] = []
    for root in roots:
        try:
            result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, root, flags)
        except Exception as e:
            errors.append(f"{root or '*'}: {e}")
            continue
        if result not in (0, _E_UNEXPECTED):
            errors.append(f"{root or '*'}: SHEmptyRecycleBinW error code {result}")
    return (not errors), "; ".join(errors)


def _service_control(service: str, action: str, timeout: int = 15) -> bool:
    try:
        result = subprocess.run(
            [system_exe("net.exe"), action, service],
            capture_output=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.returncode == 0
    except Exception:
        return False


_SERVICE_STOP_FOR_CATEGORY = {
    "windows_update": "wuauserv",
    "delivery_optimization": "DoSvc",
}


def _run_quiet(cmd: list, timeout: int) -> bool:
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.returncode == 0
    except Exception:
        return False


_OWNERSHIP_TIMEOUT_SECONDS = 1800


def _take_ownership_recursive(path: str) -> None:
    admins = "*S-1-5-32-544"
    if not _run_quiet([system_exe("takeown.exe"), "/F", path, "/R", "/D", "Y"], _OWNERSHIP_TIMEOUT_SECONDS):
        _run_quiet([system_exe("icacls.exe"), path, "/setowner", admins, "/T", "/C", "/Q"], _OWNERSHIP_TIMEOUT_SECONDS)
    _run_quiet([system_exe("icacls.exe"), path, "/grant", admins + ":F", "/T", "/C", "/Q"], _OWNERSHIP_TIMEOUT_SECONDS)


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


DELETE_PERMANENT = "permanent"
DELETE_RECYCLE = "recycle"

_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400
_FOF_WANTNUKEWARNING = 0x4000
# SHFileOperation does not handle long paths and the Recycle Bin does not store paths
# longer than MAX_PATH; such items are rejected instead of risking permanent deletion
# instead of a move.
_MAX_RECYCLE_PATH = 259

REGENERABLE_CACHE_CATEGORIES = ("thumbnail_cache", "shader_cache", "prefetch", "wer_reports",
                                "windows_update", "delivery_optimization", "app_cache")

_ALWAYS_PERMANENT_CATEGORIES = ("windows_old", "privacy_history") + REGENERABLE_CACHE_CATEGORIES


def _make_shfileop_struct():
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        # On 32-bit Windows the shellapi.h header packs this structure to 1.
        _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 0
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", ctypes.c_void_p),
            ("pTo", ctypes.c_void_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_void_p),
        ]
    return SHFILEOPSTRUCTW


def _send_to_recycle_bin(path: str) -> tuple[bool, str]:
    failed = T("junk_err_recycle_failed")
    if not os.path.isabs(path) or len(path) > _MAX_RECYCLE_PATH:
        return False, failed
    path = os.path.normpath(path)
    try:
        struct_type = _make_shfileop_struct()
        buf = ctypes.create_unicode_buffer(path + "\0")
        op = struct_type()
        op.hwnd = None
        op.wFunc = _FO_DELETE
        op.pFrom = ctypes.addressof(buf)
        op.pTo = None
        op.fFlags = (_FOF_ALLOWUNDO | _FOF_SILENT | _FOF_NOCONFIRMATION
                     | _FOF_NOERRORUI | _FOF_WANTNUKEWARNING)
        fn = ctypes.windll.shell32.SHFileOperationW
        fn.argtypes = [ctypes.POINTER(struct_type)]
        fn.restype = ctypes.c_int
        result = fn(ctypes.byref(op))
    except Exception as e:
        return False, f"{failed} ({e})"
    if result != 0 or op.fAnyOperationsAborted:
        return False, f"{failed} (0x{result & 0xFFFFFFFF:X})"
    if os.path.lexists(path):
        return False, failed
    return True, ""


def delete_junk_items(items: list[JunkItem], mode: str = DELETE_PERMANENT) -> tuple[int, int, list[str], int]:
    if mode not in (DELETE_PERMANENT, DELETE_RECYCLE):
        raise ValueError(f"nieznany tryb usuwania: {mode!r}")
    ok_count = 0
    freed_bytes = 0
    recycled_bytes = 0
    errors: list[str] = []

    if mode == DELETE_RECYCLE:
        # Empty the Recycle Bin first, otherwise we would empty it AFTER we have put
        # this run's items into it.
        items = sorted(items, key=lambda i: i.kind != "special_recycle_bin")

    services_to_toggle = sorted({
        _SERVICE_STOP_FOR_CATEGORY[item.category_key]
        for item in items
        if item.category_key in _SERVICE_STOP_FOR_CATEGORY
    })
    # Resume only the services we ACTUALLY stopped (net stop returned 0). A service that
    # was not running anyway (e.g. wuauserv is "Manual" by default) must not be
    # force-started after cleaning.
    stopped_services = [svc for svc in services_to_toggle if _service_control(svc, "stop")]

    try:
        for item in items:
            try:
                blocked = _delete_block_reason(item)
                if blocked:
                    errors.append(f"{item.path}: {blocked}")
                    continue

                if item.kind == "special_recycle_bin":
                    ok, err = _empty_recycle_bin(item.drives)
                    if ok:
                        ok_count += item.item_count or 1
                        freed_bytes += item.size_bytes
                    else:
                        errors.append(f"{item.path}: {err}")
                    continue

                if item.kind == "special_restore_points":
                    removed, errs = _delete_restore_points(item.restore_point_seqs)
                    ok_count += removed
                    if removed > 0:
                        freed_bytes += item.size_bytes
                    errors.extend(f"{item.path}: {e}" for e in errs)
                    continue

                if (mode == DELETE_RECYCLE and item.kind in ("file", "folder")
                        and item.category_key not in _ALWAYS_PERMANENT_CATEGORIES):
                    if not os.path.lexists(item.path):
                        continue
                    moved, err = _send_to_recycle_bin(item.path)
                    if moved:
                        ok_count += 1
                        recycled_bytes += item.size_bytes
                    else:
                        errors.append(f"{item.path}: {err}")
                    continue

                if item.kind == "file":
                    if os.path.exists(item.path):
                        _remove_file(item.path)
                        ok_count += 1
                        freed_bytes += item.size_bytes
                elif item.kind == "folder":
                    if os.path.isdir(item.path):
                        if item.category_key == "windows_old":
                            _take_ownership_recursive(item.path)

                        rm_errors: list[tuple] = []

                        def _on_rm_error(func, path, exc_info):
                            if func in (os.remove, os.unlink, os.rmdir) \
                                    and isinstance(exc_info[1], PermissionError):
                                try:
                                    os.chmod(path, stat.S_IWRITE)
                                    func(path)
                                    return
                                except OSError:
                                    pass
                            rm_errors.append((func, path, exc_info))

                        shutil.rmtree(item.path, onerror=_on_rm_error)
                        if rm_errors:
                            still_exists = os.path.isdir(item.path)
                            remaining = _dir_size_bytes(item.path) if still_exists else 0
                            recovered = max(item.size_bytes - remaining, 0)
                            freed_bytes += recovered

                            failed_files = sum(
                                1 for func, _p, _e in rm_errors
                                if func in (os.remove, os.unlink)
                            ) or len(rm_errors)

                            if not still_exists or recovered > 0:
                                ok_count += 1
                                errors.append(
                                    f"{item.path}: " + T("junk_err_partial", count=failed_files)
                                )
                            else:
                                errors.append(
                                    f"{item.path}: " + T("junk_err_nothing_removed")
                                )
                            continue
                        else:
                            freed_bytes += item.size_bytes
                            ok_count += 1
            except Exception as e:
                errors.append(f"{item.path}: {e}")
    finally:
        for svc in stopped_services:
            _service_control(svc, "start")

    return ok_count, freed_bytes, errors, recycled_bytes
