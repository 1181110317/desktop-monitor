# -*- coding: utf-8 -*-
"""
桌面性能监视器 · Desktop Monitor
--------------------------------
一个常驻桌面的极简悬浮窗，实时显示：
  CPU 占用 / 内存占用 / GPU 占用与显存 / 网络上行下行 / GPU 温度 / 磁盘 / 运行时长

纯 Python 标准库实现（ctypes 直读 Windows 性能接口），零第三方依赖。
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
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import font as tkfont

APP_NAME = "桌面性能监视器"
BASE = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
CFG_PATH = os.path.join(BASE, "config.json")

CREATE_NO_WINDOW = 0x08000000
HWND_BOTTOM = 1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

# ---------------------------------------------------------------- 视觉令牌
BG = "#0d1117"
BORDER = "#252f3d"
TRACK = "#1a2230"
TXT = "#e6edf3"
DIM = "#8b98a8"
FAINT = "#5a6779"
TEAL = "#4fd6c2"
BLUE = "#5e9dff"
AMBER = "#f2b451"
RED = "#f0605e"
VIOLET = "#a78bfa"

CARD_ALPHA = 1.0

CARD_COLORS = dict(bg="#0d1117", border="#252f3d", line="#1b2431", track="#1a2230",
                   text="#e6edf3", dim="#8b98a8", faint="#5a6779",
                   teal="#4fd6c2", amber="#f2b451", red="#f0605e",
                   violet="#a78bfa", blue="#5e9dff", fill="#16292b")

CARD_THEMES = {
    "dark": CARD_COLORS,
    "light": dict(bg="#fbfaf8", border="#e4dfd6", line="#eee9e1", track="#ece7df",
                  text="#191c22", dim="#5b6270", faint="#8d949f",
                  teal="#0e8a79", amber="#a26600", red="#c0392b",
                  violet="#7c5cd6", blue="#2f6fd0", fill="#e7f3f0"),
    "minimal": dict(bg="#ffffff", border="#f0ece4", line="#f4f1ea", track="#f0ece4",
                    text="#1a1d23", dim="#6b7280", faint="#9aa1ab",
                    teal="#12876f", amber="#9a6a00", red="#c2453a",
                    violet="#7c5cd6", blue="#3b6fd4", fill="#eef6f3"),
    "glass": dict(bg="#141a23", border="#2c3645", line="#1f2836", track="#1d2531",
                  text="#eaf1f8", dim="#93a0b1", faint="#6b7789",
                  teal="#59dcc8", amber="#f5bc5c", red="#f4706a",
                  violet="#b39bfb", blue="#6ea6ff", fill="#17303a"),
}


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
    if not _TB_CACHE["hwnd"]:
        _TB_CACHE["hwnd"] = _u32.FindWindowW("Shell_TrayWnd", None) or 0
    return _TB_CACHE["hwnd"]


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
    """用 PrintWindow 直接向任务栏要它自己的像素（不包含我们的浮层，也不用隐藏窗口）。

    返回 BGRX 字节串；失败返回 None。
    """
    if not tb_hwnd:
        return None
    gdi, u = _pixel_proto()
    u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    u.GetWindowRect.restype = wt.BOOL
    u.PrintWindow.argtypes = [wt.HWND, ctypes.c_void_p, wt.UINT]
    u.PrintWindow.restype = wt.BOOL
    gdi.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi.SelectObject.restype = ctypes.c_void_p
    gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi.CreateDIBSection.restype = ctypes.c_void_p
    gdi.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(BITMAPINFO),
                                     wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                     ctypes.c_void_p, wt.DWORD]

    r = wt.RECT()
    if not u.GetWindowRect(tb_hwnd, ctypes.byref(r)):
        return None
    tw, th = r.right - r.left, r.bottom - r.top
    if tw <= 0 or th <= 0:
        return None

    hdc = u.GetDC(0)
    mdc = gdi.CreateCompatibleDC(hdc)
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = tw
    bi.bmiHeader.biHeight = -th
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    hbmp = gdi.CreateDIBSection(hdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
    try:
        old = gdi.SelectObject(mdc, hbmp)
        if not u.PrintWindow(tb_hwnd, mdc, 2):     # PW_RENDERFULLCONTENT
            return None
        raw = ctypes.string_at(bits, tw * th * 4)
        ox = x - r.left
        oy = y - r.top
        if ox < 0 or oy < 0 or ox + w > tw or oy + h > th:
            return None
        out = bytearray(w * h * 4)
        for row in range(h):
            src = ((oy + row) * tw + ox) * 4
            out[row * w * 4:(row + 1) * w * 4] = raw[src:src + w * 4]
        return bytes(out)
    finally:
        try:
            gdi.SelectObject(mdc, old)
        except Exception:
            pass
        gdi.DeleteObject(ctypes.c_void_p(hbmp))
        gdi.DeleteDC(mdc)
        u.ReleaseDC(0, hdc)


def grab_bgra(x, y, w, h):
    """BitBlt 抓一块屏幕，返回 BGRX 字节串（按行、自顶向下）。"""
    gdi, u = _pixel_proto()
    gdi.CreateDIBSection.restype = ctypes.c_void_p
    gdi.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(BITMAPINFO),
                                     wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                     ctypes.c_void_p, wt.DWORD]
    u.GetDC.restype = ctypes.c_void_p
    u.GetDC.argtypes = [ctypes.c_void_p]
    u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi.SelectObject.restype = ctypes.c_void_p
    gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wt.DWORD]
    gdi.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi.DeleteDC.argtypes = [ctypes.c_void_p]

    hdc = u.GetDC(0)
    mdc = gdi.CreateCompatibleDC(hdc)
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    hbmp = gdi.CreateDIBSection(hdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
    old = gdi.SelectObject(mdc, hbmp)
    gdi.BitBlt(mdc, 0, 0, w, h, hdc, x, y, 0x00CC0020)
    buf = ctypes.string_at(bits, w * h * 4)
    gdi.SelectObject(mdc, old)
    gdi.DeleteObject(ctypes.c_void_p(hbmp))
    gdi.DeleteDC(mdc)
    u.ReleaseDC(0, hdc)
    return buf


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
    gb = n / (1024 ** 3)
    if gb >= 100:
        return f"{gb:.0f} GB"
    if gb >= 10:
        return f"{gb:.1f} GB"
    return f"{gb:.2f} GB"


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


_init_proto()


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
        ("OutOctets", ctypes.c_ulonglong),
        ("InUcastPkts", ctypes.c_ulonglong),
        ("OutUcastPkts", ctypes.c_ulonglong),
        ("InNUcastPkts", ctypes.c_ulonglong),
        ("OutNUcastPkts", ctypes.c_ulonglong),
        ("InDiscards", ctypes.c_ulonglong),
        ("OutDiscards", ctypes.c_ulonglong),
        ("InErrors", ctypes.c_ulonglong),
        ("OutErrors", ctypes.c_ulonglong),
        ("InUnknownProtos", ctypes.c_ulonglong),
        ("InHeaderBytes", ctypes.c_ulonglong),
        ("OutHeaderBytes", ctypes.c_ulonglong),
        ("InMulticastPkts", ctypes.c_ulonglong),
        ("OutMulticastPkts", ctypes.c_ulonglong),
        ("InBroadcastPkts", ctypes.c_ulonglong),
        ("OutBroadcastPkts", ctypes.c_ulonglong),
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
        count = ctypes.cast(base, ctypes.POINTER(ctypes.c_ulonglong))[0]
        stride = ctypes.sizeof(MIB_IF_ROW2)
        rows = []
        for i in range(int(count)):
            row = MIB_IF_ROW2.from_address(base + 8 + i * stride)
            alias = row.Alias or ""
            desc = row.Description or ""
            if row.OperStatus != 1 or row.Mtu == 0:
                continue
            if row.Type in (24, 23, 131, 243):  # loopback / tunnel / ...
                continue
            low = (alias + " " + desc).lower()
            if any(k in low for k in _NET_SKIP):
                continue
            # 注意：这块结构里 OutOctets 并不紧跟 InOctets。
            # 实测 offset 1208 = 接收字节、1280 = 发送字节（与 Get-NetAdapterStatistics 一致），
            # 用 row.OutOctets(1216) 会一直读到别的计数器，导致上传速度永远是几百 B。
            addr = base + 8 + i * stride
            in_oct = ctypes.cast(addr + 1208, ctypes.POINTER(ctypes.c_ulonglong)).contents.value
            out_oct = ctypes.cast(addr + 1280, ctypes.POINTER(ctypes.c_ulonglong)).contents.value
            rows.append((len(alias), alias, in_oct, out_oct))
        rows.sort(key=lambda r: r[0])
        rx = tx = 0
        seen = set()
        names = []
        for _, alias, i_o, o_o in rows:
            # 同一块物理网卡上叠了多层过滤驱动，计数器完全一致；按数值去重只取最外层
            key = (i_o, o_o)
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
        self._stop = False

    @staticmethod
    def _num(txt):
        txt = txt.strip()
        if not txt or txt.startswith("["):
            return None
        try:
            return float(txt)
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
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
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
        while not self._stop:
            if fails >= 3:          # 没有可用的 nvidia-smi 时退避，别空转
                time.sleep(30)
            lines = 0
            try:
                proc = subprocess.Popen(
                    ["nvidia-smi", f"--query-gpu={self.FIELDS}",
                     "--format=csv,noheader,nounits", "-lms", "900"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, creationflags=CREATE_NO_WINDOW,
                )
                if first:
                    self._try_name()
                    first = False
                for line in proc.stdout:
                    if self._stop:
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
                except Exception:
                    pass
                fails = 0 if lines else fails + 1
            except Exception:
                fails += 1
            if not self._stop:
                time.sleep(4)

    def stop(self):
        self._stop = True


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
        self._stop = False

    def snapshot(self):
        with self.lock:
            return self.value, self.source

    def _ps(self, script):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=12,
                creationflags=CREATE_NO_WINDOW,
            )
            txt = (out.stdout or "").strip()
            if txt:
                return float(txt.splitlines()[-1].strip())
        except Exception:
            pass
        return None

    def run(self):
        for script, name in ((PS_LHM, "LibreHardwareMonitor"), (PS_ACPI, "ACPI")):
            if self._stop:
                return
            v = self._ps(script)
            if v and 10 < v < 120:
                with self.lock:
                    self.value, self.source = v, name
                while not self._stop:
                    time.sleep(3)
                    nv = self._ps(script)
                    if nv and 10 < nv < 120:
                        with self.lock:
                            self.value = nv
                return
        with self.lock:
            self.source = ""

    def stop(self):
        self._stop = True


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
        self._class = f"DshMonitorTray{os.getpid()}"
        self._icon_path = icon_path
        try:
            self._install()
            self.ok = True
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
        self._add_icon()

    def _load_icon(self):
        u = _u32
        u.LoadImageW.restype = wt.HICON
        if self._icon_path and os.path.exists(self._icon_path):
            h = u.LoadImageW(None, self._icon_path, 1, 0, 0, 0x10 | 0x40)  # IMAGE_ICON
            if h:
                return h
        return u.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION

    def _nid(self, tip=None, flags=None):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags if flags is not None else (self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP)
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = self._load_icon()
        nid.szTip = (tip or self._tip or APP_NAME)[:127]
        return nid

    def _add_icon(self):
        nid = self._nid()
        ok = _shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid))
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
                self.app.root.after(0, self.app.toggle_detail)
            elif lp in (self.WM_RBUTTONUP, self.WM_RBUTTONDOWN, self.WM_CONTEXTMENU):
                self.app.root.after(0, self.app.tray_menu)
        elif msg == self._msg_taskbar:
            # 资源管理器重启后把图标补回去
            self.app.root.after(0, self._add_icon)

    def remove(self):
        try:
            nid = self._nid()
            _shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass
        try:
            if self.hwnd:
                _u32.DestroyWindow(self.hwnd)
        except Exception:
            pass
        self.ok = False



def log_exc(exc_type, exc, tb):
    """把异常写进 error.log，方便事后查（窗口版看不到 stderr）。"""
    try:
        path = os.path.join(BASE, "error.log")
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
        if st.BatteryLifePercent == 255:
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
        cpu = 100.0 * (1.0 - d_idle / d_total) if d_total else 0.0
        self.prev_cpu = (idle, kern, user)

        mem = MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        _k32.GlobalMemoryStatusEx(ctypes.byref(mem))
        mem_used = mem.ullTotalPhys - mem.ullAvailPhys
        mem_pct = 100.0 * mem_used / mem.ullTotalPhys if mem.ullTotalPhys else 0.0

        rx, tx, names = net_io()
        p_rx, p_tx, p_t = self.prev_net
        dt = max(0.15, now - p_t)
        down = max(0.0, (rx - p_rx) / dt)
        up = max(0.0, (tx - p_tx) / dt)
        self.prev_net = (rx, tx, now)
        self.net_names = names

        self.peak_cpu = max(self.peak_cpu, cpu)
        self.peak_mem = max(self.peak_mem, mem_pct)

        self.total_down += max(0.0, rx - p_rx)
        self.total_up += max(0.0, tx - p_tx)

        gpu = self.gpu.snapshot()
        cpu_temp, temp_src = self.temp.snapshot()
        batt = battery_status()

        free, total = disk_free()
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
            "procs": self.proc.snapshot()[0],
            "proc_count": self.proc.snapshot()[1],
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

    SNAP = 2

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.top = []            # [(name, cpu%, mem_bytes, conns)]
        self.count = 0
        self._stop = False
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
        self._stop = True

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
        snap = k.CreateToolhelp32Snapshot(self.SNAP, 0)
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
                if pid and name not in ("System Idle Process",):
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
            self.top = merged[:4]
            self.count = len(merged)

    def run(self):
        while not self._stop:
            try:
                self._sample()
            except Exception:
                if os.environ.get("DM_DEBUG"):
                    import traceback
                    traceback.print_exc()
            for _ in range(int(self.SNAP * 10)):
                if self._stop:
                    return
                time.sleep(0.1)

class LayeredStrip:
    WM_NCHITTEST = 0x0084
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONUP = 0x0205
    WM_PAINT = 0x000F
    WM_ERASEBKGND = 0x0014
    HTCLIENT = 1

    def __init__(self, app, title="DshStrip"):
        self.app = app
        self.title = title
        self.hwnd = None
        self._proc = None
        self._hinst = None
        self.x = self.y = 0
        self.w = self.h = 0

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
                if msg == self.WM_PAINT:
                    return 0
                if msg == self.WM_ERASEBKGND:
                    return 1
                if msg == self.WM_LBUTTONUP:
                    self.app.root.after(0, self.app.toggle_detail)
                    return 0
                if msg == self.WM_RBUTTONUP:
                    self.app.root.after(0, self.app.open_menu_at_cursor)
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
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = self._hinst
        wc.lpszClassName = self.title
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
                                      0x80000000 | 0x10000000,   # WS_POPUP | WS_VISIBLE
                                      x, y, w, h, None, None, self._hinst, None)
        self.x, self.y, self.w, self.h = x, y, w, h
        if not self.hwnd:
            # 诊断：换最朴素参数再试一次，看是不是某个扩展样式的问题
            self.hwnd = u.CreateWindowExW(0, self.title, "probe", 0x10000000,
                                          100, 100, 200, 80, None, None, self._hinst, None)
            if os.environ.get("DM_DEBUG"):
                print(f"[strip] retry plain hwnd={self.hwnd} err={ctypes.get_last_error()}",
                      file=sys.stderr)
        if os.environ.get("DM_DEBUG"):
            print(f"[strip] create hwnd={self.hwnd} err={ctypes.get_last_error()}",
                  file=sys.stderr)
        return self.hwnd

    # ---------------- 绘制
    def update(self, img, x=None, y=None):
        """img: PIL RGBA；None 表示只挪位置。"""
        if not self.hwnd:
            return
        u = ctypes.windll.user32
        g = ctypes.windll.gdi32
        if x is not None:
            self.x, self.y = x, y
        w, h = img.size if img is not None else (self.w, self.h)
        self.w, self.h = w, h

        u.GetDC.restype = ctypes.c_void_p
        u.GetDC.argtypes = [ctypes.c_void_p]
        u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        g.CreateCompatibleDC.restype = ctypes.c_void_p
        g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        g.CreateDIBSection.restype = ctypes.c_void_p
        g.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wt.DWORD]
        g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        g.SelectObject.restype = ctypes.c_void_p
        g.DeleteObject.argtypes = [ctypes.c_void_p]
        g.DeleteDC.argtypes = [ctypes.c_void_p]
        u.UpdateLayeredWindow.argtypes = [
            wt.HWND, ctypes.c_void_p, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
            ctypes.c_void_p, ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.c_void_p, wt.DWORD]
        u.UpdateLayeredWindow.restype = wt.BOOL

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [("BlendOp", wt.BYTE), ("BlendFlags", wt.BYTE),
                        ("SourceConstantAlpha", wt.BYTE), ("AlphaFormat", wt.BYTE)]

        hdc_screen = u.GetDC(0)
        mem = g.CreateCompatibleDC(hdc_screen)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h            # 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        hbmp = g.CreateDIBSection(hdc_screen, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        old = g.SelectObject(mem, hbmp)
        try:
            if img is not None:
                # PIL 是直通 alpha，GDI 要预乘
                src = img.tobytes("raw", "RGBA")
                buf = bytearray(len(src))
                for i in range(0, len(src), 4):
                    a = src[i + 3]
                    if a == 255:
                        buf[i:i + 3] = src[i:i + 3]
                    elif a == 0:
                        pass
                    else:
                        buf[i] = src[i] * a // 255
                        buf[i + 1] = src[i + 1] * a // 255
                        buf[i + 2] = src[i + 2] * a // 255
                    buf[i + 3] = a
                ctypes.memmove(bits, bytes(buf), len(buf))

            size = wt.SIZE(w, h)
            dst_pt = wt.POINT(self.x, self.y)
            src_pt = wt.POINT(0, 0)
            blend = BLENDFUNCTION(0, 0, 255, 1)     # AC_SRC_OVER / AC_SRC_ALPHA
            ok = u.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(dst_pt),
                                       ctypes.byref(size), mem, ctypes.byref(src_pt), 0,
                                       ctypes.byref(blend), 2)
            if os.environ.get("DM_DEBUG") and not ok:
                print(f"[strip] UpdateLayeredWindow failed err={ctypes.get_last_error()}",
                      file=sys.stderr)
        finally:
            g.SelectObject(mem, old)
            g.DeleteObject(ctypes.c_void_p(hbmp))
            g.DeleteDC(mem)
            u.ReleaseDC(0, hdc_screen)

    # ---------------- 其它
    def raise_top(self):
        if not self.hwnd:
            return
        u = ctypes.windll.user32
        u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.UINT]
        u.SetWindowPos(self.hwnd, wt.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def destroy(self):
        try:
            if self.hwnd:
                ctypes.windll.user32.DestroyWindow(self.hwnd)
        except Exception:
            pass
        self.hwnd = None

class MonitorApp:
    W, H = 324, 322          # 完整模式（96dpi 设计尺寸）
    CW, CH = 190, 72         # 紧凑模式
    STRIP_H = 34             # 任务栏条高度
    PAD = 14
    SPARK_N = 56

    def __init__(self):
        self.cfg = self.load_cfg()
        dpi_aware()
        self.stats = Stats()
        self.stats.start()

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
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
        if self.mode not in ("taskbar", "card", "mini"):
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
        self.strip = None
        self._strip_pos = None
        self.card_win = None
        self.card_canvas = None
        self._popup_win = False
        self._mouse_down = False
        self.card_theme = self.cfg.get("card_theme", "dark")
        self.style = self.cfg.get("style", "pill")
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
        self.hist = {"cpu": [], "gpu": [], "down": [], "up": [], "mem": []}
        self.state = {}
        self.clock = ""

        self.apply_geometry()
        self.apply_layer()
        self.root.after(2500, self.dock_loop)
        self.root.after(600, self.top_loop)

        self.tray = TrayIcon(self, os.path.join(BASE, "icon.ico"))
        if self.tray.ok:
            self.tray.update_tip("桌面性能监视器 · 点一下看详情")

        try:
            os.remove(os.path.join(BASE, "disabled.flag"))
        except Exception:
            pass

        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Double-Button-1>", self.on_double)
        self.canvas.bind("<Button-3>", self.on_menu)
        self.root.update_idletasks()
        self.root.bind("<Control-q>", lambda e: self.quit())
        self.root.bind("<Escape>", lambda e: self.hide_detail() if self.popup else None)

        self.tick()

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
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_cfg(self):
        try:
            if self.mode == "taskbar":
                # 贴任务栏的位置是算出来的，不要覆盖用户给悬浮窗留的位置
                x, y = self.cfg.get("x"), self.cfg.get("y")
            else:
                x = self.root.winfo_x()
                y = self.root.winfo_y()
        except Exception:
            x = y = None
        self.cfg.update({
            "x": x, "y": y, "mode": self.mode, "compact": self.compact,
            "layer": self.layer,
            "interval": self.interval, "locked": self.locked,
            "alpha": float(self.cfg.get("alpha", CARD_ALPHA)),
        })
        try:
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------------- 窗口
    def size(self):
        if self.mode == "taskbar":
            return int(self.strip_w), int(self._tb_h or 72)
        if self.mode == "mini":
            w, h = self.CW, self.CH
        else:
            w, h = self.W, self.H
        return int(round(w * self.scale)), int(round(h * self.scale))

    def apply_geometry(self):
        if self.mode == "tray":
            self.root.withdraw()
            return
        if self.mode == "taskbar":
            if not self.dock_now(force=True):
                self.show_window()
            return
        w, h = self.size()
        vx, vy, vw, vh = virtual_screen()
        x, y = self.cfg.get("x"), self.cfg.get("y")
        visible = (x is not None and y is not None
                   and vx <= x <= vx + vw - w and vy <= y <= vy + vh - h)
        if not visible:
            # 默认放在主屏右上角，而不是整个虚拟桌面的最右边
            pw = self.root.winfo_screenwidth()
            x, y = pw - w - int(24 * self.scale), int(60 * self.scale)
        self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        self.canvas.configure(width=w, height=h)
        self.show_window()

    def dock_now(self, force=False):
        """把信息条嵌进任务栏的空白处（任务不在底部时退化成贴边）。"""
        pos = taskbar_position()
        if not pos:
            return
        # 已经停好且任务栏没变：只校准层级，不再重算，避免尺寸/位置抖动
        if (not force and self._tb_rect
                and getattr(self, "_tbpos", None) == pos):
            # 只要任务栏没变就绝不再重排
            if self.mode == "taskbar":
                self.draw_strip()
            elif self.root.state() != "normal":
                self.show_window()
            self._raise_above_taskbar()
            return False
        if os.environ.get("DM_DEBUG"):
            print(f"[redock] pos={pos} rect={self._tb_rect} force={force} "
                  f"state={self.root.state()}", file=sys.stderr)
        self._tbpos = pos
        self._dock_ts = time.time()
        tx, ty, tw, th, edge = pos
        in_taskbar = edge == 3

        if in_taskbar:
            kids = taskbar_children()
            tray_left = (kids.get("TrayNotifyWnd") or (None,))[0]
            if not tray_left:
                tray_left = tx + tw - int(250 * self.scale)
            margin = int(10 * self.scale)
            w = self.strip_w
            x = max(tray_left - margin - w, tx + int(6 * self.scale))
            h = max(24, th - 1)
            y = ty + 1

            rect = (int(x), int(y), int(w), int(h))
            self._tb_h = h
            # 分层窗口：直接按 RGBA 贴上去，没有背景色块，也不需要截图
            self.root.withdraw()
            try:
                if self.strip is None:
                    self.strip = LayeredStrip(self)
                    self.strip.create(*rect)
                self._tb_rect = rect
                img = self._compose_strip(rect[2], rect[3])
                self.strip.update(img, rect[0], rect[1])
                self.strip.raise_top()
            except Exception:
                log_exc(*sys.exc_info())
                self.strip = None
            return False
        else:
            h = int(round(self.STRIP_H * self.scale))
            margin = int(6 * self.scale)
            gap = int(4 * self.scale)
            if edge == 1:      # 顶部任务栏
                x, y = tx + tw - w - margin, ty + th + gap
            elif edge == 0:    # 左侧竖排
                x, y = tx + tw + gap, ty + th - h - margin
            else:              # 右侧竖排
                x, y = tx - w - gap, ty + th - h - margin
            self._tb_h = h

        x, y = int(x), int(y)
        if (force or (self.root.winfo_x(), self.root.winfo_y()) != (x, y)
                or self.root.winfo_width() != w or self.root.winfo_height() != h):
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        if in_taskbar:
            self._raise_above_taskbar()

    def _finish_dock(self, x, y, w, h, show):
        try:
            self._finish_dock_inner(x, y, w, h, show)
        except Exception:
            if os.environ.get("DM_DEBUG"):
                import traceback
                traceback.print_exc()
            try:
                self.show_window()
            except Exception:
                pass

    def _finish_dock_inner(self, x, y, w, h, show):
        self.capture_background(x, y, w, h)
        self._tb_rect = (int(x), int(y), int(w), int(h))
        self._tb_h = h
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        if show:
            self.show_window()
        self.draw()
        self._raise_above_taskbar()
        if os.environ.get("DM_DEBUG"):
            print(f"[dock] {w}x{h}+{x}+{y} light={self.tb_light} bg={self._bg_pil is not None}",
                  file=sys.stderr)

    def _raise_above_taskbar(self):
        """看窗口中心那一点到底是谁的：被任务栏盖住就重新置顶。"""
        try:
            u = _u32
            if self.strip is not None and self.strip.hwnd:
                hwnd = self.strip.hwnd
                x = self.strip.x + max(4, self.strip.w // 2)
                y = self.strip.y + max(4, self.strip.h // 2)
            else:
                hwnd = int(self.root.wm_frame(), 16)
                if not hwnd or self.root.state() == "withdrawn":
                    return
                x = self.root.winfo_x() + max(4, self.root.winfo_width() // 2)
                y = self.root.winfo_y() + max(4, self.root.winfo_height() // 2)
            under = u.WindowFromPoint(wt.POINT(x, y))
            if not under or under == hwnd or u.IsChild(hwnd, under):
                return                      # 这个点还是自己的（或没人）

            u.SetWindowPos(hwnd, -1, 0, 0, 0, 0,   # HWND_TOPMOST
                           SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            pass

    def dock_loop(self):
        if self.mode == "taskbar":
            try:
                self.dock_now()
            except Exception:
                pass
        self.root.after(800, self.dock_loop)

    def top_loop(self):
        """点任务栏会把它顶到我们上面，这里高频地把窗口抬回来。"""
        if self.mode == "taskbar":
            try:
                self._raise_above_taskbar()
            except Exception:
                pass
        self.root.after(150, self.top_loop)

    def show_window(self):
        if self.mode == "taskbar" and self.strip is not None:
            return
        try:
            if self.root.state() == "withdrawn":
                self.root.deiconify()
                self.root.overrideredirect(True)
                self.root.wm_attributes("-topmost", 1 if self.layer == "top" else 0)
                self.root.wm_attributes("-alpha", float(self.cfg.get("alpha", CARD_ALPHA)))
                self.root.wm_attributes("-transparentcolor", "#ff00fe")
                self.canvas.pack(fill="both", expand=True)
        except Exception:
            pass

    def toggle_detail(self):
        if self.popup:
            self.hide_detail()
        else:
            self.show_detail()

    def show_detail(self):
        if self.mode == "taskbar":
            # 任务栏条留在原地不动，详情用另一个窗口弹在它上面
            self.popup = True
            self.show_popup()
            return
        if self.mode == "tray":
            self._base_mode = self.mode
        self.popup = True
        self.mode = "card"
        self.show_window()
        self.apply_card_alpha()
        self.place_popover()
        self.root.update_idletasks()
        self.draw()

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
        c.bind("<Button-1>", self.on_card_press)
        w.bind("<Escape>", lambda e: self.hide_detail())
        self.card_win = w
        self.card_canvas = c

    def card_rect(self):
        w = int(round(self.W * self.scale))
        h = int(round(self.H * self.scale))
        m = int(8 * self.scale)
        pos = taskbar_position()
        if pos:
            tx, ty, tw, th, edge = pos
            if self._tb_rect:
                cx = self._tb_rect[0] + self._tb_rect[2] / 2.0
                x = cx - w / 2.0
            else:
                x = tx + tw - w - m
            y = ty - h - m
        else:
            vx, vy, vw, vh = virtual_screen()
            x, y = vx + vw - w - m, vy + vh - h - m
        x = max(0, min(int(x), self.root.winfo_screenwidth() - w))
        return x, int(y), w, h

    def show_popup(self):
        self._ensure_card_win()
        x, y, w, h = self.card_rect()
        self._popup_win = True
        self.card_win.geometry(f"{w}x{h}+{x}+{y}")
        self.card_canvas.configure(width=w, height=h)
        if self.card_theme == "glass":
            try:
                self.card_win.wm_attributes("-alpha", 0.94)
            except Exception:
                pass
        self.card_win.deiconify()
        self.card_win.lift()
        self.card_win.attributes("-topmost", True)
        try:
            self.card_win.focus_force()
        except Exception:
            pass
        self.draw_card()
        self.root.after(250, self._popup_watch)

    def draw_card(self):
        if not self._popup_win or self.card_canvas is None:
            return
        c = self.card_canvas
        x, y, W, H = self.card_rect()
        if not HAVE_PIL:
            return
        try:
            from PIL import ImageTk
            self._card_photo = ImageTk.PhotoImage(self._compose_card(W, H), master=self.root)
        except Exception:
            log_exc(*sys.exc_info())
            return
        key = (W, H)
        if getattr(self, "_popup_key", None) != key or self._popup_item is None:
            c.delete("all")
            self._popup_item = c.create_image(0, 0, image=self._card_photo, anchor="nw")
            self._popup_key = key
        else:
            c.itemconfigure(self._popup_item, image=self._card_photo)
        self._close_box = (W / self.scale - 33, 11, W / self.scale - 11, 33)

    def on_card_press(self, e):
        if getattr(self, "_gear_box", None):
            x1, y1, x2, y2 = [self.s(v) for v in self._gear_box]
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                self.open_settings()
                return
        if not self._close_box:
            return
        x1, y1, x2, y2 = [self.s(v) for v in self._close_box]
        if x1 <= e.x <= x2 and y1 <= e.y <= y2:
            self.hide_detail()

    def _inside_popup(self, px, py):
        x, y, w, h = self.card_rect()
        if x <= px <= x + w and y <= py <= y + h:
            return True
        if self._tb_rect:
            tx, ty, tw, th = self._tb_rect
            if tx <= px <= tx + tw and ty <= py <= ty + th:
                return True
        return False

    def _popup_watch(self):
        """点空白处就关掉详情。"""
        if not self._popup_win:
            return
        try:
            down = bool(_u32.GetAsyncKeyState(0x01) & 0x8000)
            if down and not self._mouse_down:
                self._mouse_down = True
                pt = wt.POINT()
                _u32.GetCursorPos(ctypes.byref(pt))
                if not self._inside_popup(pt.x, pt.y):
                    self.hide_detail()
                    return
            elif not down:
                self._mouse_down = False
        except Exception:
            pass
        self.root.after(90, self._popup_watch)

    def hide_detail(self):
        self.popup = False
        if self._popup_win:
            self._popup_win = False
            try:
                self.card_win.withdraw()
            except Exception:
                pass
            self._close_box = None
            return
        try:
            self.root.wm_attributes("-alpha", 1.0)
        except Exception:
            pass
        self.set_mode(self._base_mode if self._base_mode in ("taskbar", "tray") else "taskbar")

    def place_popover(self):
        """详情卡片弹在任务栏条的正上方，右边缘和条对齐。"""
        w, h = self.size()
        m = int(8 * self.scale)
        pos = taskbar_position()
        if pos:
            tx, ty, tw, th, edge = pos
            if self._tb_rect:
                center = self._tb_rect[0] + self._tb_rect[2] / 2.0
                x = center - w / 2.0
            else:
                x = tx + tw - w - m
            y = ty - h - m
        else:
            vx, vy, vw, vh = virtual_screen()
            x, y = vx + vw - w - m, vy + vh - h - m
        x = max(0, int(x))
        self.root.geometry(f"{w}x{h}+{x}+{int(y)}")

    def open_menu_at_cursor(self):
        p = wt.POINT()
        _u32.GetCursorPos(ctypes.byref(p))
        m = self.build_menu()
        try:
            m.tk_popup(p.x, p.y)
        finally:
            m.grab_release()

    def tray_menu(self):
        p = wt.POINT()
        _u32.GetCursorPos(ctypes.byref(p))
        m = self.build_menu()
        try:
            m.tk_popup(p.x, p.y)
        finally:
            m.grab_release()

    def apply_layer(self):
        top = self.layer == "top"
        try:
            self.root.wm_attributes("-topmost", 1 if top else 0)
        except Exception:
            pass
        if self.layer == "bottom":
            self._lower()
            self.root.after(2500, self._lower_loop)

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
        if self.layer == "bottom":
            self._lower()
            self.root.after(2500, self._lower_loop)

    # ---------------- 事件
    def on_press(self, e):
        self._ignore_release = False
        if self.popup and self._close_box:
            x1, y1, x2, y2 = [self.s(v) for v in self._close_box]
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                # 关掉之后窗口会变回任务栏条，鼠标松开就会落在条上把它又打开；
                # 这里延后一点关闭，并吃掉紧随其后的 release
                self._ignore_release = True
                self.root.after(90, self.hide_detail)
                return
        if self.mode == "taskbar" or self.locked:
            self._drag = None
            return
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def on_drag(self, e):
        if self.locked or not self._drag:
            return
        self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

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
        vx, vy, vw, vh = virtual_screen()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        m = int(16 * self.scale)
        x = min(max(x, vx + m), vx + vw - w - m)
        y = min(max(y, vy + m), vy + vh - h - m)
        self.root.geometry(f"+{x}+{y}")

    def on_double(self, e):
        if self.popup or self.mode == "taskbar":
            return
        self.set_mode("card" if self.mode == "mini" else "mini")

    def set_mode(self, mode):
        self.popup = False
        self.mode = mode
        if mode == "tray":
            self.root.withdraw()
            self.save_cfg()
            return
        self.show_window()
        if mode == "taskbar":
            self.layer = "top"
            self.apply_layer()
        self.canvas.configure(cursor="arrow" if self.locked else "hand2")
        self.apply_geometry()
        self.root.update_idletasks()
        self.draw()
        self.root.update_idletasks()
        self.save_cfg()

    def set_style(self, name):
        self.style = name
        self.cfg["style"] = name
        self.save_cfg()
        self._strip_key = None
        self.draw()

    def set_card_theme(self, name):
        self.card_theme = name
        self.cfg["card_theme"] = name
        self.save_cfg()
        self._card_key = None
        self.apply_card_alpha()
        self.draw()

    def apply_card_alpha(self):
        try:
            if self.mode == "card" and self.card_theme == "glass":
                self.root.wm_attributes("-alpha", 0.94)
            else:
                self.root.wm_attributes("-alpha", 1.0)
        except Exception:
            pass

    def set_theme(self, name):
        self.theme = name
        self.cfg["theme"] = name
        self.save_cfg()
        self._strip_key = None
        self.draw()

    def toggle_compact(self):
        self.set_mode("mini" if self.mode != "mini" else "card")

    def on_menu(self, e):
        m = self.build_menu()
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    # ---------------- 自定义右键菜单（PIL 绘制）
    MENU_W = 250
    ROW_H = 34

    def menu_rows(self):
        st = self.style
        style_name = {"text": "素净", "pill": "胶囊", "outline": "描边"}.get(st, st)
        theme_name = {"auto": "跟随任务栏", "dark": "深色字", "light": "浅色字"}.get(self.theme, self.theme)
        card_name = {"dark": "深色", "light": "浅色", "minimal": "简约", "glass": "玻璃"}.get(self.card_theme, self.card_theme)
        layer_name = {"top": "置顶", "normal": "普通", "bottom": "贴桌面"}.get(self.layer, self.layer)
        alpha = int(round(getattr(self, "alpha", 1.0) * 100))
        return [
            ("head", "性能监视器", "", None, 30),
            ("item", "展开详情" if not self.popup else "收起详情", "", self.toggle_detail, self.ROW_H),
            ("gap", "", "", None, 8),
            ("item", "样式", style_name, lambda: self.cycle("style"), self.ROW_H),
            ("item", "主题", theme_name, lambda: self.cycle("theme"), self.ROW_H),
            ("item", "详情卡", card_name, lambda: self.cycle("card_theme"), self.ROW_H),
            ("item", "刷新", f"{self.interval / 1000:g} 秒", lambda: self.cycle("interval"), self.ROW_H),
            ("item", "不透明度", f"{alpha}%", lambda: self.cycle("alpha"), self.ROW_H),
            ("gap", "", "", None, 8),
            ("item", "窗口层级", layer_name, lambda: self.cycle("layer"), self.ROW_H),
            ("item", "锁定位置", "开" if self.locked else "关", self.toggle_lock, self.ROW_H),
            ("item", "开机自启", "开" if self.is_autostart() else "关", self.toggle_autostart, self.ROW_H),
            ("gap", "", "", None, 8),
            ("item", "归位", "", self.reset_position, self.ROW_H),
            ("item", "任务管理器", "", lambda: subprocess.Popen(["taskmgr.exe"], creationflags=CREATE_NO_WINDOW), self.ROW_H),
            ("item", "关于 · 温度说明", "", self.show_about, self.ROW_H),
            ("gap", "", "", None, 8),
            ("item", "退出", "", self.quit, self.ROW_H),
        ]

    def cycle(self, key):
        if key == "style":
            order = ["text", "pill", "outline"]
            self.set_style(order[(order.index(self.style) + 1) % len(order)] if self.style in order else "text")
        elif key == "theme":
            order = ["auto", "dark", "light"]
            self.set_theme(order[(order.index(self.theme) + 1) % len(order)] if self.theme in order else "auto")
        elif key == "card_theme":
            order = ["dark", "light", "minimal", "glass"]
            self.set_card_theme(order[(order.index(self.card_theme) + 1) % len(order)] if self.card_theme in order else "dark")
        elif key == "interval":
            order = [500, 1000, 2000, 5000]
            self.set_interval(order[(order.index(self.interval) + 1) % len(order)] if self.interval in order else 1000)
        elif key == "alpha":
            order = [1.0, 0.95, 0.9, 0.8]
            cur = getattr(self, "alpha", 1.0)
            idx = order.index(cur) if cur in order else 0
            self.set_alpha(order[(idx + 1) % len(order)])
        elif key == "layer":
            order = ["top", "normal", "bottom"]
            self.set_layer(order[(order.index(self.layer) + 1) % len(order)] if self.layer in order else "top")

    def show_menu(self, x=None, y=None):
        if getattr(self, "_menu_win", None):
            self.close_menu()
            return
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#000000")
        c = tk.Canvas(win, highlightthickness=0, bd=0, bg="#000000")
        c.pack()
        self._menu_win = win
        self._menu_canvas = c
        self._menu_hover = -1
        self._menu_rows_data = self.menu_rows()
        rows = self._menu_rows_data
        h = sum(r[4] for r in rows) + 14
        w = self.MENU_W
        if x is None:
            x = self.root.winfo_pointerx() - 20
            y = self.root.winfo_pointery() - 10
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(6, min(int(x), sw - w - 6))
        y = max(6, min(int(y), sh - h - 8))
        self._menu_rect = (x, y, w, h)
        self._menu_ts = time.time()
        win.geometry(f"{w}x{h}+{x}+{y}")
        c.configure(width=w, height=h)
        c.bind("<Motion>", self._menu_motion)
        c.bind("<Button-1>", self._menu_click)
        c.bind("<Leave>", lambda e: self._menu_hover_set(-1))
        win.bind("<Escape>", lambda e: self.close_menu())
        self._draw_menu()
        win.deiconify()
        win.lift()
        self.root.after(120, self._menu_watch)

    def _menu_hover_set(self, idx):
        if idx != self._menu_hover:
            self._menu_hover = idx
            self._draw_menu()

    def _menu_row_at(self, y):
        top = 7
        for i, r in enumerate(self._menu_rows_data):
            if top <= y < top + r[4]:
                return i if r[0] == "item" else -1
            top += r[4]
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
        try:
            if self._menu_win:
                self._menu_win.destroy()
        except Exception:
            pass
        self._menu_win = None
        self._menu_canvas = None

    def _menu_watch(self):
        if not getattr(self, "_menu_win", None):
            return
        try:
            if _u32.GetAsyncKeyState(0x01) & 0x8000:
                pt = wt.POINT()
                _u32.GetCursorPos(ctypes.byref(pt))
                x, y, w, h = getattr(self, "_menu_rect", (0, 0, 0, 0))
                if (time.time() - getattr(self, "_menu_ts", 0) > 0.25
                        and not (x <= pt.x <= x + w and y <= pt.y <= y + h)):
                    self.close_menu()
                    return
                self.root.after(120, self._menu_watch)   # 点在菜单内：交给 <Button-1>
                return
        except Exception:
            pass
        self.root.after(120, self._menu_watch)

    def _draw_menu(self):
        from PIL import Image, ImageDraw
        win = self._menu_win
        if win is None:
            return
        w = self.MENU_W
        rows = self._menu_rows_data
        h = sum(r[4] for r in rows) + 14
        SS = 2
        img = Image.new("RGB", (w * SS, h * SS), (40, 40, 44))
        d = ImageDraw.Draw(img)
        P = CARD_THEMES.get(self.card_theme, CARD_COLORS)

        def rgb(hx):
            hx = hx.lstrip("#")
            return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))

        def F(px, bold=False):
            return self._font("msyhbd.ttc" if bold else "msyh.ttc", max(8, int(round(px * SS))))

        f_item = F(13.5)
        f_head = F(11, True)
        f_val = F(12)

        d.rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], radius=10 * SS, fill=rgb(P["bg"]))
        top = 7
        for i, (kind, label, value, action, rh) in enumerate(rows):
            y0, y1 = top, top + rh
            if kind == "item" and i == self._menu_hover:
                d.rounded_rectangle([5 * SS, y0 * SS, (w - 5) * SS, y1 * SS],
                                    radius=7 * SS, fill=rgb(P["border"]))
            if kind == "head":
                d.text((14 * SS, (y0 + rh / 2) * SS), label, font=f_head,
                       fill=rgb(P["dim"]), anchor="lm")
            elif kind == "item":
                color = rgb(P["text"])
                if label == "退出":
                    color = rgb(P["red"])
                d.text((14 * SS, (y0 + rh / 2) * SS), label, font=f_item, fill=color, anchor="lm")
                if value:
                    d.text(((w - 14) * SS, (y0 + rh / 2) * SS), value, font=f_val,
                           fill=rgb(P["faint"]), anchor="rm")
                elif action and label in ("展开详情", "收起详情"):
                    d.text(((w - 14) * SS, (y0 + rh / 2) * SS), "›", font=f_item,
                           fill=rgb(P["faint"]), anchor="rm")
            top += rh

        from PIL import ImageTk
        img = img.resize((w, h), Image.LANCZOS)      # 超采样后缩回去，不然字会是两倍大
        self._menu_photo = ImageTk.PhotoImage(img, master=self.root)
        self._menu_canvas.delete("all")
        self._menu_canvas.create_image(0, 0, image=self._menu_photo, anchor="nw")

    # ---------------- 设置窗口
    def open_settings(self):
        if getattr(self, "_set_win", None):
            try:
                self._set_win.deiconify()
                self._set_win.lift()
                self._set_win.focus_force()
                return
            except Exception:
                self._set_win = None
        P = CARD_THEMES.get(self.card_theme, CARD_COLORS)
        bg, fg, sub, acc = P["bg"], P["text"], P["dim"], P["teal"]
        win = tk.Toplevel(self.root)
        self._set_win = win
        win.title("性能监视器 · 设置")
        win.configure(bg=bg)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self._close_settings)

        wrap = tk.Frame(win, bg=bg, padx=22, pady=16)
        wrap.pack(fill="both", expand=True)

        tk.Label(wrap, text="外观与行为", bg=bg, fg=fg, anchor="w",
                 font=self._f(11, "bold")).pack(fill="x")
        tk.Label(wrap, text="改动立即生效，并写入 config.json", bg=bg, fg=sub,
                 anchor="w", font=self._f(8.5)).pack(fill="x", pady=(0, 10))

        def section(title):
            f = tk.Frame(wrap, bg=bg)
            f.pack(fill="x", pady=(9, 3))
            tk.Label(f, text=title, bg=bg, fg=sub, anchor="w",
                     font=self._f(8.5)).pack(side="left")

        def radios(title, values, current, cmd):
            section(title)
            row = tk.Frame(wrap, bg=bg)
            row.pack(fill="x", anchor="w")
            var = tk.StringVar(value=str(current))
            for val, label in values:
                tk.Radiobutton(row, text=label, value=str(val), variable=var,
                               command=lambda v=val: cmd(v),
                               bg=bg, fg=fg, selectcolor=acc,
                               activebackground=bg, activeforeground=fg,
                               highlightthickness=0, bd=0, font=self._f(9),
                               anchor="w", padx=3, pady=1, indicatoron=True
                               ).pack(side="left", padx=(0, 8))
            return var

        def slider(title, lo, hi, step, current, cmd, fmt):
            section(title)
            row = tk.Frame(wrap, bg=bg)
            row.pack(fill="x")
            val_lbl = tk.Label(row, text=fmt(current), bg=bg, fg=fg, width=7,
                               anchor="e", font=self._f(9, "bold"))
            val_lbl.pack(side="right")
            var = tk.DoubleVar(value=float(current))

            def on_move(v):
                val_lbl.configure(text=fmt(float(v)))

            def on_done(v):
                cmd(float(v))

            sc = tk.Scale(row, from_=lo, to=hi, resolution=step, orient="horizontal",
                          variable=var, showvalue=False, command=on_move,
                          bg=P["border"], fg=fg, troughcolor=P["bg"],
                          activebackground=acc, highlightthickness=0, bd=0,
                          sliderrelief="raised", length=230, width=12,
                          sliderlength=22)
            sc.pack(side="left", fill="x", expand=True, padx=(0, 10))
            sc.bind("<ButtonRelease-1>", lambda e: on_done(var.get()))
            return var

        radios("任务栏样式", [("text", "素净"), ("pill", "胶囊"), ("outline", "描边")],
               self.style, self.set_style)
        radios("字色主题", [("auto", "跟随任务栏"), ("dark", "深色字"), ("light", "浅色字")],
               self.theme, self.set_theme)
        radios("详情卡主题", [("dark", "深色"), ("light", "浅色"),
                              ("minimal", "简约"), ("glass", "玻璃")],
               self.card_theme, self.set_card_theme)

        slider("刷新间隔", 0.5, 5.0, 0.5, self.interval / 1000.0,
               lambda v: self.set_interval(int(v * 1000)), lambda v: f"{v:.1f} 秒")
        slider("不透明度", 0.30, 1.0, 0.05, getattr(self, "alpha", 1.0),
               lambda v: self.set_alpha(round(v, 2)), lambda v: f"{int(v * 100)}%")

        section("其它")
        row2 = tk.Frame(wrap, bg=bg)
        row2.pack(fill="x", pady=(0, 2))
        tk.Checkbutton(row2, text="锁定位置", variable=tk.BooleanVar(value=self.locked),
                       command=self.toggle_lock, bg=bg, fg=fg, selectcolor=P["border"],
                       activebackground=bg, activeforeground=fg,
                       highlightthickness=0, bd=0, font=self._f(9), padx=3,
                       anchor="w").pack(side="left")
        tk.Checkbutton(row2, text="开机自启", variable=tk.BooleanVar(value=self.is_autostart()),
                       command=self.toggle_autostart, bg=bg, fg=fg, selectcolor=P["border"],
                       activebackground=bg, activeforeground=fg,
                       highlightthickness=0, bd=0, font=self._f(9), padx=3,
                       anchor="w").pack(side="left", padx=(14, 0))

        line = tk.Frame(wrap, bg=P["border"], height=1)
        line.pack(fill="x", pady=(14, 12))

        row3 = tk.Frame(wrap, bg=bg)
        row3.pack(fill="x")

        def btn(parent, text, cmd, color=None):
            b = tk.Button(parent, text=text, command=cmd, bg=P["border"],
                          fg=color or fg, activebackground=P["track"],
                          activeforeground=color or fg, bd=0, relief="flat",
                          font=self._f(9), padx=12, pady=5, cursor="hand2",
                          highlightthickness=0)
            b.pack(side="left", padx=(0, 8))
            return b

        btn(row3, "归位", self.reset_position)
        btn(row3, "展开详情", self.toggle_detail)
        btn(row3, "关闭", self._close_settings, P["red"])

        w, h = 430, 615
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2 - 40
        win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    def _close_settings(self):
        try:
            if getattr(self, "_set_win", None):
                self._set_win.destroy()
        except Exception:
            pass
        self._set_win = None

    def build_menu(self):
        theme = CARD_THEMES.get(self.card_theme, CARD_COLORS)
        MBG, MFG = theme["bg"], theme["text"]
        MHL, MDIS = theme["border"], theme["faint"]
        m = tk.Menu(self.root, tearoff=0, bg=MBG, fg=MFG,
                    activebackground=MHL, activeforeground=MFG,
                    disabledforeground=MDIS,
                    bd=0, relief="flat", activeborderwidth=0,
                    font=self._f(9.5))
        m.add_command(label="收起详情" if self.popup else "展开详情", command=self.toggle_detail)
        m.add_separator()

        look = tk.Menu(m, tearoff=0, bg=MBG, fg=MFG, activebackground=MHL,
                       activeforeground=MFG, bd=0, activeborderwidth=0, font=self._f(9.5))
        for key, label in (("taskbar", "任务栏条（贴任务栏）"), ("tray", "只在托盘（点开详情）"),
                           ("card", "悬浮卡片"), ("mini", "紧凑悬浮条")):
            look.add_command(label=("● " if self.mode == key else "   ") + label,
                             command=lambda k=key: self.set_mode(k))
        style_menu = tk.Menu(look, tearoff=0, bg=MBG, fg=MFG,
                             activebackground=MHL, activeforeground=MFG,
                             bd=0, font=self._f(9))
        for key, label in (("text", "素净（无底片）"), ("pill", "胶囊底片"), ("outline", "细描边")):
            style_menu.add_command(label=("● " if self.style == key else "   ") + label,
                                   command=lambda k=key: self.set_style(k))
        look.add_separator()
        look.add_cascade(label="样式", menu=style_menu)

        theme_menu = tk.Menu(look, tearoff=0, bg=MBG, fg=MFG,
                             activebackground=MHL, activeforeground=MFG,
                             bd=0, font=self._f(9))
        for key, label in (("auto", "跟随任务栏"), ("dark", "深色字"), ("light", "浅色字")):
            theme_menu.add_command(label=("● " if self.theme == key else "   ") + label,
                                   command=lambda k=key: self.set_theme(k))
        look.add_cascade(label="主题", menu=theme_menu)

        card_menu = tk.Menu(look, tearoff=0, bg=MBG, fg=MFG,
                            activebackground=MHL, activeforeground=MFG,
                            bd=0, font=self._f(9))
        for key, label in (("dark", "深色"), ("light", "浅色"),
                           ("minimal", "简约（白）"), ("glass", "玻璃（半透明）")):
            card_menu.add_command(label=("● " if self.card_theme == key else "   ") + label,
                                  command=lambda k=key: self.set_card_theme(k))
        look.add_cascade(label="详情卡主题", menu=card_menu)
        m.add_cascade(label="外观", menu=look)
        m.add_separator()

        layer = tk.Menu(m, tearoff=0, bg=MBG, fg=MFG, activebackground=MHL,
                        activeforeground=MFG, bd=0, activeborderwidth=0, font=self._f(9.5))
        for key, label in (("top", "置顶显示"), ("normal", "普通窗口"), ("bottom", "贴在桌面")):
            layer.add_command(label=("● " if self.layer == key else "   ") + label,
                              command=lambda k=key: self.set_layer(k))
        m.add_cascade(label="窗口层级", menu=layer)

        speed = tk.Menu(m, tearoff=0, bg=MBG, fg=MFG, activebackground=MHL,
                        activeforeground=MFG, bd=0, activeborderwidth=0, font=self._f(9.5))
        for ms, label in ((500, "0.5 秒"), (1000, "1 秒"), (2000, "2 秒"), (5000, "5 秒")):
            speed.add_command(label=("● " if self.interval == ms else "   ") + label,
                              command=lambda v=ms: self.set_interval(v))
        m.add_cascade(label="刷新间隔", menu=speed)

        alpha = tk.Menu(m, tearoff=0, bg=MBG, fg=MFG, activebackground=MHL,
                        activeforeground=MFG, bd=0, activeborderwidth=0, font=self._f(9.5))
        cur_a = float(self.cfg.get("alpha", CARD_ALPHA))
        for a in (1.0, 0.96, 0.92, 0.86):
            alpha.add_command(label=("● " if abs(cur_a - a) < 0.02 else "   ") + f"{int(a * 100)}%",
                              command=lambda v=a: self.set_alpha(v))
        m.add_cascade(label="不透明度", menu=alpha)

        m.add_separator()
        m.add_command(label=("✓ " if self.locked else "   ") + "锁定位置",
                      command=self.toggle_lock)
        m.add_command(label=("✓ " if self.is_autostart() else "   ") + "开机自动启动",
                      command=self.toggle_autostart)
        m.add_separator()
        m.add_command(label="归位", command=self.reset_position)
        m.add_command(label="打开任务管理器", command=lambda: subprocess.Popen(
            ["taskmgr.exe"], creationflags=CREATE_NO_WINDOW))
        m.add_command(label="关于 · 温度说明", command=self.show_about)
        m.add_command(label="退出监视器", command=self.quit)
        return m

    def reset_position(self):
        if self.mode == "taskbar":
            self.dock_now(force=True)
            return
        w, h = self.size()
        pw = self.root.winfo_screenwidth()
        self.root.geometry(f"+{pw - w - int(24 * self.scale)}+{int(60 * self.scale)}")
        self.save_cfg()

    def show_about(self):
        from tkinter import messagebox
        top = self.root.wm_attributes("-topmost")
        self.root.wm_attributes("-topmost", 1)
        messagebox.showinfo(
            f"关于 {APP_NAME}",
            "桌面性能监视器\n\n"
            "纯 Python 标准库实现的桌面悬浮监控：\n"
            "CPU / 内存 / 网络上下行 / GPU 占用与显存 / 温度 / 磁盘 / 运行时长。\n\n"
            "· 拖动可移动，双击切换完整/紧凑\n"
            "· 右键可改层级、刷新率、透明度、开机自启\n\n"
            "温度说明：Windows 不向普通程序开放 CPU 温度传感器。\n"
            "若需要真实 CPU 温度，请安装 LibreHardwareMonitor 并以\n"
            "管理员身份运行，本程序会自动读取它的数据；\n"
            "GPU 温度来自 NVIDIA 驱动，始终可用。",
            parent=self.root,
        )
        self.root.wm_attributes("-topmost", top)

    def set_layer(self, key):
        self.layer = key
        self.apply_layer()
        self.save_cfg()

    def set_interval(self, ms):
        self.interval = ms
        self.save_cfg()

    def set_alpha(self, a):
        self.cfg["alpha"] = a
        try:
            self.root.wm_attributes("-alpha", a)
        except Exception:
            pass
        self.save_cfg()

    def toggle_lock(self):
        self.locked = not self.locked
        self.canvas.configure(cursor="arrow" if self.locked else "hand2")
        self.save_cfg()

    # ---------------- 开机自启
    def _run_key(self):
        import winreg
        return winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
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
            key, _ = self._run_key()
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
        except Exception:
            pass

    # ---------------- 采样循环
    def tick(self):
        try:
            self._tick_once()
        except Exception:
            log_exc(*sys.exc_info())
        finally:
            self.root.after(self.interval, self.tick)

    def _tick_once(self):
        if self._popup_win:
            try:
                self.draw_card()
            except Exception:
                log_exc(*sys.exc_info())
        try:
            self.state = self.stats.sample()
        except Exception:
            self.state = {}
        self.push_history()
        self.clock = time.strftime("%H:%M:%S")
        self._bg_tick = getattr(self, "_bg_tick", 0) + 1
        if self._bg_tick % 3 == 0:
            self.refresh_bg()
        self._tip_tick = getattr(self, "_tip_tick", 0) + 1
        if self._tip_tick % 3 == 0:
            self.update_tray_tip()
        self.draw()

    def push_history(self):
        st = self.state
        for key, val in (
            ("cpu", st.get("cpu")),
            ("gpu", (st.get("gpu") or {}).get("util")),
            ("mem", st.get("mem_pct")),
            ("down", st.get("down")),
            ("up", st.get("up")),
        ):
            h = self.hist[key]
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
        # 写一个"用户主动退出"标记，看门狗看到它就不再把程序拉起来
        try:
            with open(os.path.join(BASE, "disabled.flag"), "w") as f:
                f.write("1")
        except Exception:
            pass
        try:
            if getattr(self, "tray", None):
                self.tray.remove()
        except Exception:
            pass
        self.save_cfg()
        try:
            self.stats.stop()
        except Exception:
            pass
        self.root.destroy()

    # ---------------- 绘图工具
    def rr(self, x1, y1, x2, y2, r, **kw):
        r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def text(self, x, y, s, size=8, fill=TXT, anchor="nw", weight="normal", family=None):
        return self.canvas.create_text(
            x, y, text=s, fill=fill, anchor=anchor,
            font=self._f(size, weight, family),
        )

    def spark(self, x0, y0, x1, y1, data, color, fill_color, vmax=None):
        if len(data) < 2:
            return
        mx = vmax if vmax else max(max(data), 1.0) * 1.15
        mx = max(mx, 1e-6)
        n = len(data)
        step = (x1 - x0) / (self.SPARK_N - 1)
        start = x1 - (n - 1) * step
        pts = []
        for i, v in enumerate(data):
            px = start + i * step
            py = y1 - clamp(v / mx, 0, 1) * (y1 - y0)
            pts += [px, py]
        poly = [start, y1] + pts + [x1, y1]
        self.canvas.create_polygon(poly, fill=fill_color, outline="")
        self.canvas.create_line(pts, fill=color, width=max(1.0, 1.4 * self.scale),
                                smooth=True, splinesteps=8)

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
        s = self.scale
        self._px_lab = max(8, int(round(8.6 * s)))
        self._px_val = max(10, int(round(12.8 * s)))
        lab = self._font("segoeui.ttf", self._px_lab)
        val = self._font("seguisb.ttf", self._px_val)

        self._w_lab = max(lab.getlength("CPU"), lab.getlength("RAM"))
        self._w_num = val.getlength("99")
        self._w_pct = lab.getlength("%")
        self._g1, self._g2, self._g3 = self.s(5), self.s(3), self.s(4)
        col1 = self._w_lab + self._g1 + self._w_num + self._g2 + self._w_pct
        self._w_netv = max(val.getlength("99.9M"), val.getlength("999 B"))
        self._w_arrow = val.getbbox("↓")[2]          # 箭头真实的墨迹右边界
        col2 = self._w_arrow + self._g3 + self._w_netv
        gap, pad = self.s(15), self.s(10)
        self.strip_cols = [("left", pad, col1), ("net", pad + col1 + gap, col2)]
        self.strip_w = int(round(pad + col1 + gap + col2 + pad))
        self._strip_key = None
        self._img_item = None

    # 任务栏取色 / 文字配色
    def palette(self):
        light = self.tb_light
        if self.theme == "dark":
            light = True
        elif self.theme == "light":
            light = False
        if light:
            return dict(ink=(20, 22, 26), dim=(77, 83, 92), faint=(107, 114, 128),
                        red=(196, 55, 44), amber=(154, 91, 0),
                        teal=(13, 111, 99), blue=(36, 86, 201))
        return dict(ink=(243, 245, 248), dim=(154, 162, 174), faint=(118, 126, 138),
                    red=(255, 111, 99), amber=(240, 166, 60),
                    teal=(63, 211, 189), blue=(110, 168, 255))

    def sample_taskbar(self, x0, w, y):
        g, u = _pixel_proto()
        cols = []
        hdc = u.GetDC(0)
        try:
            for i in range(16):
                x = int(x0 + (w - 1) * i / 15)
                c = g.GetPixel(hdc, x, int(y)) & 0xFFFFFFFF
                if c == 0xFFFFFFFF:
                    continue
                cols.append((i / 15, c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF))
        finally:
            u.ReleaseDC(0, hdc)
        return cols

    def capture_background(self, x, y, w, h):
        """把任务栏这一块原样截下来当背景，做到看不出接缝。"""
        try:
            buf = grab_taskbar_bgra(taskbar_hwnd(), x, y, w, h)
            if not buf:
                buf = grab_bgra(x, y, w, h)
        except Exception:
            if os.environ.get("DM_DEBUG"):
                import traceback
                traceback.print_exc()
            return False
        try:
            from PIL import Image
            self._bg_pil = Image.frombytes("RGB", (w, h), buf, "raw", "BGRX")
        except Exception:
            self._bg_pil = None
        try:
            n = w * h
            lum = 0.0
            for i in range(min(n, 3000)):
                off = (i * 7 % n) * 4
                lum += 0.114 * buf[off] + 0.587 * buf[off + 1] + 0.299 * buf[off + 2]
            self.tb_light = (lum / min(n, 3000)) > 140
        except Exception:
            pass
        return True

    def edge_base(self, W, H):
        """用条左右两侧的任务栏颜色，线性插值出一块底——和真背景几乎看不出接缝。"""
        from PIL import Image
        g, u = _pixel_proto()
        x, y, w, h = self._tb_rect or (0, 0, 0, 0)
        if not w:
            return Image.new("RGB", (W, H), (245, 244, 241) if self.tb_light else (32, 32, 34))
        samples = {0: [], 1: []}
        hdc = u.GetDC(0)
        try:
            for side, dx in ((0, -7), (1, w + 7)):
                for ty in (0.15, 0.5, 0.85):
                    c = g.GetPixel(hdc, int(x + dx), int(y + h * ty)) & 0xFFFFFFFF
                    if c == 0xFFFFFFFF:
                        c = 0x00F0F0F0 if self.tb_light else 0x00202020
                    samples[side].append((c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF))
        finally:
            u.ReleaseDC(0, hdc)

        def vert(pts, t):
            a, b = (0, 1) if t < 0.5 else (1, 2)
            k = (t - (0.15 if a == 0 else 0.5)) / (0.35 if a == 0 else 0.35)
            k = max(0.0, min(1.0, k))
            return tuple(int(pts[a][i] + (pts[b][i] - pts[a][i]) * k) for i in range(3))

        img = Image.new("RGB", (W, H))
        px = img.load()
        for yy in range(H):
            t = yy / float(max(1, H - 1))
            cl = vert(samples[0], t)
            cr = vert(samples[1], t)
            for xx in range(W):
                k = xx / float(max(1, W - 1))
                px[xx, yy] = (int(cl[0] + (cr[0] - cl[0]) * k),
                              int(cl[1] + (cr[1] - cl[1]) * k),
                              int(cl[2] + (cr[2] - cl[2]) * k))
        return img

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

    def _compose_strip(self, W, H):
        from PIL import Image, ImageDraw
        P = self.palette()
        st = self.state
        SS = 3

        def S(v):
            return v * SS

        layer = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)

        style = getattr(self, "style", "pill")
        if style in ("pill", "outline"):
            ph = H * 0.86
            py = (H - ph) / 2.0
            if style == "pill":
                k = 0.34 if self.tb_light else 0.14
                fill = (255, 255, 255, int(255 * k))
                edge = (255, 255, 255, int(255 * min(k + 0.25, 0.9)))
            else:
                fill = None
                edge = (0, 0, 0, 46) if self.tb_light else (255, 255, 255, 52)
            d.rounded_rectangle([S(3), S(py), S(W - 3), S(py + ph)],
                                radius=S(ph / 2.0), fill=fill, outline=edge,
                                width=max(1, SS))
            if fill is not None:
                d.rounded_rectangle([S(3), S(py), S(W - 3), S(py + ph)],
                                    radius=S(ph / 2.0), fill=fill)

        lab = self._font("segoeui.ttf", self._px_lab * SS)
        val = self._font("seguisb.ttf", self._px_val * SS)
        cols = {k: (x, w) for k, x, w in self.strip_cols}
        lx, lw = cols["left"]
        nx, nw = cols["net"]
        y1 = H * 0.335 * SS
        y2 = H * 0.665 * SS

        # 两列之间的极淡分隔线
        sep = (0, 0, 0, 20) if self.tb_light else (255, 255, 255, 24)
        dx = S((nx - self.s(7.5)))
        d.line([(dx, S(H * 0.24)), (dx, S(H * 0.76))], fill=sep, width=SS)

        # 数字右对齐到同一条竖线，% 固定位置 —— 两行严丝合缝
        x_num_r = S(lx + self._w_lab + self._g1 + self._w_num)
        x_pct = S(lx + self._w_lab + self._g1 + self._w_num + self._g2)
        x_net_r = S(nx + nw)

        def row(y, label, value, color):
            d.text((S(lx), y), label, font=lab, fill=P["dim"], anchor="lm")
            d.text((x_num_r, y), "--" if value is None else f"{value:.0f}",
                   font=val, fill=color, anchor="rm")
            d.text((x_pct, y), "%", font=lab, fill=P["faint"], anchor="lm")

        cpu = st.get("cpu")
        mem = st.get("mem_pct")
        row(y1, "CPU", cpu, P["ink"])
        row(y2, "RAM", mem,
            P["red"] if (mem or 0) >= 90 else (P["amber"] if (mem or 0) >= 75 else P["ink"]))

        aw = val.getbbox("↓")[2]                          # 墨迹右边界，不是前进宽度
        vx = S(nx) + aw + self.s(4.0)                     # 数值紧贴箭头墨迹
        # 上行在上、下行在下
        d.text((S(nx), y1), "↑", font=val, fill=P["blue"], anchor="lm")
        d.text((vx, y1), fmt_speed(st.get("up"), short=True),
               font=val, fill=P["ink"], anchor="lm")
        d.text((S(nx), y2), "↓", font=val, fill=P["teal"], anchor="lm")
        d.text((vx, y2), fmt_speed(st.get("down"), short=True),
               font=val, fill=P["ink"], anchor="lm")

        return layer.resize((W, H), Image.LANCZOS)

    def draw_strip(self):
        if self.strip is not None and self._tb_rect:
            try:
                img = self._compose_strip(self._tb_rect[2], self._tb_rect[3])
                x, y, w, h = self._tb_rect
                self.strip.update(img, x, y)
                self.strip.raise_top()
                return
            except Exception:
                log_exc(*sys.exc_info())
        st = self.state
        sig = (round(st.get("cpu") or -1), round(st.get("mem_pct") or -1),
               fmt_speed(st.get("down"), short=True), fmt_speed(st.get("up"), short=True),
               self.tb_light)
        if sig == getattr(self, "_last_sig", None) and self._img_item is not None:
            return                      # 内容没变就不重画，减少 Tcl 图像churn
        self._last_sig = sig
        c = self.canvas
        W = self.strip_w
        H = self._tb_h or int(round(48 * self.scale))
        if not HAVE_PIL:
            return
        try:
            from PIL import ImageTk
            self._photo = ImageTk.PhotoImage(self._compose_strip(W, H), master=self.root)
        except Exception:
            if os.environ.get("DM_DEBUG"):
                import traceback
                traceback.print_exc()
            return
        if self._img_item is None or self._strip_key != (W, H, id(self._bg_pil)):
            c.delete("all")
            self._img_item = c.create_image(0, 0, image=self._photo, anchor="nw")
            self._strip_key = (W, H, id(self._bg_pil))
        else:
            c.itemconfigure(self._img_item, image=self._photo)

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

    def card(self, w, h, r=13):
        self.canvas.configure(bg="#ff00fe")
        self.rr(0.5, 0.5, self.s(w) - 0.5, self.s(h) - 0.5, self.s(r),
                fill=BG, outline=BORDER, width=1)
        # 顶部一道极淡高光，增加玻璃质感
        self.canvas.create_line(self.s(16), self.s(1), self.s(w - 16), self.s(1),
                                fill="#1d2735")

    def _measure(self, text, size, weight="bold", family=None):
        key = (family, round(size, 1), weight)
        f = self._fonts.get(key)
        if f is None:
            f = tkfont.Font(root=self.root, family=family or self.f_semi,
                            size=max(5, int(round(size))), weight=weight)
            self._fonts[key] = f
        return f.measure(text)

    def num_unit(self, x, y, value, unit="%", size=16, unit_size=8,
                 color=TXT, unit_color=DIM):
        """数字 + 紧随其后的小号单位，自动贴合数字右边缘。"""
        self.canvas.create_text(x, y, text=value, fill=color, anchor="sw",
                                font=self._f(size, "bold"))
        if unit:
            ux = x + self._measure(value, size) + self.s(0.8)
            self.canvas.create_text(ux, y - self.s(size * 0.14), text=unit,
                                    fill=unit_color, anchor="sw",
                                    font=self._f(unit_size))

    # ---------------- 详情卡片（PIL 渲染，整张图一次更新，不闪）
    def _compose_card(self, W, H):
        from PIL import Image, ImageDraw
        P = CARD_THEMES.get(getattr(self, "card_theme", "dark"), CARD_COLORS)
        s = H / float(self.H)
        SS = 2
        img = Image.new("RGB", (W * SS, H * SS), (0, 0, 0))
        d = ImageDraw.Draw(img)

        def S(v):
            return v * s * SS

        def rgb(hx):
            hx = hx.lstrip("#")
            return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))

        def F(px, bold=False):
            name = "msyhbd.ttc" if bold else "msyh.ttc"
            return self._font(name, max(8, int(round(px * s * SS))))

        def txt(x, y, t, px, color, anchor="ls", bold=False):
            f = F(px, bold)
            d.text((S(x), S(y)), t, font=f, fill=rgb(color), anchor=anchor)
            return f

        def width_of(t, px, bold=False):
            return F(px, bold).getlength(t) / (s * SS)

        st = self.state
        cpu = st.get("cpu")
        gpu = st.get("gpu") or {}
        mem = st.get("mem_pct")
        util = gpu.get("util")
        DW = W / self.scale                     # 设计宽度

        d.rounded_rectangle([S(0.5), S(0.5), S(DW - 0.5), S(H / s - 0.5)], radius=S(13),
                            fill=rgb(P["bg"]), outline=rgb(P["border"]), width=max(1, SS))

        # ---- 顶栏
        dot = P["teal"] if (cpu or 0) < 60 else (P["amber"] if (cpu or 0) < 85 else P["red"])
        d.ellipse([S(13), S(16), S(19.5), S(22.5)], fill=rgb(dot))
        txt(24, 24, "SYSTEM", 9.5, P["dim"], "ls", True)
        if self.popup:
            txt(DW - 62, 24, self.clock, 9, P["faint"], "rs")
            d.ellipse([S(DW - 31), S(13), S(DW - 13), S(31)], fill=rgb("#1b2431"))
            txt(DW - 22, 25, "×", 12, P["text"], "ms", True)
            d.ellipse([S(DW - 56), S(13), S(DW - 38), S(31)], fill=rgb("#1b2431"))
            txt(DW - 47, 25, "···", 9, P["text"], "ms", True)
        else:
            txt(DW - 14, 24, self.clock, 9, P["faint"], "rs")
        d.line([S(14), S(34), S(DW - 14), S(34)], fill=rgb(P["line"]), width=max(1, SS))

        # ---- CPU
        txt(14, 49, "CPU", 9, P["dim"], "ls", True)
        txt(DW - 14, 49, f"峰值 {st.get('peak_cpu', 0):.0f}%", 8.5, P["faint"], "rs")
        num = f"{cpu:.0f}" if cpu is not None else "--"
        txt(14, 86, num, 26, P["text"], "ls", True)
        txt(14 + width_of(num, 26, True) + 2, 86, "%", 10, P["dim"], "ls")

        hist = self.hist.get("cpu") or []
        if len(hist) >= 2:
            x0, x1, y0, y1 = 110, DW - 14, 56, 84
            n = len(hist)
            step = (x1 - x0) / (self.SPARK_N - 1)
            pts = [(x0 + i * step, y1 - min(hist[i], 100) / 100.0 * (y1 - y0))
                   for i in range(n)]
            d.polygon([(S(pts[0][0]), S(y1))] + [(S(x), S(y)) for x, y in pts] +
                      [(S(pts[-1][0]), S(y1))], fill=rgb(P["fill"]))
            d.line([(S(x), S(y)) for x, y in pts], fill=rgb(P["teal"]),
                   width=max(1, int(1.6 * s * SS)), joint="curve")

        temp = f"{st['cpu_temp']:.0f}°C" if st.get("cpu_temp") else "—"
        txt(14, 104, f"{st.get('cores', 0)} 线程 · CPU 温度 {temp}", 8.5, P["faint"], "ls")
        cbar_w = DW - 28
        d.rounded_rectangle([S(14), S(111), S(DW - 14), S(114)], radius=S(1.5), fill=rgb(P["track"]))
        if cpu:
            d.rounded_rectangle([S(14), S(111), S(14 + cbar_w * min(cpu, 100) / 100.0), S(114)],
                                radius=S(1.5), fill=rgb(dot))
        d.line([S(14), S(121), S(DW - 14), S(121)], fill=rgb(P["line"]), width=max(1, SS))

        # ---- GPU / 内存
        gx, gw = 14, 132
        txt(gx, 134, "GPU", 9, P["dim"], "ls", True)
        det = "--"
        if gpu.get("mem_used") is not None and gpu.get("mem_total"):
            det = f"{gpu['mem_used'] / 1024:.1f}/{gpu['mem_total'] / 1024:.0f} GB"
        if gpu.get("temp") is not None:
            pw = f" · {gpu['power']:.0f}W" if gpu.get("power") else ""
            txt(gx + gw, 134, f"{det} · {gpu['temp']:.0f}°C{pw}", 8.5,
                P["amber"] if gpu["temp"] >= 80 else P["faint"], "rs")
        else:
            txt(gx + gw, 134, det, 8.5, P["faint"], "rs")
        gn = f"{util:.0f}" if util is not None else "--"
        txt(gx, 168, gn, 17, P["text"], "ls", True)
        txt(gx + width_of(gn, 17, True) + 2, 168, "%", 9, P["dim"], "ls")
        d.rounded_rectangle([S(gx), S(178), S(gx + gw), S(181)], radius=S(1.4), fill=rgb(P["track"]))
        if util:
            d.rounded_rectangle([S(gx), S(178), S(gx + gw * min(util, 100) / 100.0), S(181)],
                                radius=S(1.4), fill=rgb(P["violet"]))

        mx = 160
        d.line([S(153), S(128), S(153), S(184)], fill=rgb(P["line"]), width=max(1, SS))
        txt(mx, 134, "内存", 9, P["dim"], "ls", True)
        if st.get("mem_total"):
            txt(DW - 14, 134,
                f"{st['mem_used'] / 1024 ** 3:.1f}/{st['mem_total'] / 1024 ** 3:.0f} GB"
                f" · 峰值 {st.get('peak_mem', 0):.0f}%",
                8.5, P["faint"], "rs")
        mn = f"{mem:.0f}" if mem is not None else "--"
        txt(mx, 168, mn, 17, P["text"], "ls", True)
        txt(mx + width_of(mn, 17, True) + 2, 168, "%", 9, P["dim"], "ls")
        d.rounded_rectangle([S(mx), S(178), S(DW - 14), S(181)], radius=S(1.4), fill=rgb(P["track"]))
        if mem:
            col = P["red"] if mem >= 85 else (P["amber"] if mem >= 70 else P["teal"])
            d.rounded_rectangle([S(mx), S(178),
                                 S(mx + (DW - 14 - mx) * min(mem, 100) / 100.0), S(181)],
                                radius=S(1.4), fill=rgb(col))
        d.line([S(14), S(192), S(DW - 14), S(192)], fill=rgb(P["line"]), width=max(1, SS))

        # ---- 网络
        txt(14, 208, "↓", 11, P["teal"], "ls", True)
        txt(24, 208, fmt_speed(st.get("down")), 11, P["text"], "ls", True)
        txt(122, 208, "↑", 11, P["blue"], "ls", True)
        txt(132, 208, fmt_speed(st.get("up")), 11, P["text"], "ls", True)

        hist = self.hist.get("down") or []
        if len(hist) >= 2:
            x0, x1, y0, y1 = 212, DW - 14, 198, 216
            mxv = max(max(hist), 1.0) * 1.15
            n = len(hist)
            step = (x1 - x0) / (self.SPARK_N - 1)
            pts = [(x0 + i * step, y1 - min(hist[i] / mxv, 1) * (y1 - y0)) for i in range(n)]
            d.line([(S(x), S(y)) for x, y in pts], fill=rgb(P["teal"]),
                   width=max(1, int(1.4 * s * SS)), joint="curve")

        # ---- 进程
        d.line([S(14), S(216), S(DW - 14), S(216)], fill=rgb(P["line"]), width=max(1, SS))
        procs = st.get("procs") or []
        cnt = st.get("proc_count") or 0
        txt(14, 228, "进程", 9, P["dim"], "ls", True)
        txt(DW - 14, 228, f"共 {cnt} 个 · 按 CPU 排序", 8.5, P["faint"], "rs")
        yy = 244
        for name, cpu, mem_b, conns in procs[:3]:
            nm = name if len(name) <= 16 else name[:15] + "…"
            txt(14, yy, nm, 8.5, P["text"], "ls")
            txt(DW * 0.56, yy, f"{cpu:.1f}%", 8.5,
                P["amber"] if cpu >= 25 else P["dim"], "rs")
            txt(DW * 0.74, yy, f"{mem_b / 1024 ** 3:.2f} GB", 8.5, P["faint"], "rs")
            if conns:
                txt(DW - 14, yy, f"{conns} 链接", 8.5, P["faint"], "rs")
            yy += 16
        if not procs:
            txt(14, 244, "正在采集…", 8.5, P["faint"], "ls")

        # ---- 页脚
        free = st.get("disk_free")
        total = st.get("disk_total") or 1
        up_s = st.get("uptime") or 0
        days, rem = divmod(int(up_s), 86400)
        hours = rem // 3600
        up_txt = f"{days} 天 {hours} 小时" if days else f"{hours} 小时 {rem % 3600 // 60} 分"
        disk = f"C: 已用 {100 - free / total * 100:.0f}% · 剩余 {free / 1024 ** 3:.0f} GB" if free else ""
        iface = (st.get("ifaces") or [""])[0]
        txt(14, 308, f"{disk}   ·   已运行 {up_txt}"
                     + (f"   ·   {iface}" if iface else ""), 8.5, P["faint"], "ls")

        extra = []
        if st.get("mem_avail"):
            extra.append(f"可用内存 {st['mem_avail'] / 1024 ** 3:.1f} GB")
        if gpu.get("fan") is not None:
            extra.append(f"风扇 {gpu['fan']:.0f}%")
        batt = st.get("battery")
        if batt:
            extra.append(f"{'充电中' if batt['ac'] else '电池'} {batt['pct']}%")
        if extra:
            txt(14, 294, "   ·   ".join(extra), 8.5, P["faint"], "ls")

        return img.resize((W, H), Image.LANCZOS)

    def draw_full(self):
        c = self.canvas
        W, H = self.size()          # 以窗口几何为准，canvas 尺寸在小尺寸切换瞬间是旧的
        if not HAVE_PIL:
            return
        try:
            from PIL import ImageTk
            self._photo = ImageTk.PhotoImage(self._compose_card(W, H), master=self.root)
        except Exception:
            if os.environ.get("DM_DEBUG"):
                import traceback
                traceback.print_exc()
            return
        key = (W, H)
        if getattr(self, "_card_key", None) != key or self._card_item is None:
            c.delete("all")
            self._card_item = c.create_image(0, 0, image=self._photo, anchor="nw")
            self._card_key = key
        else:
            c.itemconfigure(self._card_item, image=self._photo)
        self._close_box = (W / self.scale - 33, 11, W / self.scale - 11, 33) if self.popup else None
        self._gear_box = (W / self.scale - 58, 11, W / self.scale - 36, 33) if self.popup else None

    def bar(self, x, y, x2, h, ratio, color):
        ratio = clamp(ratio, 0, 1)
        self.rr(x, y, x2, y + h, h / 2, fill=TRACK, outline="")
        if ratio > 0.004:
            self.rr(x, y, x + max(h, (x2 - x) * ratio), y + h, h / 2, fill=color, outline="")

    def draw_compact(self):
        st = self.state
        s = self.s
        W, H = self.CW, self.CH
        self.card(W, H, 11)
        c = self.canvas

        cpu = st.get("cpu") or 0
        gpu = (st.get("gpu") or {}).get("util")
        mem = st.get("mem_pct") or 0

        for x, label, val, col in ((10, "CPU", cpu, level_color(cpu)),
                                   (72, "GPU", gpu, VIOLET),
                                   (134, "RAM", mem, level_color(mem))):
            self.text(s(x), s(8), label, 6.5, DIM, "nw", "bold")
            self.num_unit(s(x), s(33), f"{val:.0f}" if val is not None else "--",
                          "%", 11, 6.5)
            self.rr(s(x), s(39), s(x + 50), s(41.4), s(1.2), fill=TRACK, outline="")
            if val:
                self.rr(s(x), s(39), s(x + 50 * clamp(val / 100.0, 0, 1)), s(41.4),
                        s(1.2), fill=col, outline="")

        c.create_line(s(8), s(48), s(W - 8), s(48), fill="#1b2431")

        self.text(s(10), s(55), "↓", 7, TEAL, "nw", "bold")
        self.text(s(18), s(54.5), fmt_speed(st.get("down"), short=True) + "/s",
                  7.5, TXT, "nw", "bold")
        self.text(s(112), s(55), "↑", 7, BLUE, "nw", "bold")
        self.text(s(120), s(54.5), fmt_speed(st.get("up"), short=True) + "/s",
                  7.5, TXT, "nw", "bold")


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
    if "--selftest" in sys.argv:
        dpi_aware()
        st = Stats()
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
    MonitorApp().root.mainloop()


if __name__ == "__main__":
    main()
