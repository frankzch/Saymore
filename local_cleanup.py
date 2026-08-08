# -*- coding: utf-8 -*-
"""本地文本整理：整理 LoRA 的 GGUF 定位 + 四人格提示词管理。

生产路径走 llama-server + 合一 multi LoRA（保守/深度/邮件/00后 同一 adapter，
靠 system 提示词切人格），见 asr_llamacpp.py。
本模块只提供 find_gguf（定位 LoRA GGUF 文件）和 system_for（按模式返回训练时的提示词）。
"""
from pathlib import Path


_LORA_DIR = Path(__file__).parent / "cleanup_lora"


def find_gguf(name):
    """按文件名在 cleanup_lora/ 下定位整理 LoRA 的 GGUF；供 voice_input 给 llama-server 挂 LoRA 用。
    找不到返回 None（如可选的 reminder/extract LoRA 未随仓库分发时，功能自动降级）。"""
    p = _LORA_DIR / name
    return str(p) if p.exists() else None


_PROMPT_FILES = {
    "小范围整理": _LORA_DIR / "basic_prompt.txt",
    "深度整理":   _LORA_DIR / "deep_prompt.txt",
    "邮件整理":   _LORA_DIR / "mail_prompt.txt",
    "00后整理":   _LORA_DIR / "genz_prompt.txt",
}

_LEGACY_SYSTEM = (
    "你是语音识别后处理助手。用户输入是一段中文口语的原始识别文本，"
    "可能有口水词、重复、语序混乱、说错重说、同音字错误、标点缺失。\n"
    "请按以下顺序处理：\n"
    "1. 先通读全段，理解说话人真正想表达的意思和逻辑；\n"
    "2. 用书面中文重写这段话，让它逻辑清晰、语义连贯、通顺易读；\n"
    "3. 重写时以“忠实说话人原意”为最高准则，不得增删观点、不得编造事实、"
    "不得改变立场，数字保持原样。\n"
    "4. 在通顺、逻辑正确的前提下，尽量保留原文中的英文词、专有名词和技术术语；"
    "若保留它们会让句子不通或逻辑不对，则可改写或替换。\n"
    "允许而且鼓励：调整语序、合并或拆分句子、补出被省略的成分、"
    "按上下文推断被识别错的同音字（如“在座”误作“再做”）。"
    "只输出重写后的文本，不要解释。"
)


def _read_prompt(path):
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


_SYSTEM = _read_prompt(_PROMPT_FILES["小范围整理"]) or _LEGACY_SYSTEM


def system_for(mode):
    """按整理模式返回该人格训练时那句 system（训推一致）。缺文件回退保守。"""
    return _read_prompt(_PROMPT_FILES.get(mode, _PROMPT_FILES["小范围整理"])) or _SYSTEM
