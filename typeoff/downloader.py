# -*- coding: utf-8 -*-
"""大文件下载器：分块 + 断点续传 + 多源回退 + 进度回调。

用法（同步）:
    from typeoff.downloader import download
    download(urls=["url1", "url2"], dest=Path("..."),
             on_progress=lambda done,total,rate: ..., cancel_flag=lambda: False)

用法（异步 + 供 UI 轮询）:
    from typeoff.downloader import start, progress, cancel
    start("asr_gguf", urls=[...], dest=Path("..."))
    ... UI poll ...
    p = progress("asr_gguf")   # {state, done, total, rate, msg}
    cancel("asr_gguf")

写入策略:
- 数据写到 <dest>.part（Range 从当前 .part 大小续传）。
- 完成后 os.replace 原子改名为 <dest>，保证 runtime_check 只在完整文件存在时判"就绪"。
- 断网/中止：.part 留在盘上，下次 start 从断点继续（多源回退时也从当前 .part 大小续）。

无 SDK 依赖，只用 requests（项目 requirements 已有）。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

import requests


# 单文件 GET 一块读多大：1 MiB 是网络吞吐与"取消响应速度"的平衡点
_CHUNK = 1024 * 1024

# HTTP 请求超时：connect 短，read 稍宽（大文件慢链路常见）
_CONNECT_TIMEOUT = 15
_READ_TIMEOUT = 60


# ── 同步核心 ───────────────────────────────────────────────

def _size_from_headers(resp) -> Optional[int]:
    """从 Content-Range 或 Content-Length 推总大小；两者都无返回 None。"""
    cr = resp.headers.get("Content-Range")
    if cr and "/" in cr:
        tail = cr.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit():
        # Content-Length 是"还剩多少"；若是断点续传响应，加上已下的
        offset = getattr(resp, "_offset_bytes", 0)
        return int(cl) + offset
    return None


def _try_single_url(url: str, dest: Path, on_progress: Callable[[int, int, float], None],
                    cancel_flag: Callable[[], bool]) -> None:
    """从一个 URL 下到 <dest>.part，成功则原子改名为 dest；失败抛异常。
    断点续传：若 .part 已有 N 字节，走 Range: bytes=N-。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    offset = part.stat().st_size if part.exists() else 0

    headers = {"User-Agent": "Typeoff/1.0"}
    if offset > 0:
        headers["Range"] = f"bytes={offset}-"

    with requests.get(url, headers=headers, stream=True,
                      timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT), allow_redirects=True) as r:
        # 206=续传成功；200=服务器忽略 Range 从头返回，只能覆盖重下
        if offset > 0 and r.status_code == 200:
            offset = 0
            part.unlink(missing_ok=True)
        elif r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}: {url}")

        r._offset_bytes = offset  # 让 _size_from_headers 能补上偏移
        total = _size_from_headers(r) or 0

        t0 = time.time()
        bytes_since = 0
        rate = 0.0
        mode = "ab" if offset > 0 else "wb"
        with open(part, mode) as fp:
            done = offset
            on_progress(done, total, 0.0)
            for chunk in r.iter_content(chunk_size=_CHUNK):
                if cancel_flag():
                    raise _CancelledError("用户已取消")
                if not chunk:
                    continue
                fp.write(chunk)
                done += len(chunk)
                bytes_since += len(chunk)
                dt = time.time() - t0
                if dt >= 0.5:  # 每 0.5s 结算一次速度，避免每块都算导致抖动
                    rate = bytes_since / dt
                    t0 = time.time()
                    bytes_since = 0
                on_progress(done, total, rate)
        on_progress(done, total or done, rate)

    # 完整了才改名 → runtime_check 判"就绪"的一刻文件必然完整
    os.replace(part, dest)


class _CancelledError(RuntimeError):
    """用户取消：不算下载失败，UI 显示"已取消"而非"下载失败"。"""


def download(urls: List[str], dest: Path,
             on_progress: Callable[[int, int, float], None] = lambda d, t, r: None,
             cancel_flag: Callable[[], bool] = lambda: False) -> None:
    """按 urls 顺序尝试；每个源失败换下一个（.part 保留供下一源续传）。
    全部失败抛出最后一次的异常；被取消抛 _CancelledError。"""
    last_err = None
    for url in urls:
        if cancel_flag():
            raise _CancelledError("用户已取消")
        try:
            _try_single_url(url, dest, on_progress, cancel_flag)
            return
        except _CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 多源逐个尝试，非致命
            last_err = e
            continue
    raise RuntimeError(f"所有下载源均失败：{last_err}")


# ── 异步管理器（给 UI 用）────────────────────────────────

# key -> {"state","done","total","rate","msg","cancel","thread"}
# state: idle / running / done / error / cancelled
_TASKS: dict = {}
_LOCK = threading.Lock()


def _init(key: str) -> dict:
    with _LOCK:
        t = _TASKS.setdefault(key, {})
        t.update(state="idle", done=0, total=0, rate=0.0, msg="")
        return t


def start(key: str, urls: List[str], dest: Path) -> dict:
    """启后台线程下载；同一 key 若已在跑则原样返回，不重开。"""
    with _LOCK:
        cur = _TASKS.get(key)
        if cur and cur.get("state") == "running":
            return dict(cur)

    task = _init(key)
    cancel_ev = threading.Event()
    task["state"] = "running"
    task["cancel"] = cancel_ev

    def on_prog(done, total, rate):
        with _LOCK:
            task["done"] = done
            task["total"] = total
            task["rate"] = rate

    def worker():
        try:
            download(urls, Path(dest), on_prog, cancel_ev.is_set)
            with _LOCK:
                task["state"] = "done"
                task["msg"] = "已完成"
        except _CancelledError:
            with _LOCK:
                task["state"] = "cancelled"
                task["msg"] = "已取消"
        except Exception as e:  # noqa: BLE001 兜底，任何异常都不吞
            with _LOCK:
                task["state"] = "error"
                task["msg"] = str(e)

    th = threading.Thread(target=worker, daemon=True)
    task["thread"] = th
    th.start()
    return dict(task)


def progress(key: Optional[str] = None) -> dict:
    """key=None 返回全部任务快照 {key: {...}}；否则返回单个（不存在则空 idle）。
    快照剔掉 cancel/thread 等非序列化字段，能直接回给前端。"""
    with _LOCK:
        if key is None:
            return {k: {kk: vv for kk, vv in v.items() if kk not in ("cancel", "thread")}
                    for k, v in _TASKS.items()}
        v = _TASKS.get(key, {"state": "idle", "done": 0, "total": 0, "rate": 0.0, "msg": ""})
        return {kk: vv for kk, vv in v.items() if kk not in ("cancel", "thread")}


def cancel(key: str) -> bool:
    """标记取消。真正停在下一个 chunk 边界（最多 1MB 传输后停）。"""
    with _LOCK:
        t = _TASKS.get(key)
        if not t or t.get("state") != "running":
            return False
        ev = t.get("cancel")
        if ev is not None:
            ev.set()
        return True


def reset(key: str) -> None:
    """把某任务从 STATE 里清掉（error/cancelled 后重开前调）。不删 .part 文件——供续传。"""
    with _LOCK:
        _TASKS.pop(key, None)
