# -*- coding: utf-8 -*-
"""运行环境检测：列出运行必需组件，检查缺失。

`check(cfg)` 返回:
  {
    "ready":   bool,              # 全部到位才 True
    "missing": [item, ...],       # 缺失清单（顺序 = REQUIRED）
    "present": [item, ...],       # 已就绪清单
  }

item = {key, label, role, size_mb, network, note, path}
- network=True  → 联网下载（安装包不带，首次运行需要拉）
- network=False → 安装包内置，缺失意味着安装包损坏或被误删

未就绪时 main.py 跳过语音后端初始化；面板 / 主窗口据此显示红色警告并引导下载。
"""
from saymore.paths import _resolve


# 需联网下载项的默认源 URL 列表。
# 目前只用 ModelScope(国内直连稳,不吃代理);HF 侧仓库还没上传,暂不放 hf-mirror/huggingface
# 源——占位符仓库会 404,让"多源回退"变成"先失败两次再走 ModelScope",观感=下载源失败。
# 后续如上传 HF,把 _HF_REPO 填成真实仓库 ID、在 _default_urls 前面加两条即可。
# 用户可在 config.json 里加 "download_sources": {"asr_gguf": ["url1", "url2"]} 覆盖。
_MS_REPO = "frankzch/Qwen3-ASR-1.7B-GGUF"

def _default_urls(filename):
    return [
        f"https://modelscope.cn/api/v1/models/{_MS_REPO}/repo?Revision=master&FilePath={filename}",
    ]


# 运行必需组件；顺序 = 主窗口"运行环境"tab 的展示顺序。
_REQUIRED = [
    # ── 联网下载（安装包不含）─────────────────────────────
    {"key": "asr_gguf",     "label": "Qwen3-ASR 主模型",      "role": "语音识别",
     "cfg_key": "gguf_model",       "size_mb": 1177, "network": True,
     "filename": "Qwen3-ASR-1.7B-IQ4_NL.gguf",
     "note": "通义千问3 语音识别 IQ4_NL 量化权重"},
    {"key": "asr_mmproj",   "label": "Qwen3-ASR 音频编码器",   "role": "语音识别",
     "cfg_key": "gguf_mmproj",      "size_mb": 340,  "network": True,
     "filename": "mmproj-Qwen3-ASR-1.7B-Q8_0.gguf",
     "note": "与主模型配套的多模态投影权重"},
    # ── 安装包内置 ───────────────────────────────────────
    {"key": "polish_lora",  "label": "文字整理 LoRA",         "role": "整理",
     "path": "polish_lora/multi-lora.gguf",              "size_mb": 34,   "network": False,
     "note": "四风格整理（保守/深度/邮件/00后 同一 adapter）"},
    {"key": "kws_dir",      "label": "命令词唤醒模型",         "role": "唤醒",
     "path": "kws-model",   "is_dir": True,             "size_mb": 39,   "network": False,
     "check_file": "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
     "note": "识别唤醒词与全局命令词（发送/回退/清空 等）"},
    {"key": "silero_vad",   "label": "Silero VAD 端点检测",   "role": "切句",
     "path": "silero_vad.onnx",                         "size_mb": 2,    "network": False,
     "note": "判断说话开始/结束"},
    {"key": "llama_exe",    "label": "llama.cpp 推理引擎",    "role": "推理引擎",
     "cfg_key": "llama_server_exe",                     "size_mb": 92,   "network": False,
     "note": "带 Vulkan GPU 支持；转写与整理都走它"},
]


def _resolve_path(item, cfg):
    """按 cfg_key(读配置)或 path(常量)定位文件；返回 Path 或 None。"""
    if "cfg_key" in item:
        raw = cfg.get(item["cfg_key"], "")
        return _resolve(raw) if raw else None
    return _resolve(item["path"])


def _exists(item, path):
    if path is None:
        return False
    if item.get("is_dir"):
        if not path.is_dir():
            return False
        cf = item.get("check_file")
        return (path / cf).exists() if cf else True
    return path.exists()


def _urls_for(item, cfg):
    """下载源 URL 列表：先看 cfg["download_sources"][key] 用户覆盖，否则用 _default_urls。"""
    if not item.get("network"):
        return []
    override = (cfg.get("download_sources") or {}).get(item["key"])
    if override:
        return list(override)
    fn = item.get("filename", "")
    return _default_urls(fn) if fn else []


def _to_info(item, path, ok, urls):
    return {
        "key":     item["key"],
        "label":   item["label"],
        "role":    item["role"],
        "size_mb": item["size_mb"],
        "network": item["network"],
        "note":    item["note"],
        "path":    str(path) if path else "",
        "exists":  ok,
        "urls":    urls,  # 需下载项的源 URL；前端"下载"按钮传给后端 download_start
    }


def check(cfg):
    """扫一遍所有必需组件，返回 ready/missing/present。"""
    missing, present = [], []
    for item in _REQUIRED:
        path = _resolve_path(item, cfg)
        ok = _exists(item, path)
        info = _to_info(item, path, ok, _urls_for(item, cfg))
        (present if ok else missing).append(info)
    return {"ready": not missing, "missing": missing, "present": present}
