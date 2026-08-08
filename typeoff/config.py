# -*- coding: utf-8 -*-
"""配置：DEFAULT_CONFIG 种子 + config.json 读写。

DEFAULT_CONFIG 是"种子"。load_config() 启动时把缺失项补写回 config.json，让
config.json 成为完整的单一配置真源——设置窗口直接读它即所见即所得。用户改过
的键原样保留，只补没有的。"""
import json
import re

import typeoff.ui.style as ui_style
from typeoff.paths import CONFIG_PATH, _resolve


DEFAULT_CONFIG = {
    "wake_words": ["小美"],   # 唤醒词（可多个）；说中其一即进入聆听状态。建议 3-4 字且不常用
    "sleep_after_seconds": 300,  # 唤醒后静默超过此秒数（默认 5 分钟）进入完整休眠：回到待唤醒并卸载模型释放内存/显存。最后 60s 悬浮窗显示倒计时
    "kws_model_dir": "kws-model",       # sherpa-onnx KWS 模型目录（解压 release 得到，含 *.onnx + tokens.txt）
    "kws_tokens_type": "phone+ppinyin", # 关键词转 token 方式：中英混合模型用 phone+ppinyin；纯中文 wenetspeech 模型用 ppinyin
    "kws_threshold": 0.18,           # 唤醒灵敏度，越小越易触发（也越易误触）；默认 0.25
    "llama_server_exe": "llama-cpp/llama-server.exe",  # llama.cpp 预编译 Vulkan 版 llama-server 路径
    "gguf_model": "models/Qwen3-ASR-1.7B-GGUF/Qwen3-ASR-1.7B-IQ4_NL.gguf",       # qwen_gguf: 主模型(LLM 解码器)；IQ4_NL(NormalFloat4，匹配训练 nf4 网格)较 Q4_K_M 提回提醒时间算术；回滚改回 Q4_K_M 文件即可
    "gguf_mmproj": "models/Qwen3-ASR-1.7B-GGUF/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf", # qwen_gguf: 音频编码器
    "llama_port": 8901,          # qwen_gguf: llama-server 本地端口
    "asr_min_confidence": 0.6,   # 置信度(生成 token 平均概率 0..1)低于此值视为没听清，播提示语丢弃不上屏；0=不启用
    "asr_zh_en_only": True,      # 只说中英文：丢弃误识别成日文假名/韩文的整句（绝不粘出日韩文）
    "language": "Chinese",       # 识别语言（Qwen 用英文语言名；留空/None=自动检测）
    "device": "auto",            # auto=启动时探测 vulkan-1.dll，有则用 GPU，无则回退 CPU；也可显式填 cuda / cpu
    "sample_rate": 16000,        # 输入采样率（Qwen3-ASR 内部按 16kHz 处理）
    "input_device": "",          # 麦克风：""=系统默认；否则填 sounddevice 里的设备名
    "vad_model": "silero_vad.onnx",  # Silero VAD v5 模型路径（相对脚本目录或绝对）。存在则用 sherpa-onnx VoiceActivityDetector 高级接口切句
    "vad_threshold": 0.5,        # Silero VAD 概率阈值：>此值视为人声。0.5 是官方默认；调高更保守（漏轻声），调低更灵敏
    "silence_rms": 0.015,        # 回退方案：VAD 模型缺失时用的 RMS 静音阈值（float32 振幅），低于此视为静音
    "silence_seconds": 0.5,      # 连续静音超过此时长即切一句，丢给后台转写
    "min_segment_seconds": 0.15, # 一句短于此时长视为误触/碎句，丢弃
    "max_segment_seconds": 15.0, # 一句超过此时长即强制切句
    "min_speech_peak": 0.08,     # 整段峰值低于此值判为背景噪音，直接跳过不识别
    "paste": True,               # True=自动粘贴; False=仅复制到剪贴板
    "simplified": True,          # True=用 OpenCC 把繁体统一转成简体
    "append_period": True,       # True=句末无结束标点时自动补一个句号
    "local_polish": True,        # True=回填前把攒的话过本地整理模型：走 llama-server+整理 LoRA
    "polish_mode": "小范围整理",  # 默认整理模式：小范围整理/深度整理/邮件整理/00后整理
    "polish_quiet_seconds": 10.0,   # 停止说话后等多久自动触发整理(秒)
    "polish_min_confidence": 0.6,   # 整理置信度低于此值：面板里该段标红提示用户复核，不丢弃/不阻断回填
    "panel_low_conf_rgb": ui_style.PANEL_LOW_CONF_RGB,  # 整理置信度偏低：红色
    "polish_context_chars": 80,  # 小范围整理的活跃窗上限(字)：只重整理最近这么多字+新句
    "hear_cue_min_gap": 3.0,     # 每识别一句回一声"嗯/好"的最小间隔(秒)
    "save_audio": True,          # True=留存语音 wav+转写结果给端到端微调攒同分布数据
    "panel": True,               # True=识别句先进右下角半透明玻璃面板缓冲，可语音纠正后再整理回填输入框
    # 磨砂面板外观：默认值统一取自 ui_style 主题，配置可覆盖
    "panel_tint": ui_style.PANEL_TINT,
    "panel_text_rgb": ui_style.PANEL_TEXT_RGB,             # 已整理：绿色
    "panel_raw_text_rgb": ui_style.PANEL_RAW_TEXT_RGB,     # 未整理：黑色
    "panel_hint_text_rgb": ui_style.PANEL_HINT_TEXT_RGB,   # 状态提示：最淡
    "panel_font_px": 13,
    "panel_max_h": 240,
    "overlay": True,             # True=屏幕显示录音状态小圆点
    "overlay_pos": None,         # 小圆窗左上角屏幕坐标 [x,y]；拖动后记住位置
    # 无云端 LLM 配置：转写/四种整理/屏幕提词/历史热词全部走本地 llama-server + 多 LoRA
    "qwen_context_file": "terms.txt",
    "bias_max_terms": 80,        # 长段偏置词表总上限（不含命令词）；优先级：命令词>屏幕热词>静态术语>历史热词
    "screen_bias_max_terms": 10, # 屏幕热词份额硬上限
    "static_bias_max_terms": 30, # 静态术语份额硬上限；历史热词无 cap，吃剩余
    "focus_window_title": "",    # 留空=通用：一律在当前前台窗口自动找输入框
    "focus_input_name": "",      # 仅当前台命中 focus_window_title 时才用 UIA 精确定位到此名字的输入控件
    "send_words": ["发送", "提交"],       # 整句只说这些词之一时，不打字而是切回窗口回填/回车提交
    "auto_enter": True,                    # 发送时是否自动按回车提交
    "confirm_words": ["弹框确认", "确认弹框"],
    "undo_words": ["回退", "撤销"],
    "clear_words": ["清空", "清空重来"],
    "polish_words": ["文本整理", "立即整理"],
    "sleep_words": ["休眠", "睡觉"],
    "quit_words": ["退出", "关闭程序"],
}

SENTENCE_END = "。！？.!?…~"     # 已以这些标点结尾则不再补句号

# 日文假名 + 韩文谚文（含 Jamo）。只说中英文时，命中即视为误识别整句丢弃
JA_KO_RE = re.compile(r"[぀-ヿ가-힣ᄀ-ᇿ㄰-㆏]")


def load_config():
    """DEFAULT_CONFIG 是"种子"：首次或新增项时给默认值。启动即把缺失项补写进
    config.json，让它成为完整的单一配置真源。用户改过的键原样保留（cfg.update
    覆盖在后），只补没有的。"""
    cfg = dict(DEFAULT_CONFIG)
    on_disk = {}
    if CONFIG_PATH.exists():
        try:
            on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(on_disk)
        except Exception as e:
            print(f"[warn] 读取 config.json 失败，使用默认配置: {e}")
            return cfg  # 文件坏了别拿默认覆盖它，留着让用户/power user 自己修
    if cfg != on_disk:  # 有缺失的默认项 → 补写回盘（用户值已在 cfg 里保留）
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


