"""按日期切分日志 + 每行贴 HH:MM:SS.mmm 时间戳。

pythonw 后台模式下装到 sys.stdout/stderr，所有 print() 免改就带时间戳，
且落到 logs/voice_input-YYYY-MM-DD.log，天变自动换文件，单文件不会无限膨胀。
"""
from __future__ import annotations

import sys
import time
import threading
from pathlib import Path


class DailyTimestampedLog:
    def __init__(self, log_dir: Path, name: str = "voice_input"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.name = name
        self._date = ""
        self._fh = None
        self._at_line_start = True
        self._lock = threading.Lock()

    def _ensure_file(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today == self._date and self._fh is not None:
            return
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        self._date = today
        path = self.log_dir / f"{self.name}-{today}.log"
        # 新文件开头写 BOM：PowerShell 5.1/记事本才不会按 GBK 误读成乱码
        is_new = not path.exists() or path.stat().st_size == 0
        self._fh = open(path, "a", encoding="utf-8", buffering=1)
        if is_new:
            self._fh.write("\ufeff")

    def write(self, s: str) -> int:
        if not s:
            return 0
        with self._lock:
            self._ensure_file()
            i = 0
            while i < len(s):
                if self._at_line_start:
                    now = time.time()
                    ts = time.strftime("%H:%M:%S", time.localtime(now))
                    ms = int((now % 1) * 1000)
                    self._fh.write(f"[{ts}.{ms:03d}] ")
                    self._at_line_start = False
                j = s.find("\n", i)
                if j < 0:
                    self._fh.write(s[i:])
                    return len(s)
                self._fh.write(s[i:j + 1])
                self._at_line_start = True
                i = j + 1
            return len(s)

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except Exception:
                    pass


def install(log_dir: Path, name: str = "voice_input") -> DailyTimestampedLog:
    log = DailyTimestampedLog(log_dir, name)
    sys.stdout = log
    sys.stderr = log
    return log
