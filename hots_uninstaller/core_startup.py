import json
import os
import shutil
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .core_appx import _run_powershell
from . import core_appx as ca
from .i18n import T
from .proc_utils import popen_no_busy_cursor

_DISABLED_PREFIX = "_HOTSDisabled_"

_PS_LIST_TASKS_CMD = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "try { $tasks = @(Get-ScheduledTask) } catch { $tasks = @() }; "
    "$tasks = @($tasks | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' }); "
    "$tasks | ForEach-Object { "
    "$acts = @($_.Actions | ForEach-Object { (\"$($_.Execute) $($_.Arguments)\").Trim() }); "
    "[PSCustomObject]@{ "
    "Name = $_.TaskName; Path = $_.TaskPath; Enabled = [bool]$_.Settings.Enabled; "
    "Actions = ($acts -join ' ; ') "
    "} "
    "} | ConvertTo-Json -Compress -Depth 3"
)

_RUN_LOCATIONS = [
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU", "Run"),
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKCU", "RunOnce"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM", "Run"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKLM", "RunOnce"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "HKLM", "Run"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce", "HKLM", "RunOnce"),
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run", "HKCU", "Policy"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run", "HKLM", "Policy"),
]

_APPX_STARTUP_ROOT = (
    r"Software\Classes\Local Settings\Software\Microsoft\Windows"
    r"\CurrentVersion\AppModel\SystemAppData"
)


def _startup_folders() -> list[tuple[str, str]]:
    folders = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        folders.append((os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup"), "User"))
    programdata = os.environ.get("ProgramData")
    if programdata:
        folders.append((os.path.join(programdata, r"Microsoft\Windows\Start Menu\Programs\StartUp"), "Common"))
    return folders


@dataclass
class StartupItem:
    kind: str
    scope: str
    enabled: bool
    command: str = ""
    hive: int = 0
    subkey: str = ""
    raw_value_name: str = ""
    run_type: str = ""
    path: str = ""
    task_folder: str = ""
    service_display: str = ""
    package_family_name: str = ""
    appx_display_name: str = ""
    install_location: str = ""
    appx_lookup_ok: bool = False

    @property
    def display_name(self) -> str:
        if self.kind == "registry":
            n = self.raw_value_name
            if n.startswith(_DISABLED_PREFIX):
                n = n[len(_DISABLED_PREFIX):]
            return n
        if self.kind == "task":
            return self.raw_value_name
        if self.kind == "service":
            return self.service_display or self.raw_value_name
        if self.kind == "appx":
            return self.appx_display_name or self.package_family_name
        name = Path(self.path).stem
        if name.startswith(_DISABLED_PREFIX):
            name = name[len(_DISABLED_PREFIX):]
        return name

    @property
    def uid(self) -> str:
        if self.kind == "registry":
            return f"reg|{self.scope}|{self.subkey}|{self.raw_value_name}"
        if self.kind == "task":
            return f"task|{self.task_folder}|{self.raw_value_name}"
        if self.kind == "service":
            return f"service|{self.raw_value_name}"
        if self.kind == "appx":
            return f"appx|{self.package_family_name}|{self.raw_value_name}"
        return f"folder|{self.path}"

    @property
    def is_missing(self) -> bool:
        cached = self.__dict__.get("_missing_cache")
        if cached is None:
            cached = self._compute_is_missing()
            self.__dict__["_missing_cache"] = cached
        return cached

    def _compute_is_missing(self) -> bool:
        if self.kind == "appx":
            if not self.appx_lookup_ok:
                # The Get-AppxPackage query failed (timeout/error): do not guess.
                # Wrongly marking an active task as a "phantom" is a worse mistake than
                # missing a truly orphaned entry.
                return False
            return not self.install_location
        if self.kind == "task":
            return False
        if self.kind == "folder":
            return not os.path.exists(self.path)
        exe = _extract_exe_path(self.command)
        if not exe:
            return self.kind == "registry"
        if os.path.isabs(exe):
            return not os.path.exists(exe)
        return shutil.which(exe) is None


def _extract_exe_path(cmd: str) -> str:
    cmd = os.path.expandvars((cmd or "").strip())
    if not cmd:
        return ""
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        result = cmd[1:end] if end != -1 else cmd[1:]
    else:
        low = cmd.lower()
        result = None
        for ext in (".exe", ".com", ".bat", ".cmd"):
            idx = low.find(ext)
            if idx != -1:
                result = cmd[:idx + len(ext)]
                break
        if result is None:
            result = cmd.split(" ")[0]
    return os.path.normpath(result) if result else result


def _approved_location_for_item(kind: str, scope: str, subkey: str, run_type: str, raw_name: str, path: str):
    """Maps an entry to its location in the StartupApproved key, where Task Manager /
    Windows Settings really store the enabled/disabled state (the Run key alone is
    not reliable: Windows can disable an entry without touching Run). Returns (hive,
    approved_subkey, value_name), or None if Windows does not track this kind of
    entry that way (RunOnce/Policy/RunServices always run unconditionally).
    IMPORTANT: raw_name/path must be the CURRENT name of the value / file, exactly
    as it is in Run / the Startup folder right now. StartupApproved is keyed 1:1 by
    that exact name (that is how Windows checks it at logon). Stripping the
    "_HOTSDisabled_" prefix here once wrote the flag under a name that no longer
    existed in Run, so Windows treated the entry as ENABLED while the UI showed it
    as disabled.
    """
    if kind == "registry":
        if run_type != "Run":
            return None
        hive = winreg.HKEY_CURRENT_USER if scope == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        sub = "Run32" if "WOW6432Node" in subkey else "Run"
        return hive, rf"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\{sub}", raw_name
    else:
        hive = winreg.HKEY_CURRENT_USER if scope == "User" else winreg.HKEY_LOCAL_MACHINE
        fname = Path(path).name
        return hive, r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder", fname


def _read_approved_disabled(hive, approved_subkey: str, value_name: str) -> Optional[bool]:
    try:
        with winreg.OpenKey(hive, approved_subkey, 0, winreg.KEY_READ) as key:
            data, reg_type = winreg.QueryValueEx(key, value_name)
    except OSError:
        return None
    if reg_type != winreg.REG_BINARY or not data:
        return None
    return data[0] not in (0x02, 0x06)


def _write_approved(hive, approved_subkey: str, value_name: str, disable: bool) -> None:
    data = bytes([0x03 if disable else 0x02]) + bytes(11)
    key = winreg.CreateKeyEx(hive, approved_subkey, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, value_name, 0, winreg.REG_BINARY, data)
    finally:
        winreg.CloseKey(key)


_PS_LIST_SERVICES_CMD = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "$svcs = @(Get-CimInstance Win32_Service | Where-Object { $_.StartMode -ne 'Manual' }); "
    "$svcs | ForEach-Object { "
    "$exe = ''; "
    "if ($_.PathName -match '^\"([^\"]+)\"') { $exe = $Matches[1] } "
    "elseif ($_.PathName -match '^(\\S+\\.exe)') { $exe = $Matches[1] } "
    "$company = ''; "
    "if ($exe -and (Test-Path -LiteralPath $exe)) { "
    "try { $company = (Get-Item -LiteralPath $exe).VersionInfo.CompanyName } catch {} "
    "}; "
    "$isMicrosoft = $company -match 'Microsoft'; "
    "$isPackaged = $exe -match '\\\\WindowsApps\\\\'; "
    "if (-not $isMicrosoft -and -not $isPackaged) { "
    "[PSCustomObject]@{ "
    "Name = $_.Name; DisplayName = $_.DisplayName; StartMode = $_.StartMode; "
    "State = $_.State; Path = $_.PathName "
    "} "
    "} "
    "} | ConvertTo-Json -Compress -Depth 3"
)


def _reg_read_all_values(hive, subkey) -> list[tuple[str, str, int]]:
    out = []
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
            i = 0
            while True:
                try:
                    name, value, reg_type = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if reg_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                    out.append((name, str(value), reg_type))
    except OSError:
        pass
    return out


def _list_scheduled_tasks() -> list[StartupItem]:
    ok, out = _run_powershell(_PS_LIST_TASKS_CMD, timeout=20)
    if not ok or not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]

    items: list[StartupItem] = []
    for entry in data or []:
        try:
            name = entry.get("Name") or ""
            if not name:
                continue
            # Settings.Enabled (the real on/off flag), NOT .State
            # (Ready/Running/Disabled/Queued is the current EXECUTION state). A task
            # that is actively running (e.g. MSI Afterburner) has .State="Running" even
            # when it is disabled, so the toggle would falsely show "enabled" after
            # every refresh.
            enabled = bool(entry.get("Enabled", True))
            items.append(StartupItem(
                kind="task", scope="Task", enabled=enabled,
                command=(entry.get("Actions") or "").strip(),
                raw_value_name=name, task_folder=entry.get("Path") or "\\",
            ))
        except Exception:
            continue
    return items


def _list_services() -> list[StartupItem]:
    ok, out = _run_powershell(_PS_LIST_SERVICES_CMD, timeout=25)
    if not ok or not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]

    items: list[StartupItem] = []
    for entry in data or []:
        try:
            name = entry.get("Name") or ""
            if not name:
                continue
            start_mode = (entry.get("StartMode") or "").strip().lower()
            items.append(StartupItem(
                kind="service", scope="Service", enabled=(start_mode == "auto"),
                command=entry.get("Path") or "",
                raw_value_name=name, service_display=entry.get("DisplayName") or "",
            ))
        except Exception:
            continue
    return items


def _cleanup_self_healed_entries(items: list[StartupItem]) -> list[StartupItem]:
    live_registry = {
        (it.hive, it.subkey, it.raw_value_name): it
        for it in items
        if it.kind == "registry" and not it.raw_value_name.startswith(_DISABLED_PREFIX)
    }
    live_folder = {
        (os.path.dirname(it.path), os.path.basename(it.path)): it
        for it in items
        if it.kind == "folder" and not os.path.basename(it.path).startswith(_DISABLED_PREFIX)
    }

    cleaned: list[StartupItem] = []
    for it in items:
        if it.kind == "registry" and it.raw_value_name.startswith(_DISABLED_PREFIX):
            base_name = it.raw_value_name[len(_DISABLED_PREFIX):]
            live = live_registry.get((it.hive, it.subkey, base_name))
            if live is not None:
                try:
                    with winreg.OpenKey(it.hive, it.subkey, 0, winreg.KEY_ALL_ACCESS) as key:
                        winreg.DeleteValue(key, it.raw_value_name)
                except OSError:
                    pass
                loc = _approved_location_for_item("registry", live.scope, live.subkey, live.run_type, base_name, "")
                if loc is not None:
                    try:
                        _write_approved(*loc, disable=False)
                        live.enabled = True
                    except OSError:
                        pass
                continue
        elif it.kind == "folder" and os.path.basename(it.path).startswith(_DISABLED_PREFIX):
            folder = os.path.dirname(it.path)
            base_name = os.path.basename(it.path)[len(_DISABLED_PREFIX):]
            live = live_folder.get((folder, base_name))
            if live is not None:
                try:
                    os.remove(it.path)
                except OSError:
                    pass
                loc = _approved_location_for_item("folder", live.scope, "", "", "", live.path)
                if loc is not None:
                    try:
                        _write_approved(*loc, disable=False)
                        live.enabled = True
                    except OSError:
                        pass
                continue
        cleaned.append(it)
    return cleaned


def _list_appx_startup_tasks() -> list[StartupItem]:
    items: list[StartupItem] = []
    try:
        friendly_names = ca.get_start_apps_friendly_names()
    except Exception:
        friendly_names = {}

    try:
        install_locations = ca.get_appx_install_locations()
    except Exception:
        install_locations = None
    lookup_ok = install_locations is not None
    install_locations = install_locations or {}

    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _APPX_STARTUP_ROOT, 0, winreg.KEY_READ)
    except OSError:
        return items

    manifest_cache: dict[str, Optional[set]] = {}

    with root:
        i = 0
        while True:
            try:
                family = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                fam_key = winreg.OpenKey(root, family, 0, winreg.KEY_READ)
            except OSError:
                continue
            with fam_key:
                j = 0
                while True:
                    try:
                        task_id = winreg.EnumKey(fam_key, j)
                    except OSError:
                        break
                    j += 1
                    try:
                        with winreg.OpenKey(fam_key, task_id, 0, winreg.KEY_READ) as task_key:
                            state, reg_type = winreg.QueryValueEx(task_key, "State")
                    except OSError:
                        continue
                    if reg_type != winreg.REG_DWORD:
                        continue

                    loc = install_locations.get(family, "")
                    confirmed = None
                    if loc:
                        if family not in manifest_cache:
                            manifest_cache[family] = ca.manifest_startup_task_ids(loc)
                        confirmed = manifest_cache[family]

                    if confirmed is not None:
                        if task_id not in confirmed:
                            continue
                    elif state == 0:
                        continue

                    name = friendly_names.get(family) or ""
                    if not name and loc:
                        name = ca.resolve_manifest_display_name(
                            loc, os.path.basename(loc.rstrip("\\/")))
                    name = name or family.split("_")[0]
                    items.append(StartupItem(
                        kind="appx", scope="AppX", enabled=(state in (2, 4)),
                        command=family, raw_value_name=task_id,
                        package_family_name=family, appx_display_name=name,
                        install_location=loc,
                        appx_lookup_ok=lookup_ok,
                    ))
    return items


def list_local_startup_items() -> list[StartupItem]:
    items: list[StartupItem] = []

    for hive, subkey, scope, run_type in _RUN_LOCATIONS:
        for name, value, _reg_type in _reg_read_all_values(hive, subkey):
            own_enabled = not name.startswith(_DISABLED_PREFIX)
            enabled = own_enabled
            loc = _approved_location_for_item("registry", scope, subkey, run_type, name, "")
            if loc is not None:
                windows_disabled = _read_approved_disabled(*loc)
                if windows_disabled:
                    enabled = False
                elif not own_enabled:
                    try:
                        _write_approved(*loc, disable=True)
                    except OSError:
                        pass
            items.append(StartupItem(
                kind="registry", scope=scope, enabled=enabled, command=value,
                hive=hive, subkey=subkey, raw_value_name=name, run_type=run_type,
            ))

    for folder, scope in _startup_folders():
        try:
            entries = os.listdir(folder)
        except Exception:
            continue
        for entry in entries:
            if entry.lower() == "desktop.ini":
                continue
            full = os.path.join(folder, entry)
            if not os.path.isfile(full):
                continue
            own_enabled = not Path(entry).stem.startswith(_DISABLED_PREFIX)
            enabled = own_enabled
            loc = _approved_location_for_item("folder", scope, "", "", "", full)
            if loc is not None:
                windows_disabled = _read_approved_disabled(*loc)
                if windows_disabled:
                    enabled = False
                elif not own_enabled:
                    try:
                        _write_approved(*loc, disable=True)
                    except OSError:
                        pass
            items.append(StartupItem(
                kind="folder", scope=scope, enabled=enabled, command=full, path=full,
            ))

    return _cleanup_self_healed_entries(items)


def list_startup_items() -> list[StartupItem]:
    items = list_local_startup_items()
    items.extend(_list_scheduled_tasks())
    items.extend(_list_services())
    items.extend(_list_appx_startup_tasks())

    items.sort(key=lambda it: (not it.enabled, it.display_name.lower()))
    return items


def toggle_startup_item(item: StartupItem) -> tuple[bool, str]:
    disable = item.enabled
    try:
        if item.kind == "appx":
            # The same registry branch that Task Manager's Startup tab uses to
            # enable/disable such tasks (per an independent Procmon analysis). It is NOT
            # an officially documented Microsoft contract (like StartupApproved above);
            # when in doubt, verify the state in Task Manager after toggling here.
            sub = f"{_APPX_STARTUP_ROOT}\\{item.package_family_name}\\{item.raw_value_name}"
            new_state = 1 if disable else 2
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_SET_VALUE)
            try:
                winreg.SetValueEx(key, "State", 0, winreg.REG_DWORD, new_state)
            finally:
                winreg.CloseKey(key)
            return True, ""

        if item.kind == "task":
            # Tasks have their own native on/off mechanism in Task Scheduler (nothing to
            # rename, unlike Run/folder entries), so we just call the cmdlet. IMPORTANT:
            # without an explicit $ErrorActionPreference='Stop' and a state check AFTER
            # the change, a failed attempt (e.g. "Access is denied" on tasks protected
            # by stronger permissions, typical for UAC-bypass tasks created by other
            # programs) ends with exit code 0 although nothing changed: PowerShell.exe
            # does not return an error for a non-terminating cmdlet error by default.
            # The UI would then show a false success.
            verb = "Disable-ScheduledTask" if disable else "Enable-ScheduledTask"
            name_q = item.raw_value_name.replace("'", "''")
            path_q = item.task_folder.replace("'", "''")
            ok, out = _run_powershell(
                "$ErrorActionPreference = 'Stop'; "
                f"{verb} -TaskName '{name_q}' -TaskPath '{path_q}' | Out-Null; "
                f"if ((Get-ScheduledTask -TaskName '{name_q}' -TaskPath '{path_q}').Settings.Enabled) "
                "{ 'ENABLED' } else { 'DISABLED' }",
                timeout=15,
            )
            if not ok:
                low = out.lower()
                if "permissiondenied" in low or "access is denied" in low or "0x80070005" in low:
                    return False, T("err_task_toggle_access_denied")
                return False, out
            now_disabled = out.strip().upper() == "DISABLED"
            if disable and not now_disabled:
                return False, T("err_task_disable_failed")
            if not disable and now_disabled:
                return False, T("err_task_enable_failed")
            return True, ""

        if item.kind == "service":
            # "Disabling" a service from this program means startup type Disabled, NOT
            # Manual. The listing filter shows "Auto" and "Disabled" but SKIPS "Manual",
            # so with Manual the entry would vanish from the list right after being
            # disabled.
            start_type = "Disabled" if disable else "Automatic"
            name_q = item.raw_value_name.replace("'", "''")
            ok, err = _run_powershell(
                f"Set-Service -Name '{name_q}' -StartupType {start_type}", timeout=15,
            )
            return (True, "") if ok else (False, err)

        if item.kind == "registry":
            with winreg.OpenKey(item.hive, item.subkey, 0, winreg.KEY_ALL_ACCESS) as key:
                value, reg_type = winreg.QueryValueEx(key, item.raw_value_name)
                if disable:
                    new_name = _DISABLED_PREFIX + item.raw_value_name
                else:
                    new_name = item.raw_value_name[len(_DISABLED_PREFIX):] \
                        if item.raw_value_name.startswith(_DISABLED_PREFIX) else item.raw_value_name
                winreg.SetValueEx(key, new_name, 0, reg_type, value)
                winreg.DeleteValue(key, item.raw_value_name)
            approved_name, approved_path = new_name, item.path
        else:
            p = Path(item.path)
            if disable:
                new_path = p.with_name(_DISABLED_PREFIX + p.name)
            else:
                stem_name = p.name
                if stem_name.startswith(_DISABLED_PREFIX):
                    stem_name = stem_name[len(_DISABLED_PREFIX):]
                new_path = p.with_name(stem_name)
            os.rename(p, new_path)
            approved_name, approved_path = item.raw_value_name, str(new_path)

        loc = _approved_location_for_item(
            item.kind, item.scope, item.subkey, item.run_type, approved_name, approved_path,
        )
        if loc is not None:
            try:
                _write_approved(*loc, disable=disable)
            except OSError:
                pass

        if item.kind == "registry" and item.raw_value_name != new_name:
            old_loc = _approved_location_for_item(
                "registry", item.scope, item.subkey, item.run_type, item.raw_value_name, "",
            )
            if old_loc is not None:
                try:
                    with winreg.OpenKey(old_loc[0], old_loc[1], 0, winreg.KEY_SET_VALUE) as k:
                        winreg.DeleteValue(k, old_loc[2])
                except OSError:
                    pass
        return True, ""
    except Exception as e:
        return False, str(e)


def remove_startup_item(item: StartupItem) -> tuple[bool, str]:
    try:
        if item.kind == "service":
            if not item.is_missing:
                # Deleting a service (sc delete) can permanently break the application
                # it belongs to: too risky for "remove" while that application really
                # exists. The enable/disable toggle (Automatic/Disabled) does the same
                # safely and reversibly.
                return False, T("err_service_remove_blocked")
            name_q = item.raw_value_name.replace("'", "''")
            ok, out = _run_powershell(
                "$ErrorActionPreference = 'Stop'; "
                f"sc.exe delete '{name_q}' | Out-Null; "
                f"if (Get-Service -Name '{name_q}' -ErrorAction SilentlyContinue) "
                "{ 'STILL_THERE' } else { 'GONE' }",
                timeout=15,
            )
            if not ok:
                low = out.lower()
                if "access is denied" in low or "0x80070005" in low:
                    return False, T("err_service_remove_access_denied")
                return False, out
            if out.strip() != "GONE":
                return False, T("err_service_remove_failed")
            return True, ""
        if item.kind == "appx":
            return False, T("err_appx_startup_remove_blocked")
        if item.kind == "task":
            # As in toggle_startup_item: do not trust the PowerShell exit code alone
            # (Confirm:$false with no exception does not always mean the task REALLY
            # disappeared; "Delete" and "Change state" are separate permissions in the
            # task's security descriptor, so one may succeed where the other gets Access
            # denied). Verify afterwards that the task is really gone.
            name_q = item.raw_value_name.replace("'", "''")
            path_q = item.task_folder.replace("'", "''")
            ok, out = _run_powershell(
                "$ErrorActionPreference = 'Stop'; "
                f"Unregister-ScheduledTask -TaskName '{name_q}' -TaskPath '{path_q}' -Confirm:$false | Out-Null; "
                f"if (Get-ScheduledTask -TaskName '{name_q}' -TaskPath '{path_q}' -ErrorAction SilentlyContinue) "
                "{ 'STILL_THERE' } else { 'GONE' }",
                timeout=15,
            )
            if not ok:
                low = out.lower()
                if "permissiondenied" in low or "access is denied" in low or "0x80070005" in low:
                    return False, T("err_task_remove_access_denied")
                return False, out
            if out.strip() != "GONE":
                return False, T("err_task_remove_failed")
            return True, ""
        if item.kind == "registry":
            with winreg.OpenKey(item.hive, item.subkey, 0, winreg.KEY_ALL_ACCESS) as key:
                winreg.DeleteValue(key, item.raw_value_name)
            return True, ""
        else:
            os.remove(item.path)
            return True, ""
    except Exception as e:
        return False, str(e)


def open_containing_folder(item: StartupItem) -> None:
    import subprocess
    try:
        if item.kind == "folder":
            popen_no_busy_cursor(["explorer.exe", "/select,", item.path])
        elif item.kind == "appx":
            if item.install_location and os.path.exists(item.install_location):
                popen_no_busy_cursor(["explorer.exe", item.install_location])
        else:
            exe = _extract_exe_path(item.command)
            if exe and os.path.exists(exe):
                popen_no_busy_cursor(["explorer.exe", "/select,", exe])
    except Exception:
        pass


def resolve_target_path(item: StartupItem) -> str:
    if item.kind == "folder":
        return item.path
    if item.kind == "appx":
        return item.install_location
    exe = _extract_exe_path(item.command)
    return exe or item.command or ""


def task_full_path(item: StartupItem) -> str:
    folder = (item.task_folder or "\\").rstrip("\\")
    return f"{folder}\\{item.raw_value_name}" if folder else f"\\{item.raw_value_name}"


def open_task_scheduler() -> bool:
    import subprocess
    try:
        popen_no_busy_cursor(["mmc.exe", "taskschd.msc"])
        return True
    except Exception:
        return False


def open_registry_key(item: StartupItem) -> bool:
    import subprocess
    if item.kind == "registry":
        hive_name = "HKEY_CURRENT_USER" if item.hive == winreg.HKEY_CURRENT_USER else "HKEY_LOCAL_MACHINE"
        target = f"{hive_name}\\{item.subkey}"
    elif item.kind == "service":
        target = rf"HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\{item.raw_value_name}"
    elif item.kind == "appx":
        target = rf"HKEY_CURRENT_USER\{_APPX_STARTUP_ROOT}\{item.package_family_name}\{item.raw_value_name}"
    else:
        return False
    try:
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Applets\Regedit",
        ) as k:
            winreg.SetValueEx(k, "LastKey", 0, winreg.REG_SZ, target)
    except OSError:
        pass
    try:
        popen_no_busy_cursor(["regedit.exe", "/m"])
        return True
    except Exception:
        try:
            popen_no_busy_cursor(["regedit.exe"])
            return True
        except Exception:
            return False


def show_file_properties(path: str) -> bool:
    import ctypes

    if not path or not os.path.exists(path):
        return False

    class _SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("fMask", ctypes.c_ulong),
            ("hwnd", ctypes.c_void_p),
            ("lpVerb", ctypes.c_wchar_p),
            ("lpFile", ctypes.c_wchar_p),
            ("lpParameters", ctypes.c_wchar_p),
            ("lpDirectory", ctypes.c_wchar_p),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.c_wchar_p),
            ("hKeyClass", ctypes.c_void_p),
            ("dwHotKey", ctypes.c_ulong),
            ("hIcon", ctypes.c_void_p),
            ("hProcess", ctypes.c_void_p),
        ]

    SEE_MASK_INVOKEIDLIST = 0x0000000C
    info = _SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_INVOKEIDLIST
    info.lpVerb = "properties"
    info.lpFile = path
    info.nShow = 1
    try:
        return bool(ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)))
    except Exception:
        return False
