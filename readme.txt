HOTS Uninstaller v1.0
=====================

A desktop application for removing programs completely - including the
files and registry entries they leave behind.
Part of the HOTS Tools family (Handy OS Tools Suite). Free and open-source,
released under GPLv3.


OVERVIEW
--------
HOTS Uninstaller lists every program registered in Windows, removes it with
its own uninstaller, and then scans for the leftovers (folders, files,
registry keys, services) so you can review and clean them up. It also
manages Microsoft Store apps, startup entries and junk files, and can
record exactly what an installer adds to your system.

Nothing is deleted without a review step, and registry keys are backed up
before they are removed.

Requires Windows 10 or Windows 11 (64-bit). Must be run as Administrator.
The installer / shortcut is already configured to request this
automatically.


MAIN FEATURES
-------------
- Full list of installed programs with live search, sorting and details
  (publisher, version, size, install date); recent installs are grouped
  at the top
- Native uninstall in one click, with an optional system restore point
  created first (on by default)
- Leftover scan after uninstalling: folders, files, registry keys,
  services and shortcut-history entries that probably belong to the
  program. You choose what to delete
  - nothing is selected for you, and the select-all button ticks only the
    high-confidence items; items with no independent link to the program
    are marked "uncertain" and user data (bookmarks, passwords) is flagged
    separately - both have to be ticked by hand
  - protected system folders and registry keys, links/junctions and files
    of other installed packages are skipped
  - files in use are scheduled for deletion on the next restart
  - every registry key is backed up before it is removed
- Forced removal of ghost entries: programs whose uninstaller no longer
  exists can be cleaned out of the list (registry entry only, with a
  backup)
- Windows Apps: uninstall Microsoft Store (UWP) apps, with warnings for
  core Windows components and an option to show hidden components and
  apps of other accounts
- Startup manager: registry Run keys, Startup folders, scheduled tasks,
  services and Store app startup tasks. Disabling is reversible and uses
  Windows' own startup switch; entries can also be removed (services can
  only be switched on/off)
- Junk files: 19 categories (temp files, Windows Update cache, crash
  dumps, error reports, logs, browser cache, thumbnails, Prefetch, GPU
  shader cache, app caches, Recycle Bin and more). Opt-in extras: browser
  history, Windows.old and old restore points. Nothing is deleted
  automatically; optional Recycle Bin mode
- Install Monitor: takes a snapshot of the registry and disk, runs the
  installer (.exe or .msi) and finishes on its own when the installation
  settles. Records added registry entries, files and folders, services,
  scheduled tasks and install folders outside the usual locations. The
  log is used automatically when you later uninstall that program
- Registry backups: restore any removed key with one click; backups are
  integrity-checked; up to 50 are kept
- Uninstall history: programs removed through the app, kept for 3 months
- "Remove with HOTS" for portable .exe files: removes the file and its
  traces (shortcuts, Prefetch, registry history) without touching the
  folder it sits in (the right-click entry is an optional setup choice,
  off by default)
- Light and dark themes with 5 accent colors
- Built-in update checker (via GitHub Releases); checks on startup by
  default, can be switched off in Options > About
- Available in 5 languages: English, Polish, French, German, Spanish
  (the app starts in your Windows language when it is supported)


SAFETY
------
- Nothing is removed without a review step and your confirmation
- System-critical folders and registry keys are always off-limits, as are
  links and junctions
- Nothing is pre-selected, and items that are only possibly related are
  never included by the select-all button
- System services are never touched; a leftover service is removed only
  if it is an application service whose program file no longer exists
- The data folder, backups and logs are validated before the elevated app
  reads or deletes anything
- The only network request the app makes on its own is the update check
  against GitHub Releases (on by default, can be switched off in
  Options > About). No telemetry, no account, no ads

Removing the wrong thing can still break a program. Keep the
restore-point option on for anything important.


WHAT'S NEW IN 1.0
-----------------
First public release.
- Programs: list, native uninstall, leftover scan, ghost entry removal
- Windows Apps: Microsoft Store (UWP) app removal
- Startup manager
- Junk files cleanup
- Install Monitor with automatic use of its log at uninstall time
- Registry backups and uninstall history
- 5 languages, dark and light theme, 5 accent colors


DISCLAIMER
----------
HOTS Uninstaller is provided in good faith but without any warranty. The
author is not responsible for any damage, data loss, system issues, or
other consequences resulting from the use of this application.
Uninstalling programs and deleting files and registry entries can affect
how Windows and other software work - review every list before you
confirm. You use this software at your own risk.


SUPPORT
-------
If HOTS Uninstaller saves you time, or you'd simply like to say thanks:

  Ko-fi:   ko-fi.com/darsono
  PayPal:  paypal.me/darsonodark
  Email:   hots.support@gmail.com
  www:     hotstools.com

No registration required. Any amount is appreciated.


LICENSE
-------
GNU General Public License v3.0
(c) 2026 Darsono
