import os

from PySide6.QtCore import QFileInfo, QObject, Qt, Signal
from PySide6.QtGui import (
    QBitmap, QGuiApplication, QIcon, QImage, QImageReader, QPainter, QPixmap,
    QRegion,
)
from PySide6.QtWidgets import QFileIconProvider

from .bg_tasks import is_shutting_down, start_bg_thread
from .icons import FIF
from . import core_uninstall as cu
from .constants import DARK
from .widgets_qt import colored_svg_icon

_ICON_PROVIDER = QFileIconProvider()
_ICON_CACHE: dict = {}
_SQUARE_PIXMAP_CACHE: dict = {}
# (namespace, uid) -> icon source resolved in a background thread WITHOUT touching
# QIcon/QPixmap (those may only be created in the GUI thread): ("image", QImage),
# ("content", path), ("file", path), ("computer", None) or None.
_SOURCE_CACHE: dict = {}
_LAST_GOOD_ICON: dict = {}
_GENERIC_ICON = None


def generic_icon() -> QIcon:
    global _GENERIC_ICON
    if _GENERIC_ICON is None:
        try:
            _GENERIC_ICON = colored_svg_icon(FIF.APPLICATION, DARK["fg2"])
        except Exception:
            _GENERIC_ICON = QIcon()
    return _GENERIC_ICON


def file_source(path: str):
    if not path or not os.path.exists(path):
        return None
    return ("file", path)


def content_source(path: str):
    if not path or not os.path.exists(path):
        return None
    try:
        if not QImageReader(path).canRead():
            return None
    except Exception:
        return None
    return ("content", path)


_ALPHA_BLANK_TABLE = bytes(0 if v <= 10 else 1 for v in range(256))


def is_blank_icon_file(path: str) -> bool:
    img = QImage(path)
    if img.isNull():
        return True
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    if w == 0 or h == 0:
        return True
    stride = img.bytesPerLine()
    ptr = img.constBits()
    if hasattr(ptr, "setsize"):
        ptr.setsize(stride * h)
    raw = bytes(ptr)[:stride * h]
    for y in range(h):
        row = raw[y * stride: y * stride + w * 4]
        if any(row[3::4].translate(_ALPHA_BLANK_TABLE)):
            return False
    return True


def logo_source(path: str):
    if not path or not os.path.exists(path) or is_blank_icon_file(path):
        return None
    return content_source(path)


def _icon_from_source(src):
    if not src:
        return None
    kind, value = src
    if kind == "image":
        return QIcon(QPixmap.fromImage(value))
    if kind == "content":
        return QIcon(value)
    if kind == "file":
        return _ICON_PROVIDER.icon(QFileInfo(value))
    if kind == "computer":
        return _ICON_PROVIDER.icon(QFileIconProvider.IconType.Computer)
    return None


def _image_from_resource(path: str, index: int = 0, size: int = 48):
    if not path or not os.path.isfile(path):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        # ctypes treats every return type as a 32-bit c_int unless argtypes/restype are
        # set explicitly. The handles returned by the calls below (GetDC,
        # ExtractIconExW, ...) are pointer-sized, so on 64-bit Windows they can be
        # silently truncated, which causes intermittent missing icons (this was the real
        # cause of the NVIDIA driver icon NVI2.dll,0 disappearing between runs).
        shell32.ExtractIconExW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int,
            ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON),
            wintypes.UINT,
        ]
        shell32.ExtractIconExW.restype = wintypes.UINT

        user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.c_void_p]
        user32.GetIconInfo.restype = wintypes.BOOL

        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetObjectW.restype = ctypes.c_int

        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC

        gdi32.GetDIBits.argtypes = [
            wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int

        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int

        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL

        user32.DestroyIcon.argtypes = [wintypes.HICON]
        user32.DestroyIcon.restype = wintypes.BOOL

        h_large = wintypes.HICON()
        got_icon = False
        try:
            shell32.SHDefExtractIconW.argtypes = [
                wintypes.LPCWSTR, ctypes.c_int, wintypes.UINT,
                ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON),
                wintypes.UINT,
            ]
            shell32.SHDefExtractIconW.restype = ctypes.c_long
            hr = shell32.SHDefExtractIconW(
                path, index, 0, ctypes.byref(h_large), None, size,
            )
            got_icon = (hr == 0 and bool(h_large.value))
        except Exception:
            got_icon = False
        if not got_icon:
            h_large = wintypes.HICON()
            extracted = shell32.ExtractIconExW(
                path, index, ctypes.byref(h_large), None, 1,
            )
            if not extracted or not h_large.value:
                return None

        class ICONINFO(ctypes.Structure):
            _fields_ = [
                ("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP),
            ]

        class BITMAP(ctypes.Structure):
            _fields_ = [
                ("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p),
            ]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        info = ICONINFO()
        image = None
        try:
            if not user32.GetIconInfo(h_large, ctypes.byref(info)):
                return None
            try:
                bm = BITMAP()
                if not gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(BITMAP), ctypes.byref(bm)):
                    return None
                width, height = int(bm.bmWidth), int(bm.bmHeight)
                if width <= 0 or height <= 0:
                    return None

                header = BITMAPINFOHEADER()
                header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                header.biWidth = width
                header.biHeight = -height
                header.biPlanes = 1
                header.biBitCount = 32
                header.biCompression = 0

                buf = ctypes.create_string_buffer(width * height * 4)
                hdc = user32.GetDC(None)
                if not hdc:
                    return None
                try:
                    got = gdi32.GetDIBits(
                        hdc, info.hbmColor, 0, height, buf,
                        ctypes.byref(header), 0,
                    )
                finally:
                    user32.ReleaseDC(None, hdc)
                if not got:
                    return None

                data = buf.raw
                if not any(data[3::4]):
                    arr = bytearray(data)
                    alpha = b"\xff" * (width * height)
                    try:
                        mask_buf = ctypes.create_string_buffer(width * height * 4)
                        mdc = user32.GetDC(None)
                        if mdc:
                            try:
                                mgot = gdi32.GetDIBits(
                                    mdc, info.hbmMask, 0, height, mask_buf,
                                    ctypes.byref(header), 0,
                                )
                            finally:
                                user32.ReleaseDC(None, mdc)
                            if mgot:
                                m = mask_buf.raw
                                mask_alpha = bytes(
                                    0 if (b or g or r) else 255
                                    for b, g, r in zip(m[0::4], m[1::4], m[2::4])
                                )
                                if any(mask_alpha):
                                    alpha = mask_alpha
                    except Exception:
                        pass
                    arr[3::4] = alpha
                    data = bytes(arr)

                image = QImage(data, width, height, QImage.Format.Format_ARGB32).copy()
            finally:
                if info.hbmColor:
                    gdi32.DeleteObject(info.hbmColor)
                if info.hbmMask:
                    gdi32.DeleteObject(info.hbmMask)
        finally:
            user32.DestroyIcon(h_large)

        if image is None or image.isNull():
            return None
        return image
    except Exception:
        return None


def _split_icon_reference(raw: str) -> tuple[str, int]:
    raw = (raw or "").strip().strip('"')
    if "," in raw:
        head, _, tail = raw.rpartition(",")
        tail = tail.strip()
        if head.strip() and (tail.lstrip("-").isdigit()):
            return head.strip().strip('"'), int(tail)
    return raw, 0


def prepare_icon(namespace: str, uid: str, source_fn) -> None:
    key = (namespace, uid)
    if key in _SOURCE_CACHE:
        return
    try:
        src = source_fn()
    except Exception:
        src = None
    _SOURCE_CACHE[key] = src


def ready_icon(namespace: str, uid: str):
    key = (namespace, uid)
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached
    if key not in _SOURCE_CACHE:
        return _LAST_GOOD_ICON.get(key)
    try:
        icon = _icon_from_source(_SOURCE_CACHE[key])
    except Exception:
        icon = None
    if icon is None or icon.isNull():
        icon = generic_icon()
    _ICON_CACHE[key] = icon
    _LAST_GOOD_ICON[key] = icon
    return icon


def get_icon(namespace: str, uid: str, source_fn) -> QIcon:
    prepare_icon(namespace, uid, source_fn)
    return ready_icon(namespace, uid)


def retry_failed_icons(namespace: str) -> None:
    failed = [k for k, src in _SOURCE_CACHE.items()
              if k[0] == namespace and (src is None or src == ("computer", None))]
    for k in failed:
        _SOURCE_CACHE.pop(k, None)
        _ICON_CACHE.pop(k, None)
    if failed:
        for k in [k for k in _SQUARE_PIXMAP_CACHE if k[0] == namespace]:
            _SQUARE_PIXMAP_CACHE.pop(k, None)


class IconPrefetcher(QObject):
    progress = Signal()
    _BATCH = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self._generation = 0

    def start(self, jobs: list) -> None:
        self._generation += 1
        generation = self._generation

        def worker():
            done = 0
            for namespace, uid, source_fn in jobs:
                if is_shutting_down() or generation != self._generation:
                    return
                prepare_icon(namespace, uid, source_fn)
                done += 1
                if done % self._BATCH == 0:
                    self.progress.emit()
            if not is_shutting_down():
                self.progress.emit()

        start_bg_thread(worker)


def _device_ratio(dpr=None) -> float:
    if dpr:
        return float(dpr)
    screen = QGuiApplication.primaryScreen()
    return float(screen.devicePixelRatio()) if screen else 1.0


def to_square_pixmap(icon: QIcon, size: int, trim_padding: bool = False,
                     dpr=None) -> QPixmap:
    dpr = _device_ratio(dpr)
    px = max(1, round(size * dpr))

    sizes = icon.availableSizes()
    if sizes:
        best = max(sizes, key=lambda s: s.width() * s.height())
        src = icon.pixmap(best)
    else:
        src = icon.pixmap(max(px, 128), max(px, 128))
    src.setDevicePixelRatio(1.0)

    if trim_padding and not src.isNull() and src.hasAlphaChannel():
        try:
            mask = QBitmap.fromImage(src.toImage().createAlphaMask())
            bbox = QRegion(mask).boundingRect()
            if (bbox.isValid()
                    and bbox.width() >= src.width() * 0.15
                    and bbox.height() >= src.height() * 0.15
                    and (bbox.width() < src.width() * 0.94
                         or bbox.height() < src.height() * 0.94)):
                src = src.copy(bbox)
        except Exception:
            pass

    canvas = QPixmap(px, px)
    canvas.fill(Qt.transparent)
    if src.isNull():
        canvas.setDevicePixelRatio(dpr)
        return canvas

    scaled = src.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    painter = QPainter(canvas)
    painter.drawPixmap((px - scaled.width()) // 2, (px - scaled.height()) // 2, scaled)
    painter.end()
    canvas.setDevicePixelRatio(dpr)
    return canvas


def cached_square_pixmap(namespace: str, uid: str, icon: QIcon, size: int,
                          trim_padding: bool = False, dpr=None) -> QPixmap:
    dpr = _device_ratio(dpr)
    cache_key = (namespace, uid, size, trim_padding, round(dpr, 2))
    cached = _SQUARE_PIXMAP_CACHE.get(cache_key)
    if cached is not None:
        return cached
    pixmap = to_square_pixmap(icon, size, trim_padding=trim_padding, dpr=dpr)
    _SQUARE_PIXMAP_CACHE[cache_key] = pixmap
    return pixmap


_HELPER_EXE_PATTERNS = (
    "uninst", "unins",
    "setup", "install",
    "update", "updater",
    "maintenancetool",
    "crashpad", "crashhandler",
    "vcredist", "vc_redist", "dotnetfx", "redist",
    "elevate", "elevation",
)


def _looks_like_helper_exe(filename: str) -> bool:
    stem = filename.lower()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    return any(p in stem for p in _HELPER_EXE_PATTERNS)


def _scan_exe_candidates(folder: str) -> list:
    try:
        with os.scandir(folder) as it:
            return [e for e in it if e.is_file() and e.name.lower().endswith(".exe")]
    except Exception:
        return []


_MAX_SUBDIRS_SCANNED = 40


def _find_main_exe(install_location: str, display_name: str) -> str:
    if not install_location or not os.path.isdir(install_location):
        return ""

    entries = _scan_exe_candidates(install_location)
    if not entries:
        try:
            with os.scandir(install_location) as it:
                subdirs = [e.path for e in it if e.is_dir()][:_MAX_SUBDIRS_SCANNED]
        except Exception:
            subdirs = []
        for sub in subdirs:
            entries.extend(_scan_exe_candidates(sub))
    if not entries:
        return ""

    candidates = [e for e in entries if not _looks_like_helper_exe(e.name)] or entries

    norm_name = "".join(ch for ch in display_name.lower() if ch.isalnum())
    if norm_name:
        for e in candidates:
            stem = "".join(ch for ch in os.path.splitext(e.name)[0].lower() if ch.isalnum())
            if stem and (stem in norm_name or norm_name.startswith(stem)):
                return e.path

    try:
        return max(candidates, key=lambda e: e.stat().st_size).path
    except Exception:
        return candidates[0].path


_RESOURCE_ICON_EXTS = (".dll", ".exe", ".ocx", ".cpl", ".icl", ".scr")


def app_icon_key(app: "cu.InstalledApp") -> str:
    return app.display_icon or app.install_location or app.uid


def app_icon_source(app: "cu.InstalledApp", size: int = 48):
    raw, icon_index = _split_icon_reference(app.display_icon)
    if raw and os.path.isdir(raw):
        raw = ""
    if raw:
        if os.path.splitext(raw)[1].lower() in _RESOURCE_ICON_EXTS:
            image = _image_from_resource(raw, icon_index, size)
            if image is not None:
                return ("image", image)
        src = file_source(raw)
        if src is not None:
            return src

    if app.is_windows_installer:
        msi_icon, msi_index = _split_icon_reference(cu._msi_product_property(app.key_name, "ProductIcon"))
        msi_icon = os.path.normpath(os.path.expandvars(msi_icon)) if msi_icon else msi_icon
        if msi_icon and os.path.splitext(msi_icon)[1].lower() in _RESOURCE_ICON_EXTS:
            image = _image_from_resource(msi_icon, msi_index, size)
            if image is not None:
                return ("image", image)
        if msi_icon:
            src = content_source(msi_icon)
            if src is None and os.path.splitext(msi_icon)[1]:
                src = file_source(msi_icon)
            if src is not None:
                return src

    src = file_source(_find_main_exe(app.install_location, app.display_name))
    if src is not None:
        return src

    src = file_source(cu._extract_exe_path(app.uninstall_string or app.quiet_uninstall_string))
    if src is not None:
        return src
    return ("computer", None)


def app_icon_if_ready(app: "cu.InstalledApp"):
    return ready_icon("win32", app_icon_key(app))


def app_icon_large(app: "cu.InstalledApp") -> QIcon:
    return get_icon("win32-large", app_icon_key(app), lambda: app_icon_source(app, 128))


def app_icon(app: "cu.InstalledApp") -> QIcon:
    return get_icon("win32", app_icon_key(app), lambda: app_icon_source(app))
