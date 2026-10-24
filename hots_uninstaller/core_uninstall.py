import ctypes
import hashlib
import json
import os
import sys
import re
import struct
import subprocess
import tempfile
import threading
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .i18n import T
from .proc_utils import launch_deelevated, popen_no_busy_cursor, system_exe

_UNINSTALL_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
_UNINSTALL_SUBKEY_WOW64 = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"

_SCAN_ROOTS = [
    (winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_SUBKEY, False),
    (winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_SUBKEY_WOW64, True),
    (winreg.HKEY_CURRENT_USER, _UNINSTALL_SUBKEY, False),
    (winreg.HKEY_CURRENT_USER, _UNINSTALL_SUBKEY_WOW64, True),
]

_HIVE_NAMES = {
    winreg.HKEY_LOCAL_MACHINE: "HKLM",
    winreg.HKEY_CURRENT_USER: "HKCU",
}

_HIVE_FULL_NAMES = {
    winreg.HKEY_LOCAL_MACHINE: "HKEY_LOCAL_MACHINE",
    winreg.HKEY_CURRENT_USER: "HKEY_CURRENT_USER",
}

try:
    _MSI_DLL = ctypes.WinDLL("msi.dll")
    _MSI_DLL.MsiGetProductInfoW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    _MSI_DLL.MsiGetProductInfoW.restype = ctypes.c_uint
except Exception:
    _MSI_DLL = None


def _msi_product_property(product_code: str, prop_name: str) -> str:
    if _MSI_DLL is None or not product_code:
        return ""
    try:
        size = wintypes.DWORD(0)
        ret = _MSI_DLL.MsiGetProductInfoW(product_code, prop_name, None, ctypes.byref(size))
        if ret != 0 or size.value <= 0:
            return ""
        size.value += 1
        buf = ctypes.create_unicode_buffer(size.value)
        ret = _MSI_DLL.MsiGetProductInfoW(product_code, prop_name, buf, ctypes.byref(size))
        if ret != 0:
            return ""
        return buf.value or ""
    except Exception:
        return ""


def _normalize_folder_name(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _build_install_dir_index() -> dict:
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
    ]
    local_appdata = os.environ.get("LocalAppData")
    if local_appdata:
        roots.append(os.path.join(local_appdata, "Programs"))

    index: dict = {}
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            with os.scandir(root) as it:
                top_dirs = [e for e in it if e.is_dir()]
        except Exception:
            continue
        for entry in top_dirs:
            norm = _normalize_folder_name(entry.name)
            if norm and norm not in index:
                index[norm] = entry.path
            try:
                with os.scandir(entry.path) as it2:
                    for sub in it2:
                        if not sub.is_dir():
                            continue
                        sub_norm = _normalize_folder_name(sub.name)
                        if sub_norm and sub_norm not in index:
                            index[sub_norm] = sub.path
            except Exception:
                continue
    return index


def _decode_console_bytes(data: bytes) -> str:
    if not data:
        return ""
    try:
        oem_cp = ctypes.windll.kernel32.GetOEMCP()
        return data.decode(f"cp{oem_cp}", errors="replace")
    except Exception:
        return data.decode("utf-8", errors="replace")

# Folders that must NEVER be touched when scanning/removing leftovers: protection
# against accidentally deleting the system (e.g. a badly written installer put the whole
# "Program Files" in InstallLocation instead of the product subfolder; see install_loc
# in scan_leftovers). The C:\... set is only a FALLBACK for missing environment
# variables; the real protection is built dynamically in _protected_dir_set() below, so
# it also works when Windows is installed on a drive other than C:.
_PROTECTED_DIRS = {
    r"c:\windows", r"c:\windows\system32", r"c:\windows\syswow64",
    r"c:\program files", r"c:\program files (x86)", r"c:\programdata", r"c:\users",
}


def _protected_dir_set() -> set[str]:
    paths = set(_PROTECTED_DIRS)
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        v = os.environ.get(env)
        if v:
            paths.add(v)
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        paths.add(os.path.dirname(userprofile))
    else:
        sysdrive = os.environ.get("SystemDrive")
        if sysdrive:
            paths.add(sysdrive + r"\Users")

    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.add(os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs"))
    program_data = os.environ.get("ProgramData")
    if program_data:
        paths.add(os.path.join(program_data, r"Microsoft\Windows\Start Menu\Programs"))
    if userprofile:
        paths.add(os.path.join(userprofile, "Desktop"))
    public = os.environ.get("PUBLIC")
    if public:
        paths.add(os.path.join(public, "Desktop"))
    if userprofile:
        paths.add(userprofile)
        for name in ("Documents", "Downloads", "Pictures", "Music", "Videos", "Favorites",
                     "Contacts", "Links", "Saved Games", "3D Objects", "Searches"):
            paths.add(os.path.join(userprofile, name))
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial",
                "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432"):
        v = os.environ.get(env)
        if v:
            paths.add(v)

    return {os.path.normcase(os.path.normpath(p)) for p in paths if p}


def _protected_container_set() -> set[str]:
    paths = set()
    appdata = os.environ.get("APPDATA")
    local = os.environ.get("LOCALAPPDATA")
    userprofile = os.environ.get("USERPROFILE")
    program_data = os.environ.get("ProgramData")
    if userprofile:
        paths.add(os.path.join(userprofile, "AppData"))
        paths.add(os.path.join(userprofile, "AppData", "LocalLow"))
    for base in (appdata, local, program_data):
        if base:
            paths.add(base)
            paths.add(os.path.join(base, "Microsoft"))
    if local:
        paths.update(os.path.join(local, n) for n in ("Programs", "Packages", "Temp"))
    return {os.path.normcase(os.path.normpath(p)) for p in paths if p}


def _windows_dir_norm() -> str:
    return os.path.normcase(os.path.normpath(
        os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"))


def _is_under_windows_dir(norm: str) -> bool:
    windir = _windows_dir_norm()
    return norm == windir or norm.startswith(windir + os.sep)


def _is_system_container(norm: str) -> bool:
    return bool(re.fullmatch(r"[a-z]:\\?", norm)) or norm in _protected_dir_set()


def format_size_kb(kb: int) -> str:
    if kb <= 0:
        return "—"
    if kb < 1024:
        return f"{kb} KB"
    mb = kb / 1024.0
    if mb >= 1024:
        return f"{mb / 1024.0:.2f} GB"
    return f"{mb:.1f} MB"


@dataclass
class InstalledApp:
    key_name: str
    hive: int
    hive_name: str
    is_wow64: bool
    display_name: str
    display_version: str = ""
    publisher: str = ""
    install_date: str = ""
    install_date_estimated: bool = False
    estimated_size_kb: int = 0
    uninstall_string: str = ""
    quiet_uninstall_string: str = ""
    install_location: str = ""
    display_icon: str = ""
    system_component: bool = False
    is_windows_installer: bool = False
    help_link: str = ""
    url_info_about: str = ""
    is_update: bool = False
    has_parent: bool = False
    no_remove: bool = False
    install_source: str = ""

    @property
    def uid(self) -> str:
        return f"{self.hive_name}\\{'WOW64' if self.is_wow64 else 'NATIVE'}\\{self.key_name}"

    @property
    def is_component(self) -> bool:
        return (self.system_component or self.is_update
                or self.has_parent or self.no_remove)

    @property
    def size_display(self) -> str:
        return format_size_kb(self.estimated_size_kb)

    @property
    def install_date_display(self) -> str:
        d = (self.install_date or "").strip()
        if len(d) == 8 and d.isdigit():
            return f"{d[6:8]}.{d[4:6]}.{d[0:4]}"
        return d

    @property
    def is_ghost(self) -> bool:
        if self.no_remove:
            return False
        if self.is_windows_installer:
            return False
        if not self.uninstall_string and not self.quiet_uninstall_string:
            return True
        return bool(self.missing_uninstaller_path)

    @property
    def missing_uninstaller_path(self) -> str:
        if self.is_windows_installer:
            return ""
        target = _uninstall_target_path(self.uninstall_string or self.quiet_uninstall_string)
        if target and not os.path.exists(target):
            return target
        return ""

    @property
    def installed_from_package_cache(self) -> bool:
        return "\\package cache\\" in (self.install_source or "").lower().replace("/", "\\")

    @property
    def is_part_of_larger_package(self) -> bool:
        return self.system_component or self.installed_from_package_cache

    @property
    def is_chained_msi_subpackage(self) -> bool:
        if not self.is_windows_installer:
            return False
        cmd = self.uninstall_string or self.quiet_uninstall_string
        if not _msi_uninstall_string_is_broken(cmd):
            return False
        return self.is_part_of_larger_package


def _reg_get_str(key, name: str) -> str:
    try:
        val, _ = winreg.QueryValueEx(key, name)
        return str(val) if val is not None else ""
    except Exception:
        return ""


def _reg_get_int(key, name: str) -> int:
    try:
        val, _ = winreg.QueryValueEx(key, name)
        return int(val)
    except Exception:
        return 0


_FILETIME_UNIX_EPOCH_DIFF = 116444736000000000


def _normalize_install_date(raw: str) -> str:
    text = (raw or "").strip()
    year = month = day = None
    if len(text) == 8 and text.isdigit():
        year, month, day = int(text[:4]), int(text[4:6]), int(text[6:])
    else:
        m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", text)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?!\d)", text)
            if m:
                first, second, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if first > 12 >= second:
                    day, month = first, second
                elif second > 12 >= first:
                    month, day = first, second
    if year is None or month is None or day is None:
        return ""
    try:
        return datetime(year, month, day).strftime("%Y%m%d")
    except ValueError:
        return ""


def _key_last_write_date(key) -> str:
    try:
        _, _, filetime = winreg.QueryInfoKey(key)
        if not filetime:
            return ""
        unix_ts = (filetime - _FILETIME_UNIX_EPOCH_DIFF) / 10_000_000
        return datetime.utcfromtimestamp(unix_ts).strftime("%Y%m%d")
    except Exception:
        return ""


_CMD_EXE_END_RE = re.compile(r'^(.*?\.(?:exe|com|bat|cmd))(?=$|[\s",/])', re.IGNORECASE)

_RUNDLL_PAYLOAD_RE = re.compile(r'^"?(.+?\.(?:dll|cpl|ocx|exe))(?=["\s,]|$)', re.IGNORECASE)


def _split_command(cmd: str) -> tuple[str, str]:
    cmd = os.path.expandvars((cmd or "").strip())
    if not cmd:
        return "", ""
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end != -1:
            return cmd[1:end], cmd[end + 1:].strip()
        return cmd[1:], ""
    m = _CMD_EXE_END_RE.match(cmd)
    if m:
        return m.group(1), cmd[m.end():].strip()
    head, _, rest = cmd.partition(" ")
    return head, rest.strip()


def _extract_exe_path(cmd: str) -> str:
    exe = _split_command(cmd)[0]
    return os.path.normpath(exe) if exe else exe


def _resolve_bare_command(name: str) -> str:
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    dirs = [os.path.join(sysroot, "System32"), sysroot]
    dirs += [d.strip('"') for d in os.environ.get("PATH", "").split(os.pathsep) if d.strip()]
    names = [name] if os.path.splitext(name)[1] else [name + ".exe", name]
    for d in dirs:
        for n in names:
            candidate = os.path.join(d, n)
            if os.path.exists(candidate):
                return candidate
    return name


def _uninstall_target_path(cmd: str) -> str:
    exe, rest = _split_command(cmd)
    if not exe:
        return ""

    if os.path.basename(exe).lower() in ("rundll32", "rundll32.exe"):
        m = _RUNDLL_PAYLOAD_RE.match(rest)
        if m and ("\\" in m.group(1) or "/" in m.group(1)):
            return os.path.normpath(m.group(1))

    if "\\" not in exe and "/" not in exe:
        return _resolve_bare_command(exe)
    return os.path.normpath(exe)


def list_installed_apps(include_system: bool = False) -> list[InstalledApp]:
    results: list[InstalledApp] = []
    seen_uids = set()
    _install_dir_index_cache = []

    def _install_dir_index() -> dict:
        if not _install_dir_index_cache:
            _install_dir_index_cache.append(_build_install_dir_index())
        return _install_dir_index_cache[0]

    for hive, subkey, is_wow64 in _SCAN_ROOTS:
        try:
            root = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)
        except OSError:
            continue

        with root:
            i = 0
            while True:
                try:
                    key_name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1

                try:
                    with winreg.OpenKey(root, key_name, 0, winreg.KEY_READ) as k:
                        display_name = _reg_get_str(k, "DisplayName")
                        if not display_name:
                            continue

                        install_date = _normalize_install_date(_reg_get_str(k, "InstallDate"))
                        install_date_estimated = False
                        if not install_date:
                            install_date = _key_last_write_date(k)
                            install_date_estimated = bool(install_date)

                        system_component = _reg_get_int(k, "SystemComponent") == 1
                        release_type = _reg_get_str(k, "ReleaseType")
                        parent_key = _reg_get_str(k, "ParentKeyName")
                        is_update = release_type.lower() in ("hotfix", "security update", "update")
                        # NoRemove=1 is Windows' own signal that the entry is
                        # informational and cannot be uninstalled from the UI. Some
                        # programs (e.g. ESET antivirus) set it DELIBERATELY despite
                        # having a working UninstallString, to discourage deleting the
                        # registry key as a shortcut and force their own uninstaller. So
                        # NoRemove does not mean "nothing can be done"; it is evaluated
                        # below, after uninstall_string/quiet_uninstall_string are read.
                        no_remove_flag = _reg_get_int(k, "NoRemove") == 1

                        install_location = _reg_get_str(k, "InstallLocation")
                        if install_location:
                            install_location = os.path.expandvars(install_location).strip().strip('"')
                        uninstall_string = _reg_get_str(k, "UninstallString")
                        quiet_uninstall_string = _reg_get_str(k, "QuietUninstallString")
                        no_remove = no_remove_flag and not (uninstall_string or quiet_uninstall_string)

                        if not install_location:
                            exe_path = _extract_exe_path(uninstall_string or quiet_uninstall_string)
                            candidate = os.path.dirname(exe_path) if exe_path else ""
                            if candidate and os.path.isdir(candidate):
                                install_location = candidate

                        is_windows_installer = _reg_get_int(k, "WindowsInstaller") == 1
                        if not install_location and is_windows_installer:
                            msi_loc = _msi_product_property(key_name, "InstallLocation").strip().strip('"')
                            if msi_loc:
                                msi_loc = os.path.expandvars(msi_loc)
                            if msi_loc and os.path.isdir(msi_loc):
                                install_location = msi_loc

                        if not install_location and display_name:
                            guessed = _install_dir_index().get(_normalize_folder_name(display_name))
                            if guessed:
                                install_location = guessed

                        if install_location and _is_protected_dir(install_location):
                            # Safety fuse: whatever the source (raw InstallLocation, a
                            # guess from UninstallString, the MSI cache, a guess from
                            # the folder name), if the "whole installation of the
                            # program" turns out to be e.g. C:\Windows\System32, it is
                            # certainly an installer mistake. Without this it gave
                            # absurd sizes (e.g. "8 GB") and allowed opening a system
                            # folder through "Open location".
                            install_location = ""

                        estimated_size_kb = _reg_get_int(k, "EstimatedSize")
                        if estimated_size_kb <= 0 and install_location and os.path.isdir(install_location):
                            estimated_size_kb = _dir_size_kb(install_location)

                        app = InstalledApp(
                            key_name=key_name,
                            hive=hive,
                            hive_name=_HIVE_NAMES.get(hive, str(hive)),
                            is_wow64=is_wow64,
                            display_name=display_name,
                            display_version=_reg_get_str(k, "DisplayVersion"),
                            publisher=_reg_get_str(k, "Publisher"),
                            install_date=install_date,
                            install_date_estimated=install_date_estimated,
                            estimated_size_kb=estimated_size_kb,
                            uninstall_string=uninstall_string,
                            quiet_uninstall_string=quiet_uninstall_string,
                            install_location=install_location,
                            display_icon=_reg_get_str(k, "DisplayIcon"),
                            system_component=system_component,
                            is_windows_installer=is_windows_installer,
                            help_link=_reg_get_str(k, "HelpLink"),
                            url_info_about=_reg_get_str(k, "URLInfoAbout"),
                            is_update=is_update,
                            has_parent=bool(parent_key),
                            no_remove=no_remove,
                            install_source=_reg_get_str(k, "InstallSource") if is_windows_installer else "",
                        )
                        if app.uid in seen_uids:
                            continue
                        seen_uids.add(app.uid)
                        results.append(app)
                except OSError:
                    continue

    if not include_system:
        results = [a for a in results if not a.is_component]

    results.sort(key=lambda a: (a.display_name or "").lower())
    return results


_MSI_INSTALL_SWITCH_RE = re.compile(r"(?i)(?:^|\s)/i\b")
_MSI_UNINSTALL_SWITCH_RE = re.compile(r"(?i)(?:^|\s)/x\b")


def _msi_uninstall_string_is_broken(cmd: str) -> bool:
    low = (cmd or "").lower()
    if "msiexec" not in low:
        return False
    return bool(_MSI_INSTALL_SWITCH_RE.search(cmd)) and not bool(_MSI_UNINSTALL_SWITCH_RE.search(cmd))


def _looks_like_product_code(key_name: str) -> bool:
    k = (key_name or "").strip()
    return k.startswith("{") and k.endswith("}")


def build_uninstall_command(app: InstalledApp, silent: bool = False) -> Optional[str]:
    if silent and app.quiet_uninstall_string:
        cmd = app.quiet_uninstall_string
    elif app.uninstall_string:
        cmd = app.uninstall_string
    elif app.quiet_uninstall_string:
        cmd = app.quiet_uninstall_string
    else:
        cmd = None

    if cmd and app.is_windows_installer and _looks_like_product_code(app.key_name) \
            and _msi_uninstall_string_is_broken(cmd) and app.is_part_of_larger_package:
        ui = "/qn" if silent else ""
        return f"msiexec.exe /X{app.key_name} {ui} /norestart".strip()

    if not cmd:
        return cmd
    return os.path.expandvars(cmd)


def run_uninstaller(app: InstalledApp, silent: bool = False) -> tuple[bool, str]:
    cmd = build_uninstall_command(app, silent=silent)
    if not cmd:
        return False, "no_uninstall_string"

    try:
        # SECURITY: cmd is a ready command line from the registry
        # (UninstallString/QuietUninstallString), also read from HKEY_CURRENT_USER,
        # which ANY process of the same user can write, even an unprivileged one. This
        # program runs elevated, so shell=True was a real privilege-escalation path:
        # cmd.exe interprets shell metacharacters (&, |, >, %VAR%), and a crafted HKCU
        # entry could run ANY command as administrator. shell=False with cmd as a plain
        # string (NOT a list!) bypasses cmd.exe entirely: Popen passes the string
        # straight to CreateProcess as lpCommandLine, which splits it into file +
        # arguments with the same quoting rules as list2cmdline, without any cmd.exe
        # side effects. Expansion of %ProgramFiles%-like tokens, formerly done by
        # cmd.exe, is done explicitly in build_uninstall_command.
        subprocess.Popen(
            cmd,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if silent else 0,
        )
        return True, ""
    except Exception as e:
        return False, str(e)


def run_uninstaller_process(app: InstalledApp, silent: bool = False) -> tuple[Optional[object], str]:
    cmd = build_uninstall_command(app, silent=silent)
    if not cmd:
        return None, "no_uninstall_string"

    try:
        if app.hive == winreg.HKEY_CURRENT_USER and ctypes.windll.shell32.IsUserAnAdmin():
            proc = launch_deelevated(cmd, hide=silent)
            if proc is not None:
                _log_uninstall(f"started pid {proc.pid} (not elevated): {cmd}")
                return proc, ""
            _log_uninstall("could not start without elevation, using elevated launch")
        proc = subprocess.Popen(
            cmd,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if silent else 0,
        )
        _log_uninstall(f"started pid {proc.pid}: {cmd}")
        return proc, ""
    except Exception as e:
        _log_uninstall(f"start failed: {e!r}")
        return None, str(e)


def _uninstall_key_exists(app: "InstalledApp") -> bool:
    subkey = _UNINSTALL_SUBKEY_WOW64 if app.is_wow64 else _UNINSTALL_SUBKEY
    try:
        access = winreg.KEY_READ | (winreg.KEY_WOW64_32KEY if app.is_wow64 else 0)
        with winreg.OpenKey(app.hive, f"{subkey}\\{app.key_name}", 0, access):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
    except Exception:
        return True


_PROC_K32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PROC_K32.OpenProcess.restype = wintypes.HANDLE
_PROC_K32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_PROC_K32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_PROC_K32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_PROC_K32.CloseHandle.restype = wintypes.BOOL
_PROC_K32.CloseHandle.argtypes = [wintypes.HANDLE]


def _process_image_path(pid: int) -> str:
    h = _PROC_K32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if _PROC_K32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        _PROC_K32.CloseHandle(h)
    return ""


_last_matched_process = ""


def _processes_running_in_dirs(dirs_low: list[str]) -> bool:
    global _last_matched_process
    if not dirs_low:
        return False
    try:
        from . import core_monitor as _cm
        pids = list(_cm._list_processes())
    except Exception:
        return False
    me = os.getpid()
    prefixes = [d.rstrip("\\") + "\\" for d in dirs_low]
    for pid in pids:
        if pid in (0, me):
            continue
        raw = _process_image_path(pid)
        if raw and os.path.normcase(os.path.normpath(raw)).startswith(tuple(prefixes)):
            _last_matched_process = raw
            return True
    return False


def _monitor_log_folders(app: "InstalledApp") -> list[str]:
    try:
        from . import core_monitor as _cm
        entry = _cm.find_monitor_log_for_app(app.display_name, app.key_name)
        if entry is None:
            return []
        out = []
        for it in _cm.load_monitor_log(entry.path):
            if it.kind == "folder" and it.path and not _is_protected_dir(it.path):
                out.append(os.path.normcase(os.path.normpath(it.path)))
        return out
    except Exception:
        return []


def _log_uninstall(text: str) -> None:
    try:
        print(f"[uninstall] {text}", file=sys.stderr)
    except Exception:
        pass


def _is_descendant(pid: int, root_pid: int, procs: dict) -> bool:
    seen = set()
    cur = pid
    for _ in range(16):
        entry = procs.get(cur)
        if entry is None:
            return False
        parent = entry[0]
        if parent == root_pid:
            return True
        if parent in seen or parent == cur:
            return False
        seen.add(cur)
        cur = parent
    return False


def _uninstaller_active(proc, basename: str) -> str:
    if proc.poll() is None:
        return f"launched process {proc.pid} is running"
    try:
        from . import core_monitor as _cm
        procs = _cm._list_processes()
        generic = _cm._GENERIC_HOST_EXE_BASENAMES
        for pid, (_ppid, name) in procs.items():
            if basename and name == basename:
                return f"{basename} (pid {pid}) is running"
            if name not in generic and _is_descendant(pid, proc.pid, procs):
                return f"{name} (pid {pid}) started by the uninstaller is running"
    except Exception:
        return "process list unavailable"
    return ""


def uninstaller_still_running(proc, app: "InstalledApp") -> bool:
    if proc is None:
        return False
    try:
        basename = os.path.basename(_extract_exe_path(build_uninstall_command(app) or "")).lower()
    except Exception:
        basename = ""
    return bool(_uninstaller_active(proc, basename))


def wait_for_uninstall_completion(
    proc,
    app: "InstalledApp",
    poll_interval: float = 1.0,
    settle_seconds: float = 2.5,
    process_wait_seconds: float = 90.0,
    max_seconds: float = 20 * 60,
    should_abort: Optional[Callable[[], bool]] = None,
    idle_exit_seconds: float = 30.0,
) -> None:
    try:
        dirs_low = [os.path.normcase(os.path.normpath(d)) for d in _install_dir_candidates(app)]
    except Exception:
        dirs_low = []
    install_dirs_n = len(dirs_low)
    dirs_low = list(dict.fromkeys(dirs_low + _monitor_log_folders(app)))
    try:
        print(f"[uninstall] watched folders: {install_dirs_n} from the program entry, "
              f"{len(dirs_low) - install_dirs_n} from the monitor log; {dirs_low[:5]}", file=sys.stderr)
    except Exception:
        pass
    try:
        from . import core_monitor as _cm
        exe_name = os.path.basename(_extract_exe_path(build_uninstall_command(app) or "")).lower()
        idle_check = bool(exe_name) and exe_name not in _cm._GENERIC_HOST_EXE_BASENAMES
    except Exception:
        exe_name, idle_check = "", False
    idle_since: Optional[float] = None
    last_reason = None
    logged_wait = False
    quiet_polls = 0
    start = time.monotonic()
    key_gone_since: Optional[float] = None
    logged_key_gone = False

    while True:
        if should_abort is not None and should_abort():
            return
        now = time.monotonic()
        if now - start > max_seconds:
            return

        if _uninstall_key_exists(app):
            key_gone_since = None
            if idle_check:
                reason = _uninstaller_active(proc, exe_name)
                if reason != last_reason:
                    last_reason = reason
                    if reason:
                        _log_uninstall(f"waiting: {reason}")
                if reason:
                    idle_since = None
                elif idle_since is None:
                    idle_since = now
                    _log_uninstall(f"uninstaller exited (code {proc.returncode}), entry still present")
                elif now - idle_since >= idle_exit_seconds:
                    _log_uninstall("nothing running and entry still present - ending wait")
                    return
        else:
            if key_gone_since is None:
                key_gone_since = now
            elif now - key_gone_since >= settle_seconds:
                if not logged_key_gone:
                    logged_key_gone = True
                    _log_uninstall("uninstall entry removed")
                if now - key_gone_since >= settle_seconds + process_wait_seconds:
                    return
                if _processes_running_in_dirs(dirs_low):
                    quiet_polls = 0
                    if not logged_wait:
                        logged_wait = True
                        try:
                            print(f"[uninstall] waiting for process: {_last_matched_process}", file=sys.stderr)
                        except Exception:
                            pass
                else:
                    quiet_polls += 1
                if quiet_polls >= 2:
                    if logged_wait:
                        try:
                            print(f"[uninstall] process finished after {now - key_gone_since:.0f}s", file=sys.stderr)
                        except Exception:
                            pass
                    return

        time.sleep(poll_interval)



def create_restore_point(description: str, timeout: int = 90) -> tuple[bool, str]:
    freq_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore"
    freq_name = "SystemRestorePointCreationFrequency"
    prev_value = None
    had_value = False
    key_opened = False

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, freq_path, 0, winreg.KEY_ALL_ACCESS) as key:
            key_opened = True
            try:
                prev_value, _ = winreg.QueryValueEx(key, freq_name)
                had_value = True
            except FileNotFoundError:
                had_value = False
            winreg.SetValueEx(key, freq_name, 0, winreg.REG_DWORD, 0)
    except Exception:
        pass

    try:
        safe_desc = (description or "HOTS Uninstaller").replace("'", "''")
        ps_cmd = (
            "$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            f"Checkpoint-Computer -Description '{safe_desc}' "
            f"-RestorePointType 'APPLICATION_UNINSTALL'"
        )
        result = subprocess.run(
            [system_exe("powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            return True, ""
        err = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
        return False, err or f"exit code {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)
    finally:
        if key_opened:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, freq_path, 0, winreg.KEY_ALL_ACCESS) as key:
                    if had_value:
                        winreg.SetValueEx(key, freq_name, 0, winreg.REG_DWORD, prev_value)
                    else:
                        try:
                            winreg.DeleteValue(key, freq_name)
                        except FileNotFoundError:
                            pass
            except Exception:
                pass


def force_remove_registry_entry(app: InstalledApp) -> tuple[bool, str, str]:
    subkey = _UNINSTALL_SUBKEY_WOW64 if app.is_wow64 else _UNINSTALL_SUBKEY
    full_subkey = f"{subkey}\\{app.key_name}"

    backup_path, _general_error, failed = _backup_registry_keys_combined(
        [(app.hive, full_subkey, None)], app.display_name or app.key_name,
    )
    if failed:
        # Without a saved backup the entry is not deleted: it could not be restored
        # afterwards.
        return False, T("leftover_err_backup_failed", path=full_subkey,
                        error=next(iter(failed.values()))), ""

    ok, error = _delete_uninstall_entry(app, subkey, full_subkey)
    return ok, error, backup_path


def _delete_uninstall_entry(app: InstalledApp, subkey: str, full_subkey: str) -> tuple[bool, str]:
    try:
        winreg.DeleteKeyEx(
            app.hive, full_subkey,
            access=winreg.KEY_WOW64_32KEY if app.is_wow64 else 0,
        )
        return True, ""
    except FileNotFoundError:
        return True, ""
    except Exception:
        try:
            with winreg.OpenKey(app.hive, subkey, 0, winreg.KEY_ALL_ACCESS) as parent:
                winreg.DeleteKey(parent, app.key_name)
            return True, ""
        except Exception as e2:
            return False, str(e2)


def remove_ghost_with_leftovers(app: InstalledApp, items: "list[LeftoverItem]") -> tuple[int, list[str], int, bool]:
    subkey = _UNINSTALL_SUBKEY_WOW64 if app.is_wow64 else _UNINSTALL_SUBKEY
    full_subkey = f"{subkey}\\{app.key_name}"
    entry_id = (app.hive, full_subkey.lower())
    name = app.display_name or app.key_name

    targets = [(app.hive, full_subkey, None)] + _collect_registry_targets(items)
    _path, general_error, failed = _backup_registry_keys_combined(targets, name)
    _apply_backup_result(items, failed)

    errors: list[str] = []
    if general_error:
        errors.append(T("err_registry_backup_failed_general", error=general_error))
    assoc_plan = plan_association_restores(items)
    results: list = []
    ok_count, delete_errors, pending_reboot, freed_kb = _delete_leftovers_tracked(
        items, name, results)
    errors.extend(delete_errors)
    errors.extend(apply_association_restores(assoc_plan))

    entry_removed = False
    if not delete_errors:
        if entry_id in failed:
            errors.append(T("leftover_err_backup_failed", path=full_subkey, error=failed[entry_id]))
        else:
            entry_removed, error = _delete_uninstall_entry(app, subkey, full_subkey)
            if entry_removed:
                results.append(("ok", "registry", f"{app.hive_name}\\{full_subkey}", ""))
            else:
                errors.append(f"{full_subkey}: {error}")
    _record_leftover_log(name, freed_kb, results)
    return ok_count, errors, pending_reboot, entry_removed


def open_registry_key(app: InstalledApp) -> bool:
    subkey = _UNINSTALL_SUBKEY_WOW64 if app.is_wow64 else _UNINSTALL_SUBKEY
    hive_name = _HIVE_FULL_NAMES.get(app.hive, "HKEY_LOCAL_MACHINE")
    # NOTE: no "Computer\" prefix. It is only shown in regedit's address bar and is NOT
    # the format expected by the LastKey value (that prefix was why navigation did not
    # work at all).
    target = f"{hive_name}\\{subkey}\\{app.key_name}"
    try:
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Applets\Regedit",
        ) as k:
            winreg.SetValueEx(k, "LastKey", 0, winreg.REG_SZ, target)
    except OSError:
        pass
    try:
        # "/m" (undocumented regedit switch) opens a NEW independent instance instead of
        # just raising an already open window; without it, if Registry Editor was
        # already open, LastKey would be written but ignored.
        popen_no_busy_cursor(["regedit.exe", "/m"])
        return True
    except Exception:
        try:
            popen_no_busy_cursor(["regedit.exe"])
            return True
        except Exception:
            return False


@dataclass
class LeftoverItem:
    kind: str
    path: str
    size_kb: int = 0
    hive: int = 0
    subkey: str = ""
    value_name: str = ""
    # Result of the backup step (set by backup_registry_keys_for_items): a registry
    # entry whose backup could not be saved is NOT deleted.
    backup_failed: bool = field(default=False, compare=False)
    backup_error: str = field(default="", compare=False)
    # A registry key at the "Software\<Name>" level whose name is a PUBLISHER, not a
    # product (e.g. Corel for WinZip). It may hold data of other programs, so it is
    # deleted only when empty. Keys named after the product (e.g. 7-Zip) are deleted
    # entirely.
    shared_root: bool = field(default=False, compare=False)
    preselected: bool = field(default=False, compare=False)
    # An item from the Installation Monitor log that has NOTHING linking it to this
    # program (not in its folder, name does not match, key does not point to its files)
    # may have landed in the log as background noise. It is shown but NEVER preselected
    # (see _monitor_item_corroborated).
    uncertain: bool = field(default=False, compare=False)
    # User data (browser profile: bookmarks, passwords); set together with `uncertain`
    # by mark_user_data(), so it is never preselected.
    user_data: bool = field(default=False, compare=False)


_GENERIC_TOKENS = {
    "the", "for", "and", "pro", "plus", "app", "apps", "inc", "ltd", "llc",
    "co", "corp", "corporation", "company", "software", "studio", "studios",
    "group", "systems", "technologies", "tech", "labs", "team", "of", "new",
    "microsoft", "windows",
}


def _normalize_name_tokens(name: str) -> list[str]:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", " ", name or "").strip().lower()
    tokens = [t for t in cleaned.split(" ") if len(t) >= 3]
    return tokens


_WEAK_TOKENS = {
    "media", "player", "editor", "viewer", "manager", "tool", "tools", "suite",
    "client", "desktop", "free", "lite", "center", "centre", "launcher",
    "updater", "update", "service", "services", "driver", "drivers", "runtime",
    "helper", "setup", "installer", "utility", "utilities", "version", "edition",
    "professional", "standard", "premium", "ultimate", "enterprise", "personal",
    "home", "business", "online", "cloud", "mobile", "security", "antivirus",
    "browser", "download", "downloader", "converter", "recorder", "reader",
    "writer", "maker", "creator", "designer", "toolkit", "framework", "library",
    "package", "plugin", "extension", "component", "components", "database",
    "server", "system", "control", "panel", "assistant", "wizard", "game",
    "games", "video", "audio", "music", "photo", "image", "document", "office",
    "data", "file", "files", "disk", "network", "internet", "portable",
    "redistributable", "language", "pack", "hotfix", "preview", "beta", "alpha",
    "release", "x64", "x86", "amd64", "win64", "win32", "64bit", "32bit",
    "user", "users", "machine", "admin",
}

_SHORT_TOKEN_BLOCKLIST = {
    "pdf", "usb", "net", "web", "vpn", "sdk", "api", "gui", "cpu", "gpu", "ssd",
    "dll", "exe", "sql", "iso", "dvd", "cad", "com", "all", "one", "any", "key",
    "box", "hub", "kit", "lab", "max", "mix", "run", "tab", "top", "win", "www",
    "xml",
}


def _strong_name_candidates(tokens: list[str]) -> list[str]:
    return [t for t in tokens
            if len(t) >= 4 and not t.isdigit()
            and t not in _GENERIC_TOKENS and t not in _WEAK_TOKENS]


def _primary_match_token(tokens: list[str]) -> str:
    candidates = _strong_name_candidates(tokens)
    return candidates[0] if candidates else ""


def _product_name_tokens(tokens: list[str], pub_tokens: list[str]) -> list[str]:
    pub = set(pub_tokens)
    product = [t for t in tokens if t not in pub]
    if any(len(t) >= 4 for t in product):
        return product
    return tokens


def _primary_name_token(tokens: list[str], other_tokens: set, other_split_tokens: set) -> str:
    strong = _strong_name_candidates(tokens)
    for t in strong:
        if t not in other_tokens:
            return t
    for t in tokens:
        if (len(t) == 3 and t.isalpha() and t not in _GENERIC_TOKENS
                and t not in _SHORT_TOKEN_BLOCKLIST
                and t not in other_tokens and t not in other_split_tokens):
            return t
    return strong[0] if strong else ""


def _split_camel_case(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return s


def _entry_match_tokens(name: str) -> set[str]:
    return set(_normalize_name_tokens(_split_camel_case(name))) | set(_normalize_name_tokens(name))


_VARIANT_MARKER_TOKENS = {
    "lite", "pro", "plus", "beta", "alpha", "preview", "trial", "demo",
    "free", "premium", "uninstaller", "installer", "setup", "updater",
    "helper", "service", "server", "client", "tools", "suite", "edition",
    "mini", "full", "pack", "portable",
}


_INSTALL_ARTIFACT_TOKENS = {
    "x64", "x86", "win32", "win64", "setup", "msi", "app", "data", "cache",
    "config", "settings", "profile", "temp", "backup", "backups", "old",
}


def _looks_like_different_variant(entry_tokens: set, app_tokens: set) -> bool:
    if (entry_tokens & _VARIANT_MARKER_TOKENS) - app_tokens:
        return True
    extra = entry_tokens - app_tokens - _INSTALL_ARTIFACT_TOKENS
    extra = {t for t in extra if not t.isdigit()}
    return bool(extra)


_DIR_SIZE_FILE_LIMIT = 200000


def _scan_dir_bytes(path: str, limit_files: int) -> tuple[int, bool, bool]:
    total = 0
    count = 0
    errors = False
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        count += 1
                        if count > limit_files:
                            return total, errors, True
                        total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        errors = True
        except OSError:
            errors = True
    return total, errors, False


def _dir_size_kb(path: str, limit_files: int = _DIR_SIZE_FILE_LIMIT) -> int:
    try:
        total, _errors, truncated = _scan_dir_bytes(path, limit_files)
    except Exception:
        return 0
    if truncated:
        return 0
    return total // 1024


def _is_protected_dir(path: str) -> bool:
    norm = os.path.normcase(os.path.normpath(path))
    return (_is_system_container(norm) or _is_under_windows_dir(norm)
            or norm in _protected_container_set() or _is_os_state_path(path))


def _candidate_folder_roots() -> list[str]:
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA", "APPDATA"):
        p = os.environ.get(env)
        if p:
            roots.append(p)
    return roots


def _tokens_match(entry_tokens: set[str], wanted_tokens) -> bool:
    if isinstance(wanted_tokens, str):
        wanted_tokens = {wanted_tokens}
    wanted_tokens = {w for w in wanted_tokens if w}
    if not wanted_tokens:
        return False
    for t in entry_tokens:
        for w in wanted_tokens:
            if t == w or (t.startswith(w) and t[len(w):].isdigit()):
                return True
    return False


def _prefetch_dir() -> str:
    return os.path.join(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows", "Prefetch")


def _is_windows_own_exe(exe_name: str) -> bool:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    name = exe_name.strip()
    if not name:
        return False
    for folder in (root, os.path.join(root, "System32"), os.path.join(root, "SysWOW64")):
        try:
            if os.path.isfile(os.path.join(folder, name)):
                return True
        except OSError:
            continue
    return False


def _scan_prefetch_leftovers(token: str) -> list["LeftoverItem"]:
    if not token:
        return []
    prefetch_dir = _prefetch_dir()
    try:
        entries = os.listdir(prefetch_dir)
    except Exception:
        return []

    items: list[LeftoverItem] = []
    for entry in entries:
        low = entry.lower()
        if not low.endswith(".pf"):
            continue
        if not _tokens_match(_entry_match_tokens(entry), token):
            continue
        if _is_windows_own_exe(entry.rsplit("-", 1)[0]):
            continue
        full = os.path.join(prefetch_dir, entry)
        try:
            size_kb = max(1, os.path.getsize(full) // 1024)
        except Exception:
            size_kb = 0
        items.append(LeftoverItem(kind="file", path=full, size_kb=size_kb))
    return items


def _shortcut_roots() -> list[str]:
    roots = []
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        roots.append(os.path.join(userprofile, "Desktop"))
    public = os.environ.get("PUBLIC")
    if public:
        roots.append(os.path.join(public, "Desktop"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs"))
    program_data = os.environ.get("ProgramData")
    if program_data:
        roots.append(os.path.join(program_data, "Microsoft", "Windows", "Start Menu", "Programs"))
    return roots


_WINDOWS_START_FOLDERS = frozenset({
    "accessories", "system tools", "administrative tools", "windows accessories",
    "windows system", "windows administrative tools", "windows powershell",
    "windows ease of access", "accessibility", "startup",
})


def _shortcut_belongs_elsewhere(full: str) -> bool:
    if os.path.basename(os.path.dirname(full)).lower() in _WINDOWS_START_FOLDERS:
        return True
    if not full.lower().endswith(".lnk"):
        return False
    try:
        from .core_portable import read_lnk_target
        target = read_lnk_target(full)
    except Exception:
        return False
    return bool(target) and os.path.exists(target)


def _match_shortcut_file(full: str, entry: str, primary: str, app_tokens: set) -> Optional["LeftoverItem"]:
    low = entry.lower()
    if not (low.endswith(".lnk") or low.endswith(".url")):
        return None
    stem = os.path.splitext(entry)[0]
    entry_tokens = _entry_match_tokens(stem)
    if not _tokens_match(entry_tokens, primary):
        return None
    if _looks_like_different_variant(entry_tokens, app_tokens):
        return None
    if _shortcut_belongs_elsewhere(full):
        return None
    try:
        size_kb = max(1, os.path.getsize(full) // 1024)
    except Exception:
        size_kb = 0
    return LeftoverItem(kind="file", path=full, size_kb=size_kb)


def _shortcut_dir_fully_matches(dir_path: str, primary: str, app_tokens: set) -> bool:
    try:
        entries = os.listdir(dir_path)
    except Exception:
        return False
    if not entries:
        return False
    for entry in entries:
        full = os.path.join(dir_path, entry)
        if os.path.isdir(full):
            return False
        if _match_shortcut_file(full, entry, primary, app_tokens) is None:
            return False
    return True


def _scan_shortcut_dir_or_collapse(dir_path: str, primary: str, app_tokens: set,
                                    seen_paths: set) -> list["LeftoverItem"]:
    if _shortcut_dir_fully_matches(dir_path, primary, app_tokens):
        norm = os.path.normcase(os.path.normpath(dir_path))
        if norm in seen_paths:
            return []
        seen_paths.add(norm)
        return [LeftoverItem(kind="folder", path=dir_path, size_kb=_dir_size_kb(dir_path))]

    found: list[LeftoverItem] = []
    try:
        entries = os.listdir(dir_path)
    except Exception:
        return found
    for entry in entries:
        full = os.path.join(dir_path, entry)
        if os.path.isdir(full):
            continue
        item = _match_shortcut_file(full, entry, primary, app_tokens)
        if item is None:
            continue
        norm = os.path.normcase(os.path.normpath(full))
        if norm in seen_paths:
            continue
        seen_paths.add(norm)
        found.append(item)
    return found


def _scan_shortcut_leftovers(primary: str, app_tokens: set) -> list["LeftoverItem"]:
    if not primary:
        return []
    seen_paths: set = set()
    items: list[LeftoverItem] = []
    for root in _shortcut_roots():
        try:
            entries = os.listdir(root)
        except Exception:
            continue
        for entry in entries:
            full = os.path.join(root, entry)
            if os.path.isdir(full):
                if _is_protected_dir(full):
                    continue
                items.extend(_scan_shortcut_dir_or_collapse(full, primary, app_tokens, seen_paths))
                continue
            item = _match_shortcut_file(full, entry, primary, app_tokens)
            if item is None:
                continue
            norm = os.path.normcase(os.path.normpath(full))
            if norm in seen_paths:
                continue
            seen_paths.add(norm)
            items.append(item)
    return items


def _other_apps_tokens(exclude_uid: str) -> set[str]:
    return _other_apps_token_sets(exclude_uid)[0]


def _other_apps_token_sets(exclude_uid: str) -> tuple[set[str], set[str]]:
    tokens: set[str] = set()
    split_tokens: set[str] = set()
    try:
        for other in list_installed_apps(include_system=True):
            if other.uid == exclude_uid:
                continue
            tokens.update(_normalize_name_tokens(other.display_name))
            tokens.update(_normalize_name_tokens(other.publisher))
            split_tokens |= _entry_match_tokens(other.display_name)
            split_tokens |= _entry_match_tokens(other.publisher)
    except Exception:
        pass
    return tokens, split_tokens


def _scan_microsoft_children(hive, base: str, match_tokens: set[str],
                             app_and_pub_tokens: set[str]) -> list["LeftoverItem"]:
    found: list[LeftoverItem] = []
    try:
        root = winreg.OpenKey(hive, base, 0, winreg.KEY_READ)
    except OSError:
        return found
    with root:
        i = 0
        while True:
            try:
                sub_name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            sub_tokens = _entry_match_tokens(sub_name)
            if sub_tokens and sub_tokens <= app_and_pub_tokens \
                    and _tokens_match(sub_tokens, match_tokens):
                full_path = f"{base}\\{sub_name}"
                found.append(LeftoverItem(
                    kind="registry",
                    path=f"{_HIVE_NAMES.get(hive, hive)}\\{full_path}",
                    hive=hive, subkey=full_path,
                ))
    return found


def _scan_registry_subtree(hive, base: str, match_tokens: set[str],
                            app_and_pub_tokens: set[str], depth: int,
                            max_depth: int,
                            product_tokens: Optional[set] = None) -> list["LeftoverItem"]:
    found: list[LeftoverItem] = []
    try:
        root = winreg.OpenKey(hive, base, 0, winreg.KEY_READ)
    except OSError:
        return found
    with root:
        i = 0
        while True:
            try:
                sub_name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            low = sub_name.lower()
            if low in ("classes", "wow6432node", "policies"):
                continue
            if sub_name.startswith(".") and base.lower().endswith("classes"):
                continue
            sub_tokens = _entry_match_tokens(sub_name)
            full_path = f"{base}\\{sub_name}"
            if low == "microsoft":
                if depth == 1 and "microsoft" in app_and_pub_tokens:
                    found.extend(_scan_microsoft_children(hive, full_path, match_tokens, app_and_pub_tokens))
                continue
            if _tokens_match(sub_tokens, match_tokens):
                if _looks_like_different_variant(sub_tokens, app_and_pub_tokens):
                    continue
                found.append(LeftoverItem(
                    kind="registry",
                    path=f"{_HIVE_NAMES.get(hive, hive)}\\{full_path}",
                    hive=hive, subkey=full_path,
                ))
                if product_tokens and depth < max_depth and not _tokens_match(sub_tokens, product_tokens):
                    found.extend(_scan_registry_subtree(
                        hive, full_path, product_tokens, app_and_pub_tokens,
                        depth + 1, max_depth))
            elif depth < max_depth:
                found.extend(_scan_registry_subtree(
                    hive, full_path, match_tokens, app_and_pub_tokens,
                    depth + 1, max_depth, product_tokens))
    return found


_UFH_SHC_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\UFH\SHC"

_APPCOMPAT_STORE_SUBKEY = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Compatibility Assistant\Store"
_MUICACHE_SUBKEYS = [
    r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache",
    r"Software\Microsoft\Windows\Shell\MuiCache",
]
_FIREWALL_RULES_SUBKEY = r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules"
_FIREWALL_APP_RE = re.compile(r"App=([^|]+)", re.IGNORECASE)


_CLASSES_SKIP = {"clsid", "interface", "typelib", "appid", "installer", "local settings",
                 "wow6432node", "mime", "hardware", "licenses", "recordedtv"}
_SYSTEM_CLASS_NAMES = frozenset({
    "*", "http", "https", "ftp", "mailto", "file", "telnet", "news", "nntp", "ldap", "webcal",
    "tel", "sms", "callto", "search-ms", "shell", "folder", "directory", "drive", "unknown",
    "allfilesystemobjects", "applications", "systemfileassociations", "desktop.ini",
    "txtfile", "htmlfile", "inffile", "batfile", "cmdfile", "exefile", "lnkfile", "regfile",
    "vbsfile", "jsfile", "chm.file", "cplfile", "dllfile", "inifile", "urlfile", "internetshortcut",
    "mscfile", "msifile", "msufile", "scrfile", "sysfile", "themefile", "ttffile", "zipfile",
    "cabfile", "wsffile", "vbefile", "jsefile", "wshfile", "pngfile", "jpegfile",
    "giffile", "bmpfile", "icofile", "avifile", "mp3file", "wavfile", "mpegfile",
})
_CLASSES_COMMAND_SUBKEYS = (r"shell\open\command", "DefaultIcon")


def _classes_key_default(hive, subkey: str) -> str:
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as k:
            data, _t = winreg.QueryValueEx(k, "")
    except OSError:
        return ""
    return os.path.expandvars(data).lower() if isinstance(data, str) else ""


def _scan_classes_by_location(install_dirs_low: list[str]) -> list["LeftoverItem"]:
    found: list[LeftoverItem] = []
    if not install_dirs_low:
        return found
    base = r"Software\Classes"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            root = winreg.OpenKey(hive, base, 0, winreg.KEY_READ)
        except OSError:
            continue
        with root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if name.startswith(".") or name.lower() in _CLASSES_SKIP \
                        or name.lower() in _SYSTEM_CLASS_NAMES:
                    continue
                full = f"{base}\\{name}"
                for sub in _CLASSES_COMMAND_SUBKEYS:
                    command = _classes_key_default(hive, f"{full}\\{sub}")
                    if command and any(_command_refers_to(command, d) for d in install_dirs_low):
                        found.append(LeftoverItem(
                            kind="registry",
                            path=f"{_HIVE_NAMES.get(hive, hive)}\\{full}",
                            hive=hive, subkey=full,
                        ))
                        break
    return found


def _scan_path_named_value_leftovers(hive, subkey: str, install_dirs_low: list[str]) -> list["LeftoverItem"]:
    if not install_dirs_low:
        return []
    found: list[LeftoverItem] = []
    try:
        key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)
    except OSError:
        return found
    with key:
        i = 0
        while True:
            try:
                value_name, _data, _t = winreg.EnumValue(key, i)
            except OSError:
                break
            i += 1
            low = (value_name or "").lower()
            if not any(_path_prefix_match(low, d) for d in install_dirs_low):
                continue
            found.append(LeftoverItem(
                kind="registry_value",
                path=f"{_HIVE_NAMES.get(hive, hive)}\\{subkey} \u2192 {value_name}",
                hive=hive, subkey=subkey, value_name=value_name,
            ))
    return found


def _scan_firewall_rule_leftovers(install_dirs_low: list[str]) -> list["LeftoverItem"]:
    if not install_dirs_low:
        return []
    found: list[LeftoverItem] = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _FIREWALL_RULES_SUBKEY, 0, winreg.KEY_READ)
    except OSError:
        return found
    with key:
        i = 0
        while True:
            try:
                value_name, value_data, _t = winreg.EnumValue(key, i)
            except OSError:
                break
            i += 1
            m = _FIREWALL_APP_RE.search(str(value_data or ""))
            if not m:
                continue
            app_path_low = m.group(1).strip().lower()
            if not any(_path_prefix_match(app_path_low, d) for d in install_dirs_low):
                continue
            found.append(LeftoverItem(
                kind="registry_value",
                path=f"HKLM\\{_FIREWALL_RULES_SUBKEY} \u2192 {value_name}",
                hive=winreg.HKEY_LOCAL_MACHINE,
                subkey=_FIREWALL_RULES_SUBKEY,
                value_name=value_name,
            ))
    return found


_RUN_SUBKEYS = [
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
]
_APP_PATHS_SUBKEYS = [
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
]


def _command_refers_to(command_low: str, needle_low: str) -> bool:
    if not needle_low:
        return False
    if _path_prefix_match(command_low, needle_low):
        return True
    if not needle_low.endswith(".exe"):
        return False
    start = 0
    while True:
        idx = command_low.find(needle_low, start)
        if idx == -1:
            return False
        end = idx + len(needle_low)
        before_ok = idx == 0 or command_low[idx - 1] in ' "\''
        after_ok = end >= len(command_low) or command_low[end] in ' "\''
        if before_ok and after_ok:
            return True
        start = idx + 1


def _scan_run_leftovers(needles_low: list[str]) -> list["LeftoverItem"]:
    if not needles_low:
        return []
    found: list[LeftoverItem] = []
    for hive, subkey in _RUN_SUBKEYS:
        try:
            key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    value_name, value_data, _t = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not isinstance(value_data, str):
                    continue
                command = os.path.expandvars(value_data).lower()
                if not any(_command_refers_to(command, n) for n in needles_low):
                    continue
                found.append(LeftoverItem(
                    kind="registry_value",
                    path=f"{_HIVE_NAMES.get(hive, hive)}\\{subkey} \u2192 {value_name}",
                    hive=hive, subkey=subkey, value_name=value_name,
                ))
    return found


def _scan_app_paths_leftovers(needles_low: list[str]) -> list["LeftoverItem"]:
    if not needles_low:
        return []
    found: list[LeftoverItem] = []
    for hive, base in _APP_PATHS_SUBKEYS:
        try:
            root = winreg.OpenKey(hive, base, 0, winreg.KEY_READ)
        except OSError:
            continue
        with root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                full = f"{base}\\{name}"
                try:
                    with winreg.OpenKey(hive, full, 0, winreg.KEY_READ) as sub:
                        try:
                            default = winreg.QueryValueEx(sub, "")[0]
                        except OSError:
                            default = ""
                except OSError:
                    continue
                if not isinstance(default, str) or not default:
                    continue
                target = os.path.expandvars(default).strip().strip('"').lower()
                if any(_path_prefix_match(target, n) for n in needles_low):
                    found.append(LeftoverItem(
                        kind="registry",
                        path=f"{_HIVE_NAMES.get(hive, hive)}\\{full}",
                        hive=hive, subkey=full,
                    ))
    return found


def _registry_key_matches(hive: int, subkey: str, matcher, max_values: int = 300,
                          max_depth: int = 2, match_names: bool = False) -> bool:
    budget = [max_values]

    def walk(sub: str, depth: int) -> bool:
        try:
            key = winreg.OpenKey(hive, sub, 0, winreg.KEY_READ)
        except OSError:
            return False
        with key:
            i = 0
            while budget[0] > 0:
                try:
                    _name, data, _t = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                budget[0] -= 1
                if isinstance(data, str) and matcher(os.path.expandvars(data).lower()):
                    return True
            if depth < max_depth:
                j = 0
                while budget[0] > 0:
                    try:
                        child = winreg.EnumKey(key, j)
                    except OSError:
                        break
                    j += 1
                    if match_names and matcher(child.lower()):
                        return True
                    if walk(f"{sub}\\{child}", depth + 1):
                        return True
        return False

    return walk(subkey, 0)


def _registry_key_refers_to_dirs(hive: int, subkey: str, dirs_low: list[str],
                                 max_values: int = 300, max_depth: int = 2) -> bool:
    return _registry_key_matches(
        hive, subkey, lambda command: any(_command_refers_to(command, d) for d in dirs_low),
        max_values, max_depth)


def leftover_match_context(app: "InstalledApp") -> tuple[list[str], set]:
    tokens = _normalize_name_tokens(app.display_name)
    pub_tokens = _normalize_name_tokens(app.publisher)
    other_tokens, other_split_tokens = _other_apps_token_sets(app.uid)
    primary = _primary_name_token(_product_name_tokens(tokens, pub_tokens), other_tokens, other_split_tokens) if tokens else None
    pub_primary = _primary_match_token(pub_tokens)
    match_tokens = {t for t in (primary, pub_primary) if t and t not in other_tokens}
    install_dirs_low = [d.lower() for d in _install_dir_candidates(app)]
    return install_dirs_low, match_tokens


def _monitor_item_corroborated(app: "InstalledApp", item: "LeftoverItem",
                               install_dirs_low: list[str], match_tokens: set) -> bool:
    path_low = item.path.lower()
    if item.kind in ("file", "folder"):
        if any(_path_prefix_match(path_low, d) for d in install_dirs_low):
            return True
        if item.kind == "folder" and any(_path_prefix_match(d, path_low) for d in install_dirs_low):
            return True
        components = [c for c in re.split(r"[\\/]+", item.path) if c]
        return bool(match_tokens) and any(_entry_match_tokens(c) & match_tokens for c in components)

    if app.is_windows_installer and app.key_name and app.key_name.lower() in path_low:
        return True
    subkey = item.subkey or ""
    if match_tokens and any(_entry_match_tokens(c) & match_tokens for c in subkey.split("\\") if c):
        return True
    if item.kind == "registry_value":
        if match_tokens and item.value_name and _entry_match_tokens(item.value_name) & match_tokens:
            return True
        try:
            with winreg.OpenKey(item.hive, subkey, 0, winreg.KEY_READ) as key:
                data, _t = winreg.QueryValueEx(key, item.value_name)
        except OSError:
            return False
        if isinstance(data, str) and install_dirs_low:
            command = os.path.expandvars(data).lower()
            return any(_command_refers_to(command, d) for d in install_dirs_low)
        return False
    if install_dirs_low and item.kind == "registry" and subkey:
        return _registry_key_refers_to_dirs(item.hive, subkey, install_dirs_low)
    return False


_GUID_RE = re.compile(r"\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}")
_COM_SEED_RE = re.compile(r"^software\\(?:wow6432node\\)?classes\\(?:clsid|typelib|appid)\\(\{[0-9a-f-]{36}\})(?:\\|$)")
_GUID_ANY_RE = re.compile(r"\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}")
_ID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{16,}(?![0-9a-f])")
_CLOSURE_PASSES = 3
_CLOSURE_MAX_VALUES = 100


def _registry_collect_guids(hive: int, subkey: str, max_values: int = 60, max_depth: int = 2) -> set:
    found: set = set()
    budget = [max_values]

    def walk(sub: str, depth: int) -> None:
        try:
            key = winreg.OpenKey(hive, sub, 0, winreg.KEY_READ)
        except OSError:
            return
        with key:
            i = 0
            while budget[0] > 0:
                try:
                    _name, data, _t = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                budget[0] -= 1
                if isinstance(data, str):
                    found.update(_GUID_ANY_RE.findall(data.lower()))
            if depth < max_depth:
                j = 0
                while budget[0] > 0:
                    try:
                        child = winreg.EnumKey(key, j)
                    except OSError:
                        break
                    j += 1
                    walk(f"{sub}\\{child}", depth + 1)

    walk(subkey, 0)
    return found


def _top_level_exe_names(dirs_low: list[str]) -> set:
    names: set = set()
    for d in dirs_low:
        try:
            with os.scandir(d) as it:
                names.update(e.name.lower() for e in it if e.name.lower().endswith(".exe"))
        except OSError:
            continue
    return names


def expand_corroboration(items: list["LeftoverItem"], install_dirs_low: list[str]) -> None:
    exe_names = _top_level_exe_names(install_dirs_low)
    guid_cache: dict = {}
    for _ in range(_CLOSURE_PASSES):
        seeds = [it for it in items if not it.uncertain and not it.user_data]
        fs_seeds: list[str] = []
        reg_seeds: set = set()
        guids: set = set()
        ids: set = set()
        for it in seeds:
            ids.update(_ID_RE.findall(it.path.lower()))
            if it.kind in ("file", "folder"):
                norm = os.path.normcase(os.path.normpath(it.path))
                if len(norm) >= 8 and not _is_protected_dir(it.path):
                    fs_seeds.append(norm)
            elif it.kind == "registry" and not _is_protected_registry_key(it.hive, it.subkey):
                low = (it.subkey or "").lower()
                reg_seeds.add((it.hive, low))
                m = _COM_SEED_RE.match(low)
                if m:
                    guids.add(m.group(1))
                cache_key = (it.hive, low)
                if cache_key not in guid_cache:
                    guid_cache[cache_key] = _registry_collect_guids(it.hive, it.subkey)
                guids |= guid_cache[cache_key]
        folder_seeds = [os.path.normcase(os.path.normpath(it.path)) + os.sep
                        for it in seeds if it.kind == "folder"
                        and os.path.normcase(os.path.normpath(it.path)) in fs_seeds]
        dirs = list(install_dirs_low) + fs_seeds
        use_refs = bool(fs_seeds or guids or ids)
        changed = False
        for it in items:
            if not it.uncertain or it.user_data:
                continue
            try:
                promote = False
                path_low = it.path.lower()
                if ids and any(i in path_low for i in ids):
                    promote = True
                elif it.kind in ("file", "folder"):
                    norm = os.path.normcase(os.path.normpath(it.path))
                    promote = any(norm.startswith(f) for f in folder_seeds)
                else:
                    low = (it.subkey or "").lower()
                    promote = it.kind == "registry" and any(
                        hive == it.hive and low.startswith(sub + "\\") for hive, sub in reg_seeds)
                    if not promote and it.kind == "registry" and exe_names:
                        promote = low.rsplit("\\", 1)[-1] in exe_names
                    if not promote and guids:
                        promote = any(g in low or g in (it.value_name or "").lower() for g in guids)
                    if not promote and use_refs:
                        def matcher(text, _dirs=dirs, _guids=guids | ids):
                            return (any(_command_refers_to(text, d) for d in _dirs)
                                    or any(g in text for g in _guids))
                        if it.kind == "registry_value":
                            data = _read_string_value(it.hive, it.subkey, it.value_name or "")
                            promote = bool(data) and matcher(os.path.expandvars(data).lower())
                        elif it.kind == "registry":
                            promote = _registry_key_matches(it.hive, it.subkey, matcher,
                                                            _CLOSURE_MAX_VALUES, match_names=True)
                if promote:
                    it.uncertain = False
                    changed = True
            except Exception:
                continue
        if not changed:
            break


_USER_DATA_FILES = frozenset({"places.sqlite", "logins.json", "key4.db", "login data"})
_PROFILE_META_FILES = frozenset({"profiles.ini", "installs.ini"})
_USER_DATA_EXTENSIONS = frozenset({".kdbx", ".kdb", ".pst", ".ost", ".ppk", ".pfx", ".p12", ".wallet"})
_USER_DATA_WALK_DEPTH = 4
_USER_DATA_WALK_LIMIT = 3000
_VAULT_WALK_DEPTH = 6
_VAULT_WALK_LIMIT = 50000


def _is_vault_file_name(name: str) -> bool:
    return os.path.splitext(name.lower())[1] in _USER_DATA_EXTENSIONS


def _folder_has_vault_file(path: str) -> bool:
    queue = [(path, 0)]
    seen = 0
    while queue:
        current, depth = queue.pop(0)
        try:
            with os.scandir(current) as it:
                for entry in it:
                    seen += 1
                    if seen > _VAULT_WALK_LIMIT:
                        return False
                    if entry.is_dir(follow_symlinks=False):
                        if depth < _VAULT_WALK_DEPTH and not _is_reparse_folder(entry.path):
                            queue.append((entry.path, depth + 1))
                    elif _is_vault_file_name(entry.name):
                        return True
        except OSError:
            continue
    return False


def _folder_has_user_data(path: str) -> bool:
    queue = [(path, 0)]
    seen = 0
    while queue:
        current, depth = queue.pop(0)
        try:
            with os.scandir(current) as it:
                for entry in it:
                    seen += 1
                    if seen > _USER_DATA_WALK_LIMIT:
                        return False
                    if entry.is_dir(follow_symlinks=False):
                        if depth < _USER_DATA_WALK_DEPTH and not _is_reparse_folder(entry.path):
                            queue.append((entry.path, depth + 1))
                    elif entry.name.lower() in _USER_DATA_FILES:
                        return True
        except OSError:
            continue
    return False


def _is_user_data_item(item: "LeftoverItem", roots: list[str], install_dirs_low: list[str]) -> bool:
    if item.kind not in ("file", "folder"):
        return False
    if item.kind == "file" and _is_vault_file_name(item.path):
        return True
    if item.kind == "folder" and _folder_has_vault_file(item.path):
        return True
    norm = os.path.normcase(os.path.normpath(item.path))
    if not any(norm == r or norm.startswith(r + os.sep) for r in roots):
        return False
    if any(_path_prefix_match(norm, d) for d in install_dirs_low):
        return False
    parts = norm.split(os.sep)
    if "profile groups" in parts:
        return True
    if item.kind == "file":
        return parts[-1] in _PROFILE_META_FILES or parts[-1] in _USER_DATA_FILES
    return _folder_has_user_data(item.path)


def mark_user_data(items: list["LeftoverItem"], install_dirs_low: Optional[list[str]] = None) -> None:
    roots = [os.path.normcase(os.path.normpath(p))
             for p in (os.environ.get("USERPROFILE"), os.environ.get("ProgramData")) if p]
    for it in items:
        try:
            if _is_user_data_item(it, roots, install_dirs_low or []):
                it.user_data = True
                it.uncertain = True
        except Exception:
            continue


def _scan_microsoft_folder_children(container: str, primary: str, app_tokens: set,
                                    seen_paths: set) -> list["LeftoverItem"]:
    found: list[LeftoverItem] = []
    try:
        entries = os.listdir(container)
    except Exception:
        return found
    for entry in entries:
        full = os.path.join(container, entry)
        if not os.path.isdir(full) or _is_protected_dir(full):
            continue
        entry_tokens = _entry_match_tokens(entry)
        if not entry_tokens or not entry_tokens <= app_tokens or not _tokens_match(entry_tokens, primary):
            continue
        norm = os.path.normcase(os.path.normpath(full))
        if norm not in seen_paths:
            seen_paths.add(norm)
            found.append(LeftoverItem(kind="folder", path=full, size_kb=_dir_size_kb(full)))
    return found


def _trusted_parent_tokens(app: "InstalledApp", app_tokens: set) -> set:
    return (app_tokens | _entry_match_tokens(app.publisher)) - (_WEAK_TOKENS | _GENERIC_TOKENS)


def _scan_folder_subtree(root: str, primary: str, app_tokens: set, depth: int,
                          max_depth: int, seen_paths: set,
                          trusted_tokens: Optional[set] = None,
                          parent_trusted: bool = True,
                          allow_microsoft: bool = True) -> list["LeftoverItem"]:
    found: list[LeftoverItem] = []
    try:
        entries = os.listdir(root)
    except Exception:
        return found
    for entry in entries:
        full = os.path.join(root, entry)
        if not os.path.isdir(full):
            continue
        if _is_protected_dir(full):
            if entry.lower() == "microsoft" and allow_microsoft and depth < max_depth \
                    and os.path.normcase(os.path.normpath(full)) in _protected_container_set():
                found.extend(_scan_microsoft_folder_children(full, primary, app_tokens, seen_paths))
            continue
        entry_tokens = _entry_match_tokens(entry)
        if _tokens_match(entry_tokens, primary):
            if _looks_like_different_variant(entry_tokens, app_tokens):
                continue
            norm = os.path.normcase(os.path.normpath(full))
            if norm not in seen_paths:
                seen_paths.add(norm)
                item = LeftoverItem(kind="folder", path=full, size_kb=_dir_size_kb(full))
                if trusted_tokens is not None and not parent_trusted:
                    item.uncertain = True
                found.append(item)
        elif depth < max_depth:
            found.extend(_scan_folder_subtree(
                full, primary, app_tokens, depth + 1, max_depth, seen_paths,
                trusted_tokens,
                trusted_tokens is None or bool(entry_tokens & trusted_tokens),
                allow_microsoft))
    return found


def _path_prefix_match(haystack_low: str, needle_low: str) -> bool:
    """Substring check for paths with a boundary at the end of the match: needle must
    end at a path segment boundary (backslash) or at the end of the string, NOT in
    the middle of a longer folder name. A space is NOT a boundary, because real
    paths contain it (e.g. "Program Files"). Without this, "...\\HOTS" would also
    falsely match inside "...\\HOTS Hosts\\...".
    """
    if not needle_low:
        return False
    start = 0
    while True:
        idx = haystack_low.find(needle_low, start)
        if idx == -1:
            return False
        end = idx + len(needle_low)
        if end >= len(haystack_low) or haystack_low[end] == "\\":
            return True
        start = idx + 1


def _install_dir_candidates(app: "InstalledApp") -> list[str]:
    candidates = []
    loc = (app.install_location or "").strip().strip('"').rstrip("\\/")
    if loc:
        candidates.append(loc)

    icon = (app.display_icon or "").strip().strip('"')
    if icon:
        icon = icon.split(",")[0]
        d = os.path.dirname(icon).rstrip("\\/")
        if d:
            candidates.append(d)

    for cmd in (app.uninstall_string, app.quiet_uninstall_string):
        exe = _extract_exe_path(cmd or "")
        if exe:
            d = os.path.dirname(exe).rstrip("\\/")
            if d:
                candidates.append(d)

    seen = set()
    result = []
    for c in candidates:
        norm = c.lower()
        if norm in seen:
            continue
        seen.add(norm)
        # A system folder (System32, Program Files, a drive root...) must NEVER be the
        # root of path matching: otherwise a program with an icon from shell32.dll, or
        # an uninstaller given as the full path to msiexec.exe, would match the
        # MuiCache/Firewall/AppCompat entries of all system programs.
        if _is_protected_dir(c) or re.fullmatch(r"[A-Za-z]:", c):
            continue
        result.append(c)
    return result


def _ufh_shc_basename_stems(value_parts: list[str]) -> set[str]:
    tokens: set[str] = set()
    for part in value_parts:
        basename = part.rsplit("\\", 1)[-1]
        stem = os.path.splitext(basename)[0]
        tokens.update(_entry_match_tokens(stem))
    return tokens


def _scan_ufh_shc_leftovers(app: "InstalledApp", primary: str, primary_ambiguous: bool,
                             app_tokens: set[str]) -> list["LeftoverItem"]:
    found: list[LeftoverItem] = []
    install_dirs = [d.lower() for d in _install_dir_candidates(app)]

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _UFH_SHC_SUBKEY, 0, winreg.KEY_READ)
    except OSError:
        return found

    with key:
        i = 0
        while True:
            try:
                value_name, value_data, _value_type = winreg.EnumValue(key, i)
            except OSError:
                break
            i += 1
            if isinstance(value_data, (list, tuple)):
                value_parts = [str(p) for p in value_data]
            elif isinstance(value_data, str):
                value_parts = [value_data]
            else:
                continue
            data_str = " ".join(value_parts)
            if not data_str:
                continue
            data_low = data_str.lower()

            matched = False
            if any(_path_prefix_match(data_low, d) for d in install_dirs):
                matched = True
            elif primary and not primary_ambiguous:
                has_shortcut_ext = (".lnk" in data_low) or (".exe" in data_low)
                if has_shortcut_ext:
                    stem_tokens = _ufh_shc_basename_stems(value_parts)
                    if primary in stem_tokens and not _looks_like_different_variant(stem_tokens, app_tokens):
                        matched = True

            if not matched:
                continue

            found.append(LeftoverItem(
                kind="registry_value",
                path=f"HKCU\\{_UFH_SHC_SUBKEY} \u2192 {value_name}",
                size_kb=0,
                hive=winreg.HKEY_CURRENT_USER,
                subkey=_UFH_SHC_SUBKEY,
                value_name=value_name,
            ))

    return found


def _compress_msi_guid(guid: str) -> str:
    g = (guid or "").strip("{}").replace("-", "").upper()
    if len(g) != 32 or not all(c in "0123456789ABCDEF" for c in g):
        return ""
    d1, d2, d3, d4 = g[0:8], g[8:12], g[12:16], g[16:32]
    return d1[::-1] + d2[::-1] + d3[::-1] + "".join(d4[i + 1] + d4[i] for i in range(0, len(d4), 2))


_MSI_USERDATA_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\UserData"


def _scan_msi_productcode_leftovers(app: InstalledApp) -> list[LeftoverItem]:
    items: list[LeftoverItem] = []
    if not app.is_windows_installer:
        return items
    key = (app.key_name or "").strip()
    if not (key.startswith("{") and key.endswith("}")):
        return items
    compressed = _compress_msi_guid(key)
    if not compressed:
        return items

    def _check(hive, subkey: str) -> None:
        try:
            with winreg.OpenKey(hive, subkey):
                pass
        except OSError:
            return
        hive_label = "HKEY_LOCAL_MACHINE" if hive == winreg.HKEY_LOCAL_MACHINE else "HKEY_CURRENT_USER"
        items.append(LeftoverItem(kind="registry", path=f"{hive_label}\\{subkey}",
                                   hive=hive, subkey=subkey))

    _check(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Classes\Installer\Products\{compressed}")
    _check(winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\Installer\Products\{compressed}")

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MSI_USERDATA_SUBKEY) as root:
            i = 0
            while True:
                try:
                    sid = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                _check(winreg.HKEY_LOCAL_MACHINE, rf"{_MSI_USERDATA_SUBKEY}\{sid}\Products\{compressed}")
    except OSError:
        pass

    try:
        cached_msi = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Installer" / f"{compressed}.msi"
        if cached_msi.is_file():
            items.append(LeftoverItem(kind="file", path=str(cached_msi),
                                       size_kb=cached_msi.stat().st_size // 1024))
    except OSError:
        pass

    return items


_GUID_RE = re.compile(r"\{[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\}")


def _scan_installer_icon_cache(app: InstalledApp) -> list:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return []
    codes = []
    for text in (app.key_name, app.uninstall_string, app.quiet_uninstall_string):
        for m in _GUID_RE.findall(text or ""):
            if m.lower() not in [c.lower() for c in codes]:
                codes.append(m)
    items = []
    for code in codes:
        icon_cache = Path(appdata) / "Microsoft" / "Installer" / code
        try:
            if icon_cache.is_dir() and not _is_reparse_folder(str(icon_cache)):
                items.append(LeftoverItem(kind="folder", path=str(icon_cache)))
        except OSError:
            pass
    return items


def _is_own_uninstall_registration(app: InstalledApp, hive: int, subkey: str) -> bool:
    key = (app.key_name or "").strip().lower()
    if not key:
        return False
    low = (subkey or "").strip().lower()
    return low in (f"{_UNINSTALL_SUBKEY}\\{key}".lower(), f"{_UNINSTALL_SUBKEY_WOW64}\\{key}".lower())


def _drop_nested_folder_duplicates(items: list["LeftoverItem"]) -> list["LeftoverItem"]:
    folder_idx = [i for i, it in enumerate(items) if it.kind == "folder"]
    folder_idx.sort(key=lambda i: len(os.path.normcase(os.path.normpath(items[i].path))))
    kept_outer: list[str] = []
    redundant_idx: set[int] = set()
    for i in folder_idx:
        norm = os.path.normcase(os.path.normpath(items[i].path))
        if any(norm == outer or norm.startswith(outer + "\\") for outer in kept_outer):
            redundant_idx.add(i)
        else:
            kept_outer.append(norm)
    if not redundant_idx:
        return items
    return [it for i, it in enumerate(items) if i not in redundant_idx]


def _dedupe_items(items: list["LeftoverItem"]) -> list["LeftoverItem"]:
    unique_items: list[LeftoverItem] = []
    seen_item_keys: set = set()
    for it in items:
        key = _item_key(it)
        if key not in seen_item_keys:
            seen_item_keys.add(key)
            unique_items.append(it)
    return unique_items


def _item_key(it: "LeftoverItem") -> tuple:
    if it.kind in ("file", "folder"):
        return (it.kind, os.path.normcase(os.path.normpath(it.path)))
    return (it.kind, it.path.lower())


_PKG_REPOSITORY_SUBKEY = (r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion"
                          r"\AppModel\Repository\Packages")
_sideloaded_packages_cache: tuple[float, list] = (0.0, [])
_package_scan_logged: dict = {}


def _sideloaded_packages() -> list:
    global _sideloaded_packages_cache
    stamp, cached = _sideloaded_packages_cache
    if cached and time.monotonic() - stamp < 120:
        return cached
    from . import core_appx
    found = core_appx.list_sideloaded_packages() or []
    _sideloaded_packages_cache = (time.monotonic(), found)
    return found


def _scan_registered_package_leftovers(app: "InstalledApp", primary: Optional[str],
                                        primary_ambiguous: bool) -> list["LeftoverItem"]:
    items: list[LeftoverItem] = []
    if not primary or primary_ambiguous:
        return items
    weak = _WEAK_TOKENS | _GENERIC_TOKENS
    pub_tokens = _entry_match_tokens(app.publisher) - weak
    if not pub_tokens:
        return items
    try:
        packages = _sideloaded_packages()
    except Exception as e:
        _log_uninstall(f"package scan failed: {e!r}")
        return items
    families = []
    for pkg in packages:
        name = pkg["family"].rsplit("_", 1)[0]
        if _tokens_match(_entry_match_tokens(name), {primary}) \
                and (_entry_match_tokens(pkg["publisher"]) - weak) & pub_tokens:
            families.append(pkg["family"])
    _package_scan_log_key = (app.key_name, len(packages), tuple(families))
    if _package_scan_logged.get("last") != _package_scan_log_key:
        _package_scan_logged["last"] = _package_scan_log_key
        _log_uninstall(f"package scan: {len(packages)} developer-signed packages, matched {families}")
    if not families:
        return items
    _matched_package_families.update(families)

    for family in families:
        for root in (os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Packages"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages")):
            folder = os.path.join(root, family) if root else ""
            if folder and os.path.isdir(folder):
                items.append(LeftoverItem(kind="folder", path=folder, size_kb=_dir_size_kb(folder)))

    pub_ids = {f.rsplit("_", 1)[1]: f.rsplit("_", 1)[0] for f in families}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PKG_REPOSITORY_SUBKEY, 0, winreg.KEY_READ) as root_key:
            i = 0
            while True:
                try:
                    full = winreg.EnumKey(root_key, i)
                except OSError:
                    break
                i += 1
                parts = full.split("_")
                if len(parts) >= 5 and pub_ids.get(parts[-1]) == parts[0]:
                    subkey = f"{_PKG_REPOSITORY_SUBKEY}\\{full}"
                    items.append(LeftoverItem(kind="registry", path=f"HKEY_CURRENT_USER\\{subkey}",
                                               hive=winreg.HKEY_CURRENT_USER, subkey=subkey))
    except OSError:
        pass
    return items


_remembered_exe_stems: dict = {}
_GENERIC_EXE_STEMS = {
    "setup", "install", "installer", "uninstall", "uninst", "unins000", "unins001",
    "update", "updater", "launcher", "helper", "crashpad_handler", "crashreporter",
    "cmd", "powershell", "explorer", "msiexec", "rundll32", "regsvr32", "node", "python",
}
_TRACING_SUBKEYS = (r"SOFTWARE\Microsoft\Tracing", r"SOFTWARE\WOW6432Node\Microsoft\Tracing")
_TRACING_RE = re.compile(r"^(.+)_(?:rasapi32|rasmancs)$", re.IGNORECASE)
_FILEEXTS_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
_ASSOC_TOASTS_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\ApplicationAssociationToasts"
_INSTALLER_FOLDERS_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\Folders"


def remember_app_executables(app: "InstalledApp") -> None:
    uid = getattr(app, "uid", None)
    if not uid:
        return
    stems: set = set()
    try:
        for d in _install_dir_candidates(app):
            if not os.path.isdir(d):
                continue
            base_depth = d.count(os.sep)
            for root, dirs, files in os.walk(d):
                if root.count(os.sep) - base_depth >= 2:
                    dirs[:] = []
                stems.update(os.path.splitext(f)[0].lower() for f in files
                             if f.lower().endswith((".exe", ".bin")))
                if len(stems) > 500:
                    break
    except Exception:
        stems = set()
    _remembered_exe_stems[uid] = stems - _GENERIC_EXE_STEMS


def _hive_label(hive: int) -> str:
    return _HIVE_NAMES.get(hive, str(hive))


def _scan_tracing_leftovers(exe_stems: set) -> list:
    found: list = []
    if not exe_stems:
        return found
    for subkey in _TRACING_SUBKEYS:
        subs, _vals = _reg_enum(winreg.HKEY_LOCAL_MACHINE, subkey)
        for name in subs:
            m = _TRACING_RE.match(name)
            if m and m.group(1).lower() in exe_stems:
                full = f"{subkey}\\{name}"
                found.append(LeftoverItem(kind="registry", path=f"{_hive_label(winreg.HKEY_LOCAL_MACHINE)}\\{full}",
                                          hive=winreg.HKEY_LOCAL_MACHINE, subkey=full))
    return found


def _scan_association_leftovers(token: str) -> list:
    found: list = []
    if not token or len(token) < 4:
        return found
    hkcu = winreg.HKEY_CURRENT_USER

    def ours(name: str) -> bool:
        return token in re.sub(r"[^a-z0-9]", "", (name or "").lower())

    def add_value(subkey: str, value_name: str) -> None:
        found.append(LeftoverItem(kind="registry_value",
                                  path=f"{_hive_label(hkcu)}\\{subkey} \u2192 {value_name}",
                                  hive=hkcu, subkey=subkey, value_name=value_name))

    _subs, vals = _reg_enum(hkcu, _ASSOC_TOASTS_SUBKEY)
    for name, _d in vals:
        if ours(name):
            add_value(_ASSOC_TOASTS_SUBKEY, name)
    exts, _v = _reg_enum(hkcu, _FILEEXTS_SUBKEY)
    for ext in exts:
        for choice in ("UserChoice", "UserChoiceLatest\\ProgId"):
            sub = f"{_FILEEXTS_SUBKEY}\\{ext}\\{choice}"
            _s, v = _reg_enum(hkcu, sub)
            for name, data in v:
                if name == "ProgId" and isinstance(data, str) and ours(data):
                    add_value(sub, name)
    return found


def _scan_installer_folders_leftovers(install_dirs_low: list) -> list:
    found = _scan_path_named_value_leftovers(
        winreg.HKEY_LOCAL_MACHINE, _INSTALLER_FOLDERS_SUBKEY, install_dirs_low)
    return [it for it in found if not os.path.isdir(it.value_name)]


def _scan_prefetch_by_exe(exe_stems: set) -> list:
    if not exe_stems:
        return []
    prefetch_dir = _prefetch_dir()
    try:
        entries = os.listdir(prefetch_dir)
    except OSError:
        return []
    found: list = []
    for entry in entries:
        low = entry.lower()
        m = re.match(r"^(.+)\.(?:exe|bin)-[0-9a-f]{8}\.pf$", low)
        if m and m.group(1) in exe_stems and not _is_windows_own_exe(entry.rsplit("-", 1)[0]):
            full = os.path.join(prefetch_dir, entry)
            try:
                size_kb = max(1, os.path.getsize(full) // 1024)
            except OSError:
                size_kb = 0
            found.append(LeftoverItem(kind="file", path=full, size_kb=size_kb))
    return found


def _scan_installer_folders_for_items(items: list) -> list:
    folders = {os.path.normcase(os.path.normpath(i.path))
               for i in items if i.kind == "folder" and not i.uncertain}
    if not folders:
        return []
    found: list = []
    _subs, vals = _reg_enum(winreg.HKEY_LOCAL_MACHINE, _INSTALLER_FOLDERS_SUBKEY)
    for name, _d in vals:
        if os.path.normcase(os.path.normpath(name)) in folders:
            found.append(LeftoverItem(
                kind="registry_value",
                path=f"{_hive_label(winreg.HKEY_LOCAL_MACHINE)}\\{_INSTALLER_FOLDERS_SUBKEY} \u2192 {name}",
                hive=winreg.HKEY_LOCAL_MACHINE, subkey=_INSTALLER_FOLDERS_SUBKEY, value_name=name))
    return found


def _scan_ext_keys_for_removed_progids(items: list) -> list:
    progids = set()
    for it in items:
        if it.kind == "registry" and not it.uncertain:
            m = re.match(r"^software\\classes\\([^\\.][^\\]*)$", it.subkey or "", re.IGNORECASE)
            if m:
                progids.add(m.group(1).lower())
    exts = set()
    for name in progids:
        tail = name.rsplit(".", 1)[-1] if "." in name else ""
        if re.fullmatch(r"[a-z0-9_+-]{1,12}", tail):
            exts.add("." + tail)
    found: list = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for ext in sorted(exts):
            sub = f"Software\\Classes\\{ext}"
            subs, vals = _reg_enum(hive, sub)
            default = next((str(d) for n, d in vals if n == "" and isinstance(d, str)), "")
            target = sub if default.lower() in progids else ""
            if not target:
                owp = next((x for x in subs if x.lower() == "openwithprogids"), None)
                if owp:
                    _s, v = _reg_enum(hive, f"{sub}\\{owp}")
                    if any(n.lower() in progids for n, _d in v):
                        target = f"{sub}\\{owp}"
            if target:
                found.append(LeftoverItem(kind="registry", path=f"{_hive_label(hive)}\\{target}",
                                          hive=hive, subkey=target))
    hkcu = winreg.HKEY_CURRENT_USER

    def add_value(sub: str, name: str) -> None:
        found.append(LeftoverItem(kind="registry_value",
                                  path=f"{_hive_label(hkcu)}\\{sub} \u2192 {name}",
                                  hive=hkcu, subkey=sub, value_name=name))

    for ext in sorted(exts):
        owp = f"{_FILEEXTS_SUBKEY}\\{ext}\\OpenWithProgids"
        for n, _d in _reg_enum(hkcu, owp)[1]:
            if n.lower() in progids:
                add_value(owp, n)
        for choice in ("UserChoice", "UserChoiceLatest\\ProgId"):
            sub = f"{_FILEEXTS_SUBKEY}\\{ext}\\{choice}"
            for n, d in _reg_enum(hkcu, sub)[1]:
                if n == "ProgId" and isinstance(d, str) and d.lower() in progids:
                    add_value(sub, n)
    for n, _d in _reg_enum(hkcu, _ASSOC_TOASTS_SUBKEY)[1]:
        if any(n.lower().startswith(p + "_") for p in progids):
            add_value(_ASSOC_TOASTS_SUBKEY, n)
    return found


def scan_leftovers(app: InstalledApp) -> list[LeftoverItem]:
    items: list[LeftoverItem] = []
    _package_unregister_state.clear()
    _matched_package_families.clear()
    _own_package_families.clear()
    tokens = _normalize_name_tokens(app.display_name)
    pub_tokens = _normalize_name_tokens(app.publisher)
    if not tokens:
        return items

    other_tokens, other_split_tokens = _other_apps_token_sets(app.uid)

    primary = _primary_name_token(_product_name_tokens(tokens, pub_tokens), other_tokens, other_split_tokens)
    if not primary:
        primary = None
    primary_ambiguous = bool(primary) and primary in other_tokens

    seen_paths = set()
    app_tokens = _entry_match_tokens(app.display_name)

    install_loc = (app.install_location or "").strip().strip('"')
    if install_loc and os.path.isdir(install_loc) and not _is_protected_dir(install_loc):
        norm = os.path.normcase(os.path.normpath(install_loc))
        if norm not in seen_paths:
            seen_paths.add(norm)
            items.append(LeftoverItem(kind="folder", path=install_loc,
                                       size_kb=_dir_size_kb(install_loc)))

    for root in _candidate_folder_roots() if (primary and not primary_ambiguous) else []:
        items.extend(_scan_folder_subtree(root, primary, app_tokens, depth=1, max_depth=2, seen_paths=seen_paths,
                                          trusted_tokens=_trusted_parent_tokens(app, app_tokens),
                                          allow_microsoft="microsoft" in _entry_match_tokens(app.publisher)))

    items = _drop_nested_folder_duplicates(items)

    if primary and not primary_ambiguous:
        items.extend(_scan_prefetch_leftovers(primary))
        items.extend(_scan_shortcut_leftovers(primary, app_tokens))

    reg_search_roots = [
        (winreg.HKEY_CURRENT_USER, r"Software"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node"),
    ]
    reg_classes_roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Classes"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Classes"),
    ]
    pub_primary = _primary_match_token(pub_tokens)
    match_tokens = {t for t in (primary, pub_primary) if t and t not in other_tokens}
    app_and_pub_tokens = app_tokens | _entry_match_tokens(app.publisher)

    if match_tokens:
        for hive, base in reg_search_roots:
            items.extend(_scan_registry_subtree(
                hive, base, match_tokens, app_and_pub_tokens, depth=1, max_depth=2,
                product_tokens={primary} if primary else None))
        for hive, base in reg_classes_roots:
            items.extend(_scan_registry_subtree(
                hive, base, match_tokens, app_and_pub_tokens, depth=1, max_depth=1))

    items.extend(_scan_ufh_shc_leftovers(app, primary, primary_ambiguous, app_tokens))
    items.extend(_scan_registered_package_leftovers(app, primary, primary_ambiguous))

    install_dirs_low = [d.lower() for d in _install_dir_candidates(app)]
    if install_dirs_low:
        items.extend(_scan_firewall_rule_leftovers(install_dirs_low))
        items.extend(_scan_run_leftovers(install_dirs_low))
        items.extend(_scan_app_paths_leftovers(install_dirs_low))
        items.extend(_scan_classes_by_location(install_dirs_low))
        items.extend(_scan_path_named_value_leftovers(
            winreg.HKEY_CURRENT_USER, _APPCOMPAT_STORE_SUBKEY, install_dirs_low))
        for muicache_subkey in _MUICACHE_SUBKEYS:
            items.extend(_scan_path_named_value_leftovers(
                winreg.HKEY_CURRENT_USER, muicache_subkey, install_dirs_low))
        items.extend(_scan_installer_folders_leftovers(install_dirs_low))
    items.extend(_scan_tracing_leftovers(_remembered_exe_stems.get(app.uid, set())))
    items.extend(_scan_prefetch_by_exe(_remembered_exe_stems.get(app.uid, set())))
    if primary and not primary_ambiguous:
        items.extend(_scan_association_leftovers(primary))

    if True:
        seen_all = {_item_key(it) for it in items}
        for it in list(_scan_msi_productcode_leftovers(app)) + _scan_installer_icon_cache(app):
            key = _item_key(it)
            if key not in seen_all:
                seen_all.add(key)
                items.append(it)

    from . import core_monitor as _core_monitor
    log_entry = _core_monitor.find_monitor_log_for_app(app.display_name, app.key_name)
    if log_entry is not None:
        seen_all = {_item_key(it) for it in items}
        for it in _core_monitor.load_monitor_log(log_entry.path):
            key = _item_key(it)
            if key not in seen_all:
                seen_all.add(key)
                if not _monitor_log_item_exists(it):
                    continue
                it.uncertain = not _monitor_item_corroborated(
                    app, it, install_dirs_low, match_tokens)
                items.append(it)
        promote_association_backups(items)

    # Safety net: this program's OWN "Uninstall" registry key (see
    # _is_own_uninstall_registration) must never land on the leftovers list, whichever
    # source above proposed it. In practice this concerns the Monitor log: it records
    # EVERY key added during the installation, including the program's own ARP
    # registration, and without this filter it went straight onto the "to delete" list,
    # although its existence just means the program has not really been uninstalled
    # (yet).
    items = _dedupe_items(items)

    items = [it for it in items
             if not (it.kind == "registry" and _is_own_uninstall_registration(app, it.hive, it.subkey))]

    items = [it for it in items if not leftover_is_blocked(it, app.display_name)]
    items.extend(_scan_installer_folders_for_items(items))
    known_paths = {it.path.lower() for it in items}
    items.extend(it for it in _scan_ext_keys_for_removed_progids(items)
                 if it.path.lower() not in known_paths and not leftover_is_blocked(it, app.display_name))
    _mark_shared_roots(items, app)
    if any(it.kind == "registry_value" for it in items):
        own_dirs = [d for d in [install_loc] if d]
        items = [it for it in items if not _firewall_rule_not_ours(it, own_dirs)]

    items = drop_foreign_assoc_values(items)

    fs_other, reg_other = _other_apps_owned_paths(app)
    if fs_other or reg_other:
        items = [it for it in items if not _is_shared_with_other_apps(it, fs_other, reg_other)]

    def _mark_publisher_containers() -> None:
        if not (pub_primary and pub_primary != primary):
            return
        own_loc = os.path.normcase(os.path.normpath(install_loc)) if install_loc else ""
        for it in items:
            if it.uncertain or it.kind not in ("registry", "folder"):
                continue
            if own_loc and it.kind == "folder" and os.path.normcase(os.path.normpath(it.path)) == own_loc:
                continue
            leaf_tokens = _entry_match_tokens(it.path.replace("/", "\\").rstrip("\\").rsplit("\\", 1)[-1])
            if _tokens_match(leaf_tokens, {pub_primary}) and not (primary and _tokens_match(leaf_tokens, {primary})):
                it.uncertain = True

    _mark_publisher_containers()
    mark_user_data(items, install_dirs_low)
    expand_corroboration(items, install_dirs_low)
    _mark_publisher_containers()
    return items


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400


def _send_to_recycle_bin(path: str) -> bool:
    return _shfile_delete(path) == 0


def _shfile_delete(path: str) -> int:
    try:
        full = os.path.abspath(path)
        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = _FO_DELETE
        op.pFrom = full + "\0\0"
        op.pTo = None
        op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
        op.fAnyOperationsAborted = False
        op.hNameMappings = None
        op.lpszProgressTitle = None
        return int(ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)))
    except Exception:
        return -1


def _recycle_with_retry(path: str, attempts: int = 3, delay: float = 0.7) -> bool:
    for n in range(attempts):
        code = _shfile_delete(path)
        if code == 0 or not os.path.lexists(path):
            return True
        _log_uninstall(f"recycle failed ({n + 1}/{attempts}): {path!r} code=0x{code & 0xFFFFFFFF:X}")
        if n < attempts - 1:
            time.sleep(delay)
    return False


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", wintypes.DWORD)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


_TOKEN_ADJUST_PRIVILEGES = 0x0020
_TOKEN_QUERY = 0x0008
_SE_PRIVILEGE_ENABLED = 0x00000002
_MOVEFILE_DELAY_UNTIL_REBOOT = 0x00000004

_shutdown_privilege_enabled = None

# Explicit prototypes (argtypes/restype) for the WinAPI functions used below. WITHOUT
# THEM GetCurrentProcess() returns the pseudo-handle -1, which ctypes treats as a plain
# 32-bit c_int; on 64-bit Python the upper 32 bits of the HANDLE argument passed later
# may be undefined (garbage), which gave OpenProcessToken() a bad handle and silently
# broke the whole schedule-removal-on-reboot path ("could not delete or schedule
# deletion on restart" although it should have worked).
_advapi32 = ctypes.windll.advapi32
_kernel32b = ctypes.windll.kernel32
_kernel32b.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32b.GetCurrentProcess.argtypes = []
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
_advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(_LUID)]
_advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL
_advapi32.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(_TOKEN_PRIVILEGES),
                                             wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
_kernel32b.MoveFileExW.restype = wintypes.BOOL
_kernel32b.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_kernel32b.CloseHandle.restype = wintypes.BOOL
_kernel32b.CloseHandle.argtypes = [wintypes.HANDLE]


def _enable_shutdown_privilege() -> bool:
    global _shutdown_privilege_enabled
    if _shutdown_privilege_enabled is not None:
        return _shutdown_privilege_enabled
    try:
        h_token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(
                _kernel32b.GetCurrentProcess(),
                _TOKEN_ADJUST_PRIVILEGES | _TOKEN_QUERY, ctypes.byref(h_token)):
            _shutdown_privilege_enabled = False
            return False
        try:
            luid = _LUID()
            if not _advapi32.LookupPrivilegeValueW(None, "SeShutdownPrivilege", ctypes.byref(luid)):
                _shutdown_privilege_enabled = False
                return False
            tp = _TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0] = _LUID_AND_ATTRIBUTES(luid, _SE_PRIVILEGE_ENABLED)
            ok = _advapi32.AdjustTokenPrivileges(h_token, False, ctypes.byref(tp), 0, None, None)
            _shutdown_privilege_enabled = bool(ok) and _kernel32b.GetLastError() == 0
            return _shutdown_privilege_enabled
        finally:
            _kernel32b.CloseHandle(h_token)
    except Exception:
        _shutdown_privilege_enabled = False
        return False


_reboot_fail_log_left = 5


def _log_reboot_fail(text: str) -> None:
    global _reboot_fail_log_left
    if _reboot_fail_log_left > 0:
        _reboot_fail_log_left -= 1
        _log_uninstall(text)


def _schedule_delete_on_reboot(path: str) -> bool:
    if not _enable_shutdown_privilege():
        _log_reboot_fail(f"schedule at restart skipped, no shutdown privilege: {path!r}")
        return False
    try:
        if _kernel32b.MoveFileExW(str(path), None, _MOVEFILE_DELAY_UNTIL_REBOOT):
            return True
        _log_reboot_fail(f"schedule at restart failed: {path!r} error={_kernel32b.GetLastError()}")
        return False
    except Exception:
        return False


_reboot_details: dict = {}
_reboot_details_lock = threading.Lock()
_REBOOT_DETAILS_CAP = 200


def pop_reboot_details(path: str) -> list[str]:
    with _reboot_details_lock:
        return _reboot_details.pop(os.path.normcase(os.path.normpath(path)), [])


def _schedule_locked_item_for_reboot(path: str) -> bool:
    scheduled: list[str] = []

    def schedule(p: str) -> bool:
        if _schedule_delete_on_reboot(p):
            scheduled.append(p)
            return True
        return False

    if os.path.isdir(path) and not os.path.islink(path):
        all_ok = True
        for root, dirs, files in os.walk(path, topdown=False):
            for f in files:
                if not schedule(os.path.join(root, f)):
                    all_ok = False
            for d in dirs:
                if not schedule(os.path.join(root, d)):
                    all_ok = False
        if not schedule(path):
            all_ok = False
        result = all_ok
    else:
        result = schedule(path)
    with _reboot_details_lock:
        _reboot_details[os.path.normcase(os.path.normpath(path))] = scheduled[:_REBOOT_DETAILS_CAP]
    _log_uninstall(f"scheduled at restart: {path!r} items={len(scheduled)} complete={result}")
    return result


def _registry_backup_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "HOTS Uninstaller" / "RegistryBackups"


def _registry_backup_index_path() -> Path:
    return _registry_backup_dir() / "index.json"


_INDEX_LOCK = threading.RLock()

_BACKUP_FILE_RE = re.compile(r"^(?P<name>.+)_(?P<stamp>\d{8}_\d{6})(?:_\d+)?\.reg$", re.IGNORECASE)


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _size_to_kb(size_bytes: int) -> int:
    return max(1, (size_bytes + 1023) // 1024) if size_bytes > 0 else 0


def _read_index_file(path: Path) -> Optional[list]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _entry_from_backup_file(path: str, match) -> dict:
    text = ""
    for enc in ("utf-16", "utf-8-sig"):
        try:
            text = Path(path).read_text(encoding=enc)
            if "[" in text:
                break
        except (OSError, UnicodeError):
            continue
    short = {v: _HIVE_NAMES[k] for k, v in _HIVE_FULL_NAMES.items() if k in _HIVE_NAMES}
    targets: list[dict] = []
    hives: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"^\[(HKEY_[A-Z_]+)\\(.+)\]$", line.strip())
        if not m:
            continue
        hive = short.get(m.group(1), m.group(1))
        hives.add(hive)
        target = {"k": f"{hive}\\{m.group(2)}", "v": None}
        if target not in targets:
            targets.append(target)
    try:
        deleted_at = datetime.strptime(match.group("stamp"), "%Y%m%d_%H%M%S").isoformat(timespec="seconds")
    except ValueError:
        try:
            deleted_at = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
        except OSError:
            deleted_at = ""
    name = match.group("name").replace("_", " ").strip()
    return {
        "app_name": "" if name.lower() == "leftover" else name,
        "hive": "+".join(sorted(hives)),
        "subkey": "; ".join(t["k"] for t in targets[:2]),
        "targets": targets,
        "path": path,
        "size_kb": _size_to_kb(os.path.getsize(path)),
        "deleted_at": deleted_at,
    }


def _adopt_orphan_backups(entries: list[dict]) -> list[dict]:
    folder = _registry_backup_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        return entries
    known = {_norm_path(e.get("path", "")) for e in entries}
    result = list(entries)
    for filename in names:
        match = _BACKUP_FILE_RE.match(filename)
        if not match:
            continue
        full = str(folder / filename)
        if _norm_path(full) in known:
            continue
        try:
            result.append(_entry_from_backup_file(full, match))
        except Exception:
            continue
    return result


def _load_backup_index() -> list[dict]:
    with _INDEX_LOCK:
        main = _registry_backup_index_path()
        entries = _read_index_file(main)
        if entries is None:
            entries = _read_index_file(main.with_name(main.name + ".bak"))
        entries = [e for e in (entries or []) if isinstance(e, dict)]
        return _adopt_orphan_backups(entries)


def _save_backup_index(entries: list[dict]) -> bool:
    with _INDEX_LOCK:
        idx = _registry_backup_index_path()
        tmp = idx.with_name(idx.name + ".tmp")
        try:
            idx.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
            previous = _read_index_file(idx)
            if previous is not None:
                try:
                    idx.with_name(idx.name + ".bak").write_text(
                        json.dumps(previous, indent=2, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            os.replace(tmp, idx)
            return True
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return False


_MAX_REGISTRY_BACKUPS = 50
MAX_REGISTRY_BACKUPS = _MAX_REGISTRY_BACKUPS


# Registry backup integrity. The .reg files and the index live in the user's profile
# (%APPDATA%), where ANY process of that user can write, while they are restored and
# deleted by the ELEVATED program. Therefore: 1) we touch only .reg files located
# DIRECTLY in the backup folder (not any path given in the index) and not through a
# link, 2) when a backup is created its SHA-256 is stored in HKLM (writing there needs
# administrator rights, so an ordinary process cannot forge it), and it is verified
# before restoring. A file that was replaced or added by hand has no matching hash and
# is not imported.
_BACKUP_HASH_KEY = r"Software\HOTS Uninstaller\BackupHashes"
_MONITOR_HASH_KEY = r"Software\HOTS Uninstaller\MonitorLogHashes"


def _is_backup_file(path: str) -> bool:
    try:
        if not path or not str(path).lower().endswith(".reg"):
            return False
        folder = _registry_backup_dir()
        if _is_reparse_folder(str(folder)) or _is_reparse_folder(str(folder.parent)):
            return False
        if os.path.islink(path):
            return False
        real_parent = os.path.normcase(os.path.realpath(os.path.dirname(os.path.abspath(path))))
        return real_parent == os.path.normcase(os.path.realpath(str(folder)))
    except Exception:
        return False


def _file_sha256(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _seal_backup(path: str) -> bool:
    digest = _file_sha256(path)
    if not digest:
        return False
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, _BACKUP_HASH_KEY, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.SetValueEx(k, os.path.basename(path).lower(), 0, winreg.REG_SZ, digest)
        return True
    except OSError:
        return False


def _unseal_backup(path: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _BACKUP_HASH_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.DeleteValue(k, os.path.basename(path).lower())
    except OSError:
        pass


def _backup_verified(path: str) -> bool:
    if not _is_backup_file(path):
        return False
    digest = _file_sha256(path)
    if not digest:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _BACKUP_HASH_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            stored, _ = winreg.QueryValueEx(k, os.path.basename(path).lower())
    except OSError:
        return False
    return isinstance(stored, str) and stored.lower() == digest.lower()


def remove_backup_hash_store() -> None:
    try:
        _delete_registry_key_recursive(winreg.HKEY_LOCAL_MACHINE, _BACKUP_HASH_KEY)
    except OSError:
        pass
    try:
        _delete_registry_key_recursive(winreg.HKEY_LOCAL_MACHINE, _MONITOR_HASH_KEY)
    except OSError:
        pass
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, _BACKUP_HASH_KEY.rsplit("\\", 1)[0])
    except OSError:
        pass


def _remove_backup_file(path: str) -> None:
    if path and os.path.isfile(path) and _is_backup_file(path):
        os.remove(path)
        _unseal_backup(path)


def _enforce_backup_limit(entries: list[dict], limit: int = _MAX_REGISTRY_BACKUPS) -> list[dict]:
    if len(entries) <= limit:
        return entries
    entries_sorted = sorted(entries, key=lambda e: e.get("deleted_at", ""))
    overflow, kept = entries_sorted[:-limit], entries_sorted[-limit:]
    for e in overflow:
        try:
            _remove_backup_file(e.get("path", ""))
        except OSError:
            pass
    return kept


def _record_backup_entry(app_name: str, hive_name: str, subkey: str, backup_path: str,
                         targets: Optional[list] = None) -> bool:
    with _INDEX_LOCK:
        entries = _load_backup_index()
        norm = _norm_path(backup_path)
        entries = [e for e in entries if _norm_path(e.get("path", "")) != norm]
        try:
            size_kb = _size_to_kb(os.path.getsize(backup_path))
        except OSError:
            size_kb = 0
        entry = {
            "app_name": app_name or "",
            "hive": hive_name,
            "subkey": subkey,
            "path": backup_path,
            "size_kb": size_kb,
            "deleted_at": datetime.now().isoformat(timespec="seconds"),
        }
        if targets:
            entry["targets"] = targets
        if not _seal_backup(backup_path):
            print(f"[backup] nie zapisano skrotu kopii {backup_path} (brak uprawnien "
                  f"administratora?) — tej kopii nie bedzie mozna przywrocic", file=sys.stderr)
        entries.append(entry)
        return _save_backup_index(_enforce_backup_limit(entries))


_UNINSTALL_HISTORY_RETENTION_DAYS = 90

_HISTORY_LOCK = threading.RLock()

# Uninstall logs: one .txt file per history entry, in a separate folder. A history entry
# holds ONLY the file name (no path), and every open and delete goes through
# _uninstall_log_path(). The history file lives in the user's profile and the program
# runs as administrator, so a tampered entry must not be able to redirect deletion or
# opening to a foreign file.
_UNINSTALL_LOG_RE = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,100}_\d{8}_\d{6}(?:_\d+)?\.txt$")
_MAX_LOG_LINES = 20000


def _uninstall_history_path() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "HOTS Uninstaller" / "uninstall_history.json"


def _uninstall_logs_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "HOTS Uninstaller" / "UninstallLogs"


def _logs_dir_safe() -> bool:
    d = _uninstall_logs_dir()
    return not (_is_reparse_folder(str(d)) or _is_reparse_folder(str(d.parent)))


def _uninstall_log_path(name: str) -> Optional[str]:
    try:
        if (not isinstance(name, str) or os.path.basename(name) != name
                or not _UNINSTALL_LOG_RE.match(name) or not _logs_dir_safe()):
            return None
        path = _uninstall_logs_dir() / name
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        return str(path)
    except Exception:
        return None


def _remove_uninstall_log(name: str) -> None:
    path = _uninstall_log_path(name)
    if path:
        os.remove(path)


def _log_clean(value) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))[:1000]


def write_uninstall_log(app_name: str, freed_kb: int, results: list,
                        confirmed: bool = True) -> str:
    try:
        if not _logs_dir_safe():
            return ""
        d = _uninstall_logs_dir()
        d.mkdir(parents=True, exist_ok=True)

        now = datetime.now()
        labels = {"ok": "REMOVED", "partial": "PARTIALLY REMOVED", "reboot": "AT RESTART",
                  "gone": "ALREADY GONE", "error": "FAILED", "skipped": "SKIPPED"}
        kinds = {"folder": "Folder", "file": "File",
                 "registry": "Registry key", "registry_value": "Registry value"}
        counts: dict[str, int] = {}
        lines = []
        for i, (status, kind, path, error) in enumerate(results):
            counts[status] = counts.get(status, 0) + 1
            if i < _MAX_LOG_LINES:
                line = f"[{labels.get(status, status)}] {kinds.get(kind, kind)}: {_log_clean(path)}"
                if error:
                    line += f"  -- {_log_clean(error)}"
                lines.append(line)
        if len(results) > _MAX_LOG_LINES:
            lines.append(f"... {len(results) - _MAX_LOG_LINES} more items not listed")

        head = [
            "HOTS Uninstaller - uninstall log",
            f"Program   : {_log_clean(app_name) or '-'}",
            f"Date      : {now:%Y-%m-%d %H:%M:%S}",
            "Status    : " + ("uninstall confirmed" if confirmed
                              else "NOT confirmed (program still detected on this computer)"),
            "Freed     : " + (format_size_kb(freed_kb) if freed_kb > 0 else "unknown"),
            f"Leftovers : removed {counts.get('ok', 0)}, "
            + (f"partially removed {counts['partial']}, " if counts.get("partial") else "")
            + (f"skipped {counts['skipped']}, " if counts.get("skipped") else "")
            + f"at restart {counts.get('reboot', 0)}, "
            f"already gone {counts.get('gone', 0)}, failed {counts.get('error', 0)}",
            "",
        ]
        tail = [
            "",
            "Files and folders were sent to the Recycle Bin. Items marked AT RESTART were",
            "locked and are deleted permanently (not via the Recycle Bin) when the computer",
            "restarts. Registry keys were exported first - see the Registry backups tab.",
        ] if results else ["No leftovers were removed."]

        text = "\n".join(head + lines + tail) + "\n"
        base = f"{_safe_backup_name(app_name)}_{now:%Y%m%d_%H%M%S}"
        for n in range(100):
            name = f"{base}.txt" if n == 0 else f"{base}_{n}.txt"
            try:
                # "x" = create a NEW file only (never overwrites, never follows a
                # planted link).
                with open(d / name, "x", encoding="utf-8-sig", newline="\r\n") as f:
                    f.write(text)
                return name
            except FileExistsError:
                continue
        return ""
    except Exception:
        return ""


def uninstall_logs_folder_path() -> str:
    return str(_uninstall_logs_dir())


def open_uninstall_logs_folder() -> None:
    try:
        if not _logs_dir_safe():
            return
        d = _uninstall_logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        popen_no_busy_cursor(["explorer.exe", str(d)])
    except Exception:
        pass


def open_uninstall_log(log_file: str) -> bool:
    path = _uninstall_log_path(log_file)
    if not path:
        return False
    try:
        popen_no_busy_cursor(["notepad.exe", path])
        return True
    except Exception:
        return False


def _load_uninstall_history_raw() -> list[dict]:
    try:
        raw = _uninstall_history_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def _save_uninstall_history_raw(entries: list[dict]) -> bool:
    """Writes via a temp file + os.replace: an interrupted write does not destroy the
    history, and a planted link in place of the file is replaced, not read (the
    program writes here as administrator).
    """
    try:
        path = _uninstall_history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        with open(tmp, "x", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _prune_uninstall_history(entries: list[dict]) -> list[dict]:
    cutoff = datetime.now() - timedelta(days=_UNINSTALL_HISTORY_RETENTION_DAYS)
    kept = []
    for e in entries:
        try:
            when = datetime.fromisoformat(e.get("uninstalled_at", ""))
        except (ValueError, TypeError):
            when = None
        if when is not None and when >= cutoff:
            kept.append(e)
        else:
            try:
                _remove_uninstall_log(e.get("log") or "")
            except OSError:
                pass
    return kept


def record_uninstall_history(app_name: str, freed_kb: int = 0, log_file: str = "",
                             portable: bool = False) -> None:
    with _HISTORY_LOCK:
        entries = _load_uninstall_history_raw()
        entry = {
            "app_name": app_name or "",
            "uninstalled_at": datetime.now().isoformat(timespec="seconds"),
            "freed_kb": freed_kb,
        }
        if log_file:
            entry["log"] = log_file
        if portable:
            entry["portable"] = True
        entries.append(entry)
        _save_uninstall_history_raw(_prune_uninstall_history(entries))


def clear_uninstall_history() -> list[str]:
    errors: list[str] = []
    with _HISTORY_LOCK:
        if not _save_uninstall_history_raw([]):
            return [str(_uninstall_history_path())]
        try:
            names = os.listdir(_uninstall_logs_dir()) if _logs_dir_safe() else []
        except OSError:
            names = []
        for name in names:
            try:
                _remove_uninstall_log(name)
            except OSError as ex:
                errors.append(f"{name}: {ex}")
    return errors


@dataclass
class UninstallHistoryEntry:
    app_name: str
    uninstalled_at: str
    freed_kb: int
    log_file: str = ""
    log_exists: bool = False
    portable: bool = False


def delete_uninstall_history_entry(entry: UninstallHistoryEntry) -> tuple[bool, str]:
    key = (entry.app_name, entry.uninstalled_at, entry.log_file)
    with _HISTORY_LOCK:
        entries = _load_uninstall_history_raw()
        kept = []
        for e in entries:
            if ((e.get("app_name") or "—", e.get("uninstalled_at", ""), e.get("log") or "") == key):
                try:
                    _remove_uninstall_log(e.get("log") or "")
                except OSError as ex:
                    return False, str(ex)
            else:
                kept.append(e)
        if not _save_uninstall_history_raw(kept):
            return False, str(_uninstall_history_path())
    return True, ""


def list_uninstall_history() -> list[UninstallHistoryEntry]:
    with _HISTORY_LOCK:
        entries = _load_uninstall_history_raw()
        pruned = _prune_uninstall_history(entries)
        if len(pruned) != len(entries):
            _save_uninstall_history_raw(pruned)
    pruned.sort(key=lambda e: e.get("uninstalled_at", ""), reverse=True)
    result = []
    for e in pruned:
        log = e.get("log") if isinstance(e.get("log"), str) else ""
        result.append(UninstallHistoryEntry(
            app_name=e.get("app_name") or "—",
            uninstalled_at=e.get("uninstalled_at", ""),
            freed_kb=e.get("freed_kb", 0),
            log_file=log,
            log_exists=bool(log) and _uninstall_log_path(log) is not None,
            portable=e.get("portable") is True,
        ))
    return result


@dataclass
class RegistryBackupEntry:
    app_name: str
    hive_name: str
    subkey: str
    path: str
    size_kb: int
    deleted_at: str
    exists: bool
    restored_at: str = ""


def _entry_subkey_label(entry: dict) -> str:
    targets = entry.get("targets")
    if not isinstance(targets, list) or not targets:
        text = entry.get("subkey", "") or ""
        text = re.sub(r"(\d+) wartosci", lambda m: T("backups_n_values", n=m.group(1)), text)
        return re.sub(r"\(\+(\d+) wiecej\)", lambda m: T("backups_more_keys", n=m.group(1)), text)
    labels = []
    for t in targets:
        key = t.get("k", "")
        names = t.get("v")
        if names is None:
            labels.append(key)
        else:
            part = ", ".join(names) if len(names) <= 2 else T("backups_n_values", n=len(names))
            labels.append(f"{key} \u2192 {part}")
    label = "; ".join(labels[:2])
    if len(labels) > 2:
        label += " " + T("backups_more_keys", n=len(labels) - 2)
    return label


def list_registry_backups() -> list[RegistryBackupEntry]:
    entries = _load_backup_index()
    result = []
    for e in entries:
        path = e.get("path", "")
        exists = os.path.isfile(path)
        size_kb = e.get("size_kb", 0)
        if exists:
            try:
                size_kb = _size_to_kb(os.path.getsize(path))
            except OSError:
                pass
        result.append(RegistryBackupEntry(
            app_name=e.get("app_name") or "—",
            hive_name=e.get("hive", ""),
            subkey=_entry_subkey_label(e),
            path=path,
            size_kb=size_kb,
            deleted_at=e.get("deleted_at", ""),
            exists=exists,
            restored_at=e.get("restored_at", ""),
        ))
    result.sort(key=lambda x: x.deleted_at, reverse=True)
    return result


def _mark_backup_restored(path: str) -> None:
    with _INDEX_LOCK:
        entries = _load_backup_index()
        stamp = datetime.now().isoformat(timespec="seconds")
        changed = False
        for e in entries:
            if _norm_path(e.get("path", "")) == _norm_path(path):
                e["restored_at"] = stamp
                changed = True
        if changed:
            _save_backup_index(entries)


def restore_registry_backup(path: str) -> tuple[bool, str]:
    if not os.path.isfile(path):
        return False, T("err_backup_file_missing")
    # Verify first: the file must be in the backup folder and have a matching hash in
    # HKLM, otherwise NOTHING is imported into the registry as administrator.
    if not _backup_verified(path):
        return False, T("err_backup_unverified")
    try:
        result = subprocess.run(
            [system_exe("reg.exe"), "import", path],
            capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            _mark_backup_restored(path)
            return True, ""
        err = _decode_console_bytes(result.stderr or result.stdout).strip()
        return False, err or f"exit code {result.returncode}"
    except Exception as e:
        return False, str(e)


def backup_folder_path() -> str:
    return str(_registry_backup_dir())


def open_backup_folder() -> None:
    try:
        d = _registry_backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        popen_no_busy_cursor(["explorer.exe", str(d)])
    except Exception:
        pass


def reveal_backup_in_explorer(path: str) -> None:
    try:
        if os.path.exists(path):
            popen_no_busy_cursor(["explorer.exe", "/select,", path])
        else:
            popen_no_busy_cursor(["explorer.exe", str(_registry_backup_dir())])
    except Exception:
        pass


def open_backup_in_notepad(path: str) -> None:
    """Opens a .reg backup file in Notepad (explicitly notepad.exe: the default action
    for .reg in Windows is a MERGE into the registry, so os.startfile would be
    dangerous).
    """
    try:
        if os.path.isfile(path):
            popen_no_busy_cursor(["notepad.exe", path])
    except Exception:
        pass


def delete_all_registry_backups() -> list[str]:
    errors = []
    with _INDEX_LOCK:
        kept = []
        for e in _load_backup_index():
            path = e.get("path", "")
            try:
                if path and os.path.isfile(path) and not _is_backup_file(path):
                    continue
                _remove_backup_file(path)
            except OSError as ex:
                errors.append(f"{e.get('subkey', path)}: {ex}")
                kept.append(e)
        if _save_backup_index(kept) and not kept:
            try:
                _registry_backup_index_path().with_name("index.json.bak").unlink()
            except OSError:
                pass
    return errors


def delete_registry_backup(path: str) -> tuple[bool, str]:
    with _INDEX_LOCK:
        try:
            _remove_backup_file(path)
        except OSError as e:
            return False, str(e)
        entries = [e for e in _load_backup_index()
                   if _norm_path(e.get("path", "")) != _norm_path(path)]
        _save_backup_index(entries)
    return True, ""


_REG_FILE_HEADER = "Windows Registry Editor Version 5.00"


def _registry_key_exists(hive: int, subkey: str) -> Optional[bool]:
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def _export_registry_key_text(hive: int, subkey: str) -> Optional[str]:
    hive_name = _HIVE_NAMES.get(hive)
    if not hive_name:
        return None
    if _registry_key_exists(hive, subkey) is False:
        return ""
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=".reg")
        os.close(fd)
        tmp_path = tmp_name
        result = subprocess.run(
            [system_exe("reg.exe"), "export", f"{hive_name}\\{subkey}", tmp_path, "/y"],
            capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0 or not os.path.isfile(tmp_path):
            return None
        return Path(tmp_path).read_text(encoding="utf-16")
    except Exception:
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _format_reg_value_line(value_name: str, value, vtype: int) -> str:
    if value_name == "":
        name_part = "@"
    else:
        escaped_name = value_name.replace("\\", "\\\\").replace('"', '\\"')
        name_part = f'"{escaped_name}"'

    if vtype == winreg.REG_SZ:
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'{name_part}="{escaped}"'
    if vtype == winreg.REG_DWORD:
        return f"{name_part}=dword:{int(value) & 0xFFFFFFFF:08x}"
    if vtype == winreg.REG_QWORD:
        data = struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF)
        hex_str = ",".join(f"{b:02x}" for b in data)
        return f"{name_part}=hex(b):{hex_str}"
    if vtype == winreg.REG_EXPAND_SZ:
        data = str(value).encode("utf-16-le") + b"\x00\x00"
        hex_str = ",".join(f"{b:02x}" for b in data)
        return f"{name_part}=hex(2):{hex_str}"
    if vtype == winreg.REG_MULTI_SZ:
        joined = "\x00".join(value) + "\x00\x00" if value else "\x00\x00"
        data = joined.encode("utf-16-le")
        hex_str = ",".join(f"{b:02x}" for b in data)
        return f"{name_part}=hex(7):{hex_str}"
    if vtype == winreg.REG_BINARY:
        hex_str = ",".join(f"{b:02x}" for b in (value or b""))
        return f"{name_part}=hex:{hex_str}"
    try:
        raw = bytes(value)
    except Exception:
        raw = b""
    hex_str = ",".join(f"{b:02x}" for b in raw)
    return f"{name_part}=hex({vtype}):{hex_str}"


def _export_registry_values_text(hive: int, subkey: str, value_names: list[str]) -> Optional[str]:
    hive_name = _HIVE_NAMES.get(hive)
    if not hive_name:
        return None
    lines = []
    try:
        with winreg.OpenKey(hive, subkey) as key:
            for name in value_names:
                try:
                    value, vtype = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                lines.append(_format_reg_value_line(name, value, vtype))
    except FileNotFoundError:
        return ""
    except Exception:
        return None
    if not lines:
        return ""
    body = "\r\n".join(lines)
    full_hive_name = _HIVE_FULL_NAMES.get(hive, hive_name)
    return f"{_REG_FILE_HEADER}\r\n\r\n[{full_hive_name}\\{subkey}]\r\n{body}\r\n"


def _merge_reg_exports(key_texts: list[str]) -> str:
    bodies = []
    for text in key_texts:
        lines = text.splitlines()
        idx = 0
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx < len(lines) and lines[idx].strip().lower().startswith("windows registry editor"):
            idx += 1
        body = "\n".join(lines[idx:]).strip("\n")
        if body:
            bodies.append(body)
    return _REG_FILE_HEADER + "\n\n" + "\n\n".join(bodies) + "\n"


_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)


def _safe_backup_name(app_name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", app_name or "")
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"_+", "_", name).strip(" ._")[:80].strip(" ._")
    if not name:
        return "leftover"
    if name.upper() in _WINDOWS_RESERVED_NAMES:
        name = "_" + name
    return name


def _backup_registry_keys_combined(targets: list[tuple[int, str, Optional[list[str]]]],
                                    app_name: str = "") -> tuple[str, str, dict]:
    bodies: list[str] = []
    body_ids: list[tuple] = []
    target_entries: list[dict] = []
    hive_names: set[str] = set()
    failed: dict = {}
    for hive, subkey, value_names in targets:
        target_id = (hive, subkey.lower())
        hive_name = _HIVE_NAMES.get(hive, str(hive))
        if value_names is None:
            text = _export_registry_key_text(hive, subkey)
        else:
            text = _export_registry_values_text(hive, subkey, value_names)
        if text is None:
            failed[target_id] = T("err_backup_export_failed")
            continue
        if text == "":
            continue
        bodies.append(text)
        body_ids.append(target_id)
        hive_names.add(hive_name)
        target_entries.append({"k": f"{hive_name}\\{subkey}",
                               "v": None if value_names is None else list(value_names)})

    if not bodies:
        return "", "", failed

    try:
        backup_dir = _registry_backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_backup_name(app_name)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        content = _merge_reg_exports(bodies)
        backup_path = None
        for attempt in range(1, 100):
            suffix = "" if attempt == 1 else f"_{attempt}"
            candidate = backup_dir / f"{safe_name}_{stamp}{suffix}.reg"
            try:
                with open(candidate, "x", encoding="utf-16") as fh:
                    fh.write(content)
                backup_path = candidate
                break
            except FileExistsError:
                continue
        if backup_path is None:
            raise OSError("cannot create a unique backup file name")
    except Exception as e:
        for target_id in body_ids:
            failed[target_id] = str(e)
        return "", "", failed

    subkey_label = "; ".join(t["k"] for t in target_entries[:2])
    hive_label = "+".join(sorted(hive_names))
    indexed = _record_backup_entry(app_name, hive_label, subkey_label, str(backup_path), target_entries)
    return str(backup_path), ("" if indexed else "index.json"), failed


_NO_BACKUP_VALUE_SUBKEYS = frozenset(k.lower() for k in (
    _UFH_SHC_SUBKEY,
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\FeatureUsage\AppSwitched",
    _APPCOMPAT_STORE_SUBKEY,
    *_MUICACHE_SUBKEYS,
))


def _needs_registry_backup(item: LeftoverItem) -> bool:
    if item.kind == "registry_value" and (item.subkey or "").lower() in _NO_BACKUP_VALUE_SUBKEYS:
        return False
    if item.kind == "registry" and _taskcache_parts(item.hive, item.subkey) is not None:
        return False
    return True


def _collect_registry_targets(items: list[LeftoverItem]) -> list[tuple[int, str, Optional[list[str]]]]:
    whole_keys: dict[tuple, tuple[int, str]] = {}
    value_groups: dict[tuple, tuple[int, str, list[str]]] = {}
    order: list[tuple] = []
    for item in items:
        if not _needs_registry_backup(item):
            continue
        if item.kind == "registry" and _is_protected_registry_key(item.hive, item.subkey):
            continue
        if item.kind == "registry":
            key_id = (item.hive, item.subkey.lower())
            if key_id not in whole_keys:
                whole_keys[key_id] = (item.hive, item.subkey)
                order.append(key_id)
            value_groups.pop(key_id, None)
        elif item.kind == "registry_value":
            key_id = (item.hive, item.subkey.lower())
            if key_id in whole_keys:
                continue
            if key_id not in value_groups:
                value_groups[key_id] = (item.hive, item.subkey, [])
                order.append(key_id)
            names = value_groups[key_id][2]
            if item.value_name not in names:
                names.append(item.value_name)
            if _is_assoc_backup_value(item) and "" not in names:
                names.append("")

    targets: list[tuple[int, str, Optional[list[str]]]] = []
    seen: set = set()
    for key_id in order:
        if key_id in seen:
            continue
        seen.add(key_id)
        if key_id in whole_keys:
            hive, subkey = whole_keys[key_id]
            targets.append((hive, subkey, None))
        else:
            hive, subkey, names = value_groups[key_id]
            targets.append((hive, subkey, names))
    return targets


def _apply_backup_result(items: list[LeftoverItem], failed: dict) -> None:
    for it in items:
        it.backup_failed = False
        it.backup_error = ""
        if it.kind not in ("registry", "registry_value") or not _needs_registry_backup(it):
            continue
        reason = failed.get((it.hive, (it.subkey or "").lower()))
        if reason is not None:
            it.backup_failed = True
            it.backup_error = reason


def backup_registry_keys_for_items(items: list[LeftoverItem], app_name: str = "") -> tuple[str, str]:
    targets = _collect_registry_targets(items)
    if not targets:
        _apply_backup_result(items, {})
        return "", ""
    path, general_error, failed = _backup_registry_keys_combined(targets, app_name)
    _apply_backup_result(items, failed)
    return path, general_error


def _is_deletable_file_in_protected_dir(path: str) -> bool:
    parent = os.path.normcase(os.path.normpath(os.path.dirname(path)))
    ext = os.path.splitext(path)[1].lower()
    if ext in (".lnk", ".url", ".exe"):
        return parent in {os.path.normcase(os.path.normpath(r)) for r in _shortcut_roots()}
    if ext == ".msi":
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        return parent == os.path.normcase(os.path.normpath(os.path.join(windir, "Installer")))
    return False


def is_deletable_file_location(path: str) -> bool:
    if _is_os_state_path(path):
        return False
    parent = os.path.dirname(path)
    if os.path.normcase(os.path.normpath(parent)) == os.path.normcase(os.path.normpath(_prefetch_dir())):
        return True
    if _is_deletable_file_in_protected_dir(path):
        return True
    parent_norm = os.path.normcase(os.path.normpath(parent))
    return not (_is_system_container(parent_norm) or _is_under_windows_dir(parent_norm))


# Registry CONTAINER keys that are NEVER deleted as a whole (they hold data of the whole
# system and all programs). Case-insensitive comparison; "Software\WOW6432Node" is
# treated like "Software". Applies only to KEYS (kind == "registry"): single values
# inside these keys (e.g. an entry in "...\Run") can still be deleted.
_PROTECTED_REG_KEYS = {
    r"software\microsoft", r"software\microsoft\windows",
    r"software\microsoft\windows\currentversion",
    r"software\microsoft\windows\currentversion\uninstall",
    r"software\microsoft\windows\currentversion\run",
    r"software\microsoft\windows\currentversion\runonce",
    r"software\microsoft\windows\currentversion\app paths",
    r"software\microsoft\windows\currentversion\explorer",
    r"software\microsoft\windows\currentversion\policies",
    r"software\microsoft\windows nt", r"software\microsoft\windows nt\currentversion",
    r"software\policies", r"software\clients", r"software\clients\media",
    r"software\classes", r"software\classes\clsid", r"software\classes\typelib",
    r"software\classes\interface", r"software\classes\appid",
    r"software\classes\applications", r"software\classes\mimedatabase",
    r"software\classes\directory", r"software\classes\folder",
    r"software\classes\drive", r"software\classes\*",
    r"software\classes\http", r"software\classes\https", r"software\classes\ftp",
    r"software\classes\file", r"software\classes\mailto",
    r"software\classes\.exe", r"software\classes\.dll", r"software\classes\.lnk",
    r"software\classes\exefile", r"software\classes\lnkfile",
    r"system\currentcontrolset", r"system\currentcontrolset\services",
    r"system\currentcontrolset\control",
    r"software\google\chrome", r"software\google\chrome\extensions",
    r"software\microsoft\edge", r"software\microsoft\edge\extensions",
}


_OS_STATE_REG_PREFIXES = (
    r"software\microsoft\restartmanager",
    r"software\microsoft\unified store",
    r"software\microsoft\windows\currentversion\fcon",
    r"software\microsoft\gamebarapi",
    r"software\microsoft\windows\currentversion\windowproperties",
    r"software\microsoft\edgeupdate",
)
_EDGE_COMPONENT_RE = re.compile(r"\\microsoft\\edge(?:core|update|webview)?\\")
_OS_STATE_DIR_MARKERS = (
    "\\appdata\\local\\comms\\",
    "\\microsoft\\crypto\\",
)


def _is_os_state_registry(subkey: str) -> bool:
    parts = [p for p in (subkey or "").lower().split("\\") if p]
    if len(parts) >= 2 and parts[0] == "software" and parts[1] == "wow6432node":
        parts = [parts[0]] + parts[2:]
    low = "\\".join(parts)
    if any(low == k or low.startswith(k + "\\") for k in _OS_STATE_REG_PREFIXES):
        return True
    if (len(parts) >= 4 and parts[1:3] == ["microsoft", "edge"]
            and parts[3] not in ("extensions", "nativemessaginghosts")):
        return True
    return (len(parts) >= 3 and parts[-3:-1] == ["local settings", "muicache"] and parts[-1].isdigit())


def _is_os_state_path(path: str) -> bool:
    low = os.path.normcase(os.path.normpath(path)) + "\\"
    return any(m in low for m in _OS_STATE_DIR_MARKERS) or bool(_EDGE_COMPONENT_RE.search(low))


_APPX_PROGID_RE = re.compile(r"^appx[0-9a-z]{32}$")


def _is_protected_registry_key(hive: int, subkey: str) -> bool:
    parts = [p for p in (subkey or "").lower().split("\\") if p]
    if len(parts) >= 2 and parts[0] == "software" and parts[1] == "wow6432node":
        parts = [parts[0]] + parts[2:]
    if len(parts) < 2:
        return True
    if _is_os_state_registry(subkey):
        return True
    if len(parts) >= 3 and parts[1] == "classes" and parts[2].startswith("ms-"):
        return True
    if len(parts) == 3 and parts[1] == "classes" and parts[2] in _SYSTEM_CLASS_NAMES:
        return True
    if len(parts) >= 3 and parts[1] == "classes" and _APPX_PROGID_RE.match(parts[2]):
        return True
    if len(parts) >= 7 and parts[1:6] == ["microsoft", "windows", "shell", "associations", "urlassociations"] \
            and parts[6].startswith("ms-"):
        return True
    if parts[0] == "system":
        return service_name_for_subkey(hive, subkey) is None
    return "\\".join(parts) in _PROTECTED_REG_KEYS


_PROTECTED_REG_VALUE_KEYS = (
    r"software\microsoft\windows nt\currentversion\winlogon",
    r"software\microsoft\windows nt\currentversion\windows",
    r"software\microsoft\windows defender",
    r"software\microsoft\security center",
    r"software\policies\microsoft\windows defender",
    r"software\classes\component categories",
)


def _is_protected_registry_value(item: "LeftoverItem") -> bool:
    if item.kind != "registry_value":
        return False
    parts = [p for p in (item.subkey or "").lower().split("\\") if p]
    if len(parts) >= 2 and parts[0] == "software" and parts[1] == "wow6432node":
        parts = [parts[0]] + parts[2:]
    low = "\\".join(parts)
    if _is_os_state_registry(item.subkey):
        return True
    return any(low == k or low.startswith(k + "\\") for k in _PROTECTED_REG_VALUE_KEYS)


def _is_reparse_folder(path: str) -> bool:
    """Whether a FOLDER is a link (junction/symlink). Such a folder is not deleted,
    because it may have been planted to point at another folder. Files are checked
    for symlinks only (OneDrive cloud files also have the reparse attribute and are
    legitimate).
    """
    try:
        if os.path.islink(path):
            return True
        attrs = ctypes.windll.kernel32.GetFileAttributesW(ctypes.c_wchar_p(path)) & 0xFFFFFFFF
        return attrs != 0xFFFFFFFF and bool(attrs & 0x400) and bool(attrs & 0x10)
    except Exception:
        return False


_PKG_FAMILY_IN_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9.\-])([A-Za-z0-9][A-Za-z0-9.\-]{0,63})_([0-9a-hjkmnp-tv-z]{13})(?![0-9A-Za-z])")
_PKG_FULL_IN_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9.\-])([A-Za-z0-9][A-Za-z0-9.\-]{0,63})_\d+(?:\.\d+){1,3}_[A-Za-z0-9]+_[A-Za-z0-9.\-~]*"
    r"_([0-9a-hjkmnp-tv-z]{13})(?![0-9A-Za-z])")

# family name -> True if unregistered (or already gone), False if it failed.
# Cleared by every scan_leftovers() so one uninstall never inherits the state
# of another.
_package_unregister_state: dict[str, bool] = {}
_matched_package_families: set[str] = set()
_own_package_families: set[str] = set()


_PUBLISHER_DIR_RE = re.compile(r"(?:^|[\\/])publishers[\\/]([0-9a-hjkmnp-tv-z]{13})(?=[\\/]|$)")


def _package_families_in_path(path: str) -> set[str]:
    found = {f"{m.group(1)}_{m.group(2)}" for m in _PKG_FAMILY_IN_PATH_RE.finditer(path or "")}
    found |= {f"{m.group(1)}_{m.group(2)}" for m in _PKG_FULL_IN_PATH_RE.finditer(path or "")}
    found |= {f"_{m.group(1)}" for m in _PUBLISHER_DIR_RE.finditer((path or "").lower())}
    return found


def _belongs_to_other_package(path: str, app_name: str) -> bool:
    families = _package_families_in_path(path)
    if not families:
        return False
    app_tokens = set(_normalize_name_tokens(app_name)) - _WEAK_TOKENS
    for family in families:
        name = family.rsplit("_", 1)[0]
        if (family in _matched_package_families or family in _own_package_families
                or (_entry_match_tokens(name) - _WEAK_TOKENS) & app_tokens):
            return False
    return True


def _leftover_item_gone(item: LeftoverItem) -> bool:
    try:
        if item.kind in ("file", "folder"):
            return not os.path.exists(item.path)
        if item.kind == "registry":
            with winreg.OpenKey(item.hive, item.subkey, 0, winreg.KEY_READ):
                return False
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _monitor_log_item_exists(item: LeftoverItem) -> bool:
    try:
        if item.kind in ("file", "folder"):
            return os.path.lexists(item.path)
        with winreg.OpenKey(item.hive, item.subkey, 0, winreg.KEY_READ) as key:
            if item.kind == "registry_value":
                winreg.QueryValueEx(key, item.value_name or "")
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _unregister_packages_for_items(items: list[LeftoverItem], app_name: str) -> list[str]:
    errors: list[str] = []
    app_tokens = set(_normalize_name_tokens(app_name)) - _WEAK_TOKENS
    if not app_tokens:
        return errors
    from . import core_appx
    for it in items:
        if it.uncertain:
            continue
        for family in _package_families_in_path(it.path):
            if family in _package_unregister_state:
                continue
            name = family.rsplit("_", 1)[0]
            if family not in _matched_package_families and not ((_entry_match_tokens(name) - _WEAK_TOKENS) & app_tokens):
                continue
            ok, err = core_appx.unregister_package_family(family)
            _package_unregister_state[family] = ok
            if not ok:
                errors.append(T("leftover_err_package_unregister", package=family, error=err))
    return errors


# Task Scheduler tasks (TaskCache). The keys HKLM\...\Schedule\TaskCache are writable by
# SYSTEM only (ACL): even an administrator gets "Access denied" (WinError 5), and
# deleting the registry by hand would still leave the task file in System32\Tasks. So
# the task is deleted officially (schtasks /Delete), which cleans both places at once.
_TASKCACHE_PREFIX = r"software\microsoft\windows nt\currentversion\schedule\taskcache" + "\\"
_TASK_GUID_RE = re.compile(r"^\{[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\}$")


_SERVICES_SUBKEY = "SYSTEM\\CurrentControlSet\\Services"
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\- ]{1,128}$")
_SERVICE_BAD_TYPE_MASK = 0x0F
_SERVICE_PROCESS_MASK = 0x30
_SERVICE_EXISTS_MSG = "service executable still exists"
_SC_OK_CODES = (0, 1060, 1072)


def service_name_for_subkey(hive: int, subkey: str) -> Optional[str]:
    if hive != winreg.HKEY_LOCAL_MACHINE:
        return None
    sub = (subkey or "").replace("/", "\\")
    prefix = _SERVICES_SUBKEY + "\\"
    if not sub.lower().startswith(prefix.lower()):
        return None
    name = sub[len(prefix):]
    if "\\" in name or not _SERVICE_NAME_RE.match(name) or name.strip() != name:
        return None
    return name


def service_name_for_item(item: "LeftoverItem") -> Optional[str]:
    if item.kind != "registry":
        return None
    return service_name_for_subkey(item.hive, item.subkey)


def _read_service_config(name: str) -> Optional[tuple[int, str]]:
    flags = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{_SERVICES_SUBKEY}\\{name}", 0, flags) as k:
            try:
                stype, _t = winreg.QueryValueEx(k, "Type")
            except OSError:
                stype = 0
            try:
                image, _t = winreg.QueryValueEx(k, "ImagePath")
            except OSError:
                image = ""
    except OSError:
        return None
    return (stype if isinstance(stype, int) else 0), (image if isinstance(image, str) else "")


def _service_exe_path(image_path: str) -> str:
    cmd = os.path.expandvars((image_path or "").strip())
    for prefix in ("\\??\\", "\\\\?\\"):
        if cmd.startswith(prefix):
            cmd = cmd[len(prefix):]
    if cmd.lower().startswith("\\systemroot\\"):
        cmd = (os.environ.get("SystemRoot") or r"C:\Windows") + cmd[len("\\systemroot"):]
    return _extract_exe_path(cmd)


def _service_refusal(name: str) -> str:
    cfg = _read_service_config(name)
    if cfg is None:
        return "cannot read service"
    stype, image = cfg
    if stype & _SERVICE_BAD_TYPE_MASK or not stype & _SERVICE_PROCESS_MASK:
        return "refused: not an application service"
    exe = _service_exe_path(image)
    windir = os.path.normcase(os.path.normpath(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"))
    norm = os.path.normcase(exe)
    if norm == windir or norm.startswith(windir + os.sep):
        return "refused: system service"
    if exe and os.path.exists(exe):
        return _SERVICE_EXISTS_MSG
    return ""


def _remove_service_item(item: "LeftoverItem", name: str) -> tuple[str, str]:
    if _registry_key_exists(item.hive, item.subkey) is False:
        return "gone", ""
    reason = _service_refusal(name)
    if reason:
        return "error", reason
    sysroot = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    exe = os.path.join(sysroot, "System32", "sc.exe")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run([exe, "stop", name], capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=30, creationflags=flags)
        r = subprocess.run([exe, "delete", name], capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=30, creationflags=flags)
    except Exception as e:
        return "error", str(e)
    if r.returncode in _SC_OK_CODES or _registry_key_exists(item.hive, item.subkey) is False:
        return "removed", ""
    return "error", _decode_console_bytes(r.stdout or r.stderr or b"").strip()[:200] or "service could not be removed"


def _taskcache_parts(hive: int, subkey: str) -> Optional[tuple[str, str]]:
    if hive != winreg.HKEY_LOCAL_MACHINE:
        return None
    sub = (subkey or "").replace("/", "\\")
    if not sub.lower().startswith(_TASKCACHE_PREFIX):
        return None
    section, _, tail = sub[len(_TASKCACHE_PREFIX):].partition("\\")
    if not section or not tail:
        return None
    return section.lower(), tail


def _scheduled_task_path(section: str, tail: str) -> Optional[str]:
    base = _TASKCACHE_PREFIX.rstrip("\\")
    flags = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        if section == "tree":
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"Software\\Microsoft\\Windows NT\\CurrentVersion\\Schedule\\TaskCache\\Tree\\{tail}", 0, flags) as k:
                winreg.QueryValueEx(k, "Id")
            return "\\" + tail
        guid = tail.split("\\", 1)[0]
        if not _TASK_GUID_RE.match(guid):
            return None
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"Software\\Microsoft\\Windows NT\\CurrentVersion\\Schedule\\TaskCache\\Tasks\\{guid}", 0, flags) as k:
            value, _t = winreg.QueryValueEx(k, "Path")
        return value if isinstance(value, str) else None
    except OSError:
        return None


def _valid_task_path(task_path: str) -> bool:
    """Only regular application tasks. System tasks (\\Microsoft\\...) and odd paths
    are rejected: this is the last line of defense, because the program runs with
    administrator rights.
    """
    if not task_path or len(task_path) > 260 or not task_path.startswith("\\"):
        return False
    if any(ord(c) < 32 for c in task_path) or ".." in task_path or "*" in task_path:
        return False
    low = task_path.lower()
    return not any(low == p or low.startswith(p + "\\") for p in ("\\microsoft", "\\softlanding"))


def _unregister_scheduled_task(task_path: str) -> tuple[bool, str]:
    if not _valid_task_path(task_path):
        return False, "refused: not an application task"
    sysroot = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    exe = os.path.join(sysroot, "System32", "schtasks.exe")
    try:
        r = subprocess.run([exe, "/Delete", "/TN", task_path, "/F"], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return False, str(e)
    if r.returncode == 0:
        return True, ""
    return False, _decode_console_bytes(r.stderr or r.stdout or b"").strip()[:200]


def _remove_taskcache_item(item: "LeftoverItem", parts: tuple[str, str]) -> tuple[str, str]:
    if _registry_key_exists(item.hive, item.subkey) is False:
        return "gone", ""
    task_path = _scheduled_task_path(*parts)
    if task_path is None:
        if parts[0] == "tree":
            return "gone", ""
        return "error", "cannot resolve scheduled task"
    ok, msg = _unregister_scheduled_task(task_path)
    if ok or _registry_key_exists(item.hive, item.subkey) is False:
        return "removed", ""
    return "error", msg or "scheduled task could not be removed"


def _norm_cmp_path(path: str) -> str:
    return (path or "").replace("/", "\\").rstrip("\\").lower()


def _norm_reg_cmp(path: str) -> str:
    low = _norm_cmp_path(path)
    return low.replace("\\software\\wow6432node\\", "\\software\\", 1)


def _other_apps_owned_paths(app: "InstalledApp") -> tuple[set, set]:
    fs: set = set()
    reg: set = set()
    try:
        from . import core_monitor as cm
        for other in list_installed_apps(include_system=True):
            if other.uid == app.uid:
                continue
            loc = (other.install_location or "").strip().strip('"')
            if loc:
                fs.add(_norm_cmp_path(loc))
            entry = cm.find_monitor_log_for_app(other.display_name, other.key_name)
            if entry is None:
                continue
            for it in cm.load_monitor_log(entry.path):
                if it.kind in ("file", "folder"):
                    fs.add(_norm_cmp_path(it.path))
                elif it.kind in ("registry", "registry_value"):
                    reg.add(_norm_reg_cmp(it.path.split(cm._ARROW, 1)[0]))
    except Exception:
        pass
    return fs, reg


def _is_shared_with_other_apps(item: "LeftoverItem", fs_other: set, reg_other: set) -> bool:
    if item.kind in ("file", "folder"):
        mine, pool = _norm_cmp_path(item.path), fs_other
    elif item.kind == "registry":
        mine, pool = _norm_reg_cmp(item.path), reg_other
    else:
        return False
    if not mine:
        return False
    prefix = mine + "\\"
    return any(o == mine or o.startswith(prefix) for o in pool)


def _is_appx_registration_value(item: "LeftoverItem") -> bool:
    return (item.kind == "registry_value"
            and (item.subkey or "").lower().replace("\\wow6432node", "") == "software\\registeredapplications"
            and (item.value_name or "").lower().startswith("appx"))


_FIREWALL_RULES_SUBKEY = "system\\currentcontrolset\\services\\sharedaccess\\parameters\\firewallpolicy\\firewallrules"


def _firewall_rule_not_ours(item: "LeftoverItem", install_dirs: list) -> bool:
    if item.kind != "registry_value" or (item.subkey or "").lower() != _FIREWALL_RULES_SUBKEY:
        return False
    data = _read_string_value(item.hive, item.subkey, item.value_name or "")
    if not data:
        return True
    low = data.lower()
    return not any(d and d.lower().rstrip("\\") in low for d in install_dirs)


def leftover_is_blocked(item: "LeftoverItem", app_name: str) -> bool:
    try:
        if _belongs_to_other_package(item.path, app_name) or _is_appx_registration_value(item):
            return True
        if _is_protected_registry_value(item):
            return True
        if item.kind == "file":
            return os.path.islink(item.path) or not is_deletable_file_location(item.path)
        if item.kind == "folder":
            return _is_protected_dir(item.path) or _is_reparse_folder(item.path)
        if item.kind == "registry":
            if _is_protected_registry_key(item.hive, item.subkey):
                return True
            tc_parts = _taskcache_parts(item.hive, item.subkey)
            if tc_parts is not None:
                task_path = _scheduled_task_path(*tc_parts)
                if task_path is not None and not _valid_task_path(task_path):
                    return True
            svc_name = service_name_for_item(item)
            return svc_name is not None and _service_refusal(svc_name).startswith("refused")
    except Exception:
        return False
    return False


def _remove_empty_publisher_dir(path: str) -> None:
    try:
        parent = os.path.dirname(os.path.normpath(path))
        roots = {os.path.normcase(os.path.normpath(r)) for r in _candidate_folder_roots()}
        if os.path.normcase(os.path.dirname(parent)) not in roots:
            return
        if _is_protected_dir(parent) or _is_reparse_folder(parent):
            return
        if os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass


_kept_details: dict = {}


_USER_CHOICE_RE = re.compile(r"\\explorer\\fileexts\\[^\\]+\\userchoice$", re.IGNORECASE)
_USER_CHOICE_ANY_RE = re.compile(
    r"\\explorer\\fileexts\\[^\\]+\\userchoice(?:latest\\progid)?$", re.IGNORECASE)
_protected_skips: dict = {}


_WIN_ERROR_KEYS = {5: "leftover_err_access_denied", 32: "leftover_err_in_use", 33: "leftover_err_in_use"}


def _format_delete_error(path: str, exc: Exception) -> str:
    code = getattr(exc, "winerror", None)
    key = _WIN_ERROR_KEYS.get(code)
    if key:
        return f"{path}: {T(key).rstrip('.')} [WinError {code}]"
    return f"{path}: {exc}"


_key_contents: dict = {}


def _count_key_tree(hive: int, subkey: str, cap: int = 5000) -> tuple:
    n_sub = n_val = 0
    stack = [subkey]
    while stack and n_sub < cap:
        cur = stack.pop()
        subs, vals = _reg_enum(hive, cur)
        n_val += len(vals)
        n_sub += len(subs)
        stack.extend(f"{cur}\\{x}" for x in subs)
    return n_sub, n_val


def pop_key_contents(path: str) -> str:
    return _key_contents.pop(path, "")


def pop_protected_skip(path: str) -> str:
    return _protected_skips.pop(path, "")


def _delete_protected_user_choice(item: "LeftoverItem") -> bool:
    if not _USER_CHOICE_RE.search(item.subkey or ""):
        return False
    try:
        winreg.DeleteKey(item.hive, item.subkey)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def pop_kept_details(path: str) -> list:
    return _kept_details.pop(path, [])


_ASSOC_ITEM_RE = re.compile(
    r"^software\\(?:wow6432node\\)?classes\\\.[^\\]+(?:\\(?:openwithprogids|openwithlist))?$",
    re.IGNORECASE)


def _is_vendor_root_key(item: "LeftoverItem") -> bool:
    if item.kind != "registry" or item.hive not in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        return False
    parts = [p for p in (item.subkey or "").lower().split("\\") if p]
    if len(parts) >= 2 and parts[0] == "software" and parts[1] == "wow6432node":
        parts = [parts[0]] + parts[2:]
    return len(parts) == 2 and parts[0] == "software"


def deletion_order_key(item: "LeftoverItem") -> tuple:
    if item.kind == "registry" and item.shared_root:
        tier = 2
    elif item.kind == "registry" and _ASSOC_ITEM_RE.match(item.subkey or ""):
        tier = 1
    else:
        tier = 0
    return (service_name_for_item(item) is not None, tier)


def _mark_shared_roots(items: list, app: "InstalledApp") -> None:
    product = set(_product_name_tokens(
        _normalize_name_tokens(app.display_name),
        _normalize_name_tokens(getattr(app, "publisher", "") or "")))
    for it in items:
        if it.kind == "registry" and _is_vendor_root_key(it):
            key_name = it.subkey.rsplit("\\", 1)[-1]
            if not (set(_entry_match_tokens(key_name)) & product):
                it.shared_root = True


def _registry_key_is_empty(hive: int, subkey: str) -> Optional[bool]:
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
            n_sub, n_val, _ = winreg.QueryInfoKey(key)
            return n_sub == 0 and n_val == 0
    except FileNotFoundError:
        return None
    except OSError:
        return False


def _name_tokens_of_app(app_name: str) -> list[str]:
    return _strong_name_candidates(re.findall(r"[a-z0-9]+", (app_name or "").lower()))


def _assoc_name_is_ours(name: str, tokens: list[str], is_progid: bool) -> bool:
    flat = re.sub(r"[^a-z0-9]", "", name.lower())
    if any(t in flat for t in tokens):
        return True
    return is_progid and all(
        _registry_key_exists(h, "Software\\Classes\\" + name) is False
        for h in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE))


def _reg_enum(hive: int, subkey: str) -> tuple[list[str], list[tuple[str, object]]]:
    subs: list[str] = []
    vals: list[tuple[str, object]] = []
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
            i = 0
            while True:
                try:
                    subs.append(winreg.EnumKey(key, i))
                except OSError:
                    break
                i += 1
            i = 0
            while True:
                try:
                    name, data, _typ = winreg.EnumValue(key, i)
                except OSError:
                    break
                vals.append((name, data))
                i += 1
    except OSError:
        pass
    return subs, vals


def _assoc_key_own_entries(hive: int, subkey: str, app_name: str) -> tuple[list[tuple[str, str]], bool]:
    tokens = _name_tokens_of_app(app_name)
    own: list[tuple[str, str]] = []
    foreign = False
    last = subkey.rsplit("\\", 1)[-1].lower()

    def scan_list(sk: str, kind: str) -> None:
        nonlocal foreign
        _subs, vals = _reg_enum(hive, sk)
        if _subs:
            foreign = True
        for name, _data in vals:
            if kind == "list" and name.lower() == "mrulist":
                continue
            exe_name = str(_data) if kind == "list" else name
            if _assoc_name_is_ours(exe_name, tokens, is_progid=(kind == "progids")):
                own.append((sk, name))
            else:
                foreign = True

    if last == "openwithprogids":
        scan_list(subkey, "progids")
    elif last == "openwithlist":
        scan_list(subkey, "list")
    else:
        subs, vals = _reg_enum(hive, subkey)
        default = next((str(d) for n, d in vals if n == "" and isinstance(d, str)), "")
        default_ours = True
        if default:
            default_ours = _assoc_name_is_ours(default, tokens, is_progid=True)
            if default_ours:
                own.append((subkey, ""))
            else:
                foreign = True
        if default_ours:
            own.extend((subkey, n) for n, _d in vals if n.lower() in ("content type", "perceivedtype"))
        for sub in subs:
            low = sub.lower()
            if low == "openwithprogids":
                scan_list(f"{subkey}\\{sub}", "progids")
            elif low == "openwithlist":
                scan_list(f"{subkey}\\{sub}", "list")
            else:
                foreign = True
    return own, foreign


def delete_leftovers(items: list[LeftoverItem], app_name: str = "",
                      skip_registry_backup: bool = False) -> tuple[int, list[str], int]:
    ok_count = 0
    pending_reboot = 0
    errors: list[str] = []

    if not skip_registry_backup:
        _backup_path, backup_error = backup_registry_keys_for_items(items, app_name)
        if backup_error:
            errors.append(T("err_registry_backup_failed_general", error=backup_error))
    assoc_plan = [] if skip_registry_backup else plan_association_restores(items)

    errors.extend(_unregister_packages_for_items(items, app_name))

    # Services last: their executable must already be gone from disk.
    for item in sorted(items, key=deletion_order_key):
        try:
            if _belongs_to_other_package(item.path, app_name) or _is_appx_registration_value(item):
                errors.append(T("leftover_err_other_package", path=item.path))
                continue
            if _is_protected_registry_value(item):
                errors.append(T("leftover_err_protected_registry", path=item.path))
                continue
            if any(_package_unregister_state.get(f) for f in _package_families_in_path(item.path)) \
                    and _leftover_item_gone(item):
                ok_count += 1
                continue
            if item.kind == "file":
                if os.path.islink(item.path):
                    errors.append(T("leftover_err_link", path=item.path))
                    continue
                if not is_deletable_file_location(item.path):
                    errors.append(T("leftover_err_protected_folder", path=item.path))
                    continue
                if os.path.exists(item.path):
                    if not _recycle_with_retry(item.path):
                        if not item.user_data and _schedule_locked_item_for_reboot(item.path):
                            pending_reboot += 1
                        else:
                            errors.append(T("leftover_err_file_in_use", path=item.path))
                        continue
                    ok_count += 1
            elif item.kind == "folder":
                if _is_protected_dir(item.path):
                    errors.append(T("leftover_err_protected_folder", path=item.path))
                    continue
                if _is_reparse_folder(item.path):
                    errors.append(T("leftover_err_link", path=item.path))
                    continue
                if os.path.exists(item.path):
                    if not _recycle_with_retry(item.path):
                        if not item.user_data and _schedule_locked_item_for_reboot(item.path):
                            pending_reboot += 1
                        else:
                            errors.append(T("leftover_err_folder_in_use", path=item.path))
                        continue
                    ok_count += 1
                    _remove_empty_publisher_dir(item.path)
            elif item.kind == "registry":
                if _is_protected_registry_key(item.hive, item.subkey):
                    errors.append(T("leftover_err_protected_registry", path=item.path))
                    continue
                svc_name = service_name_for_item(item)
                if svc_name is not None:
                    state, msg = _remove_service_item(item, svc_name)
                    if state == "removed":
                        ok_count += 1
                    elif state == "error":
                        print(f"[leftover] service {svc_name}: {msg}", file=sys.stderr)
                        key = "leftover_err_service_exists" if msg == _SERVICE_EXISTS_MSG else "leftover_err_service"
                        errors.append(T(key, path=item.path))
                    continue
                tc_parts = _taskcache_parts(item.hive, item.subkey)
                if tc_parts is not None:
                    state, msg = _remove_taskcache_item(item, tc_parts)
                    if state == "removed":
                        ok_count += 1
                    elif state == "error":
                        errors.append(f"{item.path}: {msg}")
                    continue
                if item.backup_failed:
                    errors.append(T("leftover_err_backup_failed", path=item.path, error=item.backup_error))
                    continue
                if item.shared_root:
                    empty = _registry_key_is_empty(item.hive, item.subkey)
                    if empty is None:
                        continue
                    if empty:
                        winreg.DeleteKey(item.hive, item.subkey)
                    else:
                        subs, vals = _reg_enum(item.hive, item.subkey)
                        _kept_details[item.path] = ([f"{item.path}\\{c}" for c in subs[:8]]
                                                    or [f"{item.path} ({len(vals)} values)"])
                    ok_count += 1
                    continue
                if _ASSOC_ITEM_RE.match(item.subkey or ""):
                    own, foreign = _assoc_key_own_entries(item.hive, item.subkey, app_name)
                    if foreign:
                        for sk, name in own:
                            _delete_registry_value(item.hive, sk, name)
                        _kept_details[item.path] = [item.path]
                        ok_count += 1
                        continue
                kept: list = []
                n_sub, n_val = _count_key_tree(item.hive, item.subkey)
                if _delete_registry_key_recursive(item.hive, item.subkey, kept):
                    ok_count += 1
                    parts = ([f"{n_sub} subkey{'s' if n_sub != 1 else ''}"] if n_sub else []) \
                        + ([f"{n_val} value{'s' if n_val != 1 else ''}"] if n_val else [])
                    if parts:
                        _key_contents[item.path] = "contained " + ", ".join(parts)
                    if kept:
                        hive_name = item.path.split("\\", 1)[0]
                        _kept_details[item.path] = [f"{hive_name}\\{k}" for k in kept]
            elif item.kind == "registry_value":
                if item.backup_failed:
                    errors.append(T("leftover_err_backup_failed", path=item.path, error=item.backup_error))
                    continue
                try:
                    if _delete_registry_value(item.hive, item.subkey, item.value_name):
                        ok_count += 1
                except PermissionError:
                    if not _USER_CHOICE_ANY_RE.search(item.subkey or ""):
                        raise
                    if _delete_protected_user_choice(item):
                        ok_count += 1
                    else:
                        _protected_skips[item.path] = "protected by Windows (default app choice); Windows resets it itself"
        except Exception as e:
            errors.append(_format_delete_error(item.path, e))
    errors.extend(apply_association_restores(assoc_plan))
    return ok_count, errors, pending_reboot


def _counted_size_kb(item, folder_prefixes: list) -> int:
    if item.kind not in ("file", "folder"):
        return 0
    norm = os.path.normcase(os.path.normpath(item.path))
    if any(norm.startswith(prefix) for prefix in folder_prefixes):
        return 0
    return item.size_kb


def _delete_leftovers_tracked(items: list, app_name: str, results: list) -> tuple:
    ok_total = 0
    pending_total = 0
    freed_kb = 0
    errors: list[str] = []
    folder_prefixes = [os.path.normcase(os.path.normpath(i.path)) + os.sep
                       for i in items if i.kind == "folder"]
    for item in sorted(items, key=deletion_order_key):
        ok_count, single_errors, single_pending = delete_leftovers(
            [item], app_name=app_name, skip_registry_backup=True)
        if ok_count == 1:
            kept = pop_kept_details(item.path)
            contents = pop_key_contents(item.path)
            if kept:
                results.append(("partial", item.kind, item.path,
                                "not removed, other data remains: " + "; ".join(kept)))
            else:
                results.append(("ok", item.kind, item.path, contents))
            ok_total += 1
            freed_kb += _counted_size_kb(item, folder_prefixes)
        elif single_pending == 1:
            scheduled = pop_reboot_details(item.path)
            detail = ""
            if scheduled:
                sample = ", ".join(os.path.basename(x) for x in scheduled[:3])
                detail = (f"{len(scheduled)} locked item(s) scheduled for permanent deletion "
                          f"at restart, e.g. {sample}")
            results.append(("reboot", item.kind, item.path, detail))
            pending_total += 1
            freed_kb += _counted_size_kb(item, folder_prefixes)
        else:
            skipped = pop_protected_skip(item.path)
            if skipped:
                results.append(("skipped", item.kind, item.path, skipped))
                continue
            results.append(("error" if single_errors else "gone", item.kind, item.path,
                            "; ".join(single_errors)))
            errors.extend(single_errors)
    return ok_total, errors, pending_total, freed_kb


def _record_leftover_log(app_name: str, freed_kb: int, results: list, portable: bool = False) -> None:
    if not results:
        return
    try:
        log_file = write_uninstall_log(app_name, freed_kb, results)
        record_uninstall_history(app_name, freed_kb=freed_kb, log_file=log_file, portable=portable)
    except Exception as e:
        _log_uninstall(f"leftover log failed: {e!r}")


def delete_leftovers_logged(items: list, app_name: str = "", portable: bool = False) -> tuple:
    errors: list[str] = []
    _path, backup_error = backup_registry_keys_for_items(items, app_name)
    if backup_error:
        errors.append(T("err_registry_backup_failed_general", error=backup_error))
    assoc_plan = plan_association_restores(items)
    results: list = []
    ok_count, delete_errors, pending_reboot, freed_kb = _delete_leftovers_tracked(
        items, app_name, results)
    errors.extend(delete_errors)
    errors.extend(apply_association_restores(assoc_plan))
    _record_leftover_log(app_name, freed_kb, results, portable)
    return ok_count, errors, pending_reboot


_ASSOC_KEY_RE = re.compile(r"^software\\classes\\\.[^\\]+$", re.IGNORECASE)


@dataclass
class AssocRestore:
    hive: int
    subkey: str
    previous: str
    current: str


def _is_assoc_backup_value(item: "LeftoverItem") -> bool:
    name = (item.value_name or "").lower()
    return (item.kind == "registry_value" and bool(_ASSOC_KEY_RE.match(item.subkey or ""))
            and name.endswith(".bak") and "backup" in name)


def _read_string_value(hive: int, subkey: str, name: str) -> Optional[str]:
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
            data, typ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return data if isinstance(data, str) and typ in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else None


def promote_association_backups(items: list["LeftoverItem"]) -> None:
    certain_keys = {(it.subkey or "").lower() for it in items
                    if it.kind == "registry" and not it.uncertain}
    for it in items:
        if it.uncertain and _is_assoc_backup_value(it):
            current = _read_string_value(it.hive, it.subkey, "")
            if current and f"software\\classes\\{current}".lower() in certain_keys:
                it.uncertain = False


def plan_association_restores(items: list["LeftoverItem"]) -> list[AssocRestore]:
    """Call BEFORE removing the items. Returns only associations that certainly
    belonged to the program being removed: the default value points to a ProgID
    whose key is on the same removal list. If someone else changed the association
    (e.g. another player), nothing is touched.
    """
    removed_keys = {(it.subkey or "").lower() for it in items if it.kind == "registry"}
    plan: list[AssocRestore] = []
    seen: set = set()
    for it in items:
        if not _is_assoc_backup_value(it):
            continue
        key_id = (it.hive, it.subkey.lower())
        if key_id in seen:
            continue
        previous = _read_string_value(it.hive, it.subkey, it.value_name)
        current = _read_string_value(it.hive, it.subkey, "")
        if previous is None or not current or current == previous:
            continue
        if f"software\\classes\\{current}".lower() not in removed_keys:
            continue
        seen.add(key_id)
        plan.append(AssocRestore(it.hive, it.subkey, previous, current))
    return plan


def apply_association_restores(plan: list[AssocRestore]) -> list[str]:
    """Call AFTER removing the items. Restores the previous association only if the
    default value still points to the removed ProgID and its key is really gone
    (failed removal => untouched). Returns errors.
    """
    errors: list[str] = []
    for r in plan:
        try:
            if _read_string_value(r.hive, r.subkey, "") != r.current:
                continue
            progid_key = f"Software\\Classes\\{r.current}"
            if any(_registry_key_exists(h, progid_key) is not False
                   for h in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)):
                continue
            with winreg.OpenKey(r.hive, r.subkey, 0, winreg.KEY_SET_VALUE) as key:
                if r.previous:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, r.previous)
                else:
                    winreg.DeleteValue(key, "")
        except Exception as e:
            errors.append(f"{_HIVE_NAMES.get(r.hive, '')}\\{r.subkey}: {e}")
    return errors


_ASSOC_EXT_KEY_RE = re.compile(r"^software\\(?:wow6432node\\)?classes\\\.[^\\]+$", re.IGNORECASE)
_ASSOC_SHARED_VALUES = frozenset({"", "content type", "perceivedtype"})


def _is_shared_assoc_value(item: "LeftoverItem") -> bool:
    return (item.kind == "registry_value"
            and bool(_ASSOC_EXT_KEY_RE.match(item.subkey or ""))
            and (item.value_name or "").lower() in _ASSOC_SHARED_VALUES)


def _assoc_default_is_ours(hive: int, subkey: str, removed_keys: set) -> bool:
    current = _read_string_value(hive, subkey, "")
    if not current or "\\" in current or "/" in current:
        return False
    progid_key = subkey.rsplit("\\", 1)[0] + "\\" + current
    if progid_key.lower() in removed_keys:
        return True
    return all(_registry_key_exists(h, progid_key) is False
               for h in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE))


def drop_foreign_assoc_values(items: list["LeftoverItem"]) -> list["LeftoverItem"]:
    removed_keys = {(it.subkey or "").lower() for it in items
                    if it.kind == "registry" and not it.uncertain}
    has_backup = {(it.hive, (it.subkey or "").lower()) for it in items if _is_assoc_backup_value(it)}
    ours: dict = {}
    kept = []
    for it in items:
        if not _is_shared_assoc_value(it):
            kept.append(it)
            continue
        key_id = (it.hive, it.subkey.lower())
        if key_id not in ours:
            ours[key_id] = _assoc_default_is_ours(it.hive, it.subkey, removed_keys)
        if not ours[key_id]:
            continue
        if (it.value_name or "") == "" and key_id in has_backup:
            continue
        kept.append(it)
    return kept


def _delete_registry_key_recursive(hive: int, subkey: str, kept_out: Optional[list] = None) -> bool:
    failed: list = []

    def _walk(path: str) -> bool:
        if "\\nativemessaginghosts\\com.microsoft." in path.lower():
            if kept_out is not None:
                kept_out.append(path)
            return False
        try:
            children = []
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as key:
                i = 0
                while True:
                    try:
                        children.append(winreg.EnumKey(key, i))
                    except OSError:
                        break
                    i += 1
        except FileNotFoundError:
            return True
        except OSError as e:
            failed.append(f"{path}: {e}")
            return False
        clean = True
        for child in children:
            if not _walk(f"{path}\\{child}"):
                clean = False
        if not clean:
            return False
        try:
            winreg.DeleteKey(hive, path)
        except FileNotFoundError:
            return True
        except OSError as e:
            failed.append(f"{path}: {e}")
            return False
        return True

    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ):
            pass
    except FileNotFoundError:
        return False
    except OSError:
        pass
    if not _walk(subkey) and failed:
        raise OSError(failed[0])
    return True


def _delete_registry_value(hive: int, subkey: str, value_name: str) -> bool:
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
        return True
    except FileNotFoundError:
        return False
