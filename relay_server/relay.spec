# PyInstaller build of the relay: pyinstaller --clean --noconfirm relay.spec
# Produces one file, dist/start_relay(.exe), that must sit next to relay.ini.
# -*- mode: python -*-
import sys

from PyInstaller.utils.hooks.qt import QtLibraryInfo

# --------------------------------------------------------------- Qt plugins
# PyInstaller copies every plugin of each Qt module it finds. The relay window only needs the
# platform plugin (how a window is shown) and the native style. Everything else (GTK theme,
# Wayland, EGL, VNC, input devices, image formats...) is skipped, and so are the system
# libraries those plugins need (GTK, cairo, ...), which make up most of the Linux size.
KEEP_PLUGINS = {
    "platforms": ("qwindows", "qxcb", "qcocoa", "qoffscreen", "qminimal"),  # Wayland falls back to xcb
    "styles": None,                 # None = keep all (native Windows style)
    "xcbglintegrations": None,      # OpenGL on X11, avoids a warning
}

_collect_plugins = QtLibraryInfo.collect_plugins


def collect_wanted_plugins(self, plugin_type):
    if plugin_type not in KEEP_PLUGINS:
        return []
    files = _collect_plugins(self, plugin_type)
    names = KEEP_PLUGINS[plugin_type]
    if names is None:
        return files
    def stem(path):  # .../platforms/libqxcb.so -> qxcb, qwindows.dll -> qwindows
        name = path.replace("\\", "/").rsplit("/", 1)[-1].split(".")[0]
        return name[3:] if name.startswith("lib") else name
    return [f for f in files if stem(f[0]) in names]


QtLibraryInfo.collect_plugins = collect_wanted_plugins

# ---------------------------------------------------------------- analysis
datas = [
    ("relay/assets", "relay/assets"),           # window logo / icon
    ("relay/web/index.html", "relay/web"),      # phone page (face-api models are downloaded at start)
    ("relay/web/app.js", "relay/web"),
    ("relay/web/style.css", "relay/web"),
    ("relay/web/img", "relay/web/img"),
]

a = Analysis(
    ["start_relay.py"],
    datas=datas,
    excludes=[
        "PIL", "tkinter", "unittest", "pydoc", "pdb", "doctest", "lib2to3",  # not used
        "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtOpenGL", "PySide6.QtSvg",
        "PySide6.QtDBus", "PySide6.QtPdf", "PySide6.QtWebEngineCore",
    ],
    noarchive=False,
    optimize=1,                                 # drop asserts
)
# Qt translations (~ several MB): the window is in English only
a.datas = [d for d in a.datas if "/translations/" not in d[0].replace("\\", "/")]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="start_relay",
    icon="relay/assets/logo.ico",
    # Console program, so "start_relay.exe --no-gui" prints the log in the terminal it is run from.
    # Double-clicked (Windows creates a console just for it), the console is hidden at once and
    # only the relay window shows; the log is still written to data\relay.log.
    console=True,
    hide_console="hide-early" if sys.platform == "win32" else None,
    strip=sys.platform != "win32",              # remove debug symbols (Linux/macOS)
    upx=False,                                  # UPX shrinks a bit more but slows start-up and alarms antiviruses
)
