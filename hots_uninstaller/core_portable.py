import os
import re
import sys
import tempfile
import winreg
from pathlib import Path

from . import core_uninstall as cu

_REMOVE_ARG = "--remove"
_MAX_QUEUED_REQUESTS = 50

_NAME_SUFFIX_RE = re.compile(
    r"[\s._-]*(x64|x86|amd64|win64|win32|64bit|32bit|portable|v?\d+(\.\d+)*)$",
    re.IGNORECASE,
)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _is_under(path: str, folder: str) -> bool:
    p, f = _norm(path), _norm(folder)
    return p == f or p.startswith(f + os.sep)


def _clean_stem(stem: str) -> str:
    name = stem
    while True:
        shorter = _NAME_SUFFIX_RE.sub("", name)
        if shorter == name or not shorter:
            break
        name = shorter
    return name.strip() or stem


def portable_display_name(exe_path: str) -> str:
    return _clean_stem(os.path.splitext(os.path.basename(exe_path))[0])


def _under_any(path: str, folders: list) -> bool:
    return any(_is_under(path, f) or _is_under(path, os.path.realpath(f)) for f in folders)


def _own_dirs() -> list:
    dirs = []
    for candidate in (sys.argv[0] if sys.argv else "", sys.executable):
        if candidate:
            dirs.append(os.path.dirname(os.path.abspath(candidate)))
    return dirs


def _system_dirs() -> list:
    dirs = []
    for var in ("WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA"):
        value = os.environ.get(var)
        if value:
            dirs.append(value)
    return dirs


def validate_portable_exe(path: str) -> str:
    if (not path or not os.path.isabs(path) or not path.lower().endswith(".exe")
            or not os.path.isfile(path)):
        return "portable_err_invalid"
    real = os.path.realpath(path)
    own, system = _own_dirs(), _system_dirs()
    if _under_any(path, own) or _under_any(real, own):
        return "portable_err_self"
    if _under_any(path, system) or _under_any(real, system):
        return "portable_err_system"
    return ""


def build_portable_app(exe_path: str) -> "cu.InstalledApp":
    name = portable_display_name(exe_path)
    return cu.InstalledApp(
        key_name=f"portable:{name}",
        hive=winreg.HKEY_CURRENT_USER,
        hive_name="HKCU",
        is_wow64=False,
        display_name=name,
    )


def _scan_path_traces(exe_path: str) -> list:
    low = exe_path.lower()
    items = list(cu._scan_firewall_rule_leftovers([low]))
    items += cu._scan_path_named_value_leftovers(
        winreg.HKEY_CURRENT_USER, cu._APPCOMPAT_STORE_SUBKEY, [low])
    mui_needles = [low + ".friendlyappname", low + ".applicationcompany"]
    for subkey in cu._MUICACHE_SUBKEYS:
        items += cu._scan_path_named_value_leftovers(
            winreg.HKEY_CURRENT_USER, subkey, mui_needles)
    items += cu._scan_run_leftovers([low])
    items += cu._scan_app_paths_leftovers([low])
    for it in items:
        it.preselected = True
    return items


def _shared_folders() -> set:
    folders = {tempfile.gettempdir()}
    profile = os.environ.get("USERPROFILE")
    if profile:
        folders.add(profile)
        for name in ("Desktop", "Downloads", "Documents", "Music", "Pictures", "Videos"):
            folders.add(os.path.join(profile, name))
    public = os.environ.get("PUBLIC")
    if public:
        folders.add(public)
    return {_norm(f) for f in folders}


def _compact(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", name.lower())


_MAX_FOLDER_LEVELS_UP = 2


def _own_folder(exe_path: str) -> str:
    wanted = _compact(portable_display_name(exe_path))
    if not wanted:
        return ""
    real_exe = os.path.realpath(exe_path)
    shared = _shared_folders()
    folder = os.path.dirname(exe_path)
    match = ""
    for _ in range(_MAX_FOLDER_LEVELS_UP + 1):
        if not os.path.isdir(folder) or os.path.islink(folder) or cu._is_reparse_folder(folder):
            break
        if cu._is_protected_dir(folder) or _norm(folder) in shared:
            break
        if (_compact(_clean_stem(os.path.basename(folder))) == wanted
                and _is_under(real_exe, os.path.realpath(folder))):
            match = folder
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return match


def _scan_own_folder(exe_path: str) -> list:
    folder = _own_folder(exe_path)
    if not folder:
        return []
    return [cu.LeftoverItem(kind="folder", path=os.path.normpath(folder),
                            size_kb=cu._dir_size_kb(folder))]


def running_processes(exe_path: str) -> list:
    exact = _norm(exe_path)
    folder = _own_folder(exe_path)
    prefix = _norm(folder) + os.sep if folder else ""
    try:
        from . import core_monitor as cm
        pids = list(cm._list_processes())
    except Exception:
        return []
    me = os.getpid()
    seen = set()
    found = []
    for pid in pids:
        if pid in (0, me):
            continue
        raw = cu._process_image_path(pid)
        if not raw:
            continue
        norm = _norm(raw)
        if norm in seen:
            continue
        if norm == exact or (prefix and norm.startswith(prefix)):
            seen.add(norm)
            found.append(raw)
    return found


def _scan_exact_prefetch(exe_path: str) -> list:
    base = os.path.basename(exe_path)
    pattern = re.compile(r"^" + re.escape(base) + r"-[0-9A-F]{8}\.pf$", re.IGNORECASE)
    prefetch_dir = cu._prefetch_dir()
    items = []
    try:
        names = os.listdir(prefetch_dir)
    except OSError:
        return items
    for name in names:
        if pattern.match(name):
            full = os.path.join(prefetch_dir, name)
            try:
                size_kb = max(1, os.path.getsize(full) // 1024)
            except OSError:
                size_kb = 0
            items.append(cu.LeftoverItem(kind="file", path=full, size_kb=size_kb))
    return items


_LNK_MAX_BYTES = 256 * 1024
_LNK_MAX_DEPTH = 3


def _read_cstr(data: bytes, offset: int, unicode: bool) -> str:
    if offset <= 0 or offset >= len(data):
        return ""
    if unicode:
        end = offset
        while end + 1 < len(data) and data[end:end + 2] != b"\x00\x00":
            end += 2
        return data[offset:end].decode("utf-16-le", errors="ignore")
    end = data.find(b"\x00", offset)
    end = len(data) if end == -1 else end
    return data[offset:end].decode("mbcs" if sys.platform == "win32" else "latin-1", errors="ignore")


def read_lnk_target(path: str) -> str:
    try:
        if os.path.getsize(path) > _LNK_MAX_BYTES:
            return ""
        with open(path, "rb") as f:
            data = f.read()
        if len(data) < 0x4C or data[0:4] != b"\x4c\x00\x00\x00" or data[4:20] != (
                b"\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46"):
            return ""
        flags = int.from_bytes(data[20:24], "little")
        pos = 0x4C
        if flags & 0x01:
            pos += 2 + int.from_bytes(data[pos:pos + 2], "little")
        if not flags & 0x02:
            return ""
        info = data[pos:]
        size = int.from_bytes(info[0:4], "little")
        header_size = int.from_bytes(info[4:8], "little")
        info_flags = int.from_bytes(info[8:12], "little")
        if size < 0x1C or size > len(info) or not info_flags & 0x01:
            return ""
        info = info[:size]
        base_off = int.from_bytes(info[16:20], "little")
        suffix_off = int.from_bytes(info[24:28], "little")
        if header_size >= 0x24:
            base_u = int.from_bytes(info[28:32], "little")
            suffix_u = int.from_bytes(info[32:36], "little")
            base = _read_cstr(info, base_u, True) if base_u else _read_cstr(info, base_off, False)
            suffix = _read_cstr(info, suffix_u, True) if suffix_u else _read_cstr(info, suffix_off, False)
        else:
            base = _read_cstr(info, base_off, False)
            suffix = _read_cstr(info, suffix_off, False)
        return base + suffix
    except Exception:
        return ""


def _scan_shortcuts_to(exe_path: str) -> list:
    wanted = _norm(exe_path)
    items = []
    seen = set()
    for root in cu._shortcut_roots():
        if not os.path.isdir(root):
            continue
        root_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if dirpath.rstrip("\\/").count(os.sep) - root_depth >= _LNK_MAX_DEPTH:
                dirnames[:] = []
            for name in filenames:
                if not name.lower().endswith(".lnk"):
                    continue
                full = os.path.join(dirpath, name)
                target = read_lnk_target(full)
                if not target or _norm(target) != wanted:
                    continue
                key = _norm(full)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    size_kb = max(1, os.path.getsize(full) // 1024)
                except OSError:
                    size_kb = 0
                items.append(cu.LeftoverItem(kind="file", path=full, size_kb=size_kb, preselected=True))
    return items


def scan_portable(exe_path: str) -> list:
    try:
        size_kb = max(1, os.path.getsize(exe_path) // 1024)
    except OSError:
        size_kb = 0
    items = [cu.LeftoverItem(kind="file", path=exe_path, size_kb=size_kb, preselected=True)]
    seen = {(items[0].kind, _norm(exe_path))}

    sources = (
        lambda: _scan_shortcuts_to(exe_path),
        lambda: _scan_own_folder(exe_path),
        lambda: _scan_path_traces(exe_path),
        lambda: _scan_exact_prefetch(exe_path),
        lambda: cu.scan_leftovers(build_portable_app(exe_path)),
    )
    for source in sources:
        try:
            found = source()
        except Exception:
            continue
        for it in found:
            key = (it.kind, it.path.lower() if it.kind != "file" else _norm(it.path))
            if key in seen:
                continue
            seen.add(key)
            items.append(it)
    return items


# Paths from the queue are never deleted automatically: they go through validation and
# end up in a window where the user sees the full path and confirms.
def _queue_path() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home())
    return base / "HOTS Uninstaller" / "pending_remove.txt"


def parse_remove_arg(argv: list) -> str:
    for i, arg in enumerate(argv):
        if arg == _REMOVE_ARG and i + 1 < len(argv):
            return argv[i + 1].strip().strip('"')
    return ""


def strip_remove_args(argv: list) -> list:
    out, skip = [], False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg == _REMOVE_ARG:
            skip = True
            continue
        out.append(arg)
    return out


def pop_pending_requests() -> list:
    """Reads and clears the queue. The file is renamed before reading, so a task
    appended in the meantime lands in a new file instead of being lost.
    """
    queue = _queue_path()
    work = queue.with_suffix(".proc")
    try:
        os.replace(queue, work)
    except OSError:
        return []
    paths = []
    try:
        with open(work, "r", encoding="utf-8") as f:
            for line in f:
                p = line.strip().strip('"')
                if p and p not in paths:
                    paths.append(p)
                if len(paths) >= _MAX_QUEUED_REQUESTS:
                    break
    except OSError:
        pass
    finally:
        try:
            os.remove(work)
        except OSError:
            pass
    return paths
