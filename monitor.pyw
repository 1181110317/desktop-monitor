# -*- coding: utf-8 -*-
"""
桌面性能监视器 · Desktop Monitor
--------------------------------
一个常驻桌面的极简悬浮窗，实时显示：
  CPU 占用 / 内存占用 / GPU 占用与显存 / 网络上行下行 / GPU 温度 / 磁盘 / 运行时长

Python + Pillow 实现，ctypes 直读 Windows 性能接口。
    · 左键拖动     移动位置
    · 双击         在“完整 / 紧凑”两种尺寸间切换
    · 右键         菜单（层级、刷新率、透明度、开机自启、退出）

数据来源：
  CPU / 内存 / 网络   kernel32 · GetSystemTimes / GlobalMemoryStatusEx，iphlpapi · GetIfTable2
  GPU                   NVIDIA 驱动自带 nvidia-smi
  GPU 温度              nvidia-smi
  CPU 温度              若系统装有 LibreHardwareMonitor / OpenHardwareMonitor（管理员运行）则自动读取
"""

import ctypes
import ctypes.wintypes as wt
import json
import queue
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import font as tkfont, ttk
from contextlib import contextmanager
import math

APP_NAME = "桌面性能监视器"
APP_VERSION = "2.2.5"
BASE = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
RESOURCES = getattr(sys, "_MEIPASS", BASE)
DATA_DIR = (os.path.join(os.environ.get("LOCALAPPDATA", BASE), "DesktopMonitor")
            if getattr(sys, "frozen", False) else BASE)
CFG_PATH = os.path.abspath(os.environ.get("DM_CONFIG_PATH", os.path.join(DATA_DIR, "config.json")))
DATA_DIR = os.path.dirname(CFG_PATH)

CREATE_NO_WINDOW = 0x08000000
HWND_BOTTOM = 1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

# ---------------------------------------------------------------- 视觉令牌
BG = "#10151f"
BORDER = "#2b3545"
TRACK = "#293346"
TXT = "#f0f4fa"
DIM = "#a5b2c5"
FAINT = "#8291a8"
TEAL = "#65ddba"
BLUE = "#80b4ff"
AMBER = "#f6c76c"
RED = "#ff878d"
VIOLET = "#bca7ff"

CARD_ALPHA = 1.0

CARD_COLORS = dict(bg=BG, surface="#19212e", border=BORDER, line="#283243", track=TRACK,
                   text=TXT, dim=DIM, faint=FAINT, teal=TEAL, amber=AMBER, red=RED,
                   violet=VIOLET, blue=BLUE, fill="#1d3835")

CARD_THEMES = {
    "dark": CARD_COLORS,
    "light": dict(bg="#f3f6fa", surface="#ffffff", border="#d9e1ec", line="#e3e9f1", track="#e4eaf2",
                  text="#202c40", dim="#53657d", faint="#677a91",
                  teal="#087e69", amber="#9c6500", red="#c33d50",
                  violet="#7954c8", blue="#286dcc", fill="#d9f2e9"),
    "minimal": dict(bg="#faf9f6", surface="#ffffff", border="#e1ded5", line="#eae7df", track="#eeece5",
                    text="#292e32", dim="#606b71", faint="#717b80",
                    teal="#217965", amber="#946300", red="#bd4350",
                    violet="#7d5eb5", blue="#3e6fa7", fill="#e3f1ea"),
    "glass": dict(bg="#182334", surface="#233249", border="#384b65", line="#35455c", track="#34465f",
                  text="#f2f7ff", dim="#b1c3dc", faint="#95acca",
                  teal="#79e5cf", amber="#ffd288", red="#ff989c",
                  violet="#ccbcff", blue="#9dcaff", fill="#294b53"),
}


STRIP_STYLES = {"minimal": "融入", "glass": "雾面", "tiles": "分舱",
                "outline": "细线", "ribbon": "能量", "neon": "霓虹"}
STRIP_LAYOUTS = {"classic": ("首版", 148), "micro": ("微型", 148), "duo": ("双行", 188), "inline": ("横排", 228),
                 "gauges": ("圆环", 190), "trends": ("趋势", 244)}
STRIP_HEIGHTS = {"classic": 40, "micro": 28, "duo": 32, "inline": 32, "gauges": 32, "trends": 32}
STRIP_PROFILES = {
    "balanced": ("均衡", ("cpu", "mem", "up", "down")),
    "hardware": ("硬件", ("cpu", "mem", "gpu", "gpu_temp")),
    "network": ("网络", ("down", "up", "total_down", "total_up")),
    "battery": ("续航", ("cpu", "mem", "battery", "uptime")),
}
STRIP_METRICS = {
    "cpu": ("CPU 占用", "CPU"), "mem": ("内存占用", "RAM"),
    "gpu": ("GPU 占用", "GPU"), "vram": ("显存占用", "VRAM"),
    "cpu_temp": ("CPU 温度", "CPU°"), "gpu_temp": ("GPU 温度", "GPU°"),
    "up": ("上传速度", "上传"), "down": ("下载速度", "下载"),
    "total_up": ("累计上传", "已发"), "total_down": ("累计下载", "已收"),
    "disk": ("系统盘占用", "磁盘"), "battery": ("电池电量", "电池"),
    "uptime": ("运行时长", "运行"),
}
STRIP_ACCENTS = {"mint": "薄荷", "blue": "冰川", "violet": "鸢尾", "amber": "琥珀", "mono": "单色"}
STRIP_SIZES = {"small": ("小巧", 0.88), "normal": ("标准", 1.0), "large": ("清晰", 1.12)}
STRIP_DEFAULTS = {"style": "minimal", "strip_layout": "classic", "strip_profile": "balanced",
                  "strip_accent": "mono", "strip_size": "small"}
STRIP_CHOICES = {"style": STRIP_STYLES, "strip_layout": STRIP_LAYOUTS, "strip_profile": STRIP_PROFILES,
                 "strip_accent": STRIP_ACCENTS, "strip_size": STRIP_SIZES}


def clean_strip_items(items):
    """Keep supported measurements once, in the user's display order."""
    if not isinstance(items, (list, tuple)):
        return []
    result = []
    for key in items:
        if isinstance(key, str) and key in STRIP_METRICS and key not in result:
            result.append(key)
    return result


def selected_strip_items(cfg, **options):
    profile = options.get("strip_profile", cfg.get("strip_profile", "balanced"))
    items = options.get("strip_items", None if "strip_profile" in options else cfg.get("strip_items"))
    return clean_strip_items(items) or list(STRIP_PROFILES.get(profile, STRIP_PROFILES["balanced"])[1])


def strip_profile_for_items(items):
    return next((key for key, (_, values) in STRIP_PROFILES.items() if tuple(items) == values), "custom")


def strip_design_width(layout, count):
    columns = math.ceil(count / 2) if layout in ("classic", "micro", "duo") else count
    reference = 2 if layout in ("classic", "micro", "duo") else 4
    gap = 2 if layout == "micro" else (5 if layout == "duo" else 0)
    cell = (STRIP_LAYOUTS[layout][1] - 4 - gap * (reference - 1)) / reference
    return 4 + columns * cell + (columns - 1) * gap


def strip_design_height(layout, count):
    return 24 if count == 1 and layout in ("classic", "micro", "duo") else STRIP_HEIGHTS[layout]


def strip_metrics(state, profile="balanced", items=None):
    """Format selected measurements, keeping unavailable readings distinct from zero."""
    gpu = state.get("gpu") or {}
    battery = state.get("battery") or {}
    def percentage(used, total):
        if all(isinstance(v, (int, float)) and math.isfinite(v) for v in (used, total)) and total > 0:
            return clamp(used / total * 100, 0, 100)
        return None
    disk_total, disk_free = state.get("disk_total"), state.get("disk_free")
    disk_used = disk_total - disk_free if all(isinstance(v, (int, float)) for v in (disk_total, disk_free)) else None
    values = {"cpu": state.get("cpu"), "mem": state.get("mem_pct"), "gpu": gpu.get("util"),
              "vram": percentage(gpu.get("mem_used"), gpu.get("mem_total")),
              "cpu_temp": state.get("cpu_temp"), "gpu_temp": gpu.get("temp"),
              "disk": percentage(disk_used, disk_total), "battery": battery.get("pct")}
    result = []
    for key in selected_strip_items({"strip_profile": profile, "strip_items": items}):
        raw = values.get(key) if key in values else state.get(key)
        raw = float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) else None
        value, unit, level = "—", "", None
        if key in values:
            temperature = key in ("cpu_temp", "gpu_temp")
            unit = "°C" if temperature else "%"
            if raw is not None:
                value = f"{clamp(raw, 0, 150 if temperature else 100):.0f}"
                level = clamp(raw / 110 * 100 if temperature else raw, 0, 100)
        elif raw is not None:
            if key in ("up", "down"):
                parts = fmt_speed(raw).split(" ")
                value, unit = parts[0], parts[1]
            elif key.startswith("total_"):
                value, unit = fmt_bytes(raw).split(" ")
            else:
                hours, minutes = divmod(max(0, int(raw)) // 60, 60)
                value = f"{hours // 24}d {hours % 24}h" if hours >= 24 else f"{hours}h {minutes}m"
        result.append(dict(key=key, label=STRIP_METRICS[key][0], tag=STRIP_METRICS[key][1], value=value,
                           unit=unit if raw is not None else "", level=level, raw=raw))
    return result


def mix_color(a, b, amount):
    """Blend two theme colors without introducing a second palette."""
    return "#%02x%02x%02x" % tuple(round(int(a[i:i + 2], 16) * (1 - amount)
                                       + int(b[i:i + 2], 16) * amount) for i in (1, 3, 5))


class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uCallbackMessage", wt.UINT),
        ("uEdge", wt.UINT), ("rc", wt.RECT), ("lParam", wt.LPARAM),
    ]


def taskbar_position():
    """返回任务栏 (x, y, w, h, 贴边)：0=左 1=上 2=右 3=下；取不到就返回 None。"""
    try:
        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(APPBARDATA)
        _shell32.SHAppBarMessage.argtypes = [wt.DWORD, ctypes.c_void_p]
        _shell32.SHAppBarMessage.restype = ctypes.c_size_t
        if _shell32.SHAppBarMessage(5, ctypes.byref(abd)):  # ABM_GETTASKBARPOS
            r = abd.rc
            return r.left, r.top, r.right - r.left, r.bottom - r.top, abd.uEdge
    except Exception:
        pass
    return None


_TB_CACHE = {"hwnd": 0}


def taskbar_hwnd():
    if not _TB_CACHE["hwnd"] or not _u32.IsWindow(_TB_CACHE["hwnd"]):
        _TB_CACHE["hwnd"] = _u32.FindWindowW("Shell_TrayWnd", None) or 0
    return _TB_CACHE["hwnd"]


def same_monitor(hwnd_a, hwnd_b):
    """两个窗口是否在同一块显示器上（别的屏全屏不该把主屏的条子藏了）。"""
    try:
        u = _u32
        u.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        u.MonitorFromWindow.restype = ctypes.c_void_p
        a = u.MonitorFromWindow(wt.HWND(hwnd_a), 2)
        b = u.MonitorFromWindow(wt.HWND(hwnd_b), 2)
        return a == b
    except Exception:
        return True


def fullscreen_foreground():
    """前台窗口是否铺满整个显示器（全屏视频/游戏）。"""
    try:
        u = _u32
        u.GetForegroundWindow.restype = wt.HWND
        u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return False
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return False
        r = wt.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        u.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        u.MonitorFromWindow.restype = ctypes.c_void_p
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
        mon = u.MonitorFromWindow(hwnd, 2)          # NEAREST
        if not mon:
            return False
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not u.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return False
        mr = mi.rcMonitor
        mw = max(1, mr.right - mr.left)
        mh = max(1, mr.bottom - mr.top)
        w = r.right - r.left
        h = r.bottom - r.top
        # 真全屏会盖住任务栏（下边缘直达屏幕底）；最大化窗口只到工作区，靠这个区分
        return (w / mw >= 0.99 and h / mh >= 0.99
                and r.top <= mr.top + 4 and r.bottom >= mr.bottom - 4)
    except Exception:
        return False


def taskbar_children():
    """Shell_TrayWnd 下各子窗口的位置：托盘左边缘、任务按钮右边缘等。"""
    u = _u32
    tb = taskbar_hwnd()
    out = {}
    if not tb:
        return out
    out["__hwnd__"] = tb
    PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, lp):
        try:
            buf = ctypes.create_unicode_buffer(96)
            u.GetClassNameW(h, buf, 96)
            r = wt.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            out[buf.value] = (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
        return True

    ref = PROC(cb)
    u.EnumChildWindows(tb, ref, 0)
    return out


# Win11 把“小组件 / 天气”按钮放在通知区域左侧，但它没有独立 HWND，只能通过
# UI Automation 读取位置。内嵌条排在它左边，透明样式就不会压住天气文字。
_UIA_LOCAL = threading.local()
_WIDGETS = {"lock": threading.Lock(), "started": False, "stop": None,
            "key": None, "rect": None, "stamp": 0.0}


class UIAVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3),
                ("payload", ctypes.c_byte * 16)]


def _guid(text):
    parts = text.strip("{}").split("-")
    return GUID(int(parts[0], 16), int(parts[1], 16), int(parts[2], 16),
                (ctypes.c_ubyte * 8)(*bytes.fromhex(parts[3] + parts[4])))


def _com_call(obj, index, restype, argtypes, *args):
    """调用 COM 接口第 index 个虚函数；接口在进程内匿名使用。"""
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])(obj, *args)


def _com_release(obj):
    if obj is None or not getattr(obj, "value", obj):
        return
    try:
        _com_call(obj, 2, ctypes.c_ulong, [])            # IUnknown::Release
    except Exception:
        pass


def _variant_clear(value):
    try:
        oleaut = ctypes.WinDLL("oleaut32")
        oleaut.VariantClear.argtypes = [ctypes.POINTER(UIAVARIANT)]
        oleaut.VariantClear(ctypes.byref(value))
    except Exception:
        pass


def _uia_client():
    client = getattr(_UIA_LOCAL, "client", None)
    if client is not None:
        return client
    if getattr(_UIA_LOCAL, "failed", False):
        return None
    try:
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wt.DWORD,
                                           ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoCreateInstance.restype = ctypes.c_long
        ole32.CoInitializeEx(None, 2)                    # 每个查询线程各自 STA
        client = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(_guid("{FF48DBA4-60EF-4201-AA87-54103EEF594E}")), None, 1,
                                  ctypes.byref(_guid("{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}")),
                                  ctypes.byref(client)) == 0 and client.value:
            _UIA_LOCAL.client = client
            return client
    except Exception:
        pass
    _UIA_LOCAL.failed = True
    return None


def _uia_property(element, prop):
    value = UIAVARIANT()
    ctypes.memset(ctypes.byref(value), 0, ctypes.sizeof(value))
    if _com_call(element, 10, ctypes.c_long,                # IUIAutomationElement::GetCurrentPropertyValue
                 [ctypes.c_int, ctypes.POINTER(UIAVARIANT)],
                 ctypes.c_int(prop), ctypes.byref(value)) != 0:
        return None
    return value


def _uia_rect(element):
    value = _uia_property(element, 30001)                   # UIA_BoundingRectanglePropertyId
    if value is None:
        return None
    try:
        if value.vt != 0x2005:                              # VT_ARRAY | VT_R8
            return None
        array = ctypes.c_void_p.from_buffer(value, 8).value
        if not array:
            return None
        oleaut = ctypes.WinDLL("oleaut32")
        oleaut.SafeArrayGetLBound.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_long)]
        oleaut.SafeArrayGetUBound.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_long)]
        oleaut.SafeArrayGetElement.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_long), ctypes.c_void_p]
        low, high = ctypes.c_long(), ctypes.c_long()
        if (oleaut.SafeArrayGetLBound(array, 1, ctypes.byref(low)) or
                oleaut.SafeArrayGetUBound(array, 1, ctypes.byref(high))):
            return None
        numbers = []
        for index in range(low.value, min(high.value, low.value + 3) + 1):
            number = ctypes.c_double()
            at = ctypes.c_long(index)
            if oleaut.SafeArrayGetElement(array, ctypes.byref(at), ctypes.byref(number)):
                return None
            numbers.append(number.value)
        return tuple(round(n) for n in numbers) if len(numbers) == 4 else None
    finally:
        _variant_clear(value)


def _uia_widget_rect(tb):
    """查找 AutomationId=WidgetsButton 的矩形；找不到返回 None。"""
    client = _uia_client()
    if not client:
        return None
    element = condition = found = None
    try:
        element = ctypes.c_void_p()
        if _com_call(client, 6, ctypes.c_long,               # IUIAutomation::ElementFromHandle
                     [wt.HWND, ctypes.POINTER(ctypes.c_void_p)],
                     wt.HWND(tb), ctypes.byref(element)) != 0 or not element.value:
            return None
        oleaut = ctypes.WinDLL("oleaut32")
        oleaut.SysAllocString.argtypes = [wt.LPCWSTR]
        oleaut.SysAllocString.restype = ctypes.c_void_p
        oleaut.SysFreeString.argtypes = [ctypes.c_void_p]
        text = oleaut.SysAllocString("WidgetsButton")
        value = UIAVARIANT()
        value.vt = 8                                         # VT_BSTR
        ctypes.c_void_p.from_buffer(value, 8).value = text
        condition = ctypes.c_void_p()
        try:
            hr = _com_call(client, 23, ctypes.c_long,        # IUIAutomation::CreatePropertyCondition
                           [ctypes.c_int, UIAVARIANT, ctypes.POINTER(ctypes.c_void_p)],
                           ctypes.c_int(30011), value, ctypes.byref(condition))  # UIA_AutomationIdPropertyId
        finally:
            oleaut.SysFreeString(text)
        if hr != 0 or not condition.value:
            return None
        found = ctypes.c_void_p()
        if _com_call(element, 5, ctypes.c_long,              # IUIAutomationElement::FindFirst
                     [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.c_int(4), condition, ctypes.byref(found)) != 0 or not found.value:
            return None
        return _uia_rect(found)
    finally:
        for obj in (found, condition, element):
            _com_release(obj)


def _taskbar_widgets_key():
    """会影响天气按钮位置的廉价信号：任务栏、托盘、小组件开关。"""
    tb = taskbar_hwnd()
    if not tb:
        return None
    bounds = wt.RECT()
    if not _u32.GetWindowRect(tb, ctypes.byref(bounds)):
        return None
    tray = taskbar_children().get("TrayNotifyWnd")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced") as key:
            widgets, _ = winreg.QueryValueEx(key, "TaskbarDa")
    except OSError:
        widgets = None
    return (tb, bounds.left, bounds.top, bounds.right, bounds.bottom, tray, widgets)


def _refresh_widgets_rect():
    try:
        key = _taskbar_widgets_key()
        rect = _uia_widget_rect(key[0]) if key else None
    except Exception:
        key, rect = None, None           # 读取失败时退回托盘停靠，不影响监控本身
    with _WIDGETS["lock"]:
        _WIDGETS.update(key=key, rect=rect, stamp=time.monotonic())
    return rect


def _widgets_watch_tick():
    """布局信号变了就重测；否则最多每 30 秒兜底刷新一次。"""
    key = _taskbar_widgets_key()
    with _WIDGETS["lock"]:
        fresh = (key is not None and key == _WIDGETS["key"]
                 and time.monotonic() - _WIDGETS["stamp"] < 30)
    if fresh:
        return
    try:
        rect = _uia_widget_rect(key[0]) if key else None
    except Exception:
        rect = None
    with _WIDGETS["lock"]:
        _WIDGETS.update(key=key, rect=rect, stamp=time.monotonic())


def _widgets_watch_loop(stop):
    while not stop.is_set():
        try:
            _widgets_watch_tick()
        except Exception:
            pass
        stop.wait(0.5)


def _start_widgets_watch():
    if _WIDGETS["stop"] is not None:
        return
    _WIDGETS["stop"] = threading.Event()
    threading.Thread(target=_widgets_watch_loop, args=(_WIDGETS["stop"],),
                     name="taskbar-widgets", daemon=True).start()


def taskbar_widgets_rect():
    """通知区域左侧“小组件 / 天气”按钮的 (x, y, w, h)；位置由后台持续跟随。"""
    if sys.getwindowsversion().build < 22000:
        return None
    with _WIDGETS["lock"]:
        if _WIDGETS["started"]:
            return _WIDGETS["rect"]
        _WIDGETS["started"] = True
    try:
        rect = _refresh_widgets_rect()   # 首帧同步测一次，避免先压住天气再跳开
    except Exception:
        rect = None
    _start_widgets_watch()
    return rect


def dock_right_x(tx, w, margin, tray_left, widgets=None):
    """水平任务栏上右对齐时的 x；天气按钮紧贴托盘时排在它左侧，而不是压在它上面。"""
    anchor = tray_left
    if widgets:
        left, _, width, _ = widgets
        right = left + width
        if (width > 0 and tx <= left < anchor and right <= anchor
                and anchor - right <= max(24, margin * 4)):
            anchor = left
    return max(tx + margin, anchor - margin - w)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def _pixel_proto():
    return _g32, _u32


def grab_taskbar_bgra(tb_hwnd, x, y, w, h):
    if not tb_hwnd or w <= 0 or h <= 0:
        return None
    rect = wt.RECT()
    if not _u32.GetWindowRect(tb_hwnd, ctypes.byref(rect)):
        return None
    tw, th = rect.right - rect.left, rect.bottom - rect.top
    ox, oy = x - rect.left, y - rect.top
    if min(ox, oy) < 0 or ox + w > tw or oy + h > th:
        return None
    _u32.PrintWindow.argtypes = [wt.HWND, ctypes.c_void_p, wt.UINT]
    with dib_surface(tw, th) as (_, mem, bits):
        if not _u32.PrintWindow(tb_hwnd, mem, 2):
            return None
        raw = ctypes.string_at(bits, tw * th * 4)
        return b''.join(raw[((oy + row) * tw + ox) * 4:((oy + row) * tw + ox + w) * 4]
                        for row in range(h))


def grab_bgra(x, y, w, h):
    with dib_surface(w, h) as (screen, mem, bits):
        if not _g32.BitBlt(mem, 0, 0, w, h, screen, x, y, 0x00CC0020):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(bits, w * h * 4)


def virtual_screen():
    """整个虚拟桌面的 (x, y, 宽, 高)，多显示器下也能正确贴边。"""
    u = _u32
    try:
        x = u.GetSystemMetrics(76)
        y = u.GetSystemMetrics(77)
        w = u.GetSystemMetrics(78)
        h = u.GetSystemMetrics(79)
        if w > 0 and h > 0:
            return x, y, w, h
    except Exception:
        pass
    return 0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def level_color(pct):
    if pct is None:
        return FAINT
    if pct < 60:
        return TEAL
    if pct < 85:
        return AMBER
    return RED


def fmt_speed(bps, short=False):
    if bps is None:
        return "--"
    bps = max(0.0, bps)
    if bps < 1000:
        return f"{bps:.0f} B" if short else f"{bps:.0f} B/s"
    if bps < 1000 * 1000:
        v = bps / 1000.0
        return f"{v:.0f}K" if short else f"{v:.0f} KB/s"
    if bps < 1000 ** 3:
        v = bps / 1000 ** 2
        return f"{v:.1f}M" if short else f"{v:.1f} MB/s"
    v = bps / 1000 ** 3
    return f"{v:.2f}G" if short else f"{v:.2f} GB/s"


def fmt_bytes(n):
    if n is None:
        return "--"
    value = max(0, n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


# ---------------------------------------------------------------- Windows 采样
try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk

    HAVE_PIL = True
except Exception:                                   # 没有 Pillow 时退化成纯 Tk 文字
    HAVE_PIL = False

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_g32 = ctypes.WinDLL("gdi32", use_last_error=True)


def _init_proto():
    """把所有会用到字符串/句柄的 API 原型写清楚，否则 64 位上会越界。"""
    u, g = _u32, _g32
    u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    u.FindWindowW.restype = wt.HWND
    u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int
    u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    u.GetWindowRect.restype = wt.BOOL
    u.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
    u.RegisterWindowMessageW.restype = wt.UINT
    u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                             ctypes.c_int, ctypes.c_int, wt.UINT]
    u.LoadImageW.restype = wt.HICON
    u.GetDC.restype = ctypes.c_void_p
    u.GetDC.argtypes = [ctypes.c_void_p]
    u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.IsChild.argtypes = [wt.HWND, wt.HWND]
    u.IsChild.restype = wt.BOOL
    u.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u.GetAsyncKeyState.restype = ctypes.c_short
    u.WindowFromPoint.argtypes = [wt.POINT]
    u.WindowFromPoint.restype = wt.HWND
    u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wt.UINT]
    u.SetWindowPos.restype = wt.BOOL
    u.DestroyWindow.argtypes = [wt.HWND]
    u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.DefWindowProcW.restype = ctypes.c_ssize_t
    u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wt.HWND, ctypes.c_void_p, wt.HINSTANCE, ctypes.c_void_p]
    u.CreateWindowExW.restype = wt.HWND
    k = _k32
    u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    u.GetCursorPos.restype = wt.BOOL
    k.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    k.CreateMutexW.restype = ctypes.c_void_p
    k.GetModuleHandleW.restype = ctypes.c_void_p
    k.GetTickCount64.restype = ctypes.c_ulonglong
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    k.CreateMutexW.restype = ctypes.c_void_p
    u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    u.GetCursorPos.restype = wt.BOOL
    g.GetPixel.restype = wt.DWORD
    g.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    g.CreateDIBSection.restype = ctypes.c_void_p
    g.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wt.DWORD]
    g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    g.CreateCompatibleDC.restype = ctypes.c_void_p
    g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    g.SelectObject.restype = ctypes.c_void_p
    g.DeleteObject.argtypes = [ctypes.c_void_p]
    g.DeleteDC.argtypes = [ctypes.c_void_p]
    u.IsWindow.argtypes = [wt.HWND]
    u.IsWindowVisible.argtypes = [wt.HWND]
    u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    u.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
    u.LoadIconW.restype = wt.HICON
    u.DestroyIcon.argtypes = [wt.HICON]
    u.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
    u.ValidateRect.argtypes = [wt.HWND, ctypes.c_void_p]
    u.GetForegroundWindow.restype = wt.HWND
    u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    u.UpdateLayeredWindow.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.POINTER(wt.POINT),
        ctypes.POINTER(wt.SIZE), ctypes.c_void_p, ctypes.POINTER(wt.POINT), wt.DWORD,
        ctypes.c_void_p, wt.DWORD]
    u.UpdateLayeredWindow.restype = wt.BOOL
    g.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wt.DWORD]


_init_proto()


@contextmanager
def dib_surface(w, h):
    """A checked 32-bit GDI surface; release every handle even after allocation failure."""
    if w <= 0 or h <= 0 or w > 32768 or h > 32768:
        raise ValueError("Invalid bitmap dimensions")
    screen = mem = bitmap = old = None
    try:
        screen = _u32.GetDC(0)
        if not screen:
            raise ctypes.WinError(ctypes.get_last_error())
        mem = _g32.CreateCompatibleDC(screen)
        if not mem:
            raise ctypes.WinError(ctypes.get_last_error())
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth, bi.bmiHeader.biHeight = w, -h
        bi.bmiHeader.biPlanes, bi.bmiHeader.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        bitmap = _g32.CreateDIBSection(screen, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise ctypes.WinError(ctypes.get_last_error())
        old = _g32.SelectObject(mem, bitmap)
        if not old or old == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        yield screen, mem, bits
    finally:
        if mem and old and old != ctypes.c_void_p(-1).value:
            _g32.SelectObject(mem, old)
        if bitmap:
            _g32.DeleteObject(bitmap)
        if mem:
            _g32.DeleteDC(mem)
        if screen:
            _u32.ReleaseDC(0, screen)


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


def _ft(v):
    return (v.dwHighDateTime << 32) | v.dwLowDateTime


def cpu_times():
    idle, kern, user = FILETIME(), FILETIME(), FILETIME()
    _k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    return _ft(idle), _ft(kern), _ft(user)


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD),
        ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NET_LUID(ctypes.Structure):
    _fields_ = [("Value", ctypes.c_ulonglong)]


class MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", NET_LUID),
        ("InterfaceIndex", wt.ULONG),
        ("InterfaceGuid", GUID),
        ("Alias", wt.WCHAR * 257),
        ("Description", wt.WCHAR * 257),
        ("PhysicalAddressLength", wt.ULONG),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
        ("Mtu", wt.ULONG),
        ("Type", wt.ULONG),
        ("TunnelType", wt.ULONG),
        ("MediaType", wt.ULONG),
        ("PhysicalMediumType", wt.ULONG),
        ("AccessType", wt.ULONG),
        ("DirectionType", wt.ULONG),
        ("Flags", ctypes.c_ubyte),
        ("OperStatus", wt.ULONG),
        ("AdminStatus", wt.ULONG),
        ("MediaConnectState", wt.ULONG),
        ("NetworkGuid", GUID),
        ("ConnectionType", wt.ULONG),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
        ("InOctets", ctypes.c_ulonglong),
        ("InUcastPkts", ctypes.c_ulonglong),
        ("InNUcastPkts", ctypes.c_ulonglong),
        ("InDiscards", ctypes.c_ulonglong),
        ("InErrors", ctypes.c_ulonglong),
        ("InUnknownProtos", ctypes.c_ulonglong),
        ("InUcastOctets", ctypes.c_ulonglong),
        ("InMulticastOctets", ctypes.c_ulonglong),
        ("InBroadcastOctets", ctypes.c_ulonglong),
        ("OutOctets", ctypes.c_ulonglong),
        ("OutUcastPkts", ctypes.c_ulonglong),
        ("OutNUcastPkts", ctypes.c_ulonglong),
        ("OutDiscards", ctypes.c_ulonglong),
        ("OutErrors", ctypes.c_ulonglong),
        ("OutUcastOctets", ctypes.c_ulonglong),
        ("OutMulticastOctets", ctypes.c_ulonglong),
        ("OutBroadcastOctets", ctypes.c_ulonglong),
        ("OutQLen", ctypes.c_ulonglong),
    ]


_iphlp.GetIfTable2.restype = wt.DWORD
_iphlp.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
_iphlp.FreeMibTable.argtypes = [ctypes.c_void_p]

_NET_SKIP = (
    "loopback", "isatap", "teredo", "6to4", "pseudo", "虚拟", "virtual", "hyper-v",
    "vswitch", "vmware", "virtualbox", "docker", "wsl", "npcap", "tap-", "vpn",
    "bluetooth", "蓝牙", "wi-fi direct", "wifi direct", "本地连接*", "local area connection*",
    "wfp", "qos packet", "lightweight filter", "native wifi", "filter driver",
)


def net_io():
    """返回 (收到的总字节, 发出的总字节, [接口名])"""
    p = ctypes.c_void_p()
    if _iphlp.GetIfTable2(ctypes.byref(p)) != 0 or not p.value:
        return 0, 0, []
    base = p.value
    try:
        count = ctypes.cast(base, ctypes.POINTER(wt.ULONG))[0]
        stride = ctypes.sizeof(MIB_IF_ROW2)
        rows = []
        for i in range(int(count)):
            row = MIB_IF_ROW2.from_address(base + 8 + i * stride)
            alias = row.Alias or ""
            desc = row.Description or ""
            if row.OperStatus != 1 or row.Mtu == 0:
                continue
            if row.Type in (24, 23, 131) or not row.Flags & 1 or row.Flags & 2:
                continue
            low = (alias + " " + desc).lower()
            if any(k in low for k in _NET_SKIP):
                continue
            rows.append((len(alias), alias, row.InOctets, row.OutOctets, row.InterfaceLuid.Value))
        rows.sort(key=lambda r: r[0])
        rx = tx = 0
        seen = set()
        names = []
        for _, alias, i_o, o_o, luid in rows:
            # Distinct adapters can have identical counters; identity, not traffic, is the key.
            key = luid
            if key in seen:
                continue
            seen.add(key)
            rx += i_o
            tx += o_o
            names.append(alias)
        return rx, tx, names
    finally:
        _iphlp.FreeMibTable(ctypes.c_void_p(base))


def disk_free(path="C:\\"):
    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    ok = _k32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p(path), ctypes.byref(free), ctypes.byref(total), None
    )
    if not ok:
        return None, None
    return free.value, total.value


def uptime_seconds():
    return _k32.GetTickCount64() / 1000.0


# ---------------------------------------------------------------- GPU 读取
class GpuReader(threading.Thread):
    """常驻线程：用 nvidia-smi 的循环输出刷新 GPU 状态（不阻塞界面）。"""

    FIELDS = "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,fan.speed"

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.state = {
            "ok": False, "name": "", "util": None, "mem_used": None,
            "mem_total": None, "temp": None, "power": None, "fan": None, "ts": 0,
        }
        self._stop_event = threading.Event()

    @staticmethod
    def _num(txt):
        txt = txt.strip()
        if not txt or txt.startswith("["):
            return None
        try:
            value = float(txt)
            return value if math.isfinite(value) else None
        except ValueError:
            return None

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)
            self.state["ts"] = time.time()

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _try_name(self):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--id=0", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=6,
                creationflags=CREATE_NO_WINDOW,
            )
            name = (out.stdout or "").strip().splitlines()
            if name:
                self._set(name=name[0].strip())
        except Exception:
            pass

    def run(self):
        first = True
        fails = 0
        while not self._stop_event.is_set():
            if fails >= 3 and self._stop_event.wait(30):
                return
            lines = 0
            try:
                proc = subprocess.Popen(
                    ["nvidia-smi", "--id=0", f"--query-gpu={self.FIELDS}",
                     "--format=csv,noheader,nounits", "-lms", "900"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, creationflags=CREATE_NO_WINDOW,
                )
                self._proc = proc
                if first:
                    self._try_name()
                    first = False
                for line in proc.stdout:
                    if self._stop_event.is_set():
                        break
                    parts = [p for p in line.strip().split(",")]
                    if len(parts) < 5:
                        continue
                    lines += 1
                    self._set(
                        ok=True,
                        util=self._num(parts[0]),
                        mem_used=self._num(parts[1]),
                        mem_total=self._num(parts[2]),
                        temp=self._num(parts[3]),
                        power=self._num(parts[4]),
                        fan=self._num(parts[5]) if len(parts) > 5 else None,
                    )
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                    proc.stdout.close()
                except Exception:
                    pass
                fails = 0 if lines else fails + 1
            except Exception:
                fails += 1
            if not self._stop_event.is_set():
                self._set(ok=False, util=None, temp=None, mem_used=None, mem_total=None, power=None, fan=None)
                self._stop_event.wait(4)

    def stop(self):
        self._stop_event.set()
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


# ---------------------------------------------------------------- CPU 温度读取
PS_LHM = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$s=Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor;"
    "if(-not $s){$s=Get-CimInstance -Namespace root/OpenHardwareMonitor -ClassName Sensor};"
    "$v=$s | Where-Object {$_.SensorType -eq 'Temperature' -and $_.Name -match 'CPU Package|CPU Core|Core Average|Package'} |"
    "Sort-Object -Descending Value | Select-Object -First 1 -ExpandProperty Value;"
    "if($v){[math]::Round($v,0)}"
)
PS_ACPI = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$t=Get-CimInstance -Namespace root/WMI -ClassName MSAcpi_ThermalZoneTemperature |"
    "Select-Object -First 1 -ExpandProperty CurrentTemperature;"
    "if($t){[math]::Round($t/10-273.15,0)}"
)


class TempReader(threading.Thread):
    """探测可用的 CPU 温度来源（LibreHardwareMonitor / ACPI 热区），可用则周期刷新。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.value = None
        self.source = ""
        self._stop_event = threading.Event()

    def snapshot(self):
        with self.lock:
            return self.value, self.source

    def _ps(self, script):
        proc = None
        try:
            if self._stop_event.is_set():
                return None
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                creationflags=CREATE_NO_WINDOW,
            )
            self._proc = proc
            if self._stop_event.is_set():
                proc.terminate()
            stdout, _ = proc.communicate(timeout=12)
            txt = (stdout or "").strip()
            if txt:
                return float(txt.splitlines()[-1].strip())
        except Exception:
            pass
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()
        return None

    def run(self):
        for script, name in ((PS_LHM, "LibreHardwareMonitor"), (PS_ACPI, "ACPI")):
            if self._stop_event.is_set():
                return
            v = self._ps(script)
            if v and 10 < v < 120:
                with self.lock:
                    self.value, self.source = v, name
                while not self._stop_event.is_set():
                    if self._stop_event.wait(3):
                        return
                    nv = self._ps(script)
                    with self.lock:
                        self.value = nv if nv is not None and 10 < nv < 120 else None
                return
        with self.lock:
            self.source = ""

    def stop(self):
        self._stop_event.set()

        proc = getattr(self, "_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


# ---------------------------------------------------------------- 汇总采样
# ---------------------------------------------------------------- 托盘图标
class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wt.HICON),
    ]


class TrayIcon:
    """任务栏通知区图标：左键展开/收起详情，右键弹菜单。"""

    WM_TRAY = 0x8000 + 1
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 1, 2, 4, 0x80
    WM_LBUTTONUP, WM_RBUTTONUP, WM_RBUTTONDOWN, WM_CONTEXTMENU = 0x0202, 0x0205, 0x0204, 0x007B

    def __init__(self, app, icon_path=None):
        self.app = app
        self.ok = False
        self.hwnd = None
        self._proc = None
        self._tip = ""
        self._msg_taskbar = 0
        self._class = f"DshMonitorTray{os.getpid()}.{id(self):x}"
        self._icon_path = icon_path
        try:
            self._install()
        except Exception:
            self.ok = False

    # -------------------------------------------------- 内部
    def _install(self):
        u = _u32
        k = _k32
        shell = _shell32
        hinst = k.GetModuleHandleW(None)
        self._hinst = hinst

        # 明确原型，否则回调里 64 位参数会溢出
        u.RegisterClassW.argtypes = [ctypes.c_void_p]
        shell.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell.Shell_NotifyIconW.restype = wt.BOOL
        self._msg_taskbar = u.RegisterWindowMessageW("TaskbarCreated")

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        def proc(hwnd, msg, wp, lp):
            # 回调参数是无符号 64 位，转成有符号再传给原生函数，否则会溢出
            try:
                lp_s = lp - (1 << 64) if lp and lp > 0x7FFFFFFFFFFFFFFF else lp
            except Exception:
                lp_s = lp
            try:
                self._on_msg(hwnd, msg, wp, lp_s)
            except Exception:
                pass
            try:
                return u.DefWindowProcW(hwnd, msg, wp, lp_s)
            except Exception:
                return 0

        self._proc = WNDPROC(proc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wt.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR),
            ]

        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = self._class
        atom = u.RegisterClassW(ctypes.byref(wc))
        if os.environ.get("DM_DEBUG"):
            print(f"[tray] hinst={hinst} atom={atom} err={ctypes.get_last_error()}",
                  file=sys.stderr)

        # 普通隐藏窗口（不是 message-only，才能收到 TaskbarCreated 广播）
        self.hwnd = u.CreateWindowExW(
            0x80, self._class, "desktop-monitor", 0x80000000,  # WS_EX_TOOLWINDOW / WS_POPUP
            0, 0, 0, 0, None, None, hinst, None,
        )
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")
        self._icon = self._load_icon()
        self._add_icon()

    def _load_icon(self):
        u = _u32
        u.LoadImageW.restype = wt.HICON
        if self._icon_path and os.path.exists(self._icon_path):
            h = u.LoadImageW(None, self._icon_path, 1, 0, 0, 0x10 | 0x40)  # IMAGE_ICON
            if h:
                self._owns_icon = True
                return h
        self._owns_icon = False
        return u.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION

    def _nid(self, tip=None, flags=None):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags if flags is not None else (self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP)
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = getattr(self, "_icon", None)
        nid.szTip = (tip or self._tip or APP_NAME)[:127]
        return nid

    def _add_icon(self):
        nid = self._nid()
        ok = _shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid))
        self.ok = bool(ok)
        if os.environ.get("DM_DEBUG"):
            print(f"[tray] NIM_ADD -> {ok} hwnd={self.hwnd} tip={self._tip!r}", file=sys.stderr)

    def update_tip(self, text):
        if not self.ok or not text or text == self._tip:
            return
        self._tip = text[:127]
        nid = self._nid(tip=self._tip, flags=self.NIF_TIP)
        _shell32.Shell_NotifyIconW(self.NIM_MODIFY, ctypes.byref(nid))

    def _on_msg(self, hwnd, msg, wp, lp):
        if msg == self.WM_TRAY:
            if lp == self.WM_LBUTTONUP:
                self.app.ui_call("toggle")
            elif lp in (self.WM_RBUTTONUP, self.WM_CONTEXTMENU):
                self.app.ui_call("menu")
        elif msg == self._msg_taskbar:
            # 资源管理器重启后把图标补回去
            self.app.ui_call("readd_icon")

    def rect(self):
        if not self.ok or not self.hwnd:
            return None
        class NOTIFYICONIDENTIFIER(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("guidItem", GUID)]
        identifier = NOTIFYICONIDENTIFIER()
        identifier.cbSize, identifier.hWnd, identifier.uID = ctypes.sizeof(identifier), self.hwnd, 1
        rect = wt.RECT()
        try:
            get_rect = _shell32.Shell_NotifyIconGetRect
            get_rect.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(wt.RECT)]
            get_rect.restype = ctypes.c_long
            if get_rect(ctypes.byref(identifier), ctypes.byref(rect)) == 0:
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except (AttributeError, OSError):
            pass
        return None

    def remove(self):
        try:
            nid = self._nid()
            _shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass
        try:
            if self.hwnd:
                _u32.DestroyWindow(self.hwnd)
                self.hwnd = None
            if getattr(self, "_owns_icon", False) and getattr(self, "_icon", None):
                _u32.DestroyIcon(self._icon)
                self._icon = None
            _u32.UnregisterClassW(self._class, self._hinst)
        except Exception:
            pass
        self.ok = False



def log_exc(exc_type, exc, tb):
    """把异常写进 error.log，方便事后查（窗口版看不到 stderr）。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, "error.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n[%s]\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            traceback.print_exception(exc_type, exc, tb, file=f)
    except Exception:
        pass


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wt.BYTE),
        ("BatteryFlag", wt.BYTE),
        ("BatteryLifePercent", wt.BYTE),
        ("SystemStatusFlag", wt.BYTE),
        ("BatteryLifeTime", wt.DWORD),
        ("BatteryFullLifeTime", wt.DWORD),
    ]


def battery_status():
    """笔记本电池；台式机返回 None。"""
    try:
        st = SYSTEM_POWER_STATUS()
        if not _k32.GetSystemPowerStatus(ctypes.byref(st)):
            return None
        if st.BatteryLifePercent == 255 or st.BatteryFlag & 128:
            return None
        return {"pct": int(st.BatteryLifePercent), "ac": st.ACLineStatus == 1}
    except Exception:
        return None


class Stats:
    def __init__(self):
        self.prev_cpu = cpu_times()
        rx, tx, names = net_io()
        self.prev_net = (rx, tx, time.monotonic())
        self.net_names = names
        self.proc = ProcReader()
        self.total_down = 0.0
        self.total_up = 0.0
        self.peak_cpu = 0.0
        self.peak_mem = 0.0
        self.gpu = GpuReader()
        self.gpu.start()
        self.temp = TempReader()
        self.temp.start()
        self.cores = os.cpu_count() or 0

    def start(self):
        try:
            self.proc.start()
        except Exception:
            pass

    def sample(self):
        now = time.monotonic()
        idle, kern, user = cpu_times()
        p_idle, p_kern, p_user = self.prev_cpu
        d_idle = idle - p_idle
        d_total = (kern - p_kern) + (user - p_user)
        cpu = clamp(100.0 * (1.0 - d_idle / d_total), 0.0, 100.0) if d_total > 0 else 0.0
        self.prev_cpu = (idle, kern, user)

        mem = MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        _k32.GlobalMemoryStatusEx(ctypes.byref(mem))
        mem_used = mem.ullTotalPhys - mem.ullAvailPhys
        mem_pct = 100.0 * mem_used / mem.ullTotalPhys if mem.ullTotalPhys else 0.0

        rx, tx, names = net_io()
        p_rx, p_tx, p_t = self.prev_net
        dt = max(0.15, now - p_t)
        # Changing adapters resets the aggregate counter; do not invent a traffic spike.
        same_ifaces = names == self.net_names
        delta_rx = max(0, rx - p_rx) if same_ifaces else 0
        delta_tx = max(0, tx - p_tx) if same_ifaces else 0
        down, up = delta_rx / dt, delta_tx / dt
        self.prev_net = (rx, tx, now)
        self.net_names = names

        self.peak_cpu = max(self.peak_cpu, cpu)
        self.peak_mem = max(self.peak_mem, mem_pct)

        self.total_down += delta_rx
        self.total_up += delta_tx

        gpu = self.gpu.snapshot()
        cpu_temp, temp_src = self.temp.snapshot()
        batt = battery_status()

        free, total = disk_free()
        procs, proc_count = self.proc.snapshot()
        return {
            "cpu": cpu,
            "cores": self.cores,
            "peak_cpu": self.peak_cpu,
            "mem_used": mem_used,
            "mem_total": mem.ullTotalPhys,
            "mem_pct": mem_pct,
            "mem_avail": mem.ullAvailPhys,
            "peak_mem": self.peak_mem,
            "total_down": self.total_down,
            "total_up": self.total_up,
            "battery": batt,
            "down": down,
            "up": up,
            "ifaces": names,
            "gpu": gpu,
            "cpu_temp": cpu_temp,
            "temp_src": temp_src,
            "disk_free": free,
            "disk_total": total,
            "uptime": uptime_seconds(),
            "procs": procs,
            "proc_count": proc_count,
        }

    def stop(self):
        self.gpu.stop()
        self.temp.stop()
        try:
            self.proc.stop()
        except Exception:
            pass


# ---------------------------------------------------------------- 界面
def dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        _u32.SetProcessDPIAware()
    except Exception:
        pass




class ProcReader(threading.Thread):
    """每 2 秒采一次：进程 CPU / 内存 / 活跃 TCP 连接数（不需要管理员权限）。"""

    SNAP = 3

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.top = []            # [(name, cpu%, mem_bytes, conns)]
        self.count = 0
        self._stop_event = threading.Event()
        self._prev = {}

    # ---------- Win32 结构
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", wt.WCHAR * 260)]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [("dwState", wt.DWORD), ("dwLocalAddr", wt.DWORD), ("dwLocalPort", wt.DWORD),
                    ("dwRemoteAddr", wt.DWORD), ("dwRemotePort", wt.DWORD), ("dwOwningPid", wt.DWORD)]

    def snapshot(self):
        with self.lock:
            return list(self.top), self.count

    def stop(self):
        self._stop_event.set()

    # ---------- 采样
    def _conns_by_pid(self):
        ip = _iphlp
        ip.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL,
                                           ctypes.c_ulong, ctypes.c_int, wt.DWORD]
        size = wt.DWORD(0)
        ip.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)
        if size.value <= 0:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if ip.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 5, 0) != 0:
            return {}
        n = ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0]
        row_size = ctypes.sizeof(self.MIB_TCPROW_OWNER_PID)
        out = {}
        for i in range(int(n)):
            row = self.MIB_TCPROW_OWNER_PID.from_buffer(buf, 4 + i * row_size)
            if row.dwState == 2:          # LISTEN 不算
                continue
            out[row.dwOwningPid] = out.get(row.dwOwningPid, 0) + 1
        return out

    def _window_pids(self):
        """有可见窗口的进程 id 集合（这类才是用户关心的"软件"）。"""
        pids = set()
        try:
            u = _u32
            WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
            u.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
            u.IsWindowVisible.argtypes = [wt.HWND]
            u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

            def cb(hwnd, _lp):
                try:
                    if u.IsWindowVisible(hwnd):
                        pid = wt.DWORD()
                        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if pid.value:
                            pids.add(pid.value)
                except Exception:
                    pass
                return True

            u.EnumWindows(WNDENUMPROC(cb), 0)
        except Exception:
            pass
        return pids

    def _sample(self):
        k = _k32
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(self.PROCESSENTRY32W)]
        k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(self.PROCESSENTRY32W)]
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.OpenProcess.restype = ctypes.c_void_p
        k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wt.FILETIME)] * 4
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(self.PROCESS_MEMORY_COUNTERS),
                                               wt.DWORD]

        conns = self._conns_by_pid()
        snap = k.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return
        now = time.monotonic()
        cpu_total = cores = None
        try:
            cores = os.cpu_count() or 1
        except Exception:
            cores = 1
        rows = []
        total = 0
        prev = {}
        ent = self.PROCESSENTRY32W()
        ent.dwSize = ctypes.sizeof(self.PROCESSENTRY32W)
        try:
            ok = k.Process32FirstW(snap, ctypes.byref(ent))
            while ok:
                pid = ent.th32ProcessID
                name = ent.szExeFile or ""
                total += 1
                if pid:
                    h = k.OpenProcess(0x1000 | 0x0400, False, pid)
                    if h:
                        try:
                            c, e, kr, us = (wt.FILETIME() for _ in range(4))
                            if k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                                 ctypes.byref(kr), ctypes.byref(us)):
                                t = ((kr.dwHighDateTime << 32) | kr.dwLowDateTime) + \
                                    ((us.dwHighDateTime << 32) | us.dwLowDateTime)
                                mc = self.PROCESS_MEMORY_COUNTERS()
                                mc.cb = ctypes.sizeof(mc)
                                mem = 0
                                if psapi.GetProcessMemoryInfo(h, ctypes.byref(mc), mc.cb):
                                    mem = mc.WorkingSetSize
                                prev[pid] = (t, mem, now, name)
                        finally:
                            k.CloseHandle(h)
                ok = k.Process32NextW(snap, ctypes.byref(ent))
        finally:
            k.CloseHandle(snap)

        old = self._prev
        self._prev = {pid: (v[0], v[1], v[2]) for pid, v in prev.items()}
        for pid, (t, mem, ts, name) in prev.items():
            o = old.get(pid)
            cpu = 0.0
            if o:
                dt = max(0.2, ts - o[2])
                cpu = 100.0 * (t - o[0]) / (dt * 1e7) / cores
            rows.append((name, max(0.0, cpu), mem, conns.get(pid, 0)))
        # 同名进程（浏览器多进程）合并成一行
        agg = {}
        for name, cpu, mem, conns in rows:
            key = name or "?"
            a = agg.get(key)
            if a:
                agg[key] = (a[0] + cpu, a[1] + mem, a[2] + conns)
            else:
                agg[key] = (cpu, mem, conns)
        merged = [(k, v[0], v[1], v[2]) for k, v in agg.items()]
        merged.sort(key=lambda r: (-r[1], -r[2]))
        with self.lock:
            self.top = merged[:8]
            self.count = total

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._sample()
            except Exception:
                if os.environ.get("DM_DEBUG"):
                    import traceback
                    traceback.print_exc()
            for _ in range(int(self.SNAP * 10)):
                if self._stop_event.is_set():
                    return
                time.sleep(0.1)

class LayeredStrip:
    WM_NCHITTEST = 0x0084
    WM_MOUSEACTIVATE = 0x0021
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205
    WM_PAINT = 0x000F
    WM_ERASEBKGND = 0x0014
    HTCLIENT = 1
    MA_NOACTIVATE = 3

    def __init__(self, app, title="DshStrip"):
        self.app = app
        self.title = f"{title}.{os.getpid()}.{id(self):x}"
        self.hwnd = None
        self._proc = None
        self._hinst = None
        self.x = self.y = 0
        self.w = self.h = 0
        self._hovered = False
        self._pressed_button = None

    # ---------------- 创建
    def create(self, x, y, w, h):
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        # 回调里必须用有原型的 DefWindowProcW，否则 64 位参数溢出会让
        # 回调返回 0，WM_NCCREATE 返回 FALSE 直接导致创建窗口失败
        u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.DefWindowProcW.restype = ctypes.c_ssize_t

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        def proc(hwnd, msg, wp, lp):
            try:
                if msg == self.WM_NCHITTEST:
                    return self.HTCLIENT
                if msg == self.WM_MOUSEACTIVATE:
                    return self.MA_NOACTIVATE
                if msg == 0x0200 and not self._hovered:  # WM_MOUSEMOVE
                    self._hovered = True
                    self.app.ui_call("strip_enter")
                    class TRACKMOUSEEVENT(ctypes.Structure):
                        _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD),
                                    ("hwndTrack", wt.HWND), ("dwHoverTime", wt.DWORD)]
                    tracking = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 2, hwnd, 0)
                    _u32.TrackMouseEvent(ctypes.byref(tracking))
                    return 0
                if msg == 0x02A3:  # WM_MOUSELEAVE
                    self._hovered = False
                    self._pressed_button = None
                    self.app.ui_call("strip_leave")
                    return 0
                if msg == self.WM_PAINT:
                    _u32.ValidateRect(hwnd, None)
                    return 0
                if msg == self.WM_ERASEBKGND:
                    return 1
                if msg in (self.WM_LBUTTONDOWN, self.WM_RBUTTONDOWN):
                    self._pressed_button = msg
                    return 0
                if msg in (self.WM_LBUTTONUP, self.WM_RBUTTONUP):
                    expected = self.WM_LBUTTONDOWN if msg == self.WM_LBUTTONUP else self.WM_RBUTTONDOWN
                    pressed, self._pressed_button = self._pressed_button, None
                    if pressed == expected:
                        # Never enter Tk from WndProc; ignore releases that began outside the strip.
                        self.app.ui_call("toggle" if msg == self.WM_LBUTTONUP else "menu")
                    return 0
            except Exception:
                pass
            try:
                if lp and lp > 0x7FFFFFFFFFFFFFFF:
                    lp -= 1 << 64
                return u.DefWindowProcW(hwnd, msg, wp, lp)
            except Exception:
                return 0

        self._proc = WNDPROC(proc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
            ]

        k.GetModuleHandleW.restype = ctypes.c_void_p
        self._hinst = k.GetModuleHandleW(None)

        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.RegisterClassW.restype = wt.ATOM
        u.LoadCursorW.restype = ctypes.c_void_p
        u.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = self._hinst
        wc.lpszClassName = self.title
        wc.hCursor = u.LoadCursorW(None, ctypes.c_void_p(32649))   # IDC_HAND
        atom = u.RegisterClassW(ctypes.byref(wc))
        if os.environ.get("DM_DEBUG"):
            print(f"[strip] cls={self.title} atom={atom} hinst={self._hinst}", file=sys.stderr)

        EX = 0x00080000 | 0x00000080 | 0x00000008 | 0x08000000   # LAYERED|TOOLWINDOW|TOPMOST|NOACTIVATE
        u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wt.HWND, ctypes.c_void_p, wt.HINSTANCE, ctypes.c_void_p]
        u.CreateWindowExW.restype = wt.HWND
        u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        self.hwnd = u.CreateWindowExW(EX, self.title, "monitor-strip",
                                      0x80000000,   # Paint the WS_POPUP before showing it.
                                      x, y, w, h, None, None, self._hinst, None)
        self.x, self.y, self.w, self.h = x, y, w, h
        if os.environ.get("DM_DEBUG"):
            print(f"[strip] create hwnd={self.hwnd} err={ctypes.get_last_error()}",
                  file=sys.stderr)
        return self.hwnd

    # ---------------- 绘制
    def update(self, img, x=None, y=None):
        if not self.hwnd:
            return
        if x is not None:
            self.x = int(x)
        if y is not None:
            self.y = int(y)
        if img is None:
            _u32.SetWindowPos(self.hwnd, 0, self.x, self.y, 0, 0, 0x0001 | 0x0004 | 0x0010)
            return
        w, h = img.size
        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [("BlendOp", wt.BYTE), ("BlendFlags", wt.BYTE),
                        ("SourceConstantAlpha", wt.BYTE), ("AlphaFormat", wt.BYTE)]
        with dib_surface(w, h) as (_, mem, bits):
            # DIBs use BGRA with premultiplied alpha. Pillow performs this in C.
            raw = img.convert("RGBA").convert("RGBa").tobytes("raw", "BGRa")
            ctypes.memmove(bits, raw, len(raw))
            size, dest, origin = wt.SIZE(w, h), wt.POINT(self.x, self.y), wt.POINT(0, 0)
            blend = BLENDFUNCTION(0, 0, 255, 1)
            if not _u32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(dest),
                    ctypes.byref(size), mem, ctypes.byref(origin), 0, ctypes.byref(blend), 2):
                raise ctypes.WinError(ctypes.get_last_error())
        self.w, self.h = w, h

    # ---------------- 其它
    def hide(self):
        if self.hwnd and _u32.IsWindowVisible(self.hwnd):
            _u32.ShowWindow(self.hwnd, 0)

    def show(self):
        if self.hwnd and not _u32.IsWindowVisible(self.hwnd):
            _u32.ShowWindow(self.hwnd, 8)  # SW_SHOWNA: keep the user's foreground window.

    def raise_top(self):
        if not self.hwnd or not _u32.IsWindowVisible(self.hwnd):
            return
        taskbar = taskbar_hwnd()
        if not taskbar:
            return
        # Recover from Explorer covering the strip, without competing with menus or other popups.
        for fraction in (.5, .2, .8):
            under = _u32.WindowFromPoint(wt.POINT(self.x + round(self.w * fraction), self.y + self.h // 2))
            if under and (under == taskbar or _u32.IsChild(taskbar, under)):
                _u32.SetWindowPos(self.hwnd, wt.HWND(-1), 0, 0, 0, 0,
                                 SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
                return

    def destroy(self):
        try:
            if self.hwnd:
                _u32.DestroyWindow(self.hwnd)
            if self._hinst:
                _u32.UnregisterClassW(self.title, self._hinst)
        except Exception:
            pass
        self.hwnd = None

class StripPainter:
    """Render every taskbar style/layout with one shared measurement and spacing system."""

    def __init__(self, app, width, height, **options):
        self.app, self.width, self.height = app, width, height
        self.options = {key: options.get(key, getattr(app, key, app.cfg.get(key, default)))
                        for key, default in STRIP_DEFAULTS.items()}
        self.style = self.options["style"]
        self.layout = self.options["strip_layout"]
        self.metrics = strip_metrics(getattr(app, "state", {}), items=selected_strip_items(app.cfg, **options))
        self.columns = math.ceil(len(self.metrics) / 2) if self.layout in ("classic", "micro", "duo") else len(self.metrics)
        self.dw = strip_design_width(self.layout, len(self.metrics))
        self.dh = strip_design_height(self.layout, len(self.metrics))
        self.y_scale = self.dh / 44
        self.scale = min(width / self.dw, height / self.dh)
        self.k = self.scale * 3
        self.ox, self.oy = (width - self.dw * self.scale) / 2, (height - self.dh * self.scale) / 2
        self.image = Image.new("RGBA", (width * 3, height * 3), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.image)
        self.font_factor = (1.0 if self.layout == "micro" else 0.86) * (1.08 if self.options["strip_size"] == "large" else 1.0)
        self.p = app.palette()
        self.light = self.p["light"]
        self.hover = options.get("hover", getattr(app, "_strip_hover", False))
        variants = {
            "mint": ("teal", "blue", "violet", "amber"), "blue": ("blue", "teal", "violet", "amber"),
            "violet": ("violet", "blue", "teal", "amber"), "amber": ("amber", "violet", "blue", "teal"),
            "mono": ("ink",) * 4,
        }
        self.colors = [self.p[key] for key in variants[self.options["strip_accent"]]]

    def point(self, x, y):
        return round(self.ox * 3 + x * self.k), round(self.oy * 3 + y * self.k * self.y_scale)

    def rect(self, box, fill, radius=8, outline=None, stroke=0.7):
        self.d.rounded_rectangle((*self.point(*box[:2]), *self.point(*box[2:])),
            radius=round(radius * self.k), fill=fill, outline=outline, width=max(1, round(stroke * self.k)))

    def line(self, points, color, width=1):
        self.d.line([self.point(*point) for point in points], fill=color,
                    width=max(1, round(width * self.k)), joint="curve")

    def font(self, text, size, bold=False):
        name = ("seguisb.ttf" if bold else "segoeui.ttf") if text.isascii() else ("msyhbd.ttc" if bold else "msyh.ttc")
        return self.app._font(name, max(7, round(size * self.k * self.font_factor)))

    def text(self, x, y, text, size, color, anchor="lm", bold=False):
        self.d.text(self.point(x, y), text, font=self.font(text, size, bold), fill=color, anchor=anchor)

    def value(self, x, y, metric, size, max_width, color, anchor="rm"):
        value, unit = metric["value"], metric["unit"]
        while True:
            fv, fu = self.font(value, size, True), self.font(unit, max(6.5, size * 0.65))
            total = fv.getlength(value) / self.k + (fu.getlength(unit) / self.k + 2 if unit else 0)
            if total <= max_width or size <= 7.5:
                break
            size -= 0.5
        left = x - total if anchor == "rm" else (x - total / 2 if anchor == "mm" else x)
        self.text(left, y, value, size, color, bold=True)
        if unit:
            self.text(left + fv.getlength(value) / self.k + 2, y + 1, unit,
                      max(6.5, size * 0.65), (*self.p["dim"], 255))

    def color(self, index):
        metric = self.metrics[index]
        if metric["raw"] is None:
            return self.p["faint"]
        if metric["key"] in ("cpu", "mem", "gpu", "vram", "disk", "cpu_temp", "gpu_temp") and (metric["level"] or 0) >= 88:
            return self.p["red"]
        if metric["key"] == "battery" and metric["raw"] <= 20:
            return self.p["amber"]
        return self.colors[index % len(self.colors)]

    def history(self, metric):
        return (self.app.hist.get(metric["key"]) or [])[-self.app.SPARK_N:]

    def chart(self, box, metric, color, filled=False):
        history = self.history(metric)
        if not history:
            return
        x0, y0, x1, y1 = box
        maximum = 100 if metric["key"] in ("cpu", "mem", "gpu", "vram", "disk", "battery") else (110 if metric["key"] in ("cpu_temp", "gpu_temp") else max(max(history) * 1.1, 1))
        step = (x1 - x0) / (self.app.SPARK_N - 1)
        start = x1 - (len(history) - 1) * step
        points = [(start + i * step, y1 - clamp(v / maximum, 0, 1) * (y1 - y0)) for i, v in enumerate(history)]
        if len(points) < 2:
            return
        if filled:
            self.d.polygon([self.point(*p) for p in [(start, y1)] + points + [(x1, y1)]], fill=(*color, 20))
        self.line(points, (*color, 235), 1.2)

    def indicator(self, box, metric, color):
        level = metric["level"]
        if level is None and metric["key"] in ("up", "down") and metric["raw"] is not None:
            level = metric["raw"] / max(max(self.history(metric) or [0]), 1024) * 100
        if level is None:
            return
        x0, y0, x1, y1 = box
        if self.style == "ribbon":
            y1 = y0 + 2.4
            box = (x0, y0, x1, y1)
        self.rect(box, (*color, 25), 1)
        if level > 0:
            self.rect((x0, y0, x0 + max(1.3, (x1 - x0) * clamp(level / 100, 0, 1)), y1), (*color, 205), 1)

    def background(self):
        accent = self.colors[0]
        ink = self.p["ink"]
        if self.style == "minimal":
            return
        if self.style == "tiles":
            count = self.columns
            gap, pad = 3, 2
            width = (self.dw - 2 * pad - gap * (count - 1)) / count
            for i in range(count):
                x = pad + i * (width + gap)
                self.rect((x, 2, x + width, 42), (*self.p["surface"], 240 if self.hover else 219), 9,
                          (*self.colors[i % len(self.colors)], 90 if self.hover else 35))
            return
        fill = (*self.p["surface"], 160 if self.style == "glass" else 230)
        border = (*ink, 30)
        if self.style == "outline":
            fill, border = (*accent, 10), (*accent, 125 if self.hover else 85)
        elif self.style == "neon":
            self.rect((0.5, 0.5, self.dw - 0.5, 43.5), (*accent, 20), 11)
            fill, border = (*self.p["surface"], 232), (*accent, 180)
        elif self.style == "ribbon":
            fill, border = (*self.p["surface"], 242), (*ink, 20)
        if self.hover:
            border = (*accent, 160)
        self.rect((2, 2, self.dw - 2, 42), fill, 9, border)
        if self.style == "glass":
            self.line([(13, 3), (self.dw - 13, 3)], (255, 255, 255, 100 if self.light else 38), 0.6)

    def render(self):
        self.background()
        ink, dim = (*self.p["ink"], 255), (*self.p["dim"], 255)
        indicators = (self.app.cfg.get("strip_indicators", True) and self.style not in ("minimal", "glass")) or self.style == "ribbon"
        if self.layout == "micro":
            cell = (self.dw - 4 - 2 * (self.columns - 1)) / self.columns
            tags = {"cpu": "CPU", "mem": "RAM", "gpu": "GPU", "vram": "VRAM", "disk": "磁盘",
                    "cpu_temp": "CPU°", "gpu_temp": "GPU°",
                    "down": "↓", "up": "↑", "total_down": "收", "total_up": "发",
                    "battery": "BAT", "uptime": "运行"}
            for i, original in enumerate(self.metrics):
                metric = dict(original)
                if metric["key"] in ("down", "up"):
                    metric["unit"] = metric["unit"].replace("B/s", "").strip() or ("B" if metric["raw"] is not None else "")
                group, row = divmod(i, 2)
                x, y = 2 + group * (cell + 2), 11 + row * 22
                if i == len(self.metrics) - 1 and not row:
                    y = 22
                self.text(x + 4, y, tags[metric["key"]], 7.4, dim)
                self.value(x + cell - 5, y, metric, 9.8, cell - 26, ink)
        elif self.layout == "duo":
            width = (self.dw - 4 - 5 * (self.columns - 1)) / self.columns
            for i, metric in enumerate(self.metrics):
                group, row = divmod(i, 2)
                x, y = 2 + group * (width + 5), 13 + row * 18
                if i == len(self.metrics) - 1 and not row:
                    y = 22
                color = self.color(i)
                self.rect((x + 9, y - 2, x + 12, y + 2), (*color, 255), 1.5)
                self.text(x + 17, y, metric["tag"], 7.3, dim)
                self.value(x + width - 9, y, metric, 11.5, width - 52, ink)
                if indicators:
                    self.indicator((x + 9, y + 7, x + width - 9, y + 8.2), metric, color)
        else:
            cell = (self.dw - 4) / self.columns
            for i, metric in enumerate(self.metrics):
                x, color = 2 + i * cell, self.color(i)
                center = x + cell / 2
                if i and self.style != "tiles":
                    self.line([(x, 11), (x, 33)], (*self.p["ink"], 21), 0.6)
                if self.layout == "inline":
                    self.text(center, 12, metric["label"], 7.2, (*color, 255), "mm")
                    self.value(center, 27, metric, 14, cell - 15, ink, "mm")
                    if indicators:
                        self.indicator((x + 10, 38, x + cell - 10, 39.2), metric, color)
                elif self.layout == "gauges":
                    if metric["level"] is not None:
                        radius = 8
                        y_radius = radius / self.y_scale
                        box = (*self.point(center - radius, 16 - y_radius), *self.point(center + radius, 16 + y_radius))
                        self.d.arc(box, 0, 360, fill=(*color, 35), width=max(1, round(2.3 * self.k)))
                        if metric["level"] > 0:
                            self.d.arc(box, -90, -90 + 3.6 * metric["level"], fill=(*color, 255), width=max(1, round(2.3 * self.k)))
                        self.text(center, 16, metric["value"], 9, ink, "mm", True)
                    else:
                        self.chart((x + 12, 5, x + cell - 12, 12), metric, color)
                        self.value(center, 22, metric, 12.5, cell - 14, ink, "mm")
                    caption = metric["tag"] + (" " + metric["unit"] if metric["level"] is not None else "")
                    self.text(center, 36, caption, 7.2, (*color, 255), "mm")
                else:
                    self.text(x + 8, 12, metric["tag"], 7, dim)
                    self.value(x + cell - 8, 12, metric, 10.5, cell - 35, ink)
                    self.line([(x + 8, 37), (x + cell - 8, 37)], (*color, 25), 0.7)
                    self.chart((x + 8, 24, x + cell - 8, 37), metric, color, True)
        out = self.image.resize((self.width, self.height), Image.Resampling.LANCZOS)
        opacity = float(self.app.cfg.get("alpha", 1.0))
        out.putalpha(out.getchannel("A").point(lambda value: max(1, round(value * opacity))))
        return out


class Surface:
    """Shared, DPI-aware drawing primitives for all Pillow-rendered surfaces."""

    def __init__(self, app, w, h, design_w, palette):
        self.app, self.w, self.h, self.p = app, w, h, palette
        self.k = w / design_w * 2
        self.image = Image.new("RGB", (w * 2, h * 2), palette["bg"])
        self.d = ImageDraw.Draw(self.image)

    def rect(self, box, color, radius=10, outline=None):
        self.d.rounded_rectangle([round(v * self.k) for v in box], radius=round(radius * self.k),
            fill=color, outline=outline, width=max(1, round(self.k / 2)))

    def line(self, points, color, width=1):
        self.d.line([(round(x * self.k), round(y * self.k)) for x, y in points],
                    fill=color, width=max(1, round(width * self.k)), joint="curve")

    def text(self, x, y, value, size=10, color=None, anchor="lm", bold=False, numeric=False, max_width=None):
        font = self.app._font(("seguisb.ttf" if bold else "segoeui.ttf") if numeric
                              else ("msyhbd.ttc" if bold else "msyh.ttc"), max(8, round(size * self.k)))
        value = str(value)
        if max_width is not None and font.getlength(value) > max_width * self.k:
            while value and font.getlength(value + "…") > max_width * self.k:
                value = value[:-1]
            value += "…"
        self.d.text((round(x * self.k), round(y * self.k)), value, font=font,
                    fill=color or self.p["text"], anchor=anchor)
        return font.getlength(value) / self.k

    def bar(self, box, value, color):
        self.rect(box, self.p["track"], 2)
        if value is not None and value > 0:
            x0, y0, x1, y1 = box
            self.rect((x0, y0, x0 + max(2, (x1 - x0) * clamp(value / 100, 0, 1)), y1), color, 2)

    def chart(self, box, values, color, maximum=None, area=False, grid=True):
        x0, y0, x1, y1 = box
        if grid:
            for fraction in (0, 0.5, 1):
                y = y0 + (y1 - y0) * fraction
                self.line([(x0, y), (x1, y)], self.p["line"], 0.5)
        values = values[-self.app.SPARK_N:]
        if len(values) < 2:
            return
        peak = maximum or max(max(values) * 1.15, 1024)
        step = (x1 - x0) / (self.app.SPARK_N - 1)
        start = x1 - (len(values) - 1) * step
        points = [(start + i * step, y1 - clamp(value / peak, 0, 1) * (y1 - y0))
                  for i, value in enumerate(values)]
        if area:
            self.d.polygon([(round(x * self.k), round(y * self.k))
                for x, y in [(start, y1)] + points + [(x1, y1)]],
                fill=mix_color(color, self.p["surface"], 0.88))
        self.line(points, color, 1.5)

    def icon(self, kind, x, y, color):
        if kind == "close":
            self.line([(x - 4, y - 4), (x + 4, y + 4)], color, 1.4)
            self.line([(x - 4, y + 4), (x + 4, y - 4)], color, 1.4)
        elif kind == "settings":
            for offset, knob in ((-5, 2), (0, -3), (5, 3)):
                self.line([(x - 6, y + offset), (x + 6, y + offset)], color, 1)
                self.rect((x + knob - 1.5, y + offset - 2, x + knob + 1.5, y + offset + 2), color, 1)

    def finish(self, radius=16, keyed=True):
        img = self.image.resize((self.w, self.h), Image.Resampling.LANCZOS)
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, self.w - 1, self.h - 1),
            radius=round(radius * self.k / 2), fill=255)
        return Image.composite(img, Image.new("RGB", img.size, "#ff00fe"), mask) if keyed else img


class MonitorApp:
    W, H = 420, 548          # 完整模式（96dpi 设计尺寸）
    CW, CH = 260, 100         # 紧凑模式
    STRIP_H = 48             # 任务栏条高度
    PAD = 14
    SPARK_N = 56

    def __init__(self):
        self.cfg = self.load_cfg()
        self._closing = False
        self._tick_after = self._popup_after = self._menu_after = None
        self._lower_after = None
        self._popup_item = None
        self._fs_hidden = False
        self._proc_expanded = False
        self._card_hover = None
        self._set_win = self._menu_win = None
        dpi_aware()
        self.stats = Stats()
        self.stats.start()

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        self.root.report_callback_exception = log_exc
        try:
            self.root.iconbitmap(os.path.join(RESOURCES, "icon.ico"))
        except tk.TclError:
            pass
        try:
            self.root.wm_attributes("-transparentcolor", "#ff00fe")
        except Exception:
            pass
        try:
            self.root.wm_attributes("-alpha", float(self.cfg.get("alpha", CARD_ALPHA)))
        except Exception:
            pass

        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        # taskbar = 贴在任务栏上沿的小横条（默认） / card = 悬浮卡片 / mini = 浮动紧凑条
        self.mode = self.cfg.get("mode") or ("mini" if self.cfg.get("compact") else "taskbar")
        if self.mode not in ("taskbar", "card", "mini", "tray"):
            self.mode = "taskbar"
        self.layer = self.cfg.get("layer", "top")      # top / normal / bottom
        self.interval = int(self.cfg.get("interval", 1000))
        self.locked = bool(self.cfg.get("locked", False))
        self._drag = None
        self._ignore_release = False
        self._fonts = {}
        self.tb_cols = []            # 任务栏底色采样
        self._bg_img = None
        self._bg_pil = None
        self._photo = None
        self._img_item = None
        self._card_item = None
        self._card_key = None
        self._ui_q = queue.Queue()
        self.strip = None
        self._strip_pos = None
        self.card_win = None
        self.card_canvas = None
        self._popup_win = False
        self._mouse_down = False
        self._menu_mouse_down = False
        self.card_theme = self.cfg.get("card_theme", "dark")
        self.style = self.cfg["style"]
        self._strip_hover = False
        self.theme = self.cfg.get("theme", "auto")
        self._font_cache = {}
        self._tb_rect = None
        self.tb_light = True
        self._tb_h = None
        self.popup = False           # 详情卡片是否由托盘/任务栏条临时展开
        self._base_mode = "taskbar"
        self._close_box = None

        self.canvas = tk.Canvas(
            self.root, highlightthickness=0, bd=0, bg="#ff00fe",
            cursor="arrow" if self.locked else "hand2",
        )
        self.canvas.pack(fill="both", expand=True)
        self.root.withdraw()      # 先藏起来，第一次停靠取完任务栏底色再显形

        self._pick_fonts()
        self._build_strip()
        self.hist = {key: [] for key in STRIP_METRICS}
        self.state = {}
        self.clock = ""

        self.apply_geometry()
        self.apply_layer()
        self.root.after(2500, self.dock_loop)
        self.root.after(600, self.top_loop)

        self.tray = TrayIcon(self, os.path.join(RESOURCES, "icon.ico"))
        if self.tray.ok:
            self.tray.update_tip("桌面性能监视器 · 点一下看详情")
        elif self.mode == "tray":
            self.set_mode("card")

        try:
            os.remove(os.path.join(DATA_DIR, "disabled.flag"))
        except Exception:
            pass

        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Double-Button-1>", self.on_double)
        self.canvas.bind("<Button-3>", self.on_menu)
        self.canvas.bind("<Motion>", self.card_motion)
        self.root.update_idletasks()
        self.root.bind("<Control-q>", lambda e: self.quit())
        self.root.bind("<Escape>", lambda e: self.hide_detail() if self.popup else None)

        self.root.after(60, self._ui_poll)
        self.tick()

    def root_hwnd(self):
        return int(self.root.wm_frame(), 16)

    def work_area(self, hwnd=None):
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
        _u32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        _u32.MonitorFromWindow.restype = ctypes.c_void_p
        _u32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        mon = _u32.MonitorFromWindow(hwnd or taskbar_hwnd(), 2)
        if mon and _u32.GetMonitorInfoW(mon, ctypes.byref(info)):
            r = info.rcWork
            return r.left, r.top, r.right - r.left, r.bottom - r.top
        return virtual_screen()

    def place_window(self, win, w, h, x, y):
        win.geometry(f"{int(w)}x{int(h)}{int(x):+d}{int(y):+d}")
        if x < 0 or y < 0:
            # Tk interprets negative offsets relative to the screen's right/bottom edges.
            win.update_idletasks()
            _u32.SetWindowPos(int(win.wm_frame(), 16), 0, int(x), int(y), 0, 0,
                             SWP_NOSIZE | 0x0004 | SWP_NOACTIVATE)

    def layout_popup(self):
        x, y, w, h = self.card_rect()
        self.place_window(self.card_win, w, h, x, y)
        self.card_canvas.configure(width=w, height=h)

    def card_hit(self, x, y, scale):
        x, y = x / scale, y / scale
        for key, box in (("settings", (336, 18, 366, 48)), ("close", (374, 18, 404, 48)),
                         ("expand", (260, 374, 404, 401))):
            if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                return key
        return None

    def card_action(self, x, y, scale):
        action = self.card_hit(x, y, scale)
        if action == "settings":
            self.open_settings()
        elif action == "close":
            self.hide_detail() if self.popup else self.set_mode("tray")
        elif action == "expand":
            self._proc_expanded = not self._proc_expanded
            if self._popup_win:
                self.layout_popup()
                self.draw_card()
            else:
                self.apply_geometry()
                self.draw_full()
        return action is not None

    def card_motion(self, event, popup=False):
        scale = self.card_rect()[2] / self.W if popup else self.size()[0] / self.W
        hover = self.card_hit(event.x, event.y, scale) if popup or self.mode == "card" else None
        target = self.card_canvas if popup else self.canvas
        target.configure(cursor="hand2" if hover else ("arrow" if popup or self.locked else "hand2"))
        if hover != self._card_hover:
            self._card_hover = hover
            self.draw_card() if popup else self.draw()

    def ui_call(self, name):
        """窗口过程里只能调这个（纯 Python 入队，不碰 Tk）。"""
        try:
            self._ui_q.put(name)
        except Exception:
            pass

    def _ui_poll(self):
        try:
            while True:
                name = self._ui_q.get_nowait()
                try:
                    if name == "toggle":
                        self.toggle_detail()
                    elif name == "menu":
                        self.open_menu_at_cursor()
                    elif name in ("strip_enter", "strip_leave"):
                        self._strip_hover = name == "strip_enter"
                        self.draw_strip()
                    elif name == "readd_icon":
                        if getattr(self, "tray", None):
                            self.tray._add_icon()
                except Exception:
                    log_exc(*sys.exc_info())
        except queue.Empty:
            pass
        except Exception:
            pass
        self.root.after(60, self._ui_poll)

    # ---------------- 字体 / 缩放
    def _pick_fonts(self):
        fams = set(tkfont.families(self.root))
        cn = "Microsoft YaHei UI" if "Microsoft YaHei UI" in fams else (
            "Microsoft YaHei" if "Microsoft YaHei" in fams else "Segoe UI")
        mono = "Cascadia Mono" if "Cascadia Mono" in fams else (
            "Consolas" if "Consolas" in fams else "Courier New")
        semi = "Segoe UI Semibold" if "Segoe UI Semibold" in fams else "Segoe UI"
        self.f_cn = cn
        self.f_semi = semi
        self.f_mono = mono

    @property
    def compact(self):
        return self.mode == "mini"

    def s(self, v):
        return v * self.scale

    def font(self, size, weight="normal", family=None):
        return self._f(size, weight, family)

    def _f(self, size, weight="normal", family=None):
        size = max(5, int(round(size)))
        if weight != "normal":
            return (family or self.f_semi, size, weight)
        return (family or self.f_cn, size)

    # ---------------- 配置
    def load_cfg(self):
        data = {}
        candidates = [CFG_PATH]
        if getattr(sys, "frozen", False) and not os.environ.get("DM_CONFIG_PATH"):
            candidates.append(os.path.join(BASE, "config.json"))
        for candidate in candidates:
            try:
                with open(candidate, encoding="utf-8") as f:
                    value = json.load(f)
                if isinstance(value, dict):
                    data = value
                    break
            except (OSError, ValueError):
                pass
        old_style = data.get("style")
        if isinstance(old_style, str):
            data["style"] = {"text": "minimal", "pill": "glass"}.get(old_style, old_style)
        for key, choices in STRIP_CHOICES.items():
            if key == "strip_profile" and data.get(key) == "custom":
                continue
            if not isinstance(data.get(key), str) or data[key] not in choices:
                data[key] = STRIP_DEFAULTS[key]
        data["strip_items"] = selected_strip_items(data)
        data["strip_profile"] = strip_profile_for_items(data["strip_items"])
        for key, choices, default in (
            ("mode", ("taskbar", "card", "mini", "tray"), "taskbar"),
            ("theme", ("auto", "dark", "light"), "auto"),
            ("card_theme", tuple(CARD_THEMES), "dark"),
            ("layer", ("top", "normal", "bottom"), "top"),
        ):
            if data.get(key) not in choices:
                data[key] = "mini" if key == "mode" and data.get("compact") is True else default
        for key, default, low, high in (("interval", 1000, 500, 5000), ("alpha", 1.0, 0.3, 1.0)):
            try:
                value = float(data.get(key, default))
                data[key] = clamp(value, low, high) if math.isfinite(value) else default
            except (TypeError, ValueError, OverflowError):
                data[key] = default
        data["interval"] = int(data["interval"])
        for key in ("x", "y"):
            value = data.get(key)
            data[key] = int(value) if isinstance(value, (int, float)) and math.isfinite(value) and abs(value) < 100000 else None
        for key, default in (("locked", False), ("hide_fullscreen", True), ("strip_indicators", True)):
            data[key] = data.get(key) if isinstance(data.get(key), bool) else default
        return data

    def save_cfg(self):
        if self.mode in ("card", "mini") and not self.popup:
            try:
                self.cfg.update(x=self.root.winfo_x(), y=self.root.winfo_y())
            except tk.TclError:
                pass
        self.cfg.update(mode=self.mode, compact=self.compact, layer=self.layer,
                        interval=self.interval, locked=self.locked,
                        style=self.style, theme=self.theme, card_theme=self.card_theme)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            temp = CFG_PATH + ".tmp"
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2, allow_nan=False)
            os.replace(temp, CFG_PATH)
        except (OSError, ValueError):
            log_exc(*sys.exc_info())

    # ---------------- 窗口
    def size(self):
        if self.mode == "taskbar":
            return int(self.strip_w), int(self._tb_h or self.STRIP_H * self.scale)
        w, h = (self.CW, self.CH) if self.mode == "mini" else (self.W, self.card_design_h())
        _, _, aw, ah = self.work_area(self.root_hwnd())
        scale = min(self.scale, (aw - 16) / w, (ah - 16) / h)
        return max(1, round(w * scale)), max(1, round(h * scale))

    def apply_geometry(self):
        if self.mode == "tray":
            self.root.withdraw()
            return
        if self.mode == "taskbar":
            self.root.withdraw()
            self.dock_now(force=True)
            return
        w, h = self.size()
        ax, ay, aw, ah = self.work_area()
        x, y = self.cfg.get("x"), self.cfg.get("y")
        vx, vy, vw, vh = virtual_screen()
        if x is None or y is None or x + w < vx or x > vx + vw or y + h < vy or y > vy + vh:
            x, y = ax + aw - w - 20, ay + 40
        x, y = clamp(x, vx, vx + vw - w), clamp(y, vy, vy + vh - h)
        self.place_window(self.root, w, h, x, y)
        self.canvas.configure(width=w, height=h)
        self.show_window()

    def dock_now(self, force=False):
        if self.mode != "taskbar" or getattr(self, "_fs_hidden", False) or self._closing:
            return False
        pos = taskbar_position()
        if not pos:
            # Explorer may be restarting. Retry from dock_loop without exposing the hidden Tk root.
            return False
        tx, ty, tw, th, edge = pos
        w = self.strip_w
        margin = round(8 * self.scale)
        density = 1.0 if self.cfg["strip_layout"] == "classic" else STRIP_SIZES[self.cfg["strip_size"]][1]
        content_h = round(strip_design_height(self.cfg["strip_layout"], len(selected_strip_items(self.cfg))) * self.scale * density)
        if edge in (1, 3):
            kids = taskbar_children()
            tray = kids.get("TrayNotifyWnd")
            tray_left = tray[0] if tray else tx + tw - round(250 * self.scale)
            x = dock_right_x(tx, w, margin, tray_left, taskbar_widgets_rect())
            h = min(max(20, th - 4), content_h + 2)
            y = ty + (th - h) // 2
        else:
            h = content_h + 2
            x = tx + tw + margin if edge == 0 else tx - w - margin
            y = ty + th - h - margin
        rect = tuple(map(int, (x, y, w, h)))
        self._tb_h = h
        if self.strip is not None and self.strip.hwnd and not _u32.IsWindow(self.strip.hwnd):
            self.strip.destroy()
            self.strip = None
        try:
            if self.strip is None:
                self.strip = LayeredStrip(self)
                if not self.strip.create(*rect):
                    self.strip.destroy()
                    self.strip = None
                    return False
            changed = self._tb_rect != rect
            self._tb_rect = rect
            if changed:
                color = self.bar_color()
                self.tb_light = sum(a * b for a, b in zip(color, (0.299, 0.587, 0.114))) > 140
            if self.root.state() != "withdrawn":
                self.root.withdraw()
            if changed or force:
                self.strip.update(self._compose_strip(w, h), x, y)
            self.strip.show()
            self.strip.raise_top()
            if self._popup_win and changed:
                self.layout_popup()
            return True
        except Exception:
            log_exc(*sys.exc_info())
            return False



    def _raise_above_taskbar(self):
        """Recover a covered taskbar strip without repeatedly reordering visible windows."""
        try:
            u = _u32
            if self.strip is not None and self.strip.hwnd:
                now = time.time()
                if now - getattr(self, "_raise_ts", 0) < 1.8:
                    return
                self._raise_ts = now
                self.strip.raise_top()
                return
            hwnd = int(self.root.wm_frame(), 16)
            if not hwnd or self.root.state() == "withdrawn":
                return
            x = self.root.winfo_x() + max(4, self.root.winfo_width() // 2)
            y = self.root.winfo_y() + max(4, self.root.winfo_height() // 2)
            under = u.WindowFromPoint(wt.POINT(x, y))
            if not under or under == hwnd or u.IsChild(hwnd, under):
                return
            u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            pass

    def dock_loop(self):
        if self._closing:
            return
        if self.mode == "taskbar" and not getattr(self, "_fs_hidden", False):
            try:
                self.dock_now()
            except Exception:
                log_exc(*sys.exc_info())
        self.root.after(1200, self.dock_loop)

    def top_loop(self):
        """Check for Explorer occlusion while leaving open menus and settings undisturbed."""
        if self.mode == "taskbar" and not self._fs_hidden:
            try:
                self._raise_above_taskbar()
            except Exception:
                pass
        self.root.after(700, self.top_loop)

    def show_window(self):
        if self.mode in ("taskbar", "tray") or getattr(self, "_fs_hidden", False):
            return
        self.root.deiconify()
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", self.layer == "top")
        self.apply_card_alpha()

    def toggle_detail(self):
        if self.popup:
            self.hide_detail()
        else:
            self.show_detail()

    def _ui_trace(self, action, extra=""):
        """记录界面窗口的打开来源；文件超过 128KB 自动轮转，便于定位偶发弹窗。"""
        try:
            path = os.path.join(DATA_DIR, "dm_click.log")
            if os.path.exists(path) and os.path.getsize(path) > 128 * 1024:
                os.replace(path, path + ".1")
            chain = []
            frame = sys._getframe(1)
            for _ in range(5):
                if frame is None:
                    break
                chain.append(frame.f_code.co_name)
                frame = frame.f_back
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {action} {extra} <- {' <- '.join(chain)}\n")
        except Exception:
            pass

    def show_detail(self):
        self._ui_trace("show_detail", f"popup={self._popup_win}")
        if self.mode == "card":
            self.show_window()
            self.root.lift()
            return
        if self._popup_win:
            self.card_win.lift()
            return
        self.popup = True
        self.show_popup()

    # ---------------- 独立详情弹出窗（任务栏条不消失）
    def _ensure_card_win(self):
        if self.card_win is not None:
            return
        w = tk.Toplevel(self.root)
        w.withdraw()
        w.overrideredirect(True)
        w.configure(bg="#ff00fe")
        try:
            w.wm_attributes("-transparentcolor", "#ff00fe")
        except Exception:
            pass
        w.attributes("-topmost", True)
        c = tk.Canvas(w, bg="#ff00fe", highlightthickness=0, bd=0)
        c.pack(fill="both", expand=True)
        w.title("桌面性能监视器 · 详情")
        c.bind("<Button-1>", self.on_card_press)
        c.bind("<Motion>", lambda e: self.card_motion(e, popup=True))
        c.bind("<Button-3>", self.on_menu)
        w.bind("<Escape>", lambda e: self.hide_detail())
        w.bind("<Control-q>", lambda e: self.quit())
        self.card_win = w
        self.card_canvas = c

    def card_design_h(self):
        return self.H + (130 if self._proc_expanded else 0)

    def card_rect(self):
        ax, ay, aw, ah = self.work_area(taskbar_hwnd())
        scale = min(self.scale, (aw - 16) / self.W, (ah - 16) / self.card_design_h())
        w, h = round(self.W * scale), round(self.card_design_h() * scale)
        margin = round(10 * scale)
        pos = taskbar_position()
        anchor = self._tb_rect
        if anchor and self.mode == "taskbar":
            x = anchor[0] + anchor[2] - w
        else:
            x = ax + aw - w - margin
        y = ay + ah - h - margin
        if pos:
            tx, ty, tw, th, edge = pos
            if edge == 1:
                y = ty + th + margin
            elif edge == 0:
                x = tx + tw + margin
            elif edge == 2:
                x = tx - w - margin
        return int(clamp(x, ax, ax + aw - w)), int(clamp(y, ay, ay + ah - h)), w, h

    def show_popup(self):
        self._ui_trace("show_popup")
        self._ensure_card_win()
        self._popup_win = True
        self.popup = True
        self.layout_popup()
        self.apply_card_alpha()
        self.draw_card()
        self._mouse_down = bool(_u32.GetAsyncKeyState(0x01) & 0x8000)
        self.card_win.deiconify()
        self.card_win.lift()
        self.card_win.focus_force()
        self._popup_focus_owned = self._owns_foreground()
        if self._popup_after is None:
            self._popup_after = self.root.after(100, self._popup_watch)

    def draw_card(self):
        if not self._popup_win or self.card_canvas is None:
            return
        _, _, w, h = self.card_rect()
        self._card_photo = ImageTk.PhotoImage(self._compose_card(w, h), master=self.root)
        c = self.card_canvas
        if self._popup_item is None:
            self._popup_item = c.create_image(0, 0, image=self._card_photo, anchor="nw")
        else:
            c.itemconfigure(self._popup_item, image=self._card_photo)

    def on_card_press(self, e):
        self.card_action(e.x, e.y, self.card_rect()[2] / self.W)

    def _inside_popup(self, px, py):
        windows = [self.card_win, getattr(self, "_set_win", None), getattr(self, "_menu_win", None)]
        for win in windows:
            if win is not None and win.winfo_exists() and win.state() != "withdrawn":
                x, y = win.winfo_rootx(), win.winfo_rooty()
                if x <= px < x + win.winfo_width() and y <= py < y + win.winfo_height():
                    return True
        return self._inside_launcher(px, py)

    def _inside_launcher(self, px, py):
        tray = getattr(self, "tray", None)
        bounds = [self._tb_rect if self.mode == "taskbar" else None, tray.rect() if tray is not None else None]
        return any(x <= px < x + w and y <= py < y + h for x, y, w, h in filter(None, bounds))

    def _owns_foreground(self):
        hwnd = _u32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wt.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()

    def _popup_watch(self):
        self._popup_after = None
        if not self._popup_win or self._closing:
            return
        try:
            state = _u32.GetAsyncKeyState(0x01)
            down = bool(state & 0x8000)
            # The low bit can belong to the click that opened this popup; it is not a new press.
            clicked = down and not self._mouse_down
            self._mouse_down = down
            focused = self._owns_foreground()
            lost_focus = getattr(self, "_popup_focus_owned", False) and not focused
            if focused:
                self._popup_focus_owned = True
            if clicked or lost_focus:
                pt = wt.POINT()
                _u32.GetCursorPos(ctypes.byref(pt))
                if (clicked and not self._inside_popup(pt.x, pt.y)) or (lost_focus and not self._inside_launcher(pt.x, pt.y)):
                    self.hide_detail()
                    return
        except Exception:
            log_exc(*sys.exc_info())
        self._popup_after = self.root.after(40, self._popup_watch)

    def hide_detail(self):
        self.popup = False
        self._popup_win = False
        if self._popup_after is not None:
            self.root.after_cancel(self._popup_after)
            self._popup_after = None
        if self.card_win is not None:
            self.card_win.withdraw()
        self._card_hover = None


    def open_menu_at_cursor(self):
        p = wt.POINT()
        _u32.GetCursorPos(ctypes.byref(p))
        self.show_menu(p.x, p.y)

    def tray_menu(self):
        self.open_menu_at_cursor()

    def apply_layer(self):
        if self._lower_after is not None:
            self.root.after_cancel(self._lower_after)
            self._lower_after = None
        top = self.layer == "top"
        try:
            self.root.wm_attributes("-topmost", 1 if top else 0)
        except Exception:
            pass
        if self.layer == "bottom":
            self._lower()
            self._lower_after = self.root.after(2500, self._lower_loop)

    def _lower(self):
        try:
            hwnd = int(self.root.wm_frame(), 16)
            _u32.SetWindowPos(
                hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        except Exception:
            pass

    def _lower_loop(self):
        self._lower_after = None
        if self.layer == "bottom" and not self._closing:
            self._lower()
            self._lower_after = self.root.after(2500, self._lower_loop)

    # ---------------- 事件
    def on_press(self, e):
        self._ignore_release = False
        self._drag = None
        if self.mode == "card" and self.card_action(e.x, e.y, self.size()[0] / self.W):
            self._ignore_release = True
            return
        if self.mode not in ("taskbar", "tray") and not self.locked:
            self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def on_drag(self, e):
        if not self.locked and self._drag:
            w, h = self.size()
            self.place_window(self.root, w, h, e.x_root - self._drag[0], e.y_root - self._drag[1])

    def on_release(self, e):
        if getattr(self, "_ignore_release", False):
            self._ignore_release = False
            self._drag = None
            return
        if self.mode == "taskbar":
            self._drag = None
            self.toggle_detail()
            return
        if not self.locked:
            self.snap()
        self._drag = None
        self.save_cfg()

    def snap(self):
        w, h = self.size()
        ax, ay, aw, ah = self.work_area(self.root_hwnd())
        self.place_window(self.root, w, h,
            clamp(self.root.winfo_x(), ax + 8, ax + aw - w - 8),
            clamp(self.root.winfo_y(), ay + 8, ay + ah - h - 8))

    def on_double(self, e):
        if self.popup or self.mode == "taskbar":
            return
        self.set_mode("card" if self.mode == "mini" else "mini")

    def set_mode(self, mode):
        if mode not in ("taskbar", "tray", "card", "mini"):
            return
        if self.mode in ("card", "mini"):
            self.save_cfg()
        self.hide_detail()
        if self.strip is not None:
            self.strip.hide()
        self.mode = mode
        self._card_key = self._strip_key = None
        self._card_item = self._img_item = None
        self.canvas.delete("all")
        self.canvas.configure(cursor="arrow" if self.locked else "hand2")
        self.apply_geometry()
        self.apply_layer()
        self.root.update_idletasks()
        self.draw()
        self.save_cfg()

    def set_style(self, name):
        self.set_strip_option("style", name)

    def set_strip_option(self, key, value):
        if key not in STRIP_CHOICES or value not in STRIP_CHOICES[key]:
            return
        self.cfg[key] = value
        if key == "strip_profile":
            self.cfg["strip_items"] = list(STRIP_PROFILES[value][1])
        if key == "style":
            self.style = value
        self._apply_strip_settings()

    def set_strip_items(self, items, selected=None):
        items = clean_strip_items(items)
        if not items:
            return False
        self.cfg.update(strip_items=items, strip_profile=strip_profile_for_items(items))
        if selected is not None:
            self._settings_selected_metric = selected
        self._apply_strip_settings()
        return True

    def _apply_strip_settings(self):
        self._build_strip()
        if self.mode == "taskbar":
            self.dock_now(force=True)
        self.save_cfg()
        if self._set_win is not None:
            self.render_settings()

    def refresh_settings_preview(self):
        canvas = getattr(self, "_settings_preview_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        count = len(selected_strip_items(self.cfg))
        scale = self.scale * STRIP_SIZES[self.cfg["strip_size"]][1]
        if self.cfg["strip_layout"] == "classic":
            geometry = self._classic_geometry()
            width, height = geometry["width"], round(geometry["height"] * self.scale) + 2
        else:
            width = round(strip_design_width(self.cfg["strip_layout"], count) * scale)
            height = round(strip_design_height(self.cfg["strip_layout"], count) * scale) + 2
        bg = (235, 240, 248, 255) if self.palette()["light"] else (16, 23, 35, 255)
        image = Image.new("RGBA", (width, height), bg)
        image.alpha_composite(self._compose_strip(width, height))
        self._settings_preview = ImageTk.PhotoImage(image.convert("RGB"), master=self.root)
        canvas.configure(height=height, scrollregion=(0, 0, width, height))
        if not canvas.find_withtag("preview"):
            canvas.create_image(0, 0, image=self._settings_preview, anchor="nw", tags="preview")
        else:
            canvas.itemconfigure("preview", image=self._settings_preview)
        viewport_width = canvas.winfo_width()
        canvas.coords("preview", max(0, (viewport_width - width) // 2), 0)
        bar = self._settings_preview_scrollbar
        if width > viewport_width:
            if not bar.winfo_manager():
                bar.pack(fill="x")
        else:
            bar.pack_forget()
            canvas.xview_moveto(0)
        self._settings_preview_status.configure(text=f"{count} 项 · {width} px")

    def set_card_theme(self, name):
        if name not in CARD_THEMES:
            return
        self.card_theme = name
        self.save_cfg()
        self._card_key = None
        self.apply_card_alpha()
        self.draw()
        self.draw_card()
        if getattr(self, "_set_win", None):
            self.render_settings()

    def apply_card_alpha(self):
        alpha = float(self.cfg.get("alpha", 1.0))
        if self.card_theme == "glass":
            alpha = min(alpha, 0.94)
        for win in (self.root, self.card_win):
            if win is not None:
                win.wm_attributes("-alpha", alpha)

    def set_theme(self, name):
        if name not in ("auto", "dark", "light"):
            return
        self.theme = name
        self.cfg["theme"] = name
        self.save_cfg()
        self.draw()
        if self._set_win is not None:
            self.render_settings()

    def toggle_compact(self):
        self.set_mode("mini" if self.mode != "mini" else "card")

    def on_menu(self, e):
        self.show_menu(e.x_root, e.y_root)

    # ---------------- 自定义右键菜单（PIL 绘制）
    MENU_W = 286
    ROW_H = 34

    def menu_rows(self):
        mode_name = {"taskbar": "任务栏条", "card": "悬浮卡片", "mini": "紧凑条", "tray": "仅托盘"}[self.mode]
        return [
            ("head", "桌面性能监视器", f"v{APP_VERSION}", None, 42),
            ("item", "收起详情" if self.popup else "查看性能详情", "↗", self.toggle_detail, 36),
            ("item", "样式与布局", "设置", lambda: self.open_settings("appearance"), 36),
            ("gap", "", "", None, 10),
            ("item", "任务栏样式", STRIP_STYLES[self.style] + " ›", lambda: self.open_settings("appearance"), 36),
            ("item", "展示排版", STRIP_LAYOUTS[self.cfg["strip_layout"]][0] + " ›", lambda: self.open_settings("appearance"), 36),
            ("item", "自由选择指标", f"{len(selected_strip_items(self.cfg))} 项 ›", lambda: self.open_settings("metrics"), 36),
            ("item", "显示模式", mode_name + " ›", lambda: self.open_settings("panel"), 36),
            ("item", "面板主题", {"dark": "深空", "light": "云白", "minimal": "素纸", "glass": "雾蓝"}[self.card_theme] + " ›",
             lambda: self.open_settings("panel"), 36),
            ("gap", "", "", None, 10),
            ("item", "锁定位置", "已锁定" if self.locked else "可拖动", self.toggle_lock, 36),
            ("item", "重置位置", "", self.reset_position, 36),
            ("item", "打开任务管理器", "↗", lambda: subprocess.Popen(["taskmgr.exe"], creationflags=CREATE_NO_WINDOW), 36),
            ("item", "关于与传感器说明", "", self.show_about, 36),
            ("gap", "", "", None, 10),
            ("item", "退出监视器", "Ctrl+Q", self.quit, 36),
        ]

    def cycle(self, key):
        if key in STRIP_CHOICES:
            order = list(STRIP_CHOICES[key])
            current = self.cfg[key]
            index = order.index(current) if current in order else -1
            self.set_strip_option(key, order[(index + 1) % len(order)])
        elif key == "mode":
            order = ["taskbar", "card", "mini", "tray"]
            self.set_mode(order[(order.index(self.mode) + 1) % len(order)])
        elif key == "theme":
            order = ["auto", "dark", "light"]
            self.set_theme(order[(order.index(self.theme) + 1) % len(order)])
        elif key == "card_theme":
            order = list(CARD_THEMES)
            self.set_card_theme(order[(order.index(self.card_theme) + 1) % len(order)])
        elif key == "interval":
            order = [500, 1000, 2000, 5000]
            index = order.index(self.interval) if self.interval in order else 0
            self.set_interval(order[(index + 1) % len(order)])
        elif key == "alpha":
            order = [1.0, 0.95, 0.9, 0.8]
            current = float(self.cfg.get("alpha", 1.0))
            index = order.index(current) if current in order else 0
            self.set_alpha(order[(index + 1) % len(order)])
        elif key == "layer":
            order = ["top", "normal", "bottom"]
            self.set_layer(order[(order.index(self.layer) + 1) % len(order)])

    def show_menu(self, x=None, y=None):
        self._ui_trace("show_menu", f"existing={self._menu_win is not None}")
        if self._menu_win is not None:
            # Tray callbacks can report the same right click more than once.
            self._menu_win.lift()
            self._menu_win.focus_force()
            return
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title("桌面性能监视器 · 菜单")
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-transparentcolor", "#ff00fe")
        win.configure(bg="#ff00fe")
        canvas = tk.Canvas(win, highlightthickness=0, bd=0, bg="#ff00fe")
        canvas.pack(fill="both", expand=True)
        self._menu_win, self._menu_canvas = win, canvas
        self._menu_item = None
        self._menu_hover = -1
        self._menu_rows_data = self.menu_rows()
        dh = sum(row[4] for row in self._menu_rows_data) + 16
        ax, ay, aw, ah = self.work_area()
        scale = min(self.scale, (ah - 16) / dh)
        w, h = round(self.MENU_W * scale), round(dh * scale)
        self._menu_scale = scale
        x = self.root.winfo_pointerx() if x is None else x
        y = self.root.winfo_pointery() if y is None else y
        x, y = clamp(x, ax + 4, ax + aw - w - 4), clamp(y, ay + 4, ay + ah - h - 4)
        self._menu_rect = (x, y, w, h)
        self._menu_mouse_down = bool(_u32.GetAsyncKeyState(0x01) & 0x8000)
        self.place_window(win, w, h, x, y)
        canvas.bind("<Motion>", self._menu_motion)
        canvas.bind("<Button-1>", self._menu_click)
        canvas.bind("<Leave>", lambda e: self._menu_hover_set(-1))
        win.bind("<Escape>", lambda e: self.close_menu())
        win.bind("<Control-q>", lambda e: self.quit())
        win.bind("<Up>", lambda e: self.menu_key(-1))
        win.bind("<Down>", lambda e: self.menu_key(1))
        win.bind("<Return>", lambda e: self.menu_activate())
        self._draw_menu()
        win.deiconify()
        win.lift()
        win.focus_force()
        self._menu_focus_owned = self._owns_foreground()
        self._menu_after = self.root.after(140, self._menu_watch)

    def menu_key(self, direction):
        items = [i for i, row in enumerate(self._menu_rows_data) if row[0] == "item"]
        current = items.index(self._menu_hover) if self._menu_hover in items else (-1 if direction == 1 else 0)
        self._menu_hover_set(items[(current + direction) % len(items)])

    def menu_activate(self):
        if self._menu_hover < 0:
            return
        action = self._menu_rows_data[self._menu_hover][3]
        self.close_menu()
        if action:
            action()

    def _menu_hover_set(self, idx):
        if idx != self._menu_hover:
            self._menu_hover = idx
            self._draw_menu()

    def _menu_row_at(self, y):
        y = y / self._menu_scale
        top = 8
        for i, row in enumerate(self._menu_rows_data):
            if top <= y < top + row[4]:
                return i if row[0] == "item" else -1
            top += row[4]
        return -1

    def _menu_motion(self, e):
        self._menu_hover_set(self._menu_row_at(e.y))

    def _menu_click(self, e):
        idx = self._menu_row_at(e.y)
        if idx < 0:
            return
        action = self._menu_rows_data[idx][3]
        self.close_menu()
        if action:
            try:
                action()
            except Exception:
                log_exc(*sys.exc_info())

    def close_menu(self):
        if self._menu_after is not None:
            self.root.after_cancel(self._menu_after)
            self._menu_after = None
        if self._menu_win is not None:
            self._menu_win.destroy()
        self._menu_win = self._menu_canvas = None

    def _menu_watch(self):
        self._menu_after = None
        if self._menu_win is None or self._closing:
            return
        down = bool(_u32.GetAsyncKeyState(1) & 0x8000)
        clicked = down and not self._menu_mouse_down
        self._menu_mouse_down = down
        focused = self._owns_foreground()
        lost_focus = getattr(self, "_menu_focus_owned", False) and not focused
        if focused:
            self._menu_focus_owned = True
        if clicked or lost_focus:
            point = wt.POINT()
            _u32.GetCursorPos(ctypes.byref(point))
            x, y, w, h = self._menu_rect
            inside = x <= point.x < x + w and y <= point.y < y + h
            if (clicked and not inside) or (lost_focus and not self._inside_launcher(point.x, point.y)):
                self.close_menu()
                return
        self._menu_after = self.root.after(50, self._menu_watch)

    def _draw_menu(self):
        if self._menu_win is None:
            return
        _, _, w, h = self._menu_rect
        p = CARD_THEMES[self.card_theme]
        q = Surface(self, w, h, self.MENU_W, p)
        dh = sum(row[4] for row in self._menu_rows_data) + 16
        q.rect((0.5, 0.5, self.MENU_W - 0.5, dh - 0.5), p["bg"], 13, p["border"])
        top = 8
        for i, (kind, label, value, action, rh) in enumerate(self._menu_rows_data):
            center = top + rh / 2
            if kind == "head":
                q.text(18, center, label, 10, bold=True)
                q.text(self.MENU_W - 18, center, value, 8, p["faint"], "rm", numeric=True)
            elif kind == "gap":
                q.line([(18, center), (self.MENU_W - 18, center)], p["line"], 0.5)
            elif kind == "item":
                if i == self._menu_hover:
                    q.rect((8, top + 2, self.MENU_W - 8, top + rh - 2), p["surface"], 7)
                q.text(18, center, label, 11, p["red"] if label == "退出监视器" else p["text"])
                q.text(self.MENU_W - 18, center, value, 9, p["faint"], "rm")
            top += rh
        photo = ImageTk.PhotoImage(q.finish(13), master=self.root)
        if self._menu_item is None:
            self._menu_item = self._menu_canvas.create_image(0, 0, image=photo, anchor="nw")
        else:
            self._menu_canvas.itemconfigure(self._menu_item, image=photo)
        self._menu_photo = photo

    # ---------------- 设置窗口
    def open_settings(self, tab=None):
        self._ui_trace("open_settings", f"tab={tab} existing={self._set_win is not None}")
        if tab is not None:
            self._settings_tab = tab
        if self._set_win is not None and self._set_win.winfo_exists():
            if tab is not None:
                self.render_settings()
            self._set_win.deiconify()
            self._set_win.lift()
            self._set_win.focus_force()
            return
        self._settings_tab = tab or "appearance"
        win = tk.Toplevel(self.root)
        win.withdraw()
        self._set_win = win
        win.title("桌面性能监视器 · 偏好设置")
        win.attributes("-topmost", True)
        win.resizable(True, True)
        win.protocol("WM_DELETE_WINDOW", self._close_settings)
        win.bind("<Escape>", lambda e: self._close_settings())
        win.bind("<Control-q>", lambda e: self.quit())
        ax, ay, aw, ah = self.work_area()
        scale = min(self.scale, (aw - 40) / 460)
        w, h = round(460 * scale), min(round(560 * scale), ah - 70)
        self.place_window(win, w, h, ax + (aw - w) // 2, ay + (ah - h) // 2)
        win.minsize(min(w, round(360 * scale)), min(h, round(320 * scale)))
        self.render_settings()
        win.deiconify()
        win.lift()
        win.focus_force()

    def render_settings(self):
        win = self._set_win
        if win is None:
            return
        old_canvas = getattr(self, "_settings_canvas", None)
        same_tab = getattr(self, "_settings_rendered_tab", None) == self._settings_tab
        scroll = old_canvas.yview()[0] if same_tab and old_canvas is not None and old_canvas.winfo_exists() else 0
        self._settings_rendered_tab = self._settings_tab
        self._settings_preview_canvas = None
        for widget in win.winfo_children():
            widget.destroy()
        self._settings_vars = []
        p = CARD_THEMES[self.card_theme]
        win.configure(bg=p["bg"])
        # The title bar follows the active panel theme where DWM supports this flag.
        try:
            value = ctypes.c_int(self.card_theme in ("dark", "glass"))
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
            dwm.DwmSetWindowAttribute(int(win.wm_frame(), 16), 20, ctypes.byref(value), ctypes.sizeof(value))
        except (OSError, AttributeError):
            pass
        scale = min(self.scale, self.work_area()[2] / 480)
        px = lambda n: max(1, round(n * scale))
        font = lambda n, bold=False: (self.f_cn, -px(n), "bold" if bold else "normal")
        header = tk.Frame(win, bg=p["bg"], padx=px(14), pady=px(10))
        header.pack(fill="x")
        tk.Label(header, text="偏好设置", font=font(18, True), bg=p["bg"], fg=p["text"], anchor="w").pack(fill="x")
        navigation = tk.Frame(win, bg=p["bg"], padx=px(14))
        navigation.pack(fill="x", pady=(0, px(8)))

        def button(parent, label, command, selected=False, primary=False):
            bg = p["teal"] if primary else (p["track"] if selected else p["surface"])
            fg = p["bg"] if primary else (p["teal"] if selected else p["dim"])
            b = tk.Button(parent, text=label, command=command, font=font(12, selected or primary),
                bg=bg, fg=fg, activebackground=p["track"], activeforeground=p["text"],
                bd=0, relief="flat", highlightthickness=1, highlightbackground=p["border"],
                highlightcolor=p["teal"], cursor="hand2", padx=px(7), pady=px(5), takefocus=True)
            b.bind("<Enter>", lambda e: b.configure(bg=p["track"]) if not primary else None)
            b._normal_bg = bg
            b.bind("<Leave>", lambda e: b.configure(bg=b._normal_bg))
            return b

        def switch_tab(key):
            self._settings_tab = key
            self.render_settings()

        for i, (key, label) in enumerate((("appearance", "任务栏"), ("metrics", "指标"), ("panel", "面板"), ("behavior", "行为"), ("about", "关于"))):
            navigation.columnconfigure(i, weight=1, uniform="tabs")
            button(navigation, label, lambda k=key: switch_tab(k), self._settings_tab == key).grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else px(6), 0))

        style = ttk.Style(win)
        style.theme_use("clam")
        if self._settings_tab in ("appearance", "metrics"):
            live = tk.Frame(win, bg=p["bg"], padx=px(4), pady=px(3))
            live.pack(fill="x", padx=px(14), pady=(0, px(8)))
            top = tk.Frame(live, bg=p["bg"])
            top.pack(fill="x")
            tk.Label(top, text="实际大小预览", bg=p["bg"], fg=p["dim"], font=font(11, True)).pack(side="left")
            self._settings_preview_status = tk.Label(top, bg=p["bg"], fg=p["faint"], font=font(10))
            self._settings_preview_status.pack(side="right")
            self._settings_preview_canvas = tk.Canvas(live, bg=p["bg"], width=1, highlightthickness=0, bd=0)
            self._settings_preview_canvas.pack(fill="x", pady=(px(3), 0))
            style.layout("Monitor.Horizontal.TScrollbar", [("Horizontal.Scrollbar.trough", {
                "children": [("Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})], "sticky": "ew"})])
            style.configure("Monitor.Horizontal.TScrollbar", background=p["track"], troughcolor=p["bg"],
                            bordercolor=p["bg"], lightcolor=p["track"], darkcolor=p["track"], width=px(7), arrowsize=0)
            self._settings_preview_scrollbar = ttk.Scrollbar(live, orient="horizontal",
                command=self._settings_preview_canvas.xview, style="Monitor.Horizontal.TScrollbar")
            self._settings_preview_canvas.configure(xscrollcommand=self._settings_preview_scrollbar.set)
            self._settings_preview_canvas.bind("<Configure>", lambda e: self.refresh_settings_preview())
            self.refresh_settings_preview()

        footer = tk.Frame(win, bg=p["bg"], padx=px(14), pady=px(8))
        footer.pack(side="bottom", fill="x")
        self._settings_status = tk.Label(footer, text="更改自动保存", font=font(10), bg=p["bg"], fg=p["faint"])
        self._settings_status.pack(side="left")
        button(footer, "完成", self._close_settings, primary=True).pack(side="right")
        viewport = tk.Frame(win, bg=p["bg"])
        viewport.pack(fill="both", expand=True)
        canvas = tk.Canvas(viewport, highlightthickness=0, bd=0, bg=p["bg"])
        self._settings_canvas = canvas
        style.layout("Monitor.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
            "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})], "sticky": "ns"})])
        style.configure("Monitor.Vertical.TScrollbar", background=p["track"], troughcolor=p["bg"],
                        bordercolor=p["bg"], lightcolor=p["track"], darkcolor=p["track"], width=px(7), arrowsize=0)
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=canvas.yview, style="Monitor.Vertical.TScrollbar")
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        content = tk.Frame(canvas, bg=p["bg"], padx=px(14))
        item = canvas.create_window(0, 0, window=content, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        content.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-int(e.delta / 120), "units") if canvas.winfo_height() < content.winfo_height() and not isinstance(e.widget, (tk.Scale, tk.Listbox)) else None)

        def section(title, subtitle=""):
            frame = tk.Frame(content, bg=p["surface"], padx=px(10), pady=px(7),
                             highlightthickness=1, highlightbackground=p["border"])
            frame.pack(fill="x", pady=(0, px(6)))
            tk.Label(frame, text=title, font=font(12, True), bg=p["surface"], fg=p["text"], anchor="w").pack(fill="x")
            if subtitle:
                label = tk.Label(frame, text=subtitle, font=font(11), bg=p["surface"], fg=p["dim"], anchor="w", justify="left", wraplength=px(470))
                label.pack(fill="x", pady=(px(4), px(3)))
                frame.bind("<Configure>", lambda e: label.configure(wraplength=max(px(200), e.width - px(34))))
            return frame

        def segments(title, options, current, command, subtitle=""):
            frame = section(title, subtitle)
            row = tk.Frame(frame, bg=p["surface"])
            row.pack(fill="x", pady=(px(6), 0))
            buttons = []
            def select(value):
                command(value)
                if not row.winfo_exists():
                    return
                for val, control in buttons:
                    control._normal_bg = p["track"] if val == value else p["surface"]
                    control.configure(bg=p["track"] if val == value else p["surface"],
                                      fg=p["teal"] if val == value else p["dim"], font=font(12, val == value))
            for i, (value, label) in enumerate(options):
                row.columnconfigure(i, weight=1, uniform=title)
                control = button(row, label, lambda v=value: select(v), value == current)
                control.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else px(5), 0))
                buttons.append((value, control))
            return frame

        def slider(title, subtitle, low, high, step, current, command, format_value):
            frame = section(title, subtitle)
            row = tk.Frame(frame, bg=p["surface"])
            row.pack(fill="x", pady=(px(6), 0))
            label = tk.Label(row, text=format_value(current), bg=p["surface"], fg=p["teal"], font=font(10, True), width=7, anchor="e")
            label.pack(side="right")
            var = tk.DoubleVar(value=current)
            self._settings_vars.append(var)
            def changed(value):
                value = float(value)
                label.configure(text=format_value(value))
                command(value)
            control = tk.Scale(row, from_=low, to=high, resolution=step, variable=var,
                command=changed, orient="horizontal", showvalue=False, bg=p["surface"],
                fg=p["text"], troughcolor=p["track"], activebackground=p["teal"],
                bd=0, highlightthickness=0, sliderrelief="flat", width=px(7), sliderlength=px(17),
                length=px(230), takefocus=True)
            control.pack(side="left", fill="x", expand=True, padx=(0, px(12)))

        def toggle(title, subtitle, current, command):
            frame = section(title, subtitle)
            var = tk.BooleanVar(value=current)
            self._settings_vars.append(var)
            def changed():
                actual = command(bool(var.get()))
                if actual is not None:
                    var.set(actual)
            tk.Checkbutton(frame, text="开启", variable=var, command=changed,
                font=font(11), bg=p["surface"], fg=p["teal"], activebackground=p["surface"],
                activeforeground=p["text"], selectcolor=p["track"], bd=0,
                highlightthickness=0, cursor="hand2", anchor="w").pack(anchor="w", pady=(px(7), 0))

        if self._settings_tab == "appearance":
            frame = section("任务栏样式")
            grid = tk.Frame(frame, bg=p["surface"])
            grid.pack(fill="x", pady=(px(5), 0))
            for index, (key, name) in enumerate(STRIP_STYLES.items()):
                control = button(grid, name + (" ✓" if self.style == key else ""),
                                 lambda value=key: self.set_style(value), self.style == key)
                grid.columnconfigure(index % 3, weight=1, uniform="styles")
                control.grid(row=index // 3, column=index % 3, sticky="ew", padx=px(2), pady=px(2))
            segments("展示排版", [(key, item[0]) for key, item in STRIP_LAYOUTS.items()], self.cfg["strip_layout"],
                     lambda value: self.set_strip_option("strip_layout", value))
            frame = section("显示内容", f"已选 {len(selected_strip_items(self.cfg))} 项，可逐项勾选并调整顺序。")
            button(frame, "自由选择指标 →", lambda: switch_tab("metrics")).pack(anchor="w", pady=(px(5), 0))
            segments("强调色", list(STRIP_ACCENTS.items()), self.cfg["strip_accent"],
                     lambda value: self.set_strip_option("strip_accent", value))
            classic = self.cfg["strip_layout"] == "classic"
            segments("内容间距" if classic else "显示尺寸",
                     [(key, {"small": "紧凑", "normal": "标准", "large": "宽松"}[key] if classic else item[0]) for key, item in STRIP_SIZES.items()],
                     self.cfg["strip_size"], lambda value: self.set_strip_option("strip_size", value),
                     "首版字号固定，间距调整不会缩小文字。" if classic else "")
            segments("任务栏字色", [("auto", "自动适配"), ("dark", "深色文字"), ("light", "浅色文字")], self.theme, self.set_theme)
            def indicators(value):
                self.cfg["strip_indicators"] = value
                self.save_cfg()
                self.draw()
                self.refresh_settings_preview()
            toggle("负载刻度", "在双行和横排底部显示占用刻度，让变化一眼可见。", self.cfg["strip_indicators"], indicators)
        elif self._settings_tab == "metrics":
            items = selected_strip_items(self.cfg)
            profile = self.cfg["strip_profile"]
            segments("快捷组合" + (" · 当前自选" if profile == "custom" else ""),
                     [(key, item[0]) for key, item in STRIP_PROFILES.items()], profile,
                     lambda value: self.set_strip_option("strip_profile", value))
            frame = section("选择显示内容", "至少保留一项。减少项目会收起空位，增加项目会扩展宽度。")
            grid = tk.Frame(frame, bg=p["surface"])
            grid.pack(fill="x", pady=(px(5), 0))
            self._settings_metric_checks = {}
            for index, (key, (name, _)) in enumerate(STRIP_METRICS.items()):
                var = tk.BooleanVar(value=key in items)
                self._settings_vars.append(var)
                def changed(metric=key, variable=var):
                    current = selected_strip_items(self.cfg)
                    values = current + [metric] if variable.get() else [value for value in current if value != metric]
                    if not self.set_strip_items(values, metric if variable.get() else None):
                        variable.set(True)
                        self._settings_status.configure(text="至少保留一项显示内容", fg=p["amber"])
                control = tk.Checkbutton(grid, text=name, variable=var, command=changed,
                    font=font(12), bg=p["surface"], fg=p["text"], activebackground=p["track"],
                    activeforeground=p["text"], selectcolor=p["track"], bd=0, highlightthickness=0,
                    cursor="hand2", anchor="w", padx=px(2), pady=px(4), takefocus=True)
                column = index % 2
                grid.columnconfigure(column, weight=1, uniform="metrics")
                control.grid(row=index // 2, column=column, sticky="ew")
                self._settings_metric_checks[key] = control
            frame = section("显示顺序", "双行排版从上到下、再从左到右；横向排版从左到右。")
            row = tk.Frame(frame, bg=p["surface"])
            row.pack(fill="x", pady=(px(5), 0))
            order = tk.Listbox(row, height=min(4, len(items)), font=font(12), bg=p["bg"], fg=p["text"],
                selectbackground=p["track"], selectforeground=p["teal"], activestyle="none",
                exportselection=False, bd=0, highlightthickness=1, highlightbackground=p["border"])
            order.pack(side="left", fill="x", expand=True)
            self._settings_metric_order = order
            for index, key in enumerate(items):
                order.insert("end", f" {index + 1}.  {STRIP_METRICS[key][0]}")
            selected = getattr(self, "_settings_selected_metric", None)
            selected_index = items.index(selected) if selected in items else 0
            order.selection_set(selected_index)
            order.activate(selected_index)
            order.see(selected_index)
            controls = tk.Frame(row, bg=p["surface"])
            controls.pack(side="right", padx=(px(8), 0))
            def move(delta):
                selection = order.curselection()
                if not selection:
                    return
                index = selection[0]
                target = index + delta
                if 0 <= target < len(items):
                    values = list(items)
                    values[index], values[target] = values[target], values[index]
                    self.set_strip_items(values, items[index])
            up = button(controls, "上移 ↑", lambda: move(-1))
            up.pack(fill="x", pady=(0, px(5)))
            down = button(controls, "下移 ↓", lambda: move(1))
            down.pack(fill="x")
            def select_order(event=None):
                selection = order.curselection()
                if selection:
                    index = selection[0]
                    self._settings_selected_metric = items[index]
                    up.configure(state="normal" if index else "disabled")
                    down.configure(state="normal" if index < len(items) - 1 else "disabled")
            order.bind("<<ListboxSelect>>", select_order)
            order.bind("<Alt-Up>", lambda e: move(-1) or "break")
            order.bind("<Alt-Down>", lambda e: move(1) or "break")
            select_order()
            section("传感器说明", "显卡、温度或电池不可用时显示 —。显存和系统盘显示已用百分比；累计流量从本次启动起统计。CPU 温度可能来自 ACPI 热区，仅作参考。")
        elif self._settings_tab == "panel":
            segments("显示模式", [("taskbar", "任务栏条"), ("card", "悬浮卡片"), ("mini", "紧凑条"), ("tray", "仅托盘")], self.mode, self.set_mode)
            segments("面板主题", [("dark", "深空"), ("light", "云白"), ("minimal", "素纸"), ("glass", "雾蓝")], self.card_theme, self.set_card_theme,
                     "详情、紧凑条、菜单与设置同步使用此主题。")
            slider("不透明度", "调整监视器的通透程度，设置窗口保持清晰。", 30, 100, 5,
                   round(self.cfg["alpha"] * 100), lambda value: self.set_alpha(value / 100), lambda value: f"{value:.0f}%")
        elif self._settings_tab == "behavior":
            slider("刷新间隔", "进程约每 3 秒采样一次；温度由传感器更新。", 0.5, 5.0, 0.5,
                   self.interval / 1000, lambda v: self.set_interval(round(v * 1000)), lambda v: f"{v:g} 秒")
            segments("悬浮窗口层级", [("top", "始终置顶"), ("normal", "普通"), ("bottom", "贴桌面")], self.layer, self.set_layer)
            def lock(value):
                if self.locked != value:
                    self.toggle_lock()
            toggle("锁定位置", "固定悬浮卡片和紧凑条，避免拖动时移位。", self.locked, lock)
            def fullscreen(value):
                self.cfg["hide_fullscreen"] = value
                self.save_cfg()
                self._sync_fullscreen()
            toggle("全屏时隐藏", "在监视器所在屏幕播放全屏视频或游戏时自动隐藏。", self.cfg["hide_fullscreen"], fullscreen)
            def autostart(value):
                if self.is_autostart() != value:
                    self.toggle_autostart()
                return self.is_autostart()
            toggle("开机启动", "登录 Windows 后自动运行监视器。", self.is_autostart(), autostart)
            frame = section("找回窗口", "将悬浮窗移回可见区域，或重新停靠任务栏。")
            button(frame, "重置位置", self.reset_position).pack(anchor="w", pady=(px(9), 0))
        else:
            frame = section("桌面性能监视器", f"Desktop Monitor  ·  v{APP_VERSION}")
            for text in ("实时观察电脑负载，保持桌面简洁。", "Python + Pillow · Win32 原生采样 · MIT 开源"):
                tk.Label(frame, text=text, font=font(11), bg=p["surface"], fg=p["dim"], anchor="w").pack(fill="x", pady=(px(8), 0))
            gpu = self.state.get("gpu") or {}
            details = [f"{self.state.get('cores', 0)} 个逻辑处理器 · 可用内存 {self.state.get('mem_avail', 0) / 1024 ** 3:.1f} GB",
                       gpu.get("name") or "未检测到 NVIDIA 显卡数据"]
            sensors = []
            if gpu.get("power") is not None:
                sensors.append(f"GPU 功耗 {gpu['power']:.1f} W")
            if gpu.get("fan") is not None:
                sensors.append(f"风扇 {gpu['fan']:.0f}%")
            if self.state.get("temp_src"):
                sensors.append("温度来源 " + self.state["temp_src"])
            if sensors:
                details.append(" · ".join(sensors))
            section("当前设备", "\n".join(details))
            section("快捷操作", "左键点击任务栏条或托盘图标查看详情。右键打开菜单。\n悬浮模式可拖动，双击切换卡片与紧凑条。\nEsc 关闭弹出窗口，Ctrl+Q 退出监视器。")
            section("传感器与数据", "CPU、内存、磁盘和网速来自 Windows。网络汇总活动物理网卡。\nGPU 读取首块 NVIDIA 显卡的 nvidia-smi，其他显卡显示未提供。\nCPU 温度优先读取 LibreHardwareMonitor / OpenHardwareMonitor；ACPI 热区仅作参考，可能不代表 CPU 核心。\n进程按 CPU 占用排序并合并同名进程，内存为工作集；TCP 列是非监听连接数，不是进程网速。")
            section("本地运行", "监控数据在本机处理，不上传。温度或显卡数据不可用时显示占位，不影响其他指标。")

        def restore_scroll():
            if canvas.winfo_exists() and self._settings_canvas is canvas:
                canvas.update_idletasks()
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.yview_moveto(scroll)
        self.root.after(20, restore_scroll)

    def _close_settings(self):
        try:
            if getattr(self, "_set_win", None):
                self._set_win.destroy()
        except Exception:
            pass
        self._set_win = None


    def reset_position(self):
        self.cfg.update(x=None, y=None)
        self.apply_geometry()
        self.save_cfg()

    def show_about(self):
        self.open_settings("about")

    def set_layer(self, key):
        self.layer = key
        self.apply_layer()
        self.save_cfg()

    def set_interval(self, ms):
        self.interval = int(clamp(ms, 500, 5000))
        self.save_cfg()
        if self._tick_after is not None:
            self.root.after_cancel(self._tick_after)
        self._tick_after = self.root.after(self.interval, self.tick)
        self.draw()
        self.draw_card()

    def set_alpha(self, a):
        self.cfg["alpha"] = clamp(float(a), 0.3, 1.0)
        self.apply_card_alpha()
        self.save_cfg()
        if self.mode == "taskbar":
            self.draw_strip()

    def toggle_lock(self):
        self.locked = not self.locked
        self.canvas.configure(cursor="arrow" if self.locked else "hand2")
        self.save_cfg()

    # ---------------- 开机自启
    def _run_key(self, write=False):
        import winreg
        return winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
            winreg.KEY_READ | (winreg.KEY_SET_VALUE if write else 0),
        ), winreg

    def is_autostart(self):
        try:
            key, winreg = self._run_key()
            try:
                winreg.QueryValueEx(key, "DesktopMonitor")
                return True
            finally:
                winreg.CloseKey(key)
        except Exception:
            return False

    def toggle_autostart(self):
        import winreg
        try:
            key, _ = self._run_key(write=True)
            try:
                if self.is_autostart():
                    winreg.DeleteValue(key, "DesktopMonitor")
                else:
                    if getattr(sys, "frozen", False):
                        cmd = f'"{sys.executable}"'
                    else:
                        pw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                        exe = pw if os.path.exists(pw) else sys.executable
                        cmd = f'"{exe}" "{os.path.abspath(__file__)}"'
                    winreg.SetValueEx(key, "DesktopMonitor", 0, winreg.REG_SZ, cmd)
            finally:
                winreg.CloseKey(key)
        except OSError:
            log_exc(*sys.exc_info())
            status = getattr(self, "_settings_status", None)
            if status is not None and status.winfo_exists():
                status.configure(text="开机启动设置失败，请检查权限")

    # ---------------- 采样循环
    def tick(self):
        self._tick_after = None
        if self._closing:
            return
        try:
            self._tick_once()
        except Exception:
            log_exc(*sys.exc_info())
        finally:
            if not self._closing:
                self._tick_after = self.root.after(self.interval, self.tick)

    def _sync_fullscreen(self):
        fs = self.cfg.get("hide_fullscreen", True) and fullscreen_foreground()
        target = self.strip.hwnd if self.mode == "taskbar" and self.strip else self.root_hwnd()
        if fs and target:
            fs = same_monitor(_u32.GetForegroundWindow(), target)
        if fs == self._fs_hidden:
            return
        self._fs_hidden = fs
        if fs:
            self.hide_detail()
            self.close_menu()
            self.root.withdraw()
            if self.strip:
                self.strip.hide()
        elif self.mode == "taskbar":
            self.dock_now(force=True)
        elif self.mode != "tray":
            self.show_window()

    def _check_scale(self):
        try:
            sc = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            return
        if abs(sc - self.scale) < 0.02:
            return
        self.scale = sc
        self._font_cache.clear()
        self._build_strip()
        self._tb_rect = None
        self.apply_geometry()
        if self._popup_win:
            self.layout_popup()
        if getattr(self, "_set_win", None):
            self.render_settings()

    def _tick_once(self):
        self._scale_tick = getattr(self, "_scale_tick", 0) + 1
        if self._scale_tick % 5 == 0:
            self._check_scale()
        self._sync_fullscreen()
        try:
            self.state = self.stats.sample()
        except Exception:
            log_exc(*sys.exc_info())
        self.push_history()
        self.clock = time.strftime("%H:%M:%S")
        self.refresh_settings_preview()
        if self._scale_tick % 3 == 0:
            self.refresh_bg()
            self.update_tray_tip()
        if not self._fs_hidden:
            self.draw()
            self.draw_card()

    def push_history(self):
        for metric in strip_metrics(self.state, items=list(STRIP_METRICS)):
            h = self.hist[metric["key"]]
            val = metric["raw"]
            h.append(float(val) if val is not None else 0.0)
            if len(h) > self.SPARK_N:
                del h[: len(h) - self.SPARK_N]

    def update_tray_tip(self):
        if not getattr(self, "tray", None) or not self.tray.ok or not self.state:
            return
        st = self.state
        g = st.get("gpu") or {}
        parts = [f"CPU {st.get('cpu', 0):.0f}%", f"内存 {st.get('mem_pct', 0):.0f}%"]
        if g.get("util") is not None:
            t = f" {g['temp']:.0f}°" if g.get("temp") is not None else ""
            parts.append(f"GPU {g['util']:.0f}%{t}")
        parts.append(f"↓{fmt_speed(st.get('down'), short=True)}/s")
        parts.append(f"↑{fmt_speed(st.get('up'), short=True)}/s")
        self.tray.update_tip("  ·  ".join(parts))

    def quit(self):
        if self._closing:
            return
        self._closing = True
        self.save_cfg()
        self.hide_detail()
        self.close_menu()
        self._close_settings()
        if self.tray:
            self.tray.remove()
        if self.strip:
            self.strip.destroy()
        self.stats.stop()
        if _WIDGETS["stop"] is not None:
            _WIDGETS["stop"].set()
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "disabled.flag"), "w") as f:
                f.write("1")
        except OSError:
            pass
        for timer in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(timer)
        self.root.destroy()

    # ---------------- 绘图工具



    # ---------------- 绘制
    # ---------------- 任务栏内嵌条（PIL 抗锯齿渲染，和任务栏字体同一风格）
    def _font(self, name, size):
        key = (name, size)
        f = self._font_cache.get(key)
        if f is None:
            from PIL import ImageFont
            root = os.environ.get("WINDIR", r"C:\Windows")
            path = os.path.join(root, "Fonts", name)
            try:
                f = ImageFont.truetype(path, size)
            except Exception:
                f = ImageFont.load_default()
            self._font_cache[key] = f
        return f

    def _build_strip(self):
        density = STRIP_SIZES[self.cfg["strip_size"]][1]
        if self.cfg["strip_layout"] == "classic":
            self.strip_w = self._classic_geometry()["width"]
        else:
            self.strip_w = round(strip_design_width(self.cfg["strip_layout"], len(selected_strip_items(self.cfg))) * self.scale * density)
        self._strip_key = None
        self._img_item = None

    def _classic_geometry(self, **options):
        """Keep the first release's font sizes; compactness changes spacing, never glyph size."""
        scale = self.scale
        spacing = STRIP_SIZES[options.get("strip_size", self.cfg["strip_size"])][1]
        label_px, value_px = max(8, round(8.6 * scale)), max(10, round(12.8 * scale))
        label_font = self._font("segoeui.ttf", label_px)
        value_font = self._font("seguisb.ttf", value_px)
        metrics = strip_metrics(getattr(self, "state", {}), items=selected_strip_items(self.cfg, **options))
        labels = {"cpu": "CPU", "mem": "RAM", "gpu": "GPU", "vram": "VRAM", "disk": "DISK",
                  "cpu_temp": "CPU°", "gpu_temp": "GPU°",
                  "battery": "BAT", "uptime": "ON", "up": "↑", "down": "↓",
                  "total_down": "Σ↓", "total_up": "Σ↑"}
        records = []
        for metric in metrics:
            key, raw = metric["key"], metric["raw"]
            rate = key in ("up", "down")
            value = fmt_speed(raw, short=True) if rate else metric["value"]
            unit = "" if rate else metric["unit"]
            reserve = "999.9M" if rate else ("99d 23h" if key == "uptime" else ("999.9" if key.startswith("total_") else "100"))
            records.append(dict(metric, tag=labels[key], value=value, unit=unit, rate=rate, reserve=reserve))
        pad, gap, unit_gap = 8 * scale * spacing, 4 * scale * spacing, 2 * scale * spacing
        column_gap = 12 * scale * spacing
        columns = []
        x = pad
        for offset in range(0, len(records), 2):
            pair = records[offset:offset + 2]
            label_width = max((value_font if m["rate"] else label_font).getlength(m["tag"]) for m in pair)
            number_width = max(value_font.getlength(m["reserve"]) for m in pair)
            # No automatic font shrinking when a reading needs more room.
            number_width = max(number_width, *(value_font.getlength(m["value"]) for m in pair))
            unit_width = max(label_font.getlength(m["unit"] or ("%" if m["key"] in ("cpu", "mem", "gpu", "vram", "disk", "battery")
                                   else ("°C" if m["key"] in ("cpu_temp", "gpu_temp") else ("GB" if m["key"].startswith("total_") else "")))) for m in pair)
            number_x = x + label_width + gap
            end = number_x + number_width
            width = label_width + gap + number_width + (unit_gap + unit_width if unit_width else 0)
            columns.append(dict(label=x, number=number_x, end=end, unit=end + unit_gap, width=width, count=len(pair)))
            x += width + column_gap
        return dict(width=round(x - column_gap + pad), label_px=label_px, value_px=value_px,
                    columns=columns, records=records, height=strip_design_height("classic", len(records)),
                    separators=[c["label"] + c["width"] + column_gap / 2 for c in columns[:-1]])

    def _compose_classic(self, W, H, **options):
        geometry = self._classic_geometry(**options)
        painter = StripPainter(self, W, H, **options)
        painter.dw, painter.scale, painter.k = W / self.scale, self.scale, self.scale * 3
        painter.ox, painter.oy = 0, (H - geometry["height"] * self.scale) / 2
        painter.background()
        image, draw = painter.image, painter.d
        palette = self.palette()
        ink = (20, 22, 26) if palette["light"] else (243, 245, 248)
        dim = (77, 83, 92) if palette["light"] else (170, 180, 194)
        label = self._font("segoeui.ttf", geometry["label_px"] * 3)
        value_font = self._font("seguisb.ttf", geometry["value_px"] * 3)
        for separator in geometry["separators"]:
            draw.line([(separator * 3, H * .24 * 3), (separator * 3, H * .76 * 3)],
                      fill=(*ink, 22), width=max(1, round(self.scale)))
        for index, metric in enumerate(geometry["records"]):
            column = geometry["columns"][index // 2]
            y = H * (.5 if column["count"] == 1 else (.32 if index % 2 == 0 else .68)) * 3
            tag_color = palette["blue"] if metric["key"] == "up" else (palette["teal"] if metric["key"] == "down" else dim)
            draw.text((column["label"] * 3, y), metric["tag"], font=value_font if metric["rate"] else label,
                      fill=tag_color, anchor="lm")
            color = palette["red"] if metric["key"] == "mem" and (metric["raw"] or 0) >= 90 else ink
            if metric["rate"]:
                draw.text((column["number"] * 3, y), metric["value"], font=value_font, fill=color, anchor="lm")
            else:
                draw.text((column["end"] * 3, y), metric["value"], font=value_font, fill=color, anchor="rm")
                if metric["unit"]:
                    draw.text((column["unit"] * 3, y), metric["unit"], font=label, fill=dim, anchor="lm")
        result = image.resize((W, H), Image.Resampling.LANCZOS)
        alpha = float(self.cfg.get("alpha", 1.0))
        result.putalpha(result.getchannel("A").point(lambda opacity: max(1, round(opacity * alpha))))
        return result

    # 任务栏取色 / 文字配色
    def palette(self):
        light = self.theme == "dark" or (self.theme == "auto" and self.tb_light)
        if light:
            return dict(light=True, surface=(250, 252, 255), ink=(31, 44, 63), dim=(84, 103, 126),
                        faint=(109, 124, 145), teal=(9, 129, 108), blue=(42, 107, 202),
                        violet=(126, 81, 189), amber=(169, 110, 17), red=(194, 60, 78))
        return dict(light=False, surface=(22, 32, 48), ink=(239, 246, 255), dim=(167, 187, 211),
                    faint=(123, 145, 174), teal=(104, 226, 190), blue=(130, 181, 255),
                    violet=(197, 170, 255), amber=(249, 201, 112), red=(255, 134, 147))




    def bar_color(self):
        """实时取任务栏底色（条左右两侧各采几个点），用于胶囊填充。"""
        g, u = _pixel_proto()
        x, y, w, h = self._tb_rect or (0, 0, 0, 0)
        if not w:
            return (245, 244, 241) if self.tb_light else (32, 32, 34)
        pts = []
        hdc = u.GetDC(0)
        try:
            for dx in (-6, -3, w + 3, w + 6):
                for dy in (int(h * 0.3), int(h * 0.7)):
                    c = g.GetPixel(hdc, int(x + dx), int(y + dy)) & 0xFFFFFFFF
                    if c != 0xFFFFFFFF:
                        pts.append(((c & 0xFF), ((c >> 8) & 0xFF), ((c >> 16) & 0xFF)))
        finally:
            u.ReleaseDC(0, hdc)
        if not pts:
            return (245, 244, 241) if self.tb_light else (32, 32, 34)
        return tuple(sum(p[i] for p in pts) // len(pts) for i in range(3))

    def refresh_bg(self):
        """每几秒重新取一次任务栏底色：壁纸/主题/昼夜切换都能跟上，且不隐藏窗口。"""
        if self.mode != "taskbar" or not self._tb_rect:
            return
        try:
            c = self.bar_color()
            lum = 0.114 * c[2] + 0.587 * c[1] + 0.299 * c[0]
            light = lum > 140
            if light != self.tb_light:
                self.tb_light = light
                self._strip_key = None
            self._last_sig = None
            self.draw_strip()
        except Exception:
            pass

    def _compose_strip(self, W, H, **options):
        if options.get("strip_layout", self.cfg["strip_layout"]) == "classic":
            return self._compose_classic(W, H, **options)
        return StripPainter(self, W, H, **options).render()

    def draw_strip(self):
        if self.mode != "taskbar" or self._fs_hidden:
            return
        if self.strip is None or not self._tb_rect:
            self.dock_now(force=True)
            return
        x, y, w, h = self._tb_rect
        self.strip.update(self._compose_strip(w, h), x, y)
        self.strip.raise_top()

    def draw(self):
        if self.mode == "tray":
            return
        if self.mode == "taskbar":
            self._card_key = None
            self.draw_strip()       # 内嵌条自己管理画布，不再整块清空
            return
        if self.mode == "card":
            self._strip_key = None
            self.draw_full()
            return
        c = self.canvas
        c.delete("all")
        self._strip_key = None      # 卡片/小条会清空画布，回到任务栏时必须重建
        self._card_key = None
        if self.mode == "mini":
            self.draw_compact()
        else:
            self.draw_full()




    # ---------------- 详情卡片（PIL 渲染，整张图一次更新，不闪）
    def _compose_card(self, W, H):
        p = CARD_THEMES[self.card_theme]
        q = Surface(self, W, H, self.W, p)
        dh = self.card_design_h()
        q.rect((0.5, 0.5, 419.5, dh - 0.5), p["bg"], 17, p["border"])
        st = self.state
        cpu, mem = st.get("cpu"), st.get("mem_pct")
        gpu = st.get("gpu") or {}
        util = gpu.get("util")
        q.text(20, 29, "性能概览", 16, bold=True)
        q.text(20, 50, "DESKTOP MONITOR", 8, p["faint"], numeric=True)
        q.text(122, 50, self.clock, 8.5, p["faint"], numeric=True)
        q.rect((278, 23, 324, 43), mix_color(p["teal"], p["bg"], 0.9), 10)
        q.rect((286, 31, 290, 35), p["teal"], 2)
        q.text(296, 33, "实时", 9, p["teal"])
        for kind, x in (("settings", 336), ("close", 374)):
            hovered = self._card_hover == kind
            q.rect((x, 18, x + 30, 48), p["track"] if hovered else p["surface"], 9)
            q.icon(kind, x + 15, 33, p["red"] if hovered and kind == "close" else p["dim"])

        # Processor and its history have a full-width section of their own.
        q.rect((16, 70, 404, 176), p["surface"], 12)
        q.text(30, 90, "处理器", 10, p["dim"], bold=True)
        q.text(388, 90, f"CPU  ·  峰值 {st.get('peak_cpu', 0):.0f}%", 9, p["faint"], "rm")
        value = "—" if cpu is None else f"{cpu:.0f}"
        width = q.text(30, 127, value, 32, numeric=True, bold=True)
        q.text(34 + width, 134, "%", 12, p["dim"], numeric=True)
        temp = f"{st['cpu_temp']:.0f}°C" if st.get("cpu_temp") is not None else "温度未提供"
        if st.get("temp_src") == "ACPI" and st.get("cpu_temp") is not None:
            temp = "热区 " + temp
        q.text(30, 158, f"{st.get('cores', 0)} 线程 · {temp}", 8.5, p["faint"], max_width=152)
        q.chart((190, 111, 388, 157), self.hist["cpu"], p["teal"], 100, area=True)

        for left, right in ((16, 204), (216, 404)):
            q.rect((left, 188, right, 282), p["surface"], 12)
        q.text(30, 207, "显卡", 10, p["dim"], bold=True)
        q.text(190, 207, "GPU", 8, p["violet"], "rm", numeric=True, bold=True)
        width = q.text(30, 236, "—" if util is None else f"{util:.0f}", 25, numeric=True, bold=True)
        q.text(33 + width, 241, "%", 11, p["dim"], numeric=True)
        if util is None:
            detail = "未检测到 NVIDIA 数据"
        else:
            parts = []
            if gpu.get("temp") is not None:
                parts.append(f"{gpu['temp']:.0f}°C")
            if gpu.get("mem_used") is not None and gpu.get("mem_total"):
                parts.append(f"{gpu['mem_used'] / 1024:.1f}/{gpu['mem_total'] / 1024:.0f} GB")
            detail = " · ".join(parts) or "传感器未提供更多数据"
        q.text(30, 260, detail, 8.5, p["faint"], max_width=160)
        q.bar((30, 273, 190, 276), util, p["violet"])
        q.text(230, 207, "内存", 10, p["dim"], bold=True)
        q.text(390, 207, "RAM", 8, p["blue"], "rm", numeric=True, bold=True)
        width = q.text(230, 236, "—" if mem is None else f"{mem:.0f}", 25, numeric=True, bold=True)
        q.text(233 + width, 241, "%", 11, p["dim"], numeric=True)
        detail = (f"{st.get('mem_used', 0) / 1024 ** 3:.1f} / {st['mem_total'] / 1024 ** 3:.1f} GB 已用"
                  if st.get("mem_total") else "正在读取内存")
        q.text(230, 260, detail, 8.5, p["faint"], max_width=160)
        q.bar((230, 273, 390, 276), mem, p["red"] if (mem or 0) >= 85 else p["blue"])

        q.rect((16, 294, 404, 364), p["surface"], 12)
        q.line([(210, 307), (210, 351)], p["line"])
        for x, key, label, color in ((30, "down", "↓ 下载", p["teal"]), (230, "up", "↑ 上传", p["blue"])):
            q.text(x, 309, label, 9, color)
            q.chart((x + 102, 304, x + 160, 319), self.hist[key], color, grid=False)
            q.text(x, 332, fmt_speed(st.get(key)), 16, bold=True, numeric=True, max_width=164)
            q.text(x, 353, f"本次累计 {fmt_bytes(st.get('total_' + key, 0))}", 8, p["faint"])

        q.text(20, 388, "活跃进程", 11, bold=True)
        label = "收起列表  ↑" if self._proc_expanded else "展开全部  ↓"
        if self._card_hover == "expand":
            q.rect((304, 376, 404, 400), p["surface"], 7)
        q.text(394, 388, label, 9, p["teal"], "rm")
        q.rect((16, 402, 404, 422), p["surface"], 5)
        for x, text, anchor in ((28, "进程", "lm"), (240, "CPU", "rm"),
                                 (322, "内存", "rm"), (390, "TCP", "rm")):
            q.text(x, 412, text, 8, p["faint"], anchor)
        procs = st.get("procs") or []
        count = 8 if self._proc_expanded else 3
        for i, (name, usage, memory, connections) in enumerate(procs[:count]):
            y = 437 + i * 26
            if i % 2 == 1:
                q.rect((16, y - 12, 404, y + 12), mix_color(p["surface"], p["bg"], 0.5), 5)
            q.rect((27, y - 2.5, 32, y + 2.5), (p["teal"], p["blue"], p["violet"])[i % 3], 2)
            q.text(41, y, name.removesuffix(".exe"), 9.5, max_width=139)
            q.text(240, y, f"{usage:.1f}%", 9.5, p["amber"] if usage >= 25 else p["text"], "rm", numeric=True)
            memory_text = f"{memory / 1024 ** 3:.2f} GB" if memory >= 1024 ** 3 else f"{memory / 1024 ** 2:.0f} MB"
            q.text(322, y, memory_text, 9.5, p["dim"], "rm", numeric=True)
            q.text(390, y, str(connections) if connections else "—", 9.5,
                   p["teal"] if connections else p["faint"], "rm", numeric=True)
        if not procs:
            q.text(210, 459, "正在采集进程，请稍候…", 10, p["faint"], "mm")

        dy = 130 if self._proc_expanded else 0
        q.line([(20, 506 + dy), (400, 506 + dy)], p["line"])
        free, total = st.get("disk_free"), st.get("disk_total")
        disk = f"C: 剩余 {free / 1024 ** 3:.0f} GB" if free is not None and total else "磁盘数据未提供"
        uptime = int(st.get("uptime", 0))
        days, rem = divmod(uptime, 86400)
        runtime = f"运行 {days}天 {rem // 3600}小时" if days else f"运行 {rem // 3600}小时 {rem % 3600 // 60}分"
        q.text(20, 523 + dy, disk, 8.5, p["dim"])
        q.text(400, 523 + dy, runtime, 8.5, p["dim"], "rm")
        interfaces = " / ".join(st.get("ifaces") or []) or "无活动物理网卡"
        q.text(20, 539 + dy, interfaces, 8, p["faint"], max_width=240)
        battery = st.get("battery")
        tail = f"{'充电中' if battery['ac'] else '电池'} {battery['pct']}%" if battery else f"{st.get('proc_count', 0)} 个进程 · {self.interval / 1000:g}s 刷新"
        q.text(400, 539 + dy, tail, 8, p["faint"], "rm")
        return q.finish(17)

    def draw_full(self):
        w, h = self.size()
        self._photo = ImageTk.PhotoImage(self._compose_card(w, h), master=self.root)
        if self._card_item is None:
            self._card_item = self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        else:
            self.canvas.itemconfigure(self._card_item, image=self._photo)


    def draw_compact(self):
        w, h = self.size()
        p = CARD_THEMES[self.card_theme]
        q = Surface(self, w, h, self.CW, p)
        q.rect((0.5, 0.5, 259.5, 99.5), p["bg"], 13, p["border"])
        st = self.state
        for x, label, value, color in ((15, "CPU", st.get("cpu"), p["teal"]),
                (99, "GPU", (st.get("gpu") or {}).get("util"), p["violet"]),
                (183, "RAM", st.get("mem_pct"), p["blue"])):
            q.text(x, 17, label, 8, p["dim"], bold=True, numeric=True)
            width = q.text(x, 40, "—" if value is None else f"{value:.0f}", 22, bold=True, numeric=True)
            q.text(x + width + 2, 45, "%", 9, p["faint"], numeric=True)
            q.bar((x, 61, x + 62, 64), value, color)
        q.line([(15, 73), (245, 73)], p["line"])
        q.text(15, 87, "↓", 11, p["teal"], bold=True)
        q.text(30, 87, fmt_speed(st.get("down"), short=True) + "/s", 10, numeric=True, max_width=90)
        q.text(140, 87, "↑", 11, p["blue"], bold=True)
        q.text(155, 87, fmt_speed(st.get("up"), short=True) + "/s", 10, numeric=True, max_width=90)
        self._photo = ImageTk.PhotoImage(q.finish(13), master=self.root)
        self._img_item = self.canvas.create_image(0, 0, image=self._photo, anchor="nw")


_MUTEX_HANDLE = None


def already_running():
    """单实例：重复启动时静默退出，避免桌面上叠出好几个监视器。"""
    global _MUTEX_HANDLE
    try:
        _MUTEX_HANDLE = _k32.CreateMutexW(
            None, False, "Local\\DesktopMonitor.SingleInstance")
        return _k32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False


def main():
    if not HAVE_PIL:
        _u32.MessageBoxW(None, "缺少 Pillow。请先运行：python -m pip install -r requirements.txt", APP_NAME, 0x10)
        return
    if "--selftest" in sys.argv:
        dpi_aware()
        st = Stats()
        st.start()
        time.sleep(1.2)
        d = st.sample()
        print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
        st.stop()
        return
    if already_running():
        return
    sys.excepthook = log_exc
    try:
        threading.excepthook = lambda a: log_exc(a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass
    app = MonitorApp()
    if "--metrics" in sys.argv:
        app.root.after(350, lambda: app.open_settings("metrics"))
    elif "--settings" in sys.argv:
        app.root.after(350, app.open_settings)
    app.root.mainloop()


if __name__ == "__main__":
    main()
