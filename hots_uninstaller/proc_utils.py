import ctypes
import os
import subprocess
from ctypes import wintypes


def popen_no_busy_cursor(args: list) -> subprocess.Popen:
    si = subprocess.STARTUPINFO()
    si.dwFlags |= 0x00000080
    args = list(args)
    if args and os.path.basename(args[0]) == args[0]:
        args[0] = system_exe(args[0])
    return subprocess.Popen(args, startupinfo=si)


_SPECIAL_DIRS = {"explorer.exe": "", "regedit.exe": "", "powershell.exe": r"System32\WindowsPowerShell\v1.0"}


def _windows_dir() -> str:
    try:
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetSystemWindowsDirectoryW(buf, 260):
            return buf.value
    except Exception:
        pass
    return r"C:\Windows"


def system_exe(name: str) -> str:
    # Full path of a Windows tool, so a lookalike in the current directory or PATH is never started.
    path = os.path.join(_windows_dir(), _SPECIAL_DIRS.get(name.lower(), "System32"), name)
    return path if os.path.isfile(path) else name


class _StartupInfoW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class ShellProcess:
    def __init__(self, handle, pid: int):
        self._h = handle
        self.pid = pid
        self.returncode = None

    def poll(self):
        if self.returncode is None and self._h:
            code = wintypes.DWORD(0)
            if ctypes.windll.kernel32.GetExitCodeProcess(self._h, ctypes.byref(code)) and code.value != 259:
                self.returncode = code.value
                self._close()
        return self.returncode

    def _close(self):
        if self._h:
            ctypes.windll.kernel32.CloseHandle(self._h)
            self._h = None

    def __del__(self):
        try:
            self._close()
        except Exception:
            pass


def launch_deelevated(cmdline: str, hide: bool = False):
    k32, adv, u32 = ctypes.windll.kernel32, ctypes.windll.advapi32, ctypes.windll.user32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    u32.GetShellWindow.restype = wintypes.HWND
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)]
    adv.CreateProcessWithTokenW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
                                            wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                            ctypes.POINTER(_StartupInfoW), ctypes.POINTER(_ProcessInformation)]
    hwnd = u32.GetShellWindow()
    if not hwnd:
        return None
    shell_pid = wintypes.DWORD(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(shell_pid))
    hproc = k32.OpenProcess(0x0400, False, shell_pid.value)
    if not hproc:
        return None
    tok = wintypes.HANDLE()
    prim = wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(hproc, 0x0001 | 0x0002 | 0x0008, ctypes.byref(tok)):
            return None
        if not adv.DuplicateTokenEx(tok, 0x02000000, None, 2, 1, ctypes.byref(prim)):
            return None
        si = _StartupInfoW()
        si.cb = ctypes.sizeof(si)
        pi = _ProcessInformation()
        buf = ctypes.create_unicode_buffer(cmdline)
        if not adv.CreateProcessWithTokenW(prim, 0, None, buf, 0x08000000 if hide else 0, None, None,
                                           ctypes.byref(si), ctypes.byref(pi)):
            return None
        k32.CloseHandle(pi.hThread)
        return ShellProcess(pi.hProcess, pi.dwProcessId)
    finally:
        for h in (prim, tok, hproc):
            if h:
                k32.CloseHandle(h)
