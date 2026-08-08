"""路径基座：配置文件位置 + 相对路径解析。voice_input / audio_capture / overlay 等共用。"""
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")


def _resolve(path):
    """把相对路径解析为相对脚本所在目录。"""
    p = Path(path)
    return p if p.is_absolute() else CONFIG_PATH.parent / p
