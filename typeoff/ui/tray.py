# -*- coding: utf-8 -*-
"""系统托盘图标：常驻通知区，左键/双击拉起主界面，右键弹菜单，退出时移除。

宿主是 overlay.py 的 Win32 消息循环——本模块只封装 Shell_NotifyIcon 的增删与回调分发，
不自建窗口/消息循环。用法：建好 hwnd 后 TrayIcon(hwnd, ico_path, tooltip, on_activate,
on_menu)；wndproc 收到 TrayIcon.MSG 转 tray.handle(lp)；退出前 tray.remove()。
"""
import ctypes
import time
from ctypes import wintypes

MSG = 0x8000  # WM_APP：托盘回调消息号，wndproc 见此即调 handle()
_AUMID = "Typeoff.VoiceInput"  # 应用身份 ID：不注册的话 Win10/11 弹的 toast 会显示成来源"Python"

_NIM_ADD, _NIM_MODIFY, _NIM_DELETE, _NIM_SETVERSION = 0, 1, 2, 4
_NOTIFYICON_VERSION_4 = 4
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP, _NIF_INFO = 0x1, 0x2, 0x4, 0x10
_NIIF_INFO = 0x1
_WM_LBUTTONUP, _WM_LBUTTONDBLCLK, _WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
_IMAGE_ICON, _LR_LOADFROMFILE, _LR_DEFAULTSIZE = 1, 0x10, 0x40


class _NOTIFYICONDATA(ctypes.Structure):
    # 声明到 dwInfoFlags（NOTIFYICONDATAW V3 布局），cbSize 取本结构体大小即为有效版本
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD)]


def register_app_identity(ico_path):
    """给进程注册一个应用身份(AUMID)+ 注册表里的展示名/图标：balloon()走的 NIIF_INFO 在
    Win10/11 上其实是走 Action Center 的 toast 通知，没有 AUMID 时 Windows 会把来源显示成
    宿主进程"Python"而不是"Typeoff"。两步都失败也不影响托盘图标本身正常工作，只是通知来源
    名字不好看，所以异常直接吞掉。

    必须在进程创建任何窗口/UI 之前调用（AUMID 只能在进程生命周期内设一次、且要趁早）——
    调用方是 voice_input.py 的 main()，在拉起悬浮窗线程之前，不要挪到 TrayIcon 构造里
    （那时圆环窗口早就建好了，太晚了不生效）。"""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_AUMID)
    except Exception:
        pass
    try:
        import winreg
        key_path = f"Software\\Classes\\AppUserModelId\\{_AUMID}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Typeoff")
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(ico_path))
    except Exception:
        pass


class TrayIcon:
    def __init__(self, hwnd, ico_path, tooltip, on_activate, on_menu):
        self._on_activate = on_activate
        self._on_menu = on_menu
        self._last_activate = 0.0
        u, s = ctypes.windll.user32, ctypes.windll.shell32
        self._shell = s
        # 显式声明返回句柄类型：64 位下默认 restype=c_int 会截断 HICON 指针
        u.LoadImageW.restype = ctypes.c_void_p
        u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
        # 按系统小图标尺寸从 .ico 里挑最合适的一档，边缘清晰不糊
        hicon = u.LoadImageW(None, str(ico_path), _IMAGE_ICON, 0, 0,
                             _LR_LOADFROMFILE | _LR_DEFAULTSIZE)
        nid = _NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        nid.uCallbackMessage = MSG
        nid.hIcon = hicon
        nid.szTip = tooltip
        self._nid = nid
        s.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(nid))
        # 不设版本的话图标停留在 Win2000 兼容模式：气泡/toast 表现更旧、图标被收进“隐藏的图标”
        # 时也不会在弹通知时自动露出来。NOTIFYICON_VERSION_4 是 Vista+ 起要求的“现代”行为。
        nid.uVersion = _NOTIFYICON_VERSION_4
        s.Shell_NotifyIconW(_NIM_SETVERSION, ctypes.byref(nid))

    def handle(self, lparam):
        """wndproc 收到 MSG 时调用：低字为鼠标事件——左键/双击拉起主界面，右键弹菜单。
        双击在 Windows 下实际发出 LBUTTONUP→LBUTTONDBLCLK→LBUTTONUP 三个消息，
        全落在几十毫秒内；而主界面是 subprocess 起的独立进程，pywebview 起窗要几百毫秒，
        _focus_existing() 的查重来不及生效，三次触发会叠开三个窗口。这里做去抖：
        800ms 内只放行一次，从消息源头掐断，不依赖子进程窗口何时真正建好。"""
        event = lparam & 0xFFFF
        if event in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
            now = time.monotonic()
            if now - self._last_activate < 0.8:
                return
            self._last_activate = now
            self._on_activate()
        elif event == _WM_RBUTTONUP:
            self._on_menu()

    def set_tip(self, tooltip):
        """改悬浮在图标上的 tooltip 文字（不弹气泡，纯被动——鼠标划过去才看得到）。"""
        self._nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        self._nid.szTip = tooltip
        self._shell.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(self._nid))

    def balloon(self, title, msg):
        """从托盘图标弹一个系统气泡通知（Windows 收到后按其通知设置显示几秒/进通知中心）。"""
        self._nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP | _NIF_INFO
        self._nid.szInfo = msg
        self._nid.szInfoTitle = title
        self._nid.dwInfoFlags = _NIIF_INFO
        self._shell.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(self._nid))

    def remove(self):
        self._shell.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(self._nid))
