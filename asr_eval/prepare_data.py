# -*- coding: utf-8 -*-
"""从 ModelScope 的 AISHELL-1 测试集(2.45G zip)按 HTTP Range 只抽 N 个 wav。

zip 存在阿里云 OSS,支持 Range;配合 zipfile 的按需 seek/read,只下载被抽取
成员的字节(约 N×100KB),不落地整包。产出:
  asr_eval/data/wav/*.wav  + asr_eval/data/manifest.jsonl(每行 {wav, ref})

用法:python asr_eval/prepare_data.py --n 500 [--seed 0]
"""
from __future__ import annotations

import argparse
import io
import json
import random
import sys
import zipfile
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DS = "modelscope/speech_asr_aishell1_testsets"
OSS_TREE = f"https://modelscope.cn/api/v1/datasets/{DS}/oss/tree?Revision=master&Recursive=true"
CSV_URL = (f"https://modelscope.cn/api/v1/datasets/{DS}/repo"
           f"?Revision=master&FilePath=aishell1_test.csv")


class RangeFile(io.RawIOBase):
    """把远程支持 Range 的文件当本地可 seek 文件读;带读前缓冲合并小读,减少请求数。"""

    def __init__(self, session: requests.Session, url: str, size: int, chunk=1 << 20):
        self.s, self.url, self.size, self.chunk = session, url, size, chunk
        self.pos = 0
        self._buf = b""
        self._buf_start = 0

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else (
            self.pos + offset if whence == 1 else self.size + offset)
        return self.pos

    def tell(self):
        return self.pos

    def seekable(self):
        return True

    def readable(self):
        return True

    def _fetch(self, start, end):  # [start, end) 含头不含尾
        end = min(end, self.size)
        r = self.s.get(self.url, headers={"Range": f"bytes={start}-{end - 1}"}, timeout=60)
        r.raise_for_status()
        return r.content

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        if self.pos >= self.size or n <= 0:
            return b""
        # 命中缓冲直接切;否则拉一整块(至少 chunk)覆盖本次读,合并后续小读
        if not (self._buf_start <= self.pos and self.pos + n <= self._buf_start + len(self._buf)):
            grab = max(n, self.chunk)
            self._buf = self._fetch(self.pos, self.pos + grab)
            self._buf_start = self.pos
        off = self.pos - self._buf_start
        out = self._buf[off:off + n]
        self.pos += len(out)
        return out


def fresh_url_and_size(session):
    d = session.get(OSS_TREE, timeout=30).json()["Data"][0]
    return d["Url"], int(d["Size"])


def load_csv_pairs(session):
    """返回 [(zip 内 wav 路径, 参考文本)]。csv 首行是表头。"""
    txt = session.get(CSV_URL, timeout=30).content.decode("utf-8")
    pairs = []
    for line in txt.splitlines()[1:]:
        if "," not in line:
            continue
        path, ref = line.split(",", 1)
        if path.endswith(".wav"):
            pairs.append((path.strip(), ref.strip()))
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    (DATA / "wav").mkdir(parents=True, exist_ok=True)
    s = requests.Session()

    pairs = load_csv_pairs(s)
    print(f"test.csv 共 {len(pairs)} 句")
    random.seed(args.seed)
    picked = random.sample(pairs, min(args.n, len(pairs)))

    url, size = fresh_url_and_size(s)
    print(f"远程 zip {size/1e9:.2f}G,按 Range 抽 {len(picked)} 个 wav ...")

    rf = RangeFile(s, url, size)
    zf = zipfile.ZipFile(rf)  # 读 EOCD + 中央目录(约几 MB)
    names = set(zf.namelist())

    manifest = DATA / "manifest.jsonl"
    got = 0
    with manifest.open("w", encoding="utf-8") as mf:
        for i, (path, ref) in enumerate(picked, 1):
            if path not in names:
                print(f"  [跳过] zip 内无此成员: {path}")
                continue
            out = DATA / "wav" / Path(path).name
            with zf.open(path) as src:
                out.write_bytes(src.read())
            mf.write(json.dumps({"wav": out.name, "ref": ref}, ensure_ascii=False) + "\n")
            got += 1
            if i % 50 == 0 or i == len(picked):
                print(f"  {i}/{len(picked)} ...")
    print(f"完成:{got} 个 wav → {DATA/'wav'},清单 {manifest}")


if __name__ == "__main__":
    main()
