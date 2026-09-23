# -*- coding: utf-8 -*-
"""路径基座：项目根目录 + 配置文件位置 + 相对路径解析。

开发态：PROJECT_ROOT = saymore/../ (仓库根)。
打包态(PyInstaller onedir)：PROJECT_ROOT = Saymore.exe 所在目录,而不是 _internal/。
这样 config.json / logs / 下载的 models / kws-model / polish_lora 等用户可见资源
就摆在 {app}\ 根,别再埋 _internal 里；_internal 只留 Python 运行时。
一般相对路径按项目根解析；打包版历史切词放在独立用户目录，卸载后保留。"""
import os
import shutil
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
_HOTWORD_DATA = {"typed_history", "typed_history.jsonl", "typed_history.jsonl.bak",
                 "hotwords.json", "hotwords.txt"}


def _resolve(path):
    """一般相对路径按项目根解析；历史切词在打包版使用持久目录。"""
    p = Path(path)
    if p.is_absolute():
        return p
    if getattr(sys, "frozen", False) and str(p) in _HOTWORD_DATA:
        # 有意为之：用户切词数据独立于安装目录，卸载或重装不会清掉。
        data_dir = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Saymore"
        target = data_dir / p
        old = PROJECT_ROOT / p
        if old.exists() and not target.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(target))
        return target
    return PROJECT_ROOT / p
