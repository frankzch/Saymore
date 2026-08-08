"""麦克风列表 + 电平表探针（供设置面板选设备/试音用）。

list_input_devices() 返回按名字去重的输入设备列表（同名多后端只留一个）。
LevelMeter 独立开 InputStream 采一路 float32、只算 RMS，别的一概不做——设置窗口
subprocess 就靠它给页面吐 0..1 的电平数字，不牵连主程序的采集流。
"""
import sys
import threading

import numpy as np
import sounddevice as sd


_NAME_BLACKLIST = ("映射器", "mapper", "primary sound", "主要声音")


def _pick_hostapi(hostapis):
    """Windows 上优先 WASAPI（现代 API,无虚拟桥接,同一物理设备只出一次）;
    没有就退到默认 hostapi。其他平台一般只有一个 hostapi,取默认即可。"""
    for i, h in enumerate(hostapis):
        if h.get("name") == "Windows WASAPI":
            return i
    try:
        return sd.default.hostapi
    except Exception:
        return 0


def list_input_devices():
    """只列真实物理输入设备。返回 [{'name','is_default'}, ...]。
    Windows 上过滤掉 MME/DirectSound 重复项、"声音映射器"这类桥接虚拟设备,以及蓝牙原始描述串。"""
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception as e:  # noqa: BLE001
        print(f"[mic_probe] 列设备失败：{e}", file=sys.stderr)
        return []

    api_idx = _pick_hostapi(hostapis)
    default_in = hostapis[api_idx].get("default_input_device", -1)
    default_name = devices[default_in]["name"] if 0 <= default_in < len(devices) else None

    out = []
    seen = set()
    for d in devices:
        if d.get("max_input_channels", 0) <= 0:
            continue
        if d.get("hostapi") != api_idx:
            continue
        name = d["name"]
        low = name.lower()
        if name.startswith("@") or any(b in low for b in _NAME_BLACKLIST):
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "is_default": (name == default_name)})
    return out


class LevelMeter:
    """开一路 InputStream 只算 RMS，主循环外调用 latest() 拿最新值（0..1）。

    device_name 为空 → 系统默认设备；找不到匹配也退到默认。
    只在设置面板打开时启一次，切设备就 stop() 再 start()，页面关就 stop()。
    """

    def __init__(self, device_name="", sample_rate=16000, block_ms=50):
        self.device_name = device_name or ""
        self.sample_rate = sample_rate
        self.blocksize = int(sample_rate * block_ms / 1000)
        self._lock = threading.Lock()
        self._rms = 0.0
        self._stream = None
        self._err = None

    def _resolve_device(self):
        if not self.device_name:
            return None
        try:
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) > 0 and d["name"] == self.device_name:
                    return i
        except Exception:
            pass
        return None  # 找不到就退默认，别报错

    def _callback(self, indata, frames, time_info, status):
        if indata.size == 0:
            return
        rms = float(np.sqrt(np.mean(indata[:, 0].astype(np.float32) ** 2)))
        with self._lock:
            self._rms = rms

    def start(self):
        self.stop()
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self.blocksize,
                device=self._resolve_device(),
                callback=self._callback,
            )
            self._stream.start()
            self._err = None
        except Exception as e:  # noqa: BLE001
            self._err = str(e)
            self._stream = None

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            self._rms = 0.0

    def latest(self):
        """返回 (rms, error_or_none)。页面按 15Hz 左右轮询即可。"""
        with self._lock:
            return self._rms, self._err


if __name__ == "__main__":
    devs = list_input_devices()
    print(f"输入设备 {len(devs)} 个：")
    for d in devs:
        mark = "  (默认)" if d["is_default"] else ""
        print(f"  - {d['name']}{mark}")
