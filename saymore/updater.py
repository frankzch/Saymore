"""自动更新：查 GitHub 最新 Release → 提示 → 下载安装包 → 校验 → 静默安装并让位退出。

跑在语音后端进程里（`start(state, close_main_window)`），只在打包版生效（开发版没有安装包可装）。
和主界面子进程走文件 IPC，跟模型下载同一套路：
  .update_status.json   本模块写：当前版本 / 最新版本信息 / 下载阶段与进度，主界面横条据此渲染
  .update_control.json  主界面写 {"action": "install"}，本模块接住开始下载安装
托盘气泡复用 overlay 的 .tray_notice 通道；托盘菜单读 state["update"]。

强制更新：Release 说明里含 FORCE_MARKER 即为强制——主界面自动弹出且遮罩不可关，只能更新或退出。
校验：Release 说明里写了 `SHA-256：<64位hex>` 就按它校验，否则只核对字节数与 GitHub 资产大小一致。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from saymore.paths import CONFIG_PATH
from saymore.version import __version__

API_URL = "https://api.github.com/repos/frankzch/Saymore/releases/latest"
FORCE_MARKER = "<!-- saymore:force -->"
CHECK_DELAY_S = 30            # 启动后先等一会儿再查，别跟模型加载抢网络/CPU
CHECK_INTERVAL_S = 6 * 3600   # 之后每 6 小时查一次（GitHub 未鉴权限额 60 次/小时，远够）
_ASSET_RE = re.compile(r"^Saymore-Setup-.*\.exe$", re.I)
_SHA_RE = re.compile(r"SHA-?256\s*[：:]\s*([0-9A-Fa-f]{64})")

_DIR = CONFIG_PATH.parent
STATUS_FILE = _DIR / ".update_status.json"
CONTROL_FILE = _DIR / ".update_control.json"
NOTICE_FILE = _DIR / ".tray_notice"

_status = {"current": __version__, "latest": None, "phase": "idle",
           "done": 0, "total": 0, "error": ""}
_lock = threading.Lock()


def parse_version(s):
    """'v1.2.10' → (1, 2, 10)；解析不了返回 None。"""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)\s*$", s or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def parse_release(rel):
    """GitHub release JSON → 更新信息 dict；没有安装包资产返回 None。"""
    asset = next((a for a in rel.get("assets") or [] if _ASSET_RE.match(a.get("name", ""))), None)
    if asset is None:
        return None
    body = rel.get("body") or ""
    sha = _SHA_RE.search(body)
    return {
        "version": (rel.get("tag_name") or "").lstrip("vV"),
        "notes": body.replace(FORCE_MARKER, "").strip()[:3000],
        "force": FORCE_MARKER in body,
        "url": asset["browser_download_url"],
        "name": asset["name"],
        "size": int(asset.get("size") or 0),
        "sha256": sha.group(1).upper() if sha else "",
        "page": rel.get("html_url") or "",
    }


def check():
    """查一次最新 Release；有新版本返回更新信息，否则 None。网络错误抛异常由调用方记日志。"""
    import requests
    r = requests.get(API_URL, timeout=15,
                     headers={"Accept": "application/vnd.github+json",
                              "User-Agent": f"Saymore/{__version__}"})
    r.raise_for_status()
    info = parse_release(r.json())
    if info is None:
        print(f"[update] 最新 Release 没有安装包资产，跳过")
        return None
    if not is_newer(info["version"], __version__):
        print(f"[update] 已是最新：当前 {__version__}，最新 {info['version']}")
        return None
    return info


def _set(**kw):
    with _lock:
        _status.update(kw)
        data = json.dumps(_status, ensure_ascii=False)
    try:
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(STATUS_FILE)  # 原子替换，主界面不会读到半截 JSON
    except OSError as e:
        print(f"[update] 写状态文件失败：{e}")


def download(info, dest_dir=None):
    """下载安装包到临时目录并校验，返回本地路径；失败抛异常。进度写进状态文件。"""
    import requests
    dest_dir = Path(dest_dir or Path(tempfile.gettempdir()) / "Saymore-update")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / info["name"]
    part = dest.with_suffix(".part")
    print(f"[update] 开始下载 {info['url']}（{info['size'] / 1e6:.0f} MB）")
    _set(phase="downloading", done=0, total=info["size"], error="")
    h = hashlib.sha256()
    done = 0
    last = 0.0
    with requests.get(info["url"], stream=True, timeout=(15, 60),
                      headers={"User-Agent": f"Saymore/{__version__}"}) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if time.monotonic() - last > 0.5:
                    _set(done=done)
                    last = time.monotonic()
    _set(phase="verifying", done=done)
    if info["size"] and done != info["size"]:
        raise RuntimeError(f"下载不完整：{done} / {info['size']} 字节")
    if info["sha256"] and h.hexdigest().upper() != info["sha256"]:
        raise RuntimeError("安装包校验失败（SHA-256 不一致），已丢弃")
    os.replace(part, dest)
    print(f"[update] 下载完成并校验通过：{dest}")
    return dest


def launch_installer(path):
    """静默安装（只显示进度条）；安装完由 setup.iss 的 WizardSilent 项自动重新启动 Saymore。"""
    subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                     close_fds=True)


def _install(state, close_main_window):
    info = state.get("update")
    if not info or _status["phase"] in ("downloading", "verifying", "launching"):
        return
    try:
        path = download(info)
        _set(phase="launching")
        print("[update] 启动安装程序，Saymore 退出让位")
        launch_installer(path)
        close_main_window()
        state["quit"] = True
    except Exception as e:  # noqa: BLE001 失败要让用户在横条上看到并可重试
        print(f"[update] [error] 更新失败：{e}")
        _set(phase="error", error=str(e))


def start(state, close_main_window, show_main_window):
    """后台线程：定时检查 + 接主界面/托盘的安装请求。state["update_install"] 供托盘菜单直接调用。"""
    if not getattr(sys, "frozen", False):
        print(f"[update] 开发版（{__version__}）不检查更新")
        return
    CONTROL_FILE.unlink(missing_ok=True)
    _set()
    state["update_install"] = lambda: threading.Thread(
        target=_install, args=(state, close_main_window), daemon=True).start()

    def _checker():
        time.sleep(CHECK_DELAY_S)
        notified = None
        while not state["quit"]:
            try:
                print(f"[update] 检查更新（当前 {__version__}）")
                info = check()
                if info:
                    state["update"] = info
                    _set(latest=info)
                    print(f"[update] 发现新版本 {info['version']}{'（强制）' if info['force'] else ''}")
                    if notified != info["version"]:
                        notified = info["version"]
                        NOTICE_FILE.write_text(
                            f"发现新版本 {info['version']}，点击查看并更新", encoding="utf-8")
                    if info["force"]:
                        show_main_window()
            except Exception as e:  # noqa: BLE001 没网/被限流：记一笔，下轮再查
                print(f"[update] [warn] 检查更新失败：{e}")
            for _ in range(CHECK_INTERVAL_S):
                if state["quit"]:
                    return
                time.sleep(1)

    def _control_watcher():
        while not state["quit"]:
            try:
                if CONTROL_FILE.exists():
                    action = json.loads(CONTROL_FILE.read_text(encoding="utf-8")).get("action")
                    CONTROL_FILE.unlink(missing_ok=True)
                    if action == "install":
                        state["update_install"]()
                    elif action == "quit":  # 强制更新遮罩上的「退出程序」
                        print("[update] 用户在强制更新提示中选择退出")
                        close_main_window()
                        state["quit"] = True
            except Exception as e:  # noqa: BLE001
                print(f"[update] [warn] 处理更新请求出错：{e}")
            time.sleep(0.5)

    threading.Thread(target=_checker, daemon=True).start()
    threading.Thread(target=_control_watcher, daemon=True).start()
