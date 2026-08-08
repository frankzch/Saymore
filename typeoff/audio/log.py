# -*- coding: utf-8 -*-
"""留存每句语音+转写结果：给以后端到端(音频→整理文本)微调攒真实分布数据。

见 finetune/ROUNDS.md 方向 I 路线 3——外部数据集(ASCEND 等)是别人的说话风格,
自己的录音才是同分布素材。存储账:16kHz 单声道 16bit WAV ≈ 1.9MB/分钟。

产物(logs/audio/ 下):
  YYYYMMDD/HHMMSS_fff.wav   每句一个文件
  asr_log.jsonl             每行 {"ts","wav","text","conf"},与 wav 一一对应;
                            事后可按 ts 与整理日志对齐拼出(音频→整理文本)对
"""
import json
import time
import wave
from pathlib import Path

import numpy as np

from typeoff.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT / "logs" / "audio"


def save(audio, sr, text, conf=None):
    """audio: float32 -1..1 numpy 数组。失败只打警告——留存是旁路,绝不干扰转写主流程。"""
    try:
        ts = time.time()
        day = time.strftime("%Y%m%d", time.localtime(ts))
        name = time.strftime("%H%M%S", time.localtime(ts)) + f"_{int(ts * 1000) % 1000:03d}.wav"
        d = ROOT / day
        d.mkdir(parents=True, exist_ok=True)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(str(d / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        with (ROOT / "asr_log.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": round(ts, 3), "wav": f"{day}/{name}",
                                 "text": text, "conf": conf}, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 留存音频失败: {e}")


if __name__ == "__main__":
    # 最小自检:写进临时目录,验证 wav+jsonl 落盘且能读回
    import sys
    import tempfile
    mod = sys.modules[__name__]
    with tempfile.TemporaryDirectory() as tmp:
        mod.ROOT = Path(tmp)
        save(np.zeros(1600, dtype=np.float32), 16000, "测试", 0.9)
        wavs = list(Path(tmp).rglob("*.wav"))
        assert len(wavs) == 1
        with wave.open(str(wavs[0])) as w:
            assert w.getnframes() == 1600 and w.getframerate() == 16000
        line = json.loads((Path(tmp) / "asr_log.jsonl").read_text(encoding="utf-8"))
        assert line["text"] == "测试" and line["conf"] == 0.9
    print("self-check ok")
