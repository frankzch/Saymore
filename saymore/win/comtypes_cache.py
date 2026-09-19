# -*- coding: utf-8 -*-
"""comtypes 生成的 COM 包装(UIAutomation / SAPI)放哪。

打包后 comtypes 默认写 %TEMP%\\comtypes_cache\\Saymore-310\\,而 TEMP 会被 Windows
存储感知/磁盘清理删掉;comtypes 只在 import 时建一次目录,删了不会重建 → UIA 加载失败,
屏幕热词、输入框聚焦一直挂到重启。所以把它挪到安装目录下(与 logs/ 同级,系统不会清),
每次用前再 makedirs 兜底用户手动删除。开发态 gen_dir 在 site-packages 里,不在 TEMP,不动。
"""
import os
import tempfile


def _under_temp(p):
    tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    return os.path.normcase(os.path.abspath(p)).startswith(tmp + os.sep)


def ensure():
    """保证 comtypes 缓存目录在且不在 TEMP。须在 import uiautomation / CreateObject 之前调。"""
    try:
        import comtypes.client
        import comtypes.gen
        gd = comtypes.client.gen_dir
        if gd and _under_temp(gd):
            from saymore.paths import PROJECT_ROOT
            new = str(PROJECT_ROOT / "comtypes_cache")
            os.makedirs(new, exist_ok=True)  # 建不了(无写权限)就抛出,保持原 TEMP 目录
            # 生成时写 comtypes.client.gen_dir,导入时查 comtypes.gen.__path__,两处都要指过去
            comtypes.gen.__path__.append(new)
            comtypes.client.gen_dir = gd = new
        if gd:
            os.makedirs(gd, exist_ok=True)
    except Exception as e:
        print(f"[warn] comtypes 缓存目录设置失败: {e}")
