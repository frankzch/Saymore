# -*- coding: utf-8 -*-
"""在 AISHELL-1 测试子集上量我们现役 ASR 的 CER 和单句推理耗时。

裸 ASR(不挂整理 LoRA、无热词偏置)= 纯转写能力数。CER 按字级:两边都去标点
去空格(AISHELL 参考无标点,我们输出带标点),编辑距离/参考字数。

前提:先关闭语音程序(生产 llama-server 占 8901 + 4G 显存)。本脚本用独立端口
8902 起一个干净、无 LoRA 的 server,避免复用到挂了 LoRA 的生产 server 污染转写。

用法:python asr_eval/run_cer.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = HERE / "data"
sys.path.insert(0, str(ROOT))
from asr_llamacpp import LlamaASR  # noqa: E402

# 生产默认路径(config.json 未覆盖,见 voice_input.DEFAULT_CONFIG)
EXE = ROOT / "llama-cpp" / "llama-server.exe"
MODEL = ROOT / "models" / "Qwen3-ASR-1.7B-GGUF" / "Qwen3-ASR-1.7B-IQ4_NL.gguf"
MMPROJ = ROOT / "models" / "Qwen3-ASR-1.7B-GGUF" / "mmproj-Qwen3-ASR-1.7B-Q8_0.gguf"
PROD_PORT = 8901   # 生产端口,开跑前必须是关的
EVAL_PORT = 8902   # 评测专用,干净无 LoRA
SR = 16000

_KEEP = re.compile(r"[^一-鿿a-zA-Z0-9]")


def norm(s: str) -> str:
    """去标点/空格,只留 CJK+字母+数字,字级 CER 的规范化。"""
    return _KEEP.sub("", s)


def edit_distance(a: str, b: str) -> int:
    """字级 Levenshtein(每个字符一个 token)。"""
    if a == b:
        return 0
    m, n = len(a), len(b)
    if not m:
        return n
    if not n:
        return m
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        ai = a[i - 1]
        for j in range(1, n + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 句(0=全部);试点用")
    args = ap.parse_args()

    items = [json.loads(l) for l in (DATA / "manifest.jsonl").read_text("utf-8").splitlines() if l.strip()]
    if args.limit:
        items = items[:args.limit]
    print(f"待评测 {len(items)} 句")

    # 干净 server:无 loras;先确认生产 8901 已关(防复用污染 + 4G 显存冲突)
    probe = LlamaASR(exe=EXE, model=MODEL, mmproj=MMPROJ, port=PROD_PORT)
    if probe.alive():
        print(f"⛔ 生产 llama-server 仍在 {PROD_PORT} 端口运行。请先退出语音程序"
              f"(Ctrl+Shift+Q)再跑,否则 4G 显存塞不下两份、且会污染转写。")
        sys.exit(1)

    asr = LlamaASR(exe=EXE, model=MODEL, mmproj=MMPROJ, port=EVAL_PORT)  # loras=None → 裸转写
    print(f"启动干净 llama-server(端口 {EVAL_PORT}, 无 LoRA)...")
    t0 = time.time()
    asr.start()
    print(f"就绪 {time.time()-t0:.1f}s。开跑。")

    tot_edits = tot_ref = 0
    secs = []
    out = DATA / "cer_result.jsonl"
    t_run = time.time()
    with out.open("w", encoding="utf-8") as fo:
        for i, it in enumerate(items, 1):
            wav = DATA / "wav" / it["wav"]
            audio, sr = sf.read(str(wav), dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            ts = time.perf_counter()
            hyp, conf = asr.transcribe(audio, sr, context="", language="Chinese")
            dt = time.perf_counter() - ts
            secs.append(dt)
            r, h = norm(it["ref"]), norm(hyp)
            e = edit_distance(h, r)
            tot_edits += e
            tot_ref += len(r)
            fo.write(json.dumps({"wav": it["wav"], "ref": it["ref"], "hyp": hyp,
                                 "ref_n": len(r), "edits": e, "secs": round(dt, 2)},
                                ensure_ascii=False) + "\n")
            if i % 25 == 0 or i == len(items):
                run_cer = tot_edits / max(tot_ref, 1) * 100
                print(f"  {i}/{len(items)}  累计CER {run_cer:.2f}%  本句 {dt:.2f}s")
    asr.stop()

    wall = time.time() - t_run
    a = np.array(secs)
    cer = tot_edits / max(tot_ref, 1) * 100
    print("\n" + "=" * 40)
    print(f"句数           {len(items)}")
    print(f"参考总字数     {tot_ref}")
    print(f"编辑操作数     {tot_edits}")
    print(f"CER            {cer:.2f}%")
    print("-" * 40)
    print(f"单句推理 均值  {a.mean():.2f}s")
    print(f"          中位  {np.median(a):.2f}s")
    print(f"          p90   {np.percentile(a,90):.2f}s")
    print(f"          最大  {a.max():.2f}s")
    print(f"总墙钟         {wall/60:.1f}min  ({wall:.0f}s)")
    print(f"明细           {out}")


if __name__ == "__main__":
    main()
