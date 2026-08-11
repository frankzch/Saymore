# -*- coding: utf-8 -*-
"""Windows 命名互斥量单例守卫。

用法:
    from saymore.win.single_instance import acquire
    lock = acquire("Saymore.Backend")
    if not lock:
        # 另一实例已在跑,直接退出(或先做点前置动作再退)
        sys.exit(0)
    # 正常执行；lock 句柄要挂在进程生命周期内的变量上,
    # 别让它被 GC 掉,否则互斥量随之释放。

设计:
- 用 Local\\ 命名空间,只在当前登录会话内生效——多用户/RDP 各自独立。
- 内核对象在最后一个句柄关闭时自动释放,不用手动 CloseHandle;
  进程崩了也会被系统清理,不会留下"孤儿锁"。
- 非 Windows 或 ctypes 出错时返回 True 但 lock=None,让流程继续
  (Saymore 只在 Windows 上跑,这是兜底防呆)。
"""
import ctypes
import os
from ctypes import wintypes


ERROR_ALREADY_EXISTS = 183


class _MutexHandle:
    """薄包装,拿住句柄直到进程结束或对象被显式释放。"""
    def __init__(self, handle):
        self._handle = handle

    def release(self):
        if self._handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None

    def __del__(self):
        self.release()


def acquire(name: str):
    """尝试拿命名互斥量。拿到返回句柄对象(挂着别丢);已被占返回 None。
    非 Windows 直接返回一个占位对象(默认单例视角,不阻拦)。"""
    if os.name != "nt":
        return _MutexHandle(0)
    try:
        k = ctypes.windll.kernel32
        k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        h = k.CreateMutexW(None, False, f"Local\\{name}")
        if not h:
            return _MutexHandle(0)  # 建失败退化为不阻拦
        if ctypes.GetLastError() == ERROR_ALREADY_EXISTS:
            k.CloseHandle(h)
            return None
        return _MutexHandle(h)
    except Exception:
        return _MutexHandle(0)
