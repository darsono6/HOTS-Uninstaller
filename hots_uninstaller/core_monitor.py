import ctypes
import hashlib
import json
import os
import sys
import queue
import re
import threading
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .core_uninstall import (
    LeftoverItem, _dir_size_kb, _HIVE_NAMES, _is_protected_dir, _is_protected_registry_key,
    is_deletable_file_location, service_name_for_subkey, _SERVICES_SUBKEY,
    _SERVICE_BAD_TYPE_MASK, _SERVICE_PROCESS_MASK, _MONITOR_HASH_KEY, _valid_task_path,
)

_REGISTRY_ROOTS = [
    (winreg.HKEY_CURRENT_USER, "Software", "HKCU"),
    (winreg.HKEY_LOCAL_MACHINE, "Software", "HKLM"),
]

_MAX_REG_DEPTH = 7
_MAX_FS_DEPTH = 4
_FILETIME_EPOCH_DIFF = 11644473600.0


def _registry_top_level_tasks() -> list:
    tasks = []
    for hive, base, hive_name in _REGISTRY_ROOTS:
        try:
            with winreg.OpenKey(hive, base, 0, winreg.KEY_READ) as k:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    tasks.append((hive, f"{base}\\{name}", hive_name))
        except OSError:
            pass
    return tasks


def _run_task_pool(tasks: list, task_fn, workers: int = 8) -> None:
    q = queue.Queue()
    for t in tasks:
        q.put(t)

    def _worker():
        while True:
            try:
                task = q.get_nowait()
            except queue.Empty:
                break
            task_fn(*task)

    n = min(workers, max(1, len(tasks)))
    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


_NOISE_REGISTRY_SUBSTRINGS = (
    "\\shell\\bagmru",
    "\\explorer\\sessioninfo",
    "\\currentversion\\ufh\\shc",
    "\\currentversion\\applistbackup",
    "\\explorer\\userassist",
    "\\shell\\muicache",
    "\\hostactivitymanager",
    "\\explorer\\fileexts",
    "\\explorer\\recentdocs",
    "\\explorer\\runmru",
    "\\explorer\\typedpaths",
    "\\appcompatflags\\compatibility assistant",
    "\\currentversion\\group policy\\",
    "\\windows error reporting\\termreason",
    "\\microsoft\\wbem\\tracing",
    "\\currentversion\\volatilenotifications",
    "\\currentversion\\bits",
    "\\lowregistry\\audio\\policyconfig",
    "\\currentversion\\start\\tileproperties",
    "\\component based servicing",
    "\\local settings\\mrtcache",
    "\\explorer\\featureusage",
    "\\currentversion\\irisservice",
    "\\phone\\shellui\\windowsizing",
    "\\explorer\\prelauncherrorwatcher",
    "\\notifications\\settings\\windows.systemtoast",
    "\\currentversion\\wsx",
    "\\currentversion\\mrt\\",
    "\\windowsselfhost\\fids",
    "\\ufh\\arp",
    "startuptnoti_hotsdisabled_",
    "\\shell extensions\\cached",
    "\\windows nt\\currentversion\\notifications",
    "\\currentversion\\dpx",
    "\\microsoft\\identitycrl",
    "\\currentversion\\installer\\folders",
    "microsoftcopilotautolaunch",
    "\\windowsupdate\\orchestrator",
    "\\windowsupdate\\reporting",
    "\\systemrestore\\volatile",
    "\\security center\\svc",
    "\\security and maintenance\\checks",
    "\\microsoft\\edgeupdate",
    "\\software\\microsoft\\edge\\msemx",
    "\\currentversion\\windowproperties\\",
    "\\microsoft\\copilot\\application",
    "\\securitymanager\\capauthz\\applicationsex\\",
    "\\classes\\extensions\\contractid\\",
    "\\explorer\\virtualdesktops\\",
    "\\classes\\ms-copilot",
    "\\common\\experimentconfigs",
    "\\microsoft\\windows\\shell\\bags",
    "\\microsoft\\windows\\shellnoroam\\",
    "\\microsoft\\windows\\currentversion\\explorer\\comdlg32",
    "\\microsoft\\windows\\currentversion\\explorer\\wordwheelquery",
    "\\microsoft\\windows\\currentversion\\explorer\\streammru",
    "\\microsoft\\windows\\currentversion\\explorer\\streams\\",
    "\\microsoft\\windows\\currentversion\\explorer\\mountpoints2",
    "\\microsoft\\windows\\currentversion\\explorer\\discardable",
    "\\microsoft\\windows\\currentversion\\explorer\\menuorder",
    "\\microsoft\\windows\\currentversion\\explorer\\taskband",
    "\\microsoft\\windows\\currentversion\\explorer\\traynotify",
    "\\microsoft\\windows\\currentversion\\explorer\\startpage",
    "\\microsoft\\windows\\currentversion\\explorer\\bannerstore",
    "\\microsoft\\windows\\currentversion\\cloudstore\\",
    "\\microsoft\\windows\\currentversion\\start\\",
    "\\microsoft\\windows\\currentversion\\contentdeliverymanager\\",
    "\\microsoft\\windows\\currentversion\\capabilityaccessmanager\\",
    "\\microsoft\\windows\\currentversion\\backgroundaccessapplications\\",
    "\\microsoft\\windows\\currentversion\\pushnotifications\\",
    "\\microsoft\\windows\\currentversion\\notifications\\data\\",
    "\\microsoft\\windows\\currentversion\\notifications\\settings\\windows.",
    "\\microsoft\\windows\\currentversion\\search\\",
    "\\microsoft\\windows search\\",
    "\\microsoft\\windows\\currentversion\\appmodel\\",
    "\\microsoft\\windows\\currentversion\\appx\\",
    "\\microsoft\\windows\\currentversion\\appcontainer\\",
    "\\microsoft\\windows\\currentversion\\store\\",
    "\\microsoft\\windows\\currentversion\\diagnostics\\",
    "\\microsoft\\windows\\currentversion\\deliveryoptimization\\",
    "\\microsoft\\windows\\currentversion\\settingsync\\",
    "\\microsoft\\windows\\currentversion\\immersiveshell\\",
    "\\microsoft\\windows\\currentversion\\feeds\\",
    "\\microsoft\\windows\\currentversion\\gamedvr\\",
    "\\microsoft\\windows\\currentversion\\cdp\\",
    "\\microsoft\\windows\\currentversion\\authentication\\logonui\\",
    "\\microsoft\\windows\\currentversion\\apphost\\",
    "\\microsoft\\windows\\currentversion\\deviceaccess\\",
    "\\microsoft\\windows\\currentversion\\devicemetadatastore\\",
    "\\microsoft\\windows\\currentversion\\reliability\\",
    "\\microsoft\\windows\\currentversion\\textinput\\",
    "\\microsoft\\windows\\currentversion\\internet settings\\zonemap",
    "\\microsoft\\windows\\currentversion\\internet settings\\cache",
    "\\microsoft\\windows\\currentversion\\internet settings\\5",
    "\\microsoft\\windows\\currentversion\\wintrust\\",
    "\\microsoft\\windows\\currentversion\\installer\\rollback",
    "\\microsoft\\windows\\currentversion\\installer\\inprogress",
    "\\microsoft\\windows\\currentversion\\ufh\\",
    "\\appcompatflags\\",
    "\\microsoft\\directx\\usergpupreferences",
    "\\microsoft\\systemcertificates\\",
    "\\microsoft\\enterprisecertificates\\",
    "\\microsoft\\windows defender\\",
    "\\microsoft\\sqmclient\\",
    "\\windows error reporting\\",
    "\\microsoft\\gamebar\\",
    "\\microsoft\\tracing\\",
    "\\microsoft\\input\\",
    "\\microsoft\\ctf\\",
    "\\microsoft\\inputpersonalization\\",
    "\\software\\microsoft\\onedrive\\",
    "\\currentversion\\fcon",
    "\\taskcache\\tree\\softlanding\\",
    "\\classes\\.copilot",
    "\\urlassociations\\ms-",
    "\\app paths\\copilotapp.exe",
    "\\software\\microsoft\\gamebarapi\\",
)
_NOISE_FS_SUBSTRINGS = (
    "\\user data\\",
    "\\mozilla\\firefox\\profiles\\",
    "\\microsoft\\windows\\themes\\transcodedwallpapercache",
    "\\microsoft\\windows\\caches\\",
    "\\localstate\\diagoutputdir\\",
    "\\programdata\\eset\\",
    "\\programdata\\microsoft\\windows defender\\",
    "\\programdata\\kaspersky lab",
    "\\programdata\\avast software\\",
    "\\programdata\\avg\\",
    "\\programdata\\malwarebytes\\",
    "\\programdata\\bitdefender\\",
    "\\programdata\\norton\\",
    "\\programdata\\mcafee\\",
    "\\microsoft\\windows\\inetcache\\",
    "\\microsoft\\windows\\webcache\\",
    "\\microsoft\\windows\\recent\\",
    "\\appdata\\local\\crashdumps\\",
    "\\appdata\\local\\packages\\",
    "\\eset security\\pico\\",
    "\\ebwebview\\",
    "\\appdata\\local\\temp\\",
    "\\locallow\\microsoft\\cryptneturlcache\\",
    "\\locallow\\microsoft\\internet explorer\\",
    "\\appdata\\local\\d3dscache\\",
    "\\appdata\\local\\nvidia\\dxcache\\",
    "\\appdata\\local\\nvidia\\glcache\\",
    "\\appdata\\locallow\\nvidia\\perdriverversion\\",
    "\\microsoft\\edgeupdate\\",
    "\\microsoft\\edgecore\\",
    "\\microsoft\\edge\\temp\\",
    "\\microsoft\\edge\\application\\",
    "\\microsoft\\edge beta\\application\\",
    "\\microsoft\\edge dev\\application\\",
    "\\microsoft\\edge canary\\application\\",
    "\\microsoft\\edgewebview\\application\\",
    "\\google\\chrome\\application\\",
    "\\google\\chrome beta\\application\\",
    "\\google\\chrome sxs\\application\\",
    "\\bravesoftware\\brave-browser\\application\\",
    "\\microsoft\\copilot\\application",
    "\\microsoft\\netframework\\breadcrumbstore\\",
    "\\microsoft\\windows\\actioncentercache\\",
    "\\microsoft\\crypto\\rsa\\",
    "\\microsoft\\onedrive\\",
    "\\programdata\\microsoft\\crypto\\",
    "\\programdata\\windhawk\\",
    "\\usoshared\\logs\\",
    "\\microsoft\\tokenbroker\\",
    ".rslc\\",
    "\\microsoft\\windows\\explorer\\",
    "\\microsoft\\windows\\wer\\",
    "\\microsoft\\windows\\notifications\\",
    "\\microsoft\\windows\\history\\",
    "\\microsoft\\windows\\inetcookies\\",
    "\\microsoft\\windows\\temporary internet files\\",
    "\\microsoft\\windows\\iecompatcache\\",
    "\\microsoft\\windows\\iecompatuacache\\",
    "\\microsoft\\windows\\iedownloadhistory\\",
    "\\microsoft\\windows\\privacie\\",
    "\\microsoft\\windows\\cloudstore\\",
    "\\microsoft\\windows\\pricache\\",
    "\\microsoft\\windows\\schcache\\",
    "\\microsoft\\windows\\settingsync\\",
    "\\microsoft\\windows\\deliveryoptimization\\",
    "\\microsoft\\windows\\application shortcuts\\",
    "\\microsoft\\windows\\cloudapcache\\",
    "\\microsoft\\windows\\appcompat\\",
    "\\microsoft\\windows\\capabilityaccessmanager\\",
    "\\microsoft\\windows\\clipboard\\",
    "\\microsoft\\windows\\feedback\\",
    "\\microsoft\\windows\\usrclass.dat",
    "\\microsoft\\windows\\powershell\\",
    "\\programdata\\microsoft\\search\\",
    "\\programdata\\microsoft\\diagnosis\\",
    "\\microsoft\\fontcache\\",
    "\\microsoft\\credentials\\",
    "\\microsoft\\vault\\",
    "\\microsoft\\protect\\",
    "\\appdata\\local\\assembly\\",
    "\\connecteddevicesplatform\\",
    "\\windowsapps\\",
    "\\appdata\\local\\nvidia\\",
    "\\appdata\\local\\nvidia corporation\\",
    "\\appdata\\locallow\\nvidia\\",
    "\\appdata\\local\\amd\\dxcache\\",
    "\\appdata\\local\\amd\\glcache\\",
    "\\appdata\\local\\amd\\vkcache\\",
    "\\intel\\shadercache\\",
)


_MUICACHE_SYSTEM_REF = re.compile(
    r"@(\\\\\?\\)?(%(systemroot|windir)%\\|[a-z]:\\windows\\"
    r"|[a-z]:\\program files( \(x86\))?\\common files\\system\\"
    r"|%programfiles%\\windows |[a-z]:\\program files( \(x86\))?\\windows "
    r"|[^\\/:]+,-\d+$)")


_MUICACHE_SYSTEM_DIR_REF = re.compile(
    r"(?:%(?:systemroot|windir)%|[a-z]:\\windows)\\(?:system32|syswow64),@[^\\/:,]+,-\d+$")


_APPX_PROGID_RE = re.compile(r"\\classes\\appx[0-9a-z]{32}(?:\\|$)")


def _is_noise_registry_path(full_path: str) -> bool:
    low = full_path.lower()
    if _APPX_PROGID_RE.search(low.split(_ARROW, 1)[0]):
        return True
    folded = low.replace(_ARROW, "\\")
    if any(s in folded for s in _NOISE_REGISTRY_SUBSTRINGS):
        return True
    if f"\\registeredapplications{_ARROW}appx" in low:
        return True
    if "\\applicationassociationtoasts" in low and _ARROW in low:
        value = low.split(_ARROW, 1)[1].strip()
        if value.startswith("appx") or value.startswith("windows."):
            return True
    if "muicache\\" in low and _ARROW in low:
        data = low.split(_ARROW, 1)[1]
        return (_MUICACHE_SYSTEM_REF.match(data) is not None
                or _MUICACHE_SYSTEM_DIR_REF.match(data) is not None)
    return False


_MAX_VALUE_NAME_LEN = 260


def _is_odd_value_name(name: str) -> bool:
    return len(name) > _MAX_VALUE_NAME_LEN or any(ord(c) < 32 for c in name)


def _is_noise_item(item: LeftoverItem) -> bool:
    if item.kind in ("file", "folder"):
        return _is_noise_fs_path(item.path)
    return _is_noise_registry_path(item.path)


def _is_noise_fs_path(path: str) -> bool:
    low = path.lower().rstrip("\\/")
    name = low.rsplit("\\", 1)[-1]
    if (name in ("desktop.ini", "thumbs.db", "ehthumbs.db", "ehthumbs_vista.db")
            or name.startswith("iconcache") or name.startswith("thumbcache_")):
        return True
    low = low + "\\"
    return any(s in low for s in _NOISE_FS_SUBSTRINGS)


def _filetime_to_epoch(ft: int) -> float:
    try:
        return (ft / 10_000_000.0) - _FILETIME_EPOCH_DIFF
    except Exception:
        return 0.0


def _fs_roots() -> list[str]:
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA", "APPDATA"):
        p = os.environ.get(env)
        if p:
            roots.append(p)

    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs"))
    programdata = os.environ.get("ProgramData")
    if programdata:
        roots.append(os.path.join(programdata, r"Microsoft\Windows\Start Menu\Programs"))
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        roots.append(os.path.join(userprofile, "Desktop"))
        roots.append(os.path.join(userprofile, "AppData", "LocalLow"))
    public = os.environ.get("PUBLIC")
    if public:
        roots.append(os.path.join(public, "Desktop"))

    # NOTE: "...\Start Menu\Programs" and "Desktop" MUST stay SEPARATE roots of their
    # own even though they physically live inside ProgramData/APPDATA (which are roots
    # too): every root gets its OWN depth budget (_MAX_FS_DEPTH) counted FROM it. If
    # they were removed as "subpaths" of another root, they would be reached only
    # through the ProgramData/APPDATA budget, which is already spent getting to
    # "Microsoft\Windows\Start Menu\Programs", leaving zero budget for their own
    # children (the folders of individual programs). The "before" snapshot would not see
    # them at all, and in the comparison EVERY program folder in the Start Menu, even
    # long-existing ones, would look "new": a false alarm on every installation. A
    # duplicate caused by visiting the same path twice is filtered later in _scan_fs_new
    # (see seen_paths).
    seen = set()
    result = []
    for r in roots:
        norm = os.path.normcase(os.path.normpath(r))
        if r and os.path.isdir(r) and norm not in seen:
            seen.add(norm)
            result.append(r)
    return result


_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_INVALID_PARAMETER = 87
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_GENERIC_HOST_EXE_BASENAMES = {
    "msiexec.exe", "svchost.exe", "dllhost.exe", "rundll32.exe",
    "conhost.exe", "consent.exe", "trustedinstaller.exe", "tiworker.exe",
    "dism.exe", "wusa.exe", "explorer.exe",
}


def _list_processes() -> dict:
    result: dict = {}
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE_VALUE:
        return result
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            result[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return result


def _pid_running(pid: int) -> bool:
    _kernel32.OpenProcess.restype = wintypes.HANDLE  # default c_int would truncate a 64-bit handle
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.set_last_error(0)
    h = _kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
    try:
        code = ctypes.c_ulong(0)
        if not _kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True
        return code.value == 259
    finally:
        _kernel32.CloseHandle(h)


def _track_descendants(procs: dict, roots: set, tracked: dict) -> None:
    grew = True
    while grew:
        grew = False
        for pid, (ppid, name) in procs.items():
            if pid in tracked or pid in roots or name in _GENERIC_HOST_EXE_BASENAMES:
                continue
            if ppid in roots or ppid in tracked:
                tracked[pid] = ppid
                grew = True


def _has_live_child(tracked: dict, roots: set) -> bool:
    alive = {pid for pid in tracked if _pid_running(pid)}
    return any(tracked[pid] in roots or tracked[pid] in alive for pid in alive)


def _log_monitor_info(text: str) -> None:
    try:
        print(f"[monitor] {text}", file=sys.stderr)
    except Exception:
        pass


def _log_monitor_error(where: str, exc: BaseException) -> None:
    try:
        print(f"[monitor] {where}: {exc!r}", file=sys.stderr)
    except Exception:
        pass


def wait_for_install_completion(
    root_pid: int,
    before: "MonitorSnapshot",
    installer_exe_basename: str = "",
    process_poll_interval: float = 1.0,
    activity_poll_interval: float = 5.0,
    quiet_seconds: float = 10.0,
    basename_match_window: float = 90.0,
    max_activity_wait_seconds: float = 5 * 60,
    stub_lifetime_seconds: float = 60.0,
    stub_max_items: int = 5,
    max_child_wait_seconds: float = 15 * 60,
    max_seconds: float = 45 * 60,
    should_abort: Optional[Callable[[], bool]] = None,
    stage_cb: Optional[Callable[[], None]] = None,
) -> Optional[list]:
    start = time.monotonic()

    installer_pids = {root_pid}
    descendants: dict = {}
    basename = (installer_exe_basename or "").strip().lower()

    while True:
        if should_abort is not None and should_abort():
            _log_monitor_info("etap 1: przerwano")
            return
        now = time.monotonic()
        if now - start > max_seconds:
            _log_monitor_info("etap 1: limit calkowity")
            return

        procs = _list_processes()
        _track_descendants(procs, installer_pids, descendants)

        if (basename and basename not in _GENERIC_HOST_EXE_BASENAMES
                and now - start <= basename_match_window):
            for pid, (_ppid, name) in procs.items():
                if name == basename and pid not in installer_pids:
                    installer_pids.add(pid)

        if not any(_pid_running(pid) for pid in installer_pids):
            break
        time.sleep(process_poll_interval)

    stub_mode = time.monotonic() - start <= stub_lifetime_seconds
    _log_monitor_info(f"etap 1: instalator zakonczyl sie po {time.monotonic() - start:.0f}s, "
                      f"procesow potomnych: {len(descendants)}")

    if stage_cb is not None:
        try:
            stage_cb()
        except Exception:
            pass

    stage2_start = time.monotonic()
    cap_start = stage2_start
    child_deadline = stage2_start + max_child_wait_seconds
    probe_errors = 0
    scans = 0
    timings: dict = {}
    seen: set = set()
    last_growth = stage2_start
    anchor_tokens: set = set()
    first = True
    while True:
        if should_abort is not None and should_abort():
            _log_monitor_info("etap 2: przerwano")
            return
        now = time.monotonic()
        if now - start > max_seconds:
            _log_monitor_info("etap 2: limit calkowity")
            return
        if now - cap_start > max_activity_wait_seconds:
            _log_stage2_growth("limit czasu", seen, [])
            return

        if stub_mode and not first and now < child_deadline:
            _track_descendants(_list_processes(), installer_pids, descendants)
            if _has_live_child(descendants, installer_pids):
                last_growth = now
                cap_start = now
                _sleep_unless_aborted(process_poll_interval, should_abort)
                continue

        scan_start = time.monotonic()
        try:
            current = compare_snapshot(before, compute_sizes=False, timings=timings, workers=2)
        except Exception as e:
            _log_monitor_error("etap 2: blad sondy", e)
            probe_errors += 1
            if probe_errors >= 3:
                return
            _sleep_unless_aborted(activity_poll_interval, should_abort)
            continue
        probe_errors = 0
        scan_end = time.monotonic()
        scans += 1
        if should_abort is not None and should_abort():
            return
        keys = {(it.kind, it.path.lower()) for it in current}
        added = keys - seen
        seen |= keys
        if added and not first:
            related = [k for k in added if _stage2_related(k[1], anchor_tokens)] if anchor_tokens else list(added)
            if related:
                last_growth = scan_end
                _log_stage2_growth("przyrost", seen, sorted(related))
            else:
                _log_stage2_growth("growth ignored (background)", seen, sorted(added))
        if first:
            try:
                anchor_tokens = _stage2_anchor_tokens(current, installer_exe_basename)
            except Exception as e:
                _log_monitor_error("stage 2: program keywords", e)
                anchor_tokens = set()
            last_growth = scan_end
            stub_mode = stub_mode and len(seen) <= stub_max_items
            _log_monitor_info(f"etap 2: pierwszy skan: {len(seen)} wpisow w {scan_end - scan_start:.0f}s "
                              f"(rejestr {timings.get('reg', 0):.0f}s, dysk {timings.get('fs', 0):.0f}s), "
                              f"tryb stub: {'tak' if stub_mode else 'nie'}, "
                              f"program keywords: {sorted(anchor_tokens) or 'none (any growth counts)'}")
            slow = timings.get("reg_tasks")
            if slow:
                _log_monitor_info("etap 2: najwolniejsze galezie rejestru: " + "; ".join(
                    f"{name} {sec:.1f}s/{n} kluczy" for sec, name, n in slow))
        first = False

        if scan_end - last_growth >= quiet_seconds:
            _log_monitor_info(f"etap 2: cisza przez {quiet_seconds:.0f}s, koniec po "
                              f"{time.monotonic() - stage2_start:.0f}s, skanow: {scans}, "
                              f"nowych wpisow: {len(seen)}")
            return current
        scan_duration = scan_end - scan_start
        _sleep_unless_aborted(
            max(1.0, last_growth + quiet_seconds - scan_duration - time.monotonic()),
            should_abort)


_STAGE2_GENERIC_TOKENS = frozenset({
    "microsoft", "software", "system", "program", "programs", "files", "data", "common",
    "corporation", "corp", "inc", "ltd", "llc", "the", "and", "for", "app", "apps",
})


def _stage2_anchor_tokens(items: list, installer_exe_basename: str = "") -> set:
    candidates = _new_uninstall_candidates(items)
    if len(candidates) != 1:
        return set()
    tokens = _hint_tokens(*candidates[0])
    for it in items:
        if it.kind == "folder":
            tokens |= _hint_tokens(re.split(r"[\\/]", (it.path or "").rstrip("\\/"))[-1])
    tokens |= _hint_tokens(os.path.splitext(installer_exe_basename or "")[0])
    return tokens - _STAGE2_GENERIC_TOKENS


def _stage2_related(path_low: str, tokens: set) -> bool:
    return any(t in path_low for t in tokens)


def _sleep_unless_aborted(seconds: float, should_abort: Optional[Callable[[], bool]]) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if should_abort is not None and should_abort():
            return
        time.sleep(min(0.25, max(0.0, end - time.monotonic())))


def _log_stage2_growth(reason: str, seen: set, added: list) -> None:
    try:
        sample = "; ".join(f"{k}:{p}" for k, p in added[:5])
        print(f"[monitor] etap 2 ({reason}): {len(seen)} nowych wpisow"
              + (f", doszly: {sample}" if sample else ""), file=sys.stderr)
    except Exception:
        pass


@dataclass
class MonitorSnapshot:
    started_at: float
    registry_keys: set
    fs_paths: set
    registry_values: dict = field(default_factory=dict)
    # Registry keys at the scan depth limit (_MAX_REG_DEPTH): their VALUES are stored
    # but their subkeys are NOT. Without this list the comparison treated all their
    # (already existing) subkeys as "new" as soon as the key's timestamp changed.
    registry_unexpanded: set = field(default_factory=set)
    # Folders whose CONTENT was stored in the snapshot in full. Only these may be
    # enumerated during the comparison; for the others (at the _MAX_FS_DEPTH limit)
    # everything inside would look new.
    fs_scanned: set = field(default_factory=set)
    services: set = field(default_factory=set)
    tasks: set = field(default_factory=set)

    @property
    def item_count(self) -> int:
        return len(self.registry_keys) + len(self.fs_paths)


def _scan_registry_full(max_depth: int = _MAX_REG_DEPTH,
                         progress_cb: Optional[Callable[[int], None]] = None,
                         should_abort: Optional[Callable[[], bool]] = None) -> tuple[set, dict, set]:
    lock = threading.Lock()
    result: set = set()
    values: dict = {}
    unexpanded: set = set()
    seen = [0]
    last_report = [0.0]

    def _tick():
        seen[0] += 1
        if progress_cb is None:
            return
        now = time.monotonic()
        if now - last_report[0] >= 0.1:
            last_report[0] = now
            progress_cb(seen[0])

    def _recurse_local(hive, subkey, hive_name, depth, local_result, local_values, local_unexpanded):
        if should_abort is not None and should_abort():
            return
        full = f"{hive_name}\\{subkey}"
        if _is_noise_registry_path(full):
            return
        local_result.add(full)
        _tick()
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as k:
                value_names = []
                vi = 0
                while True:
                    try:
                        vname, _data, _t = winreg.EnumValue(k, vi)
                    except OSError:
                        break
                    vi += 1
                    value_names.append(vname)
                if value_names:
                    local_values[full] = frozenset(value_names)

                if depth >= max_depth:
                    local_unexpanded.add(full)
                    return
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    _recurse_local(hive, f"{subkey}\\{name}", hive_name, depth + 1,
                                    local_result, local_values, local_unexpanded)
        except OSError:
            pass

    def _task(hive, subkey, hive_name):
        local_result: set = set()
        local_values: dict = {}
        local_unexpanded: set = set()
        _recurse_local(hive, subkey, hive_name, 1, local_result, local_values, local_unexpanded)
        with lock:
            result.update(local_result)
            values.update(local_values)
            unexpanded.update(local_unexpanded)

    for hive, base, hive_name in _REGISTRY_ROOTS:
        full = f"{hive_name}\\{base}"
        result.add(full)
        try:
            with winreg.OpenKey(hive, base, 0, winreg.KEY_READ) as k:
                value_names = []
                vi = 0
                while True:
                    try:
                        vname, _data, _t = winreg.EnumValue(k, vi)
                    except OSError:
                        break
                    vi += 1
                    value_names.append(vname)
                if value_names:
                    values[full] = frozenset(value_names)
        except OSError:
            pass

    _run_task_pool(_registry_top_level_tasks(), _task)

    if progress_cb:
        progress_cb(len(result))
    return result, values, unexpanded


def _scan_fs_full(max_depth: int = _MAX_FS_DEPTH, base_count: int = 0,
                   progress_cb: Optional[Callable[[int], None]] = None,
                   should_abort: Optional[Callable[[], bool]] = None) -> tuple[set, set]:
    result = set()
    scanned = set()
    last_report = 0.0
    for root in _fs_roots():
        result.add(root)
        base_depth = root.rstrip("\\/").count(os.sep)
        try:
            for r, dirs, files in os.walk(root):
                if should_abort is not None and should_abort():
                    return result, scanned
                if _is_noise_fs_path(r):
                    dirs[:] = []
                    continue
                depth = r.count(os.sep) - base_depth
                for d in dirs:
                    result.add(os.path.join(r, d))
                for f in files:
                    result.add(os.path.join(r, f))
                scanned.add(r)
                if depth >= max_depth:
                    dirs[:] = []
                if progress_cb:
                    now = time.monotonic()
                    if now - last_report >= 0.1:
                        last_report = now
                        progress_cb(base_count + len(result))
        except Exception:
            continue
    if progress_cb:
        progress_cb(base_count + len(result))
    return result, scanned


_PER_USER_SERVICE_RE = re.compile(r"^[A-Za-z]+(?:svc|service)_[0-9a-f]{4,8}$", re.IGNORECASE)


def _scan_services() -> dict:
    found: dict = {}
    flags = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SERVICES_SUBKEY, 0, flags) as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if _PER_USER_SERVICE_RE.match(name) or service_name_for_subkey(
                        winreg.HKEY_LOCAL_MACHINE, f"{_SERVICES_SUBKEY}\\{name}") is None:
                    continue
                try:
                    with winreg.OpenKey(root, name, 0, flags) as k:
                        stype, _t = winreg.QueryValueEx(k, "Type")
                except OSError:
                    continue
                if isinstance(stype, int) and stype & _SERVICE_PROCESS_MASK and not stype & _SERVICE_BAD_TYPE_MASK:
                    found[name.lower()] = name
    except OSError:
        pass
    return found


def _scan_services_new(before: MonitorSnapshot) -> list[LeftoverItem]:
    items = []
    for low, name in _scan_services().items():
        if low in before.services:
            continue
        subkey = f"{_SERVICES_SUBKEY}\\{name}"
        items.append(LeftoverItem(kind="registry", path=f"HKLM\\{subkey}",
                                  hive=winreg.HKEY_LOCAL_MACHINE, subkey=subkey))
    return items


def _install_location_of(raw) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    p = os.path.expandvars(raw.strip().strip('"')).rstrip("\\/")
    if not re.match(r"^[A-Za-z]:[\\/]", p) or p.endswith((" ", ".")):
        return None
    if ".." in re.split(r"[\\/]+", p):
        return None
    p = os.path.normpath(p)
    return None if os.path.dirname(p) == p else p


def _scan_install_locations(before: MonitorSnapshot, items: list[LeftoverItem],
                            compute_sizes: bool = True) -> list[LeftoverItem]:
    roots = [os.path.normcase(os.path.normpath(r)) for r in _fs_roots()]
    known = [os.path.normcase(os.path.normpath(it.path)) for it in items if it.kind == "folder"]
    result: list[LeftoverItem] = []
    for it in items:
        if it.kind != "registry":
            continue
        idx = it.subkey.lower().find(_UNINSTALL_MARKER)
        if idx == -1:
            continue
        rest = it.subkey[idx + len(_UNINSTALL_MARKER):]
        if not rest or "\\" in rest:
            continue
        try:
            with winreg.OpenKey(it.hive, it.subkey, 0, winreg.KEY_READ) as k:
                raw, _t = winreg.QueryValueEx(k, "InstallLocation")
        except OSError:
            continue
        loc = _install_location_of(raw)
        if loc is None:
            continue
        norm = os.path.normcase(loc)
        if any(norm == b or norm.startswith(b + os.sep) for b in roots + known):
            continue
        if (_is_protected_dir(loc) or _is_noise_fs_path(loc) or not os.path.isdir(loc)
                or _is_reparse_point(loc)):
            continue
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(os.path.splitdrive(loc)[0] + "\\") == 4:
                continue
            if os.path.getctime(loc) <= before.started_at:
                continue
        except Exception:
            continue
        known.append(norm)
        result.append(LeftoverItem(kind="folder", path=loc,
                                    size_kb=_dir_size_kb(loc) if compute_sizes else 0))
    return result


_TASKCACHE_TREE = r"Software\Microsoft\Windows NT\CurrentVersion\Schedule\TaskCache\Tree"
_MAX_TASK_DEPTH = 10


def _scan_tasks() -> dict:
    found: dict = {}
    flags = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)

    def walk(rel: str, depth: int) -> None:
        if depth > _MAX_TASK_DEPTH:
            return
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                f"{_TASKCACHE_TREE}\\{rel}" if rel else _TASKCACHE_TREE, 0, flags) as k:
                if rel:
                    try:
                        winreg.QueryValueEx(k, "Id")
                        found[rel.lower()] = rel
                    except OSError:
                        pass
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    walk(f"{rel}\\{name}" if rel else name, depth + 1)
        except OSError:
            pass

    walk("", 0)
    return found


def _scan_tasks_new(before: MonitorSnapshot, known: list[LeftoverItem]) -> list[LeftoverItem]:
    if not before.tasks:
        return []
    existing = [it.path.lower() for it in known
                if it.kind == "registry" and it.path.lower().startswith("hklm\\" + _TASKCACHE_TREE.lower())]
    items = []
    for low, rel in _scan_tasks().items():
        if low in before.tasks or not _valid_task_path("\\" + rel):
            continue
        subkey = f"{_TASKCACHE_TREE}\\{rel}"
        path = f"HKLM\\{subkey}"
        pl = path.lower()
        if _is_noise_registry_path(path) or any(pl == e or pl.startswith(e + "\\") for e in existing):
            continue
        items.append(LeftoverItem(kind="registry", path=path, hive=winreg.HKEY_LOCAL_MACHINE, subkey=subkey))
    return items


def take_snapshot(progress_cb: Optional[Callable[[int], None]] = None,
                   should_abort: Optional[Callable[[], bool]] = None) -> Optional[MonitorSnapshot]:
    started_at = time.time()
    lock = threading.Lock()
    counts = {"reg": 0, "fs": 0}
    results: dict = {}

    def _report():
        if progress_cb:
            with lock:
                total = counts["reg"] + counts["fs"]
            progress_cb(total)

    def _reg_progress(n):
        with lock:
            counts["reg"] = n
        _report()

    def _fs_progress(n):
        with lock:
            counts["fs"] = n
        _report()

    def _run_registry():
        results["reg"] = _scan_registry_full(progress_cb=_reg_progress, should_abort=should_abort)

    def _run_fs():
        results["fs"] = _scan_fs_full(progress_cb=_fs_progress, should_abort=should_abort)

    t_reg = threading.Thread(target=_run_registry, daemon=True)
    t_fs = threading.Thread(target=_run_fs, daemon=True)
    t_reg.start()
    t_fs.start()
    t_reg.join()
    t_fs.join()

    if should_abort is not None and should_abort():
        return None

    reg, reg_values, reg_unexpanded = results["reg"]
    fs, fs_scanned = results["fs"]
    return MonitorSnapshot(started_at=started_at, registry_keys=reg, fs_paths=fs,
                            registry_values=reg_values,
                            registry_unexpanded=reg_unexpanded, fs_scanned=fs_scanned,
                            services=set(_scan_services()), tasks=set(_scan_tasks()))


def _scan_registry_new(before: MonitorSnapshot,
                        progress_cb: Optional[Callable[[int], None]] = None,
                        stats: Optional[dict] = None,
                        workers: int = 8) -> list[LeftoverItem]:
    """IMPORTANT: a directory/key timestamp reflects ONLY changes of its DIRECT
    children (a subkey added/removed), not deeper nested changes. So no change at a
    given level does NOT excuse skipping the recursion into previously known
    subkeys; it only allows skipping the costly search for NEW subkeys at that
    level.
    """
    results = []
    lock = threading.Lock()

    children_map: dict = {}
    for full in before.registry_keys:
        if "\\" not in full:
            continue
        parent, _, _leaf = full.rpartition("\\")
        children_map.setdefault(parent, []).append(full)

    reg_values = before.registry_values or {}

    def _add(item: LeftoverItem):
        with lock:
            results.append(item)
            n = len(results)
        if progress_cb and n % 20 == 0:
            progress_cb(n)

    def _add_new_key(hive, key_full, key_subkey):
        # A new key that is a protected shared container (e.g. Classes\\Drive) cannot be
        # deleted as a whole, so report its contents instead; everything below a new
        # key is new as well.
        if not _is_protected_registry_key(hive, key_subkey):
            _add(LeftoverItem(kind="registry", path=key_full, hive=hive, subkey=key_subkey))
            return
        try:
            with winreg.OpenKey(hive, key_subkey, 0, winreg.KEY_READ) as pk:
                j = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(pk, j)
                    except OSError:
                        break
                    j += 1
                    sub_full = f"{key_full}\\{sub_name}"
                    if _is_noise_registry_path(sub_full):
                        continue
                    _add_new_key(hive, sub_full, f"{key_subkey}\\{sub_name}")
        except OSError:
            pass

    def _visit_one(hive, full, hive_name):
        if _is_noise_registry_path(full):
            return
        subkey = full.split("\\", 1)[1] if "\\" in full else ""
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as k:
                try:
                    _, _, last_modified = winreg.QueryInfoKey(k)
                    changed = _filetime_to_epoch(last_modified) > before.started_at
                except Exception:
                    changed = True

                if changed:
                    i = 0
                    expanded = full not in before.registry_unexpanded
                    while expanded:
                        try:
                            name = winreg.EnumKey(k, i)
                        except OSError:
                            break
                        i += 1
                        child_full = f"{full}\\{name}"
                        if _is_noise_registry_path(child_full):
                            continue
                        if child_full not in before.registry_keys:
                            _add_new_key(hive, child_full, f"{subkey}\\{name}")

                    if full in before.registry_keys:
                        old_values = reg_values.get(full, frozenset())
                        vi = 0
                        while True:
                            try:
                                vname, _data, _t = winreg.EnumValue(k, vi)
                            except OSError:
                                break
                            vi += 1
                            if vname in old_values or _is_odd_value_name(vname):
                                continue
                            value_path = f"{full}{_ARROW}{vname}"
                            if _is_noise_registry_path(value_path):
                                continue
                            _add(LeftoverItem(
                                kind="registry_value",
                                path=value_path,
                                hive=hive, subkey=subkey, value_name=vname,
                            ))
        except OSError:
            pass

    def _recurse(hive, full, hive_name) -> int:
        _visit_one(hive, full, hive_name)
        n = 1
        for child_full in children_map.get(full, []):
            n += _recurse(hive, child_full, hive_name)
        return n

    task_stats: list = []

    def _timed_task(hive, full, hive_name):
        t0 = time.monotonic()
        n = _recurse(hive, full, hive_name)
        with lock:
            task_stats.append((time.monotonic() - t0, full, n))

    sizes: dict = {}

    def _size(full) -> int:
        n = sizes.get(full)
        if n is None:
            n = 1 + sum(_size(c) for c in children_map.get(full, ()))
            sizes[full] = n
        return n

    pending = []
    for hive, base, hive_name in _REGISTRY_ROOTS:
        root_full = f"{hive_name}\\{base}"
        _visit_one(hive, root_full, hive_name)
        for child_full in children_map.get(root_full, []):
            pending.append((hive, child_full, hive_name))

    total_keys = sum(_size(t[1]) for t in pending)
    chunk_limit = max(2000, total_keys // 24)
    tasks = []
    while pending:
        hive, full, hive_name = pending.pop()
        kids = children_map.get(full, [])
        if kids and _size(full) > chunk_limit:
            _visit_one(hive, full, hive_name)
            pending.extend((hive, c, hive_name) for c in kids)
        else:
            tasks.append((hive, full, hive_name))
    tasks.sort(key=lambda t: _size(t[1]), reverse=True)

    _run_task_pool(tasks, _timed_task, workers)
    if stats is not None:
        stats["reg_tasks"] = sorted(task_stats, reverse=True)[:5]

    if progress_cb:
        progress_cb(len(results))
    return results


def _scan_fs_new(before: MonitorSnapshot, base_count: int = 0,
                  progress_cb: Optional[Callable[[int], None]] = None,
                  compute_sizes: bool = True) -> list[LeftoverItem]:
    results = []
    seen_paths: set = set()

    children_map: dict = {}
    for p in before.fs_scanned:
        parent = os.path.dirname(p)
        children_map.setdefault(parent, []).append(p)

    def _recurse(path):
        if _is_noise_fs_path(path):
            return
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = 0

        if mtime > before.started_at and path in before.fs_scanned:
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        ep = entry.path
                        if ep in before.fs_paths or ep in seen_paths:
                            continue
                        if _is_noise_fs_path(ep):
                            continue
                        is_dir = entry.is_dir(follow_symlinks=False)
                        if _is_protected_dir(ep) if is_dir else not is_deletable_file_location(ep):
                            continue
                        seen_paths.add(ep)
                        if is_dir:
                            size_kb = _dir_size_kb(ep) if compute_sizes else 0
                        else:
                            try:
                                size_kb = max(1, entry.stat().st_size // 1024)
                            except Exception:
                                size_kb = 0
                        results.append(LeftoverItem(kind="folder" if is_dir else "file",
                                                     path=ep, size_kb=size_kb))
                        if progress_cb and len(results) % 20 == 0:
                            progress_cb(base_count + len(results))
            except Exception:
                pass

        for child in children_map.get(path, []):
            _recurse(child)

    for root in _fs_roots():
        if root in before.fs_paths:
            _recurse(root)
        elif root not in seen_paths and not _is_protected_dir(root):
            seen_paths.add(root)
            results.append(LeftoverItem(kind="folder", path=root,
                                         size_kb=_dir_size_kb(root) if compute_sizes else 0))

    if progress_cb:
        progress_cb(base_count + len(results))
    return results


def compare_snapshot(before: MonitorSnapshot,
                      progress_cb: Optional[Callable[[int], None]] = None,
                      compute_sizes: bool = True,
                      timings: Optional[dict] = None,
                      workers: int = 8) -> list[LeftoverItem]:
    lock = threading.Lock()
    counts = {"reg": 0, "fs": 0}
    results: dict = {}

    def _report():
        if progress_cb:
            with lock:
                total = counts["reg"] + counts["fs"]
            progress_cb(total)

    def _reg_progress(n):
        with lock:
            counts["reg"] = n
        _report()

    def _fs_progress(n):
        with lock:
            counts["fs"] = n
        _report()

    def _run_registry():
        t0 = time.monotonic()
        results["reg"] = _scan_registry_new(before, progress_cb=_reg_progress, stats=timings,
                                         workers=workers)
        if timings is not None:
            timings["reg"] = time.monotonic() - t0

    def _run_fs():
        t0 = time.monotonic()
        results["fs"] = _scan_fs_new(before, progress_cb=_fs_progress, compute_sizes=compute_sizes)
        if timings is not None:
            timings["fs"] = time.monotonic() - t0

    t_reg = threading.Thread(target=_run_registry, daemon=True)
    t_fs = threading.Thread(target=_run_fs, daemon=True)
    t_reg.start()
    t_fs.start()
    t_reg.join()
    t_fs.join()

    items: list[LeftoverItem] = []
    items.extend(results["reg"])
    items.extend(results["fs"])
    items.extend(_scan_services_new(before))
    items.extend(_scan_tasks_new(before, items))
    try:
        items.extend(_scan_install_locations(before, items, compute_sizes))
    except Exception as e:
        _log_monitor_error("install locations", e)
    return items


def fill_missing_sizes(items: list[LeftoverItem]) -> None:
    for it in items:
        if it.kind == "folder" and not it.size_kb:
            it.size_kb = _dir_size_kb(it.path)


_HIVE_MAP_REVERSE = {v: k for k, v in _HIVE_NAMES.items()}


def _monitor_logs_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "HOTS Uninstaller" / "MonitorLogs"


def _monitor_index_path() -> Path:
    return _monitor_logs_dir() / "index.json"


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MAX_LOG_BYTES = 64 * 1024 * 1024


def _is_reparse_point(path) -> bool:
    """True for a symlink or a junction. If this cannot be checked, returns True
    (better to refuse writing than to write to a foreign place).
    """
    try:
        st = os.lstat(path)
    except OSError:
        return True
    return os.path.islink(path) or bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _safe_logs_dir() -> Optional[Path]:
    d = _monitor_logs_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    if _is_reparse_point(d) or _is_reparse_point(d.parent):
        return None
    return d


def _write_atomically(target: Path, data: bytes) -> None:
    """Writes to a NEW file with a random name (O_EXCL never follows a planted link),
    then os.replace: replaces the directory entry instead of overwriting whatever a
    link points to. Same rule as in the launcher.
    """
    tmp = target.with_name(f".hots_{os.getpid()}_{os.urandom(6).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_monitor_index() -> list[dict]:
    try:
        raw = _monitor_index_path().read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("path"), str)]


def _save_monitor_index(entries: list[dict]) -> bool:
    try:
        logs_dir = _safe_logs_dir()
        if logs_dir is None:
            return False
        _write_atomically(logs_dir / "index.json",
                          json.dumps(entries, indent=2, ensure_ascii=False).encode("utf-8"))
        return True
    except Exception:
        return False


def _item_to_dict(item: LeftoverItem) -> dict:
    return {
        "kind": item.kind,
        "path": item.path,
        "size_kb": item.size_kb,
        "hive": _HIVE_NAMES.get(item.hive, "") if item.hive else "",
        "subkey": item.subkey,
        "value_name": item.value_name,
    }


_ARROW = " \u2192 "


def _item_from_dict(d) -> Optional[LeftoverItem]:
    """Loads items from a log file. The file lives in the user's profile, so another
    process may have modified it, while it is deleted by an elevated program.
    Therefore None is returned (the item is skipped) when the data is inconsistent:
    - registry entries: the path SHOWN to the user must exactly match the
    hive/subkey/value_name that will actually be deleted (otherwise the log could
    show one thing and delete another),
    - files/folders: only an absolute disk path, without ".." and without UNC.
    """
    if not isinstance(d, dict):
        return None
    kind = d.get("kind", "")
    path = d.get("path", "")
    if not isinstance(path, str) or not path or kind not in ("registry", "registry_value", "file", "folder"):
        return None
    size_kb = d.get("size_kb", 0)
    if not isinstance(size_kb, int) or size_kb < 0:
        size_kb = 0

    if kind in ("file", "folder"):
        parts = re.split(r"[\\/]+", path)
        if not re.match(r"^[A-Za-z]:[\\/]", path) or ".." in parts:
            return None
        return LeftoverItem(kind=kind, path=path, size_kb=size_kb)

    hive_name = d.get("hive", "")
    subkey = d.get("subkey", "")
    value_name = d.get("value_name", "")
    if hive_name not in ("HKCU", "HKLM") or not isinstance(subkey, str) or not subkey:
        return None
    if not isinstance(value_name, str):
        return None
    if kind == "registry_value" and _is_odd_value_name(value_name):
        return None
    if subkey.startswith("\\") or subkey.endswith("\\") or "\\\\" in subkey:
        return None
    if not subkey.lower().startswith("software\\") and service_name_for_subkey(
            _HIVE_MAP_REVERSE[hive_name], subkey) is None:
        return None
    expected = f"{hive_name}\\{subkey}"
    if kind == "registry_value":
        expected += f"{_ARROW}{value_name}"
    elif value_name:
        return None
    if path != expected:
        return None
    return LeftoverItem(
        kind=kind, path=path, size_kb=size_kb,
        hive=_HIVE_MAP_REVERSE[hive_name], subkey=subkey, value_name=value_name,
    )


def save_monitor_log(app_name: str, items: list[LeftoverItem], key_name: str = "") -> str:
    if not items:
        return ""
    try:
        logs_dir = _safe_logs_dir()
        if logs_dir is None:
            return ""
        safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", app_name or "install").strip("_")[:80] or "install"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = logs_dir / f"{safe_name}_{stamp}.json"
        # The hash is computed from the SAME bytes that are written, not from a re-read
        # of the file, which someone could swap in the meantime.
        data = json.dumps([_item_to_dict(i) for i in items], indent=2, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        _write_atomically(log_path, data)
        if not _seal_monitor_log(str(log_path), digest):
            log_path.unlink(missing_ok=True)
            return ""

        entries = _load_monitor_index()
        entries.append({
            "app_name": app_name or "",
            "key_name": key_name or "",
            "path": str(log_path),
            "item_count": len(items),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        if not _save_monitor_index(entries):
            try:
                log_path.unlink(missing_ok=True)
            except OSError:
                pass
            _unseal_monitor_log(str(log_path))
            return ""
        return str(log_path)
    except Exception:
        return ""


@dataclass
class MonitorLogEntry:
    app_name: str
    path: str
    item_count: int
    created_at: str
    exists: bool = True
    key_name: str = ""


def list_monitor_logs() -> list[MonitorLogEntry]:
    entries = _load_monitor_index()
    result = []
    for e in entries:
        path = e["path"]
        created_at = e.get("created_at", "")
        count = e.get("item_count", 0)
        result.append(MonitorLogEntry(
            app_name=e.get("app_name", "") if isinstance(e.get("app_name", ""), str) else "",
            path=path,
            item_count=count if isinstance(count, int) else 0,
            created_at=created_at if isinstance(created_at, str) else "",
            exists=os.path.isfile(path),
            key_name=e.get("key_name", "") if isinstance(e.get("key_name", ""), str) else "",
        ))
    result.sort(key=lambda x: x.created_at, reverse=True)
    return result


def file_sha256(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


INSTALLER_EXTENSIONS = (".exe", ".msi")
_MSI_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


def validate_installer_path(path: str) -> tuple[bool, str]:
    p = os.path.normpath(path or "")
    low = p.lower()
    if p.startswith("\\\\"):
        return False, "monitor_drop_err_network"
    if low.endswith((".lnk", ".url")):
        return False, "monitor_drop_err_link"
    if not low.endswith(INSTALLER_EXTENSIONS):
        return False, "monitor_drop_err_type"
    if not os.path.isabs(p) or os.path.islink(p) or not os.path.isfile(p):
        return False, "monitor_drop_err_invalid"
    try:
        drive = os.path.splitdrive(p)[0] + "\\"
        if ctypes.windll.kernel32.GetDriveTypeW(drive) == 4:
            return False, "monitor_drop_err_network"
    except Exception:
        pass
    try:
        with open(p, "rb") as f:
            head = f.read(8)
    except OSError:
        return False, "monitor_drop_err_invalid"
    if low.endswith(".exe"):
        ok = head[:2] == b"MZ"
    else:
        ok = head == _MSI_SIGNATURE
    return (True, "") if ok else (False, "monitor_drop_err_invalid")


def _seal_monitor_log(path: str, digest: str) -> bool:
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, _MONITOR_HASH_KEY, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.SetValueEx(k, os.path.basename(path).lower(), 0, winreg.REG_SZ, digest)
        return True
    except OSError:
        return False


def _unseal_monitor_log(path: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MONITOR_HASH_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.DeleteValue(k, os.path.basename(path).lower())
    except OSError:
        pass


def _read_verified_log(path: str) -> Optional[bytes]:
    """Content of the log, but only if its SHA-256 matches the hash sealed in HKLM. The
    file is read ONCE and the caller parses exactly the bytes that were verified
    (two reads would leave a window for swapping the file).
    """
    try:
        with open(path, "rb") as f:
            data = f.read(_MAX_LOG_BYTES + 1)
    except OSError:
        return None
    if len(data) > _MAX_LOG_BYTES:
        return None
    digest = hashlib.sha256(data).hexdigest()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MONITOR_HASH_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            stored, _ = winreg.QueryValueEx(k, os.path.basename(path).lower())
    except OSError:
        return None
    return data if isinstance(stored, str) and stored.lower() == digest else None


def _is_in_logs_dir(path: str) -> bool:
    """Whether `path` is a .json file directly in the Monitor's logs folder. The log
    index lives in the user's profile (another process may modify it), and the
    elevated program reads and DELETES the files from its entries; without this
    check a modified index could direct deletion to any file.
    """
    try:
        base = os.path.normcase(os.path.realpath(str(_monitor_logs_dir())))
        p = os.path.normcase(os.path.realpath(path))
        return os.path.dirname(p) == base and p.endswith(".json")
    except Exception:
        return False


def load_monitor_log(path: str) -> list[LeftoverItem]:
    if not _is_in_logs_dir(path):
        return []
    raw = _read_verified_log(path)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, list):
            return []
        items = [_item_from_dict(d) for d in data]
        return [it for it in items if it is not None and not _is_noise_item(it)]
    except Exception:
        return []


def delete_monitor_log(path: str) -> tuple[bool, str]:
    try:
        if os.path.isfile(path) and _is_in_logs_dir(path):
            os.remove(path)
            _unseal_monitor_log(path)
    except OSError as e:
        return False, str(e)
    entries = _load_monitor_index()
    entries = [e for e in entries if e.get("path") != path]
    _save_monitor_index(entries)
    return True, ""


def _normalize_app_name_token(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


_UNINSTALL_MARKER = "\\currentversion\\uninstall\\"
_HINT_STOPWORDS = frozenset({
    "setup", "install", "installer", "installation", "windows", "win", "win32", "win64",
    "x64", "x86", "amd64", "arm64", "stable", "latest", "release", "web", "full",
    "offline", "msi", "exe", "bit", "update",
})


def _hint_tokens(*texts) -> set:
    out = set()
    for text in texts:
        for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
            if len(word) >= 3 and re.search(r"[a-z]", word) and word not in _HINT_STOPWORDS:
                out.add(word)
    return out


def _new_uninstall_candidates(items: list[LeftoverItem]) -> list[tuple[str, str]]:
    found: dict = {}
    for it in items:
        if it.kind != "registry":
            continue
        idx = it.subkey.lower().find(_UNINSTALL_MARKER)
        if idx == -1:
            continue
        rest = it.subkey[idx + len(_UNINSTALL_MARKER):]
        if not rest or "\\" in rest or rest.lower() in found:
            continue
        name = ""
        try:
            with winreg.OpenKey(it.hive, it.subkey, 0, winreg.KEY_READ) as k:
                value, _ = winreg.QueryValueEx(k, "DisplayName")
            name = str(value).strip() if value else ""
        except OSError:
            pass
        found[rest.lower()] = (rest, name)
    return list(found.values())


def _loose_name_tokens(app) -> set:
    from . import core_uninstall as cu
    tokens = set(cu._normalize_name_tokens(app.display_name)) | set(cu._normalize_name_tokens(app.publisher))
    return {t for t in tokens if t.isalpha() and len(t) >= 5 and t not in cu._WEAK_TOKENS}


def _drop_other_programs(items: list[LeftoverItem], chosen_key: str,
                         other_keys: list[str]) -> list[LeftoverItem]:
    from . import core_uninstall as cu
    try:
        apps = {a.key_name.lower(): a for a in cu.list_installed_apps(include_system=True)}
        chosen = apps.get(chosen_key.lower())
        others = [apps[k.lower()] for k in other_keys if k.lower() in apps]
        if chosen is None or not others:
            return items
        chosen_dirs, chosen_tokens = cu.leftover_match_context(chosen)
        chosen_ok = [cu._monitor_item_corroborated(chosen, it, chosen_dirs, chosen_tokens) for it in items]
        chosen_names = _loose_name_tokens(chosen)
        parts = [[c for c in re.split(r"[\\/]+|\u2192", it.path) if c.strip()] for it in items]
        drop = [False] * len(items)
        for other in others:
            dirs, tokens = cu.leftover_match_context(other)
            copies = [replace(it, uncertain=not cu._monitor_item_corroborated(other, it, dirs, tokens))
                      for it in items]
            cu.expand_corroboration(copies, dirs)
            loose = _loose_name_tokens(other) - chosen_names
            for i, copy in enumerate(copies):
                if chosen_ok[i] or drop[i]:
                    continue
                if not copy.uncertain or (loose and any(cu._entry_match_tokens(c) & loose for c in parts[i])):
                    drop[i] = True
    except Exception:
        return items
    return [it for i, it in enumerate(items) if not drop[i]]


def _drop_store_noise(items: list[LeftoverItem], hints) -> list[LeftoverItem]:
    from . import core_uninstall as cu
    name = " ".join(h for h in hints if h)
    kept = []
    for it in items:
        try:
            if cu._belongs_to_other_package(it.path, name) or cu._is_appx_registration_value(it):
                continue
        except Exception:
            pass
        kept.append(it)
    return kept


def attribute_install(items: list[LeftoverItem], hints) -> tuple[list[LeftoverItem], Optional[str], str]:
    items = _drop_store_noise(items, hints)
    candidates = _new_uninstall_candidates(items)
    if not candidates:
        return items, None, ""
    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        named = [c for c in candidates if c[1]] or candidates
        wanted = _hint_tokens(*hints)
        scored = sorted(
            ((sum(1 for h in wanted if any(h in c or c in h for c in _hint_tokens(name, key))), key, name)
             for key, name in named),
            key=lambda row: row[0], reverse=True)
        if scored[0][0] == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
            return items, None, ""
        chosen = (scored[0][1], scored[0][2])
    others = [key for key, _ in candidates if key.lower() != chosen[0].lower()]
    if others:
        items = _drop_other_programs(items, chosen[0], others)
    return items, (chosen[1] or None), chosen[0]


def _entry_matches_app(entry: MonitorLogEntry, token: str, key_name: str) -> bool:
    if key_name and entry.key_name:
        return entry.key_name.lower() == key_name.lower()
    return bool(token) and token == _normalize_app_name_token(entry.app_name)


def uninstall_entry_exists(key_name: str) -> bool:
    if not key_name:
        return True
    base = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\" + key_name
    views = [(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
             (winreg.HKEY_CURRENT_USER, 0)]
    for hive, flag in views:
        try:
            with winreg.OpenKey(hive, base, 0, winreg.KEY_READ | flag):
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def delete_monitor_logs_for_app(display_name: str, key_name: str = "") -> int:
    token = _normalize_app_name_token(display_name)
    removed = 0
    for entry in list_monitor_logs():
        if _entry_matches_app(entry, token, key_name):
            ok, _ = delete_monitor_log(entry.path)
            removed += 1 if ok else 0
    return removed


def prune_stale_monitor_logs() -> int:
    removed = 0
    for entry in list_monitor_logs():
        gone = not entry.exists or (entry.key_name and not uninstall_entry_exists(entry.key_name))
        if gone:
            ok, _ = delete_monitor_log(entry.path)
            removed += 1 if ok else 0
    return removed


def monitored_app_uids(apps) -> set:
    logs = [e for e in list_monitor_logs() if e.exists]
    if not logs:
        return set()
    found = set()
    for a in apps:
        token = _normalize_app_name_token(a.display_name)
        if any(_entry_matches_app(e, token, a.key_name) for e in logs):
            found.add(a.uid)
    return found


def monitored_app_dates(apps) -> dict:
    logs = [e for e in list_monitor_logs() if e.exists]
    if not logs:
        return {}
    found: dict = {}
    for a in apps:
        token = _normalize_app_name_token(a.display_name)
        matching = [e for e in logs if _entry_matches_app(e, token, a.key_name)]
        if not matching:
            continue
        dates = []
        for e in matching:
            try:
                dates.append(datetime.fromisoformat(e.created_at).date())
            except Exception:
                pass
        found[a.uid] = max(dates) if dates else None
    return found


def find_monitor_log_for_app(display_name: str, key_name: str = "") -> Optional[MonitorLogEntry]:
    token = _normalize_app_name_token(display_name)
    best = None
    for entry in list_monitor_logs():
        if not entry.exists:
            continue
        if not _entry_matches_app(entry, token, key_name):
            continue
        if best is None or entry.created_at > best.created_at:
            best = entry
    return best


def classify_items(items: list[LeftoverItem], app_name: str, key_name: str = "") -> None:
    from . import core_uninstall as cu
    app = None
    try:
        for a in cu.list_installed_apps(include_system=True):
            if (key_name and a.key_name.lower() == key_name.lower()) or \
                    (not key_name and app_name and a.display_name == app_name):
                app = a
                break
    except Exception:
        app = None
    if app is None:
        for it in items:
            it.uncertain = True
        cu.mark_user_data(items)
        return
    install_dirs_low, match_tokens = cu.leftover_match_context(app)
    for it in items:
        it.uncertain = not cu._monitor_item_corroborated(app, it, install_dirs_low, match_tokens)
    cu.promote_association_backups(items)
    cu.mark_user_data(items, install_dirs_low)
    cu.expand_corroboration(items, install_dirs_low)


_MAX_DATA_TEXT = 200


def _clean_text(value) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))[:_MAX_DATA_TEXT]


def describe_registry_value(item: LeftoverItem) -> str:
    try:
        with winreg.OpenKey(item.hive, item.subkey, 0, winreg.KEY_READ) as k:
            data, vtype = winreg.QueryValueEx(k, item.value_name)
    except OSError:
        return ""
    if isinstance(data, (bytes, bytearray)):
        head = bytes(data[:32]).hex(" ")
        return f"{len(data)} bytes: {head}" + (" ..." if len(data) > 32 else "")
    if isinstance(data, list):
        data = " | ".join(str(x) for x in data)
    return _clean_text(data)


def format_export(items: list[LeftoverItem], app_name: str) -> str:
    counts: dict = {}
    for it in items:
        counts[it.kind] = counts.get(it.kind, 0) + 1
    lines = [
        "HOTS Uninstaller - installation monitor export",
        f"Program : {_clean_text(app_name)}",
        f"Date    : {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Items   : {len(items)} (registry keys {counts.get('registry', 0)}, "
        f"values {counts.get('registry_value', 0)}, files {counts.get('file', 0)}, "
        f"folders {counts.get('folder', 0)})",
        "",
    ]
    for it in items:
        line = f"[{it.kind}] {_clean_text(it.path)}"
        if it.kind == "registry_value":
            data = describe_registry_value(it)
            if data:
                line += f"  = {data}"
        elif it.size_kb:
            line += f"  ({it.size_kb} KB)"
        if it.user_data:
            line += "  [user data]"
        elif it.uncertain:
            line += "  [uncertain]"
        lines.append(line)
    return "\n".join(lines) + "\n"
