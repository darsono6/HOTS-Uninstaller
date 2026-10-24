import json
import os
import re
import subprocess
import winreg
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from .constants import load_settings, save_settings
from .i18n import T
from .proc_utils import popen_no_busy_cursor, system_exe

_PS_LIST_CMD = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "try { $pkgs = @(Get-AppxPackage) } catch { $pkgs = @() }; "
    "$pkgs = @($pkgs | Where-Object { -not $_.IsFramework -and "
    "-not $_.IsResourcePackage -and $_.SignatureKind -ne 'System' }); "
    "$pkgs | Select-Object Name, PackageFullName, PackageFamilyName, Publisher, PublisherId, "
    "InstallLocation, Version, NonRemovable, "
    "@{Name='SignatureKind'; Expression={ $_.SignatureKind.ToString() }} "
    "| ConvertTo-Json -Compress -Depth 3"
)

_PS_LIST_CMD_ALL_USERS = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "try { $pkgs = @(Get-AppxPackage -AllUsers) } catch { $pkgs = $null }; "
    "if (-not $pkgs) { $pkgs = @(Get-AppxPackage) }; "
    "$pkgs = @($pkgs | Where-Object { -not $_.IsFramework -and "
    "-not $_.IsResourcePackage -and $_.SignatureKind -ne 'System' }); "
    "$pkgs | Select-Object Name, PackageFullName, PackageFamilyName, Publisher, PublisherId, "
    "InstallLocation, Version, NonRemovable, "
    "@{Name='SignatureKind'; Expression={ $_.SignatureKind.ToString() }}, "
    "@{Name='InstalledFor'; Expression={ @($_.PackageUserInformation | ForEach-Object { $_.ToString() }) }} "
    "| ConvertTo-Json -Compress -Depth 4"
)

_ACCOUNT_BRACKET_RE = re.compile(r"\[(.*?)\]")

_MICROSOFT_PUBLISHER_IDS = frozenset({"8wekyb3d8bbwe", "cw5n1h2txyewy"})

_GUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}$")


def _dn_value(dn: str, attr: str) -> str:
    m = re.search(r'(?:^|,\s*)' + re.escape(attr) + r'=("(?:[^"]|"")*"|[^,]*)', dn or "")
    if not m:
        return ""
    val = m.group(1).strip()
    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
        val = val[1:-1].replace('""', '"')
    return val.strip()


def _publisher_needs_manifest_lookup(publisher: str, publisher_id: str) -> bool:
    if publisher_id in _MICROSOFT_PUBLISHER_IDS:
        return False
    return "microsoft" not in (publisher or "").lower()

_WELL_KNOWN_SYSTEM_ACCOUNT_SIDS = {
    "s-1-5-18": "SYSTEM",
    "s-1-5-19": "LOCAL SERVICE",
    "s-1-5-20": "NETWORK SERVICE",
}

_PS_STARTAPPS_CMD = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "$map = @{}; "
    "Get-StartApps | ForEach-Object { "
    "$fam = ($_.AppID -split '!')[0]; "
    "if ($fam -and -not $map.ContainsKey($fam)) { $map[$fam] = $_.Name } "
    "}; "
    "$map | ConvertTo-Json -Compress -Depth 3"
)


_PS_APPX_INSTALL_LOCATIONS_CMD = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "$map = @{}; "
    "Get-AppxPackage | ForEach-Object { "
    "if ($_.PackageFamilyName -and -not $map.ContainsKey($_.PackageFamilyName)) { "
    "$map[$_.PackageFamilyName] = $_.InstallLocation "
    "} "
    "}; "
    "$map | ConvertTo-Json -Compress -Depth 3"
)


def _run_powershell(command: str, timeout: int = 30) -> tuple[bool, str]:
    # Force UTF-8 on both sides: PowerShell writes redirected (non-console) stdout in a
    # code-page dependent encoding, while subprocess.run(text=True) decodes with the
    # local default (e.g. cp1250 on Polish Windows). With non-ASCII names (e.g. from
    # Get-StartApps) this raised UnicodeDecodeError inside the subprocess reader thread,
    # which the try/except below does NOT catch, so the call silently returned
    # empty/truncated data.
    wrapped = (
        "$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        + command
    )
    try:
        result = subprocess.run(
            [system_exe("powershell.exe"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", wrapped],
            capture_output=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if result.returncode != 0:
            return False, stderr.strip()
        return True, stdout.strip()
    except Exception as e:
        return False, str(e)


_KNOWN_PACKAGE_NAMES = {
    "Microsoft.549981C3F5F10": "known_pkg_cortana",
    "Microsoft.OfficePushNotificationUtility": "known_pkg_office_push",
    "Microsoft.Office.ActionsServer": "known_pkg_office_actions",
    "Microsoft.OneDriveSync": "known_pkg_onedrive_sync",
    "aimgr": "known_pkg_office_ai_manager",
    "Microsoft.Windows.DevHome": "known_pkg_windows_advanced_settings",
}

_RISKY_CORE_PACKAGE_PREFIXES = (
    "Microsoft.WindowsStore",
    "Microsoft.MicrosoftEdge",
    "Microsoft.XboxIdentityProvider",
    "Microsoft.HEIFImageExtension",
    "Microsoft.HEVCVideoExtension",
    "Microsoft.VP9VideoExtensions",
    "Microsoft.AV1VideoExtension",
    "Microsoft.WebpImageExtension",
    "Microsoft.MPEG2VideoExtension",
    "Microsoft.WebMediaExtensions",
    "Microsoft.AVCEncoderVideoExtension",
    "Microsoft.DesktopAppInstaller",
)

_SAFE_MICROSOFT_APP_PREFIXES = (
    "Microsoft.BingWeather", "Microsoft.BingNews", "Microsoft.Todos",
    "Microsoft.WindowsCalculator", "Microsoft.WindowsCamera",
    "Microsoft.WindowsSoundRecorder", "Microsoft.WindowsAlarms",
    "Microsoft.WindowsMaps", "Microsoft.MicrosoftStickyNotes",
    "Microsoft.MicrosoftSolitaireCollection", "Microsoft.People",
    "Microsoft.ScreenSketch", "Microsoft.Paint", "Microsoft.Windows.Photos",
    "Microsoft.YourPhone", "Microsoft.ZuneMusic", "Microsoft.ZuneVideo",
    "Microsoft.GetHelp", "Microsoft.Getstarted", "Microsoft.MicrosoftOfficeHub",
    "Microsoft.549981C3F5F10", "Microsoft.Microsoft3DViewer", "Microsoft.Print3D",
    "Microsoft.WindowsFeedbackHub", "Microsoft.WindowsNotepad",
    "Microsoft.WindowsTerminal", "MSTeams", "Clipchamp.Clipchamp",
)


@dataclass
class AppxApp:
    name: str
    package_full_name: str
    package_family_name: str
    publisher: str = ""
    publisher_id: str = ""
    publisher_display_name: str = ""
    install_location: str = ""
    version: str = ""
    non_removable: bool = False
    friendly_name: str = ""
    size_kb: int = 0
    installed_for: list = field(default_factory=list)
    signature_kind: str = ""
    # True only when Get-StartApps returned any data. Without it an empty friendly_name
    # means 'unknown' (the call failed or hung), not 'the package has no tile', so
    # nothing may be hidden because of it.
    start_apps_known: bool = False
    manifest_name: str = ""

    @property
    def display_name(self) -> str:
        if self.friendly_name:
            return self.friendly_name
        if self.manifest_name:
            return self.manifest_name
        known_key = _KNOWN_PACKAGE_NAMES.get(self.name)
        if known_key:
            return T(known_key)
        n = self.name or self.package_family_name
        if "." in n and " " not in n:
            rest = n.split(".", 1)[1]
            if any(c.islower() for c in rest):
                n = rest
        return n or self.package_full_name

    @property
    def publisher_display(self) -> str:
        pub = self.publisher or ""
        cn = _dn_value(pub, "CN")
        if cn and not _GUID_RE.match(cn):
            return cn
        if self.publisher_display_name:
            return self.publisher_display_name
        return _dn_value(pub, "O") or cn or pub

    @property
    def uid(self) -> str:
        return self.package_full_name

    @property
    def is_microsoft(self) -> bool:
        if self.publisher_id in _MICROSOFT_PUBLISHER_IDS:
            return True
        return "microsoft" in f"{self.publisher} {self.publisher_display_name}".lower()

    @property
    def is_risky_core(self) -> bool:
        return self.name.startswith(_RISKY_CORE_PACKAGE_PREFIXES)

    @property
    def is_side_extension(self) -> bool:
        return (self.signature_kind == "Developer" and self.start_apps_known
                and not self.friendly_name)

    @property
    def is_uncertain_microsoft(self) -> bool:
        if self.is_risky_core or not self.is_microsoft:
            return False
        return not self.name.startswith(_SAFE_MICROSOFT_APP_PREFIXES)

    @property
    def has_no_start_entry(self) -> bool:
        return not bool(self.friendly_name)

    @property
    def is_background_component(self) -> bool:
        return (self.start_apps_known and self.has_no_start_entry
                and self.size_kb < 1024 and not self.is_risky_core)

    @property
    def is_component(self) -> bool:
        return (self.non_removable or self.is_side_extension
                or self.is_background_component or self.is_system_account_only)

    @property
    def is_system_account_only(self) -> bool:
        if not self.installed_for:
            return False
        for raw in self.installed_for:
            m = _ACCOUNT_BRACKET_RE.search(raw)
            name = (m.group(1) if m else raw).strip().lower()
            if name not in _WELL_KNOWN_SYSTEM_ACCOUNT_SIDS:
                return False
        return True

    @property
    def is_removal_blocked(self) -> bool:
        return self.non_removable or self.is_system_account_only

    @property
    def foreign_account_display(self) -> str:
        if not self.installed_for:
            return ""
        current = (os.environ.get("USERNAME") or "").strip().lower()
        if not current:
            return ""
        names = []
        for raw in self.installed_for:
            if current in raw.lower():
                return ""
            m = _ACCOUNT_BRACKET_RE.search(raw)
            name = m.group(1) if m else raw
            names.append(_WELL_KNOWN_SYSTEM_ACCOUNT_SIDS.get(name.strip().lower(), name))
        return ", ".join(names)

    @property
    def size_display(self) -> str:
        # Same formatting logic as ProgramInfo.size_display in core_uninstall.py (KB ->
        # MB/GB), deliberately duplicated instead of imported to avoid a module-level
        # circular import (core_uninstall already imports from core_appx).
        kb = self.size_kb
        if kb <= 0:
            return "—"
        if kb < 1024:
            return f"{kb} KB"
        mb = kb / 1024.0
        if mb >= 1024:
            return f"{mb / 1024.0:.2f} GB"
        return f"{mb:.1f} MB"


def resolve_manifest_display_name(loc: str, package_full_name: str) -> str:
    return _resolve_manifest_property(loc, package_full_name, "DisplayName")


def resolve_manifest_publisher_name(loc: str, package_full_name: str) -> str:
    return _resolve_manifest_property(loc, package_full_name, "PublisherDisplayName")


def _resolve_manifest_property(loc: str, package_full_name: str, tag: str) -> str:
    if not loc or not package_full_name:
        return ""
    manifest_path = os.path.join(loc, "AppxManifest.xml")
    try:
        root = ET.parse(manifest_path).getroot()
        raw = ""
        for props in root:
            if props.tag.split("}")[-1] != "Properties":
                continue
            for child in props:
                if child.tag.split("}")[-1] == tag:
                    raw = (child.text or "").strip()
                    break
            break
        if not raw:
            return ""
        if not raw.lower().startswith("ms-resource:"):
            return raw
        import ctypes
        from ctypes import wintypes
        func = ctypes.windll.shlwapi.SHLoadIndirectString
        func.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, ctypes.c_void_p]
        func.restype = ctypes.c_long
        res_id = raw[len("ms-resource:"):].lstrip("/")
        pkg_name = package_full_name.split("_", 1)[0]
        for ref in (
            raw,
            f"ms-resource://{pkg_name}/resources/{res_id}",
            f"ms-resource:///resources/{res_id}",
            f"ms-resource://{pkg_name}/{res_id}",
        ):
            buf = ctypes.create_unicode_buffer(512)
            if func(f"@{{{package_full_name}?{ref}}}", buf, 512, None) != 0:
                continue
            result = buf.value.strip()
            if result and not result.lower().startswith("ms-resource:"):
                return result
        return ""
    except Exception:
        return ""


def get_start_apps_friendly_names() -> dict:
    ok, out = _run_powershell(_PS_STARTAPPS_CMD, timeout=10)
    if not ok or not out:
        return {}
    try:
        fmap = json.loads(out)
    except Exception:
        return {}
    return {k: v for k, v in fmap.items() if isinstance(v, str)} if isinstance(fmap, dict) else {}


def get_appx_install_locations() -> Optional[dict]:
    ok, out = _run_powershell(_PS_APPX_INSTALL_LOCATIONS_CMD, timeout=15)
    if not ok or not out:
        return None
    try:
        fmap = json.loads(out)
    except Exception:
        return None
    if not isinstance(fmap, dict):
        return None
    return {k: v for k, v in fmap.items() if isinstance(v, str)}


def _appx_dir_size_kb(path: str, limit_files: int = 200000) -> int:
    # Import inside the function: core_uninstall imports from core_appx at module level,
    # so the reverse import must be lazy (as in the rest of this file).
    from .core_uninstall import _scan_dir_bytes

    try:
        total, incomplete, truncated = _scan_dir_bytes(path, limit_files)
    except Exception:
        return 0
    if truncated:
        return 0
    kb = total // 1024
    if incomplete and kb < 1024:
        return 0
    return kb


def list_appx_apps(all_users: bool = False) -> list[AppxApp]:
    cmd = _PS_LIST_CMD_ALL_USERS if all_users else _PS_LIST_CMD
    ok, out = _run_powershell(cmd, timeout=25)
    if not ok or not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]

    friendly_names = get_start_apps_friendly_names()

    apps = []

    size_cache = load_settings().get("appx_size_cache_v3", {})
    if not isinstance(size_cache, dict):
        size_cache = {}
    new_cache = {}

    for entry in data or []:
        try:
            pfn = entry.get("PackageFamilyName") or ""
            full = entry.get("PackageFullName") or ""
            loc = entry.get("InstallLocation") or ""
            if full in size_cache:
                size_kb = size_cache[full]
            else:
                size_kb = _appx_dir_size_kb(loc) if loc and os.path.isdir(loc) else 0
            new_cache[full] = size_kb
            installed_for = entry.get("InstalledFor") or []
            if not isinstance(installed_for, list):
                installed_for = [installed_for]
            friendly = friendly_names.get(pfn, "")
            manifest_name = "" if friendly else resolve_manifest_display_name(loc, full)
            pub_dn = entry.get("Publisher") or ""
            pub_id = entry.get("PublisherId") or ""
            publisher_name = (resolve_manifest_publisher_name(loc, full)
                              if _publisher_needs_manifest_lookup(pub_dn, pub_id) else "")
            apps.append(AppxApp(
                name=entry.get("Name") or "",
                package_full_name=full,
                package_family_name=pfn,
                publisher=pub_dn,
                publisher_id=pub_id,
                publisher_display_name=publisher_name,
                install_location=loc,
                version=entry.get("Version") or "",
                non_removable=bool(entry.get("NonRemovable")),
                friendly_name=friendly,
                manifest_name=manifest_name,
                size_kb=size_kb,
                installed_for=[str(x) for x in installed_for if x],
                signature_kind=entry.get("SignatureKind") or "",
                start_apps_known=bool(friendly_names),
            ))
        except Exception:
            continue

    # Reload settings right before saving: the loop above can run long (package folder
    # scan) and other parts of the program (theme, language, collapsed groups) may save
    # their changes meanwhile; writing back a stale copy would revert them.
    settings = load_settings()
    settings["appx_size_cache_v3"] = new_cache
    save_settings(settings)

    merged: dict[str, AppxApp] = {}
    order: list[str] = []
    for a in apps:
        key = a.package_full_name or a.package_family_name or a.name
        existing = merged.get(key)
        if existing is None:
            merged[key] = a
            order.append(key)
            continue
        for acc in a.installed_for:
            if acc not in existing.installed_for:
                existing.installed_for.append(acc)
        if not existing.install_location and a.install_location:
            existing.install_location = a.install_location
        if not existing.friendly_name and a.friendly_name:
            existing.friendly_name = a.friendly_name
        if not existing.manifest_name and a.manifest_name:
            existing.manifest_name = a.manifest_name
        if not existing.publisher_display_name and a.publisher_display_name:
            existing.publisher_display_name = a.publisher_display_name
        if not existing.publisher_id and a.publisher_id:
            existing.publisher_id = a.publisher_id
    apps = [merged[k] for k in order]

    apps = [a for a in apps if not a.is_system_account_only]

    apps.sort(key=lambda a: a.display_name.lower())
    return apps


def store_uri(app: AppxApp) -> str:
    return f"ms-windows-store://pdp/?PFN={app.package_family_name}"


def _registry_key_candidates(app: "AppxApp"):
    pfn = app.package_family_name
    yield (winreg.HKEY_LOCAL_MACHINE,
           r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages\{}".format(app.package_full_name))
    if pfn:
        yield (winreg.HKEY_CURRENT_USER,
               r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\SystemAppData\{}".format(pfn))
        yield (winreg.HKEY_CURRENT_USER,
               r"Software\Classes\ActivatableClasses\Package\{}".format(pfn))


def find_registry_key(app: "AppxApp"):
    for hive, subkey in _registry_key_candidates(app):
        try:
            with winreg.OpenKey(hive, subkey):
                return hive, subkey
        except OSError:
            continue
    return None


def open_registry_key(app: "AppxApp") -> bool:
    found = find_registry_key(app)
    if not found:
        return False
    hive, subkey = found
    hive_name = "HKEY_LOCAL_MACHINE" if hive == winreg.HKEY_LOCAL_MACHINE else "HKEY_CURRENT_USER"
    # NOTE: no "Computer\" prefix. It is only shown in regedit's address bar and is NOT
    # the format expected by the LastKey value (that prefix was why navigation did not
    # work at all).
    target = f"{hive_name}\\{subkey}"
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


_APPX_ERROR_HINT_KEYS = {
    "0x80070002": "err_appx_uninstall_file_not_found",
    "0x80070005": "err_appx_uninstall_access_denied",
}


def _clean_appx_error(raw: str) -> str:
    m = re.search(r"0x[0-9A-Fa-f]{8}", raw or "")
    code = m.group(0).lower() if m else None
    if code and code in _APPX_ERROR_HINT_KEYS:
        return T(_APPX_ERROR_HINT_KEYS[code])
    if code:
        return T("err_appx_uninstall_failed_code", code=code)
    return T("err_appx_uninstall_failed_generic")


def uninstall_appx_app(app: AppxApp, all_users: bool = True) -> tuple[bool, str]:
    if not app.package_full_name:
        return False, "no_package_name"
    return _remove_package_full_name(app.package_full_name, all_users)


_PACKAGE_FAMILY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,63}_[0-9a-hjkmnp-tv-z]{13}$")


def list_sideloaded_packages() -> Optional[list]:
    ok, out = _run_powershell(
        "$ErrorActionPreference = 'SilentlyContinue'; "
        "$r = @(Get-AppxPackage -AllUsers | Where-Object { -not $_.IsFramework -and -not $_.NonRemovable "
        "-and @('Developer','Enterprise','None') -contains [string]$_.SignatureKind } "
        "| Select-Object PackageFamilyName, Publisher); "
        "ConvertTo-Json -InputObject $r -Compress",
        timeout=45,
    )
    if not ok:
        return None
    try:
        data = json.loads(out) if out.strip() else []
    except Exception:
        return None
    if isinstance(data, dict):
        data = [data]
    result = []
    for entry in data if isinstance(data, list) else []:
        family = entry.get("PackageFamilyName") if isinstance(entry, dict) else None
        if isinstance(family, str) and _PACKAGE_FAMILY_RE.match(family):
            result.append({"family": family, "publisher": str(entry.get("Publisher") or "")})
    return result


def unregister_package_family(family: str) -> tuple[bool, str]:
    if not _PACKAGE_FAMILY_RE.match(family or ""):
        return False, "bad_family"
    ok, out = _run_powershell(
        "Get-AppxPackage -AllUsers | Where-Object { "
        f"$_.PackageFamilyName -eq '{family}' -and -not $_.IsFramework -and -not $_.NonRemovable "
        "-and @('Developer','Enterprise','None') -contains [string]$_.SignatureKind } "
        "| Select-Object -ExpandProperty PackageFullName",
        timeout=60,
    )
    if not ok:
        return False, _clean_appx_error(out)
    for full_name in [n.strip() for n in out.splitlines() if n.strip()]:
        done, err = _remove_package_full_name(full_name, True)
        if not done:
            return False, err
    return True, ""


def _remove_package_full_name(package_full_name: str, all_users: bool) -> tuple[bool, str]:
    quoted = package_full_name.replace("'", "''")
    scope = " -AllUsers" if all_users else ""
    ok, out = _run_powershell(f"Remove-AppxPackage -Package '{quoted}'{scope}", timeout=60)
    if not ok and all_users:
        # Observed on a Windows 10 VM: removing ANY Store package failed with 0x80070002
        # under -AllUsers, while the same code works on a regular Windows 11 machine.
        # The AllUsers store can hold orphaned references to user profiles that no
        # longer exist (typical for cloned VM images), which breaks 'for all users'
        # removal even though removal for the current user alone is possible. So retry
        # once in the narrower mode; at worst the user gets the same (sanitized) error
        # from the first attempt.
        ok2, out2 = _run_powershell(f"Remove-AppxPackage -Package '{quoted}'", timeout=60)
        if ok2:
            return True, ""
        out = out2 or out
    if not ok:
        return False, _clean_appx_error(out)
    return True, ""


def scan_appx_leftovers(app: AppxApp):
    from .core_uninstall import (
        LeftoverItem, _dir_size_kb, _normalize_name_tokens,
        _primary_match_token, _scan_prefetch_leftovers, _other_apps_tokens,
        _own_package_families,
    )

    items = []
    pfn = app.package_family_name
    _own_package_families.clear()
    if not pfn:
        return items
    _own_package_families.add(pfn)

    users_root = os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "Users")
    try:
        profile_dirs = [
            d for d in os.listdir(users_root)
            if d.lower() not in ("public", "default", "default user", "all users")
        ]
    except Exception:
        profile_dirs = []

    for profile in profile_dirs:
        pkg_dir = os.path.join(users_root, profile, "AppData", "Local", "Packages", pfn)
        if os.path.isdir(pkg_dir):
            size_kb = _dir_size_kb(pkg_dir)
            items.append(LeftoverItem(kind="folder", path=pkg_dir, size_kb=size_kb))

    registry_candidates = [
        (winreg.HKEY_CURRENT_USER,
         r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\SystemAppData\{}".format(pfn)),
        (winreg.HKEY_CURRENT_USER,
         r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\PolicyCache\{}".format(pfn)),
        (winreg.HKEY_CURRENT_USER,
         r"Software\Classes\ActivatableClasses\Package\{}".format(pfn)),
    ]
    for hive, subkey in registry_candidates:
        try:
            with winreg.OpenKey(hive, subkey):
                pass
        except FileNotFoundError:
            continue
        except Exception:
            continue
        items.append(LeftoverItem(
            kind="registry",
            path=f"HKEY_CURRENT_USER\\{subkey}",
            hive=hive,
            subkey=subkey,
        ))

    uninstall_times_key = r"Software\Microsoft\UserData\UninstallTimes"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uninstall_times_key) as k:
            winreg.QueryValueEx(k, pfn)
        items.append(LeftoverItem(
            kind="registry_value",
            path=f"HKEY_CURRENT_USER\\{uninstall_times_key} \u2192 {pfn}",
            hive=winreg.HKEY_CURRENT_USER,
            subkey=uninstall_times_key,
            value_name=pfn,
        ))
    except OSError:
        pass

    primary = _primary_match_token(_normalize_name_tokens(app.display_name))
    if primary:
        other_tokens = _other_apps_tokens("")
        try:
            for other in list_appx_apps():
                if other.package_family_name == pfn:
                    continue
                other_tokens.update(_normalize_name_tokens(other.display_name))
                other_tokens.update(_normalize_name_tokens(other.publisher))
        except Exception:
            pass
        if primary not in other_tokens:
            items.extend(_scan_prefetch_leftovers(primary))

    return items


_ICON_FAMILY_PRIORITY = (
    "square44x44logo", "square71x71logo", "square150x150logo", "square310x310logo",
)
_ICON_FAMILY_BASE_SIZE = {
    "square44x44logo": 44, "square71x71logo": 71,
    "square150x150logo": 150, "square310x310logo": 310,
}
_ICON_FALLBACK_NAMES = ("wide310x150logo", "storelogo")
_ICON_EXCLUDE_HINTS = ("targetsize-16", "targetsize-20", "targetsize-24", "altform-unplated", "altform-lightunplated")

_SCALE_RE = re.compile(r"scale-(\d+)")
_TARGETSIZE_RE = re.compile(r"targetsize-(\d+)")


def _icon_pixel_size(low_name: str, family: str) -> int:
    m = _TARGETSIZE_RE.search(low_name)
    if m:
        return int(m.group(1))
    m = _SCALE_RE.search(low_name)
    base = _ICON_FAMILY_BASE_SIZE.get(family, 44)
    if m:
        return base * int(m.group(1)) // 100
    return base


def _manifest_logo_candidates(loc: str) -> list:
    manifest_path = os.path.join(loc, "AppxManifest.xml")
    if not os.path.isfile(manifest_path):
        return []
    try:
        tree = ET.parse(manifest_path)
    except Exception:
        return []

    visual_elements = None
    for elem in tree.iter():
        if elem.tag.split("}")[-1] == "VisualElements":
            visual_elements = elem
            break
    if visual_elements is None:
        return []

    for attr_name in ("Square44x44Logo", "Square71x71Logo", "Square150x150Logo", "Square310x310Logo"):
        rel_path = visual_elements.attrib.get(attr_name)
        if not rel_path:
            continue
        family = attr_name.lower()
        rel_path = rel_path.replace("/", os.sep).replace("\\", os.sep)
        base_dir = os.path.join(loc, os.path.dirname(rel_path))
        base_name = os.path.splitext(os.path.basename(rel_path))[0].lower()
        if not os.path.isdir(base_dir):
            continue
        try:
            matches = []
            for f in os.listdir(base_dir):
                low = f.lower()
                if not low.endswith((".png", ".ico")):
                    continue
                stem = os.path.splitext(low)[0]
                if stem != base_name and not stem.startswith(base_name + "."):
                    continue
                if any(h in low for h in _ICON_EXCLUDE_HINTS):
                    continue
                matches.append(os.path.join(base_dir, f))
        except Exception:
            continue
        if matches:
            matches.sort(key=lambda p: -_icon_pixel_size(os.path.basename(p).lower(), family))
            return matches
    return []


def manifest_startup_task_ids(loc: str) -> Optional[set]:
    manifest_path = os.path.join(loc, "AppxManifest.xml")
    if not os.path.isfile(manifest_path):
        return None
    try:
        tree = ET.parse(manifest_path)
    except Exception:
        return None

    task_ids = set()
    for extension in tree.iter():
        if extension.tag.split("}")[-1] != "Extension":
            continue
        if extension.attrib.get("Category") != "windows.startupTask":
            continue
        for child in extension.iter():
            if child.tag.split("}")[-1] != "StartupTask":
                continue
            task_id = child.attrib.get("TaskId")
            if task_id:
                task_ids.add(task_id)
    return task_ids


def resolve_icon_path(app: "AppxApp") -> str:
    loc = (app.install_location or "").strip()
    if not loc or not os.path.isdir(loc):
        return ""

    manifest_candidates = _manifest_logo_candidates(loc)
    if manifest_candidates:
        return manifest_candidates[0]

    candidates = []
    try:
        base_depth = loc.rstrip("\\/").count(os.sep)
        for root, dirs, files in os.walk(loc):
            if root.count(os.sep) - base_depth > 3:
                dirs[:] = []
                continue
            for f in files:
                low = f.lower()
                if not low.endswith((".png", ".ico")):
                    continue
                if any(h in low for h in _ICON_EXCLUDE_HINTS):
                    continue
                candidates.append(os.path.join(root, f))
            if len(candidates) > 500:
                break
    except Exception:
        return ""

    if not candidates:
        return ""

    def _score(path):
        low = os.path.basename(path).lower()
        for i, name in enumerate(_ICON_FAMILY_PRIORITY):
            if name in low:
                return (0, i, -_icon_pixel_size(low, name))
        for name in _ICON_FALLBACK_NAMES:
            if name in low:
                return (1, 0, 0)
        return (2, 0, 0)

    candidates.sort(key=_score)
    return candidates[0]


def find_executable(app: "AppxApp") -> str:
    loc = (app.install_location or "").strip()
    if not loc or not os.path.isdir(loc):
        return ""
    try:
        for f in os.listdir(loc):
            if f.lower().endswith(".exe"):
                return os.path.join(loc, f)
    except Exception:
        pass
    return ""
