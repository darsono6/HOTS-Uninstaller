# Security Policy

[← Back to README](README.md)

## Overview

HOTS Uninstaller is an open-source Windows utility that uninstalls programs and Microsoft Store apps, finds and removes their leftover files and registry entries, manages startup entries, cleans junk files, and records what an installer adds to the system (Install Monitor).

Because these features delete files and change the registry, and because the application runs with Administrator privileges, security issues are taken seriously.

The source code is publicly available in this repository.

## Reporting a vulnerability

If you believe you have found a security vulnerability in HOTS Uninstaller, please **do not disclose sensitive details publicly before contacting the maintainer**.

Please report security issues privately by email:

**hots.support@gmail.com**

Where possible, include:

- HOTS Uninstaller version
- Windows version
- affected feature or component
- clear description of the issue
- steps to reproduce
- expected and actual behavior
- screenshots, logs, or proof-of-concept information if relevant

Please do not include passwords, personal data, or other sensitive information that is not necessary to reproduce the issue.

## What should be reported

Examples of security issues include:

- unintended privilege escalation
- arbitrary code execution
- deleting, overwriting or restoring files or registry keys outside what the user selected, for example by bypassing the protected-folder and protected-registry-key rules, or by abusing links, junctions or symlinks
- tampering with registry backups, monitoring logs, the uninstall history or the integrity checks that protect them
- command injection through program names, paths or other data that ends up in a command
- unsafe handling of Administrator privileges, including how installers and uninstallers are started
- insecure file or configuration handling in `%APPDATA%\HOTS Uninstaller`
- weaknesses in the setup or uninstall scripts
- vulnerabilities that could allow another local application or user to abuse HOTS Uninstaller

General bugs, usability problems and feature requests can be reported through GitHub Issues instead. So can leftover-detection mistakes that do **not** involve a protected item: the leftover scan is based on heuristics, so it can miss something or suggest a file that is not related. This is why uncertain items are never pre-selected and every deletion needs your confirmation. A suggestion that includes a protected system location is worth reporting privately.

## Threat model

HOTS Uninstaller performs legitimate system-level operations because that is required for its features.

The application is **not** designed to defend a system against a user or attacker who already has unrestricted Administrator privileges.

It **is** designed so that a **non-elevated program running as the same user** cannot use the elevated application to do things the user did not ask for. The user-writable folder `%APPDATA%\HOTS Uninstaller` is the main place where that could be attempted, so:

- directory links (junctions and symlinks) in the data folder and its sub-folders are refused or removed before the elevated application writes there, and files are written to a new file first and then moved into place instead of being written through an existing path
- registry backups and monitoring logs are sealed with a SHA-256 hash stored under `HKEY_LOCAL_MACHINE`, where a standard user cannot write; a backup or log that does not match its hash is not restored or used, and a log is read once, so the bytes that were verified are the bytes that are parsed
- the installation folder is writable only by administrators and SYSTEM (standard users get read and execute), so the files of the elevated application cannot be replaced by a standard user
- the signal that brings an already-running window to the front can only be sent, not changed or taken over, by a standard user
- before Install Monitor starts an installer it checks that the file is a local `.exe` or `.msi` (no shortcuts, links or network paths) with a valid header, and it starts it only if the file did not change while the snapshot was being taken

The leftover scan, the junk cleanup and the startup manager always refuse protected system folders, protected registry keys, links and files that belong to another installed package, regardless of what is selected.

Programs started by HOTS Uninstaller (a program's own uninstaller, or an installer you choose to monitor) **inherit its Administrator rights**. Only monitor installers you would be willing to run as an administrator.

## Supported versions

Security fixes are primarily intended for the latest released version.

| Version | Security support |
|---|---|
| Latest release | Supported |
| Older releases | Best effort |
| Development versions | Not guaranteed |

Users are encouraged to update to the latest release.

## Administrator privileges

Depending on which features you use, HOTS Uninstaller may modify:

- files and folders you select in the leftover scan or the junk cleanup, either moved to the Recycle Bin or deleted permanently (the screen says which); to delete `Windows.old` and Windows Update work folders it can take ownership of them and grant Administrators full control, as the built-in Disk Cleanup does
- the Windows Registry: keys you select in the leftover scan (each is exported to a `.reg` backup first), startup entries (Run keys and the Windows startup on/off switch) and, when you restore a backup, the keys stored in it
- Windows services (switching startup on or off; temporarily stopping and restarting the Windows Update and Delivery Optimization services while their cache is cleaned; and removing a service left behind by an uninstalled program once its program file is gone)
- scheduled tasks (disable, enable, delete)
- Microsoft Store apps (removed for the current user, or other accounts if you enable that option)
- system restore points (a new one before uninstalling, if you leave the option on; old ones when you choose that junk category, always keeping the newest)
- running programs: browsers are closed before their data is cleaned, so that files are not in use

To do this, the application calls built-in Windows tools — `powershell.exe`, `reg.exe`, `sc.exe`, `net.exe`, `schtasks.exe`, `taskkill.exe`, `takeown.exe`, `icacls.exe` and `msiexec.exe` — started by full path from the Windows folder rather than searched for on the `PATH`.

The application keeps its own data in:

- `%APPDATA%\HOTS Uninstaller` — `settings.json`, the diagnostic log `error.log` (rotated to `error.log.1` at about 250 KB), `RegistryBackups`, `MonitorLogs`, `UninstallLogs` and a short queue file used by "Remove with HOTS"
- `HKEY_LOCAL_MACHINE\Software\HOTS Uninstaller` — the integrity hashes of the backups (`BackupHashes`) and monitoring logs (`MonitorLogHashes`)
- if you chose that task during setup, the right-click entry "Remove with HOTS" for `.exe` files, under `Software\Classes\SystemFileAssociations\.exe\shell\HOTSRemove`

HOTS Uninstaller does not create firewall rules, file-system ACL locks, startup entries or policies of its own.

These operations are intentional parts of the application.

Users should review the documentation and source code before using system-level features they do not need.

## Privacy and network communication

HOTS Uninstaller does not require an online account for normal operation and works locally.

The only network request the application makes on its own is the built-in update checker, which contacts the GitHub Releases API to look for a newer version. By default it runs automatically at startup and can be switched off in **Options → About**. Like any HTTPS request, it reveals your IP address and the name `HOTS-UpdateChecker`; no other information about you or your system is sent.

Some actions open your default web browser at your request (the website, Ko-fi, PayPal, or "Search on Google" for a program name). That is then ordinary browser traffic, not a request made by the application.

Programs you uninstall or monitor may do their own network activity, which is outside this project's control. This project does not claim that every network request generated by Windows itself can be attributed to HOTS Uninstaller.

The application writes a local diagnostic log (`error.log`) to `%APPDATA%\HOTS Uninstaller`. Monitoring logs, uninstall logs and registry backups are stored there too and contain program names and file and registry paths. All of it stays on your computer and is not sent anywhere. Please review any of it before attaching it to a public issue.

## Backups and system changes

Every registry key removed as a leftover is saved as a `.reg` backup first (up to 50 backups are kept, the oldest are removed automatically), and a key is not removed if its backup could not be saved. You can restore a backup from the **Registry Backups** tab. A removed registry entry of a program that is "ghost" (its uninstaller no longer exists) is backed up the same way.

The uninstall history is kept for 3 months.

Deleted files cannot always be recovered: depending on the mode and the category, files go to the Recycle Bin or are deleted permanently, and cache-type junk is always removed permanently. Users should maintain independent system backups and should leave the **restore point** option on when removing anything important.

## Uninstallation

Uninstalling HOTS Uninstaller first shows an "Are you sure?" prompt reminding you that installing a newer version does not require uninstalling first (just run the new installer — it updates the program in place without touching your settings or history). The prompt is shown in whichever language the application is currently set to.

After that, a small wizard asks one question: **whether to delete the saved settings and history** — that is, everything in `%APPDATA%\HOTS Uninstaller` (settings, uninstall history, monitoring logs and registry backups) together with the integrity hashes stored in `HKEY_LOCAL_MACHINE\Software\HOTS Uninstaller`. It is **checked by default**. Closing the wizard or pressing Cancel cancels the uninstall. After a deletion, backed-up registry keys can no longer be restored.

There is nothing else to revert, because the application creates no locks, firewall rules or startup entries of its own. The setup program removes the installation folder and, if it was added, the "Remove with HOTS" right-click entry automatically.

If you leave the option unchecked, your data stays on the computer, and so do the integrity hashes, so your backups can still be verified if you reinstall.

A silent uninstall skips the wizard and always keeps your data. If the cleanup step fails for any reason, the data is kept as well.

## Installing over an existing version

Running the installer over an existing installation (to update or repair) first asks Windows to close any running instance of HOTS Uninstaller, then deletes the installation folder before copying the new files. This is intentional: the installation folder never contains user data (settings, history, logs and backups live in `%APPDATA%\HOTS Uninstaller`, outside it) and it avoids leaving orphaned files from older versions.

As a safeguard, this only happens when the folder is named `HOTS Uninstaller`: if you pick a different location, the setup appends the product name, so choosing a folder such as `Documents` or `C:\` can never lead to its contents being deleted.

## Responsible disclosure

Please allow reasonable time for investigation and remediation before publicly disclosing a confirmed vulnerability.

Security reports help improve HOTS Uninstaller and are appreciated.
