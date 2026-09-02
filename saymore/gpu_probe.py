"""显卡探测:判断本机是否有「≥4GB 专用显存的独显 + 可用 Vulkan 驱动」,据此把
计算设备定为 GPU(cuda)还是 CPU。只在首启(config 里 device=auto 时)用一次,
把确定值写回 config,让设置界面如实显示。

为什么这么判:
- 本程序 GPU 路径走 llama.cpp 的 **Vulkan** 后端(跨 N/A/I 三家),所以必须 vulkan-1.dll
  能加载,否则显卡再好也用不上 → CPU。
- 「4GB 以上显存」按用户口径只认**独显专用显存**:用 DXGI 读每块适配器的
  DedicatedVideoMemory。核显(Intel/AMD 核显)专用显存通常只有几百 MB(大头是与内存
  共享的 SharedSystemMemory),会自然落选判成 CPU——这正是想要的,别把用户放到又慢又
  占内存的核显路上。
纯 Windows,只用 ctypes + 系统自带 dxgi.dll / vulkan-1.dll,不加任何依赖。
"""

import ctypes
from ctypes import POINTER, byref, c_void_p, c_uint32, c_size_t, wintypes

# 4GB 卡实际报出的 DedicatedVideoMemory 常略低于 4*1024^3(系统预留),门槛压到 3.5GiB:
# 能稳收 4GB 卡,又把 3GB(~3.22e9)、2GB(~2.1e9)挡在外面。
_MIN_VRAM_BYTES = int(3.5 * 1024 ** 3)

_HRESULT = ctypes.c_long  # 失败为负;DXGI_ERROR_NOT_FOUND=0x887A0002 作有符号即为负


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", c_uint32), ("DeviceId", c_uint32),
        ("SubSysId", c_uint32), ("Revision", c_uint32),
        ("DedicatedVideoMemory", c_size_t),
        ("DedicatedSystemMemory", c_size_t),
        ("SharedSystemMemory", c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", c_uint32),
    ]


# IID_IDXGIFactory1 {770aae78-f26f-4dba-a829-253c83d1b387}
_IID_IDXGIFactory1 = _GUID(0x770aae78, 0xf26f, 0x4dba,
                           (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
_DXGI_ADAPTER_FLAG_SOFTWARE = 2  # WARP 软件适配器,跳过


def _com(iface, index, restype, *argtypes):
    """从 COM 接口指针的 vtable 里取第 index 个方法,做成可调用体。第一个参数是 this。"""
    vtbl = ctypes.cast(iface, POINTER(c_void_p))[0]
    fn_ptr = ctypes.cast(vtbl, POINTER(c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(fn_ptr)


def _release(iface):
    if iface:
        _com(iface, 2, ctypes.c_ulong)(iface)  # IUnknown::Release


def max_dedicated_vram_bytes():
    """遍历所有硬件适配器,返回最大的专用显存字节数;失败/无显卡返回 0。"""
    try:
        dxgi = ctypes.WinDLL("dxgi.dll")
    except OSError:
        return 0
    create = dxgi.CreateDXGIFactory1
    create.restype = _HRESULT
    create.argtypes = [POINTER(_GUID), POINTER(c_void_p)]
    factory = c_void_p()
    if create(byref(_IID_IDXGIFactory1), byref(factory)) < 0 or not factory:
        return 0
    best = 0
    try:
        enum = _com(factory, 12, _HRESULT, c_uint32, POINTER(c_void_p))  # EnumAdapters1
        i = 0
        while True:
            adapter = c_void_p()
            if enum(factory, i, byref(adapter)) < 0 or not adapter:
                break  # DXGI_ERROR_NOT_FOUND:枚举到头
            try:
                get_desc = _com(adapter, 10, _HRESULT, POINTER(_DXGI_ADAPTER_DESC1))  # GetDesc1
                desc = _DXGI_ADAPTER_DESC1()
                if get_desc(adapter, byref(desc)) >= 0 and not (desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE):
                    best = max(best, int(desc.DedicatedVideoMemory))
            finally:
                _release(adapter)
            i += 1
    finally:
        _release(factory)
    return best


def _vulkan_available():
    try:
        ctypes.WinDLL("vulkan-1.dll")  # 有 GPU 驱动就有它;<1ms
        return True
    except OSError:
        return False


def detect_device():
    """返回 "cuda"(用 GPU)或 "cpu"。条件:Vulkan 驱动可用 且 有独显专用显存 ≥4GB。
    任何探测异常都保守回退到 "cpu"——宁可慢也别在没显存的机器上崩 llama-server。"""
    try:
        if not _vulkan_available():
            return "cpu"
        vram = max_dedicated_vram_bytes()
        return "cuda" if vram >= _MIN_VRAM_BYTES else "cpu"
    except Exception as e:  # noqa: BLE001 —— 探测失败绝不能拖垮启动
        print(f"[warn] GPU 探测异常,回退 CPU: {e}")
        return "cpu"


if __name__ == "__main__":
    vram = max_dedicated_vram_bytes()
    print(f"vulkan={_vulkan_available()} 独显专用显存={vram/1024**3:.2f} GiB "
          f"→ device={detect_device()}")
