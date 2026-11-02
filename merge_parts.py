#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_parts.py — 把已经下载好的 *.parts 分片目录直接合并成 MP4

用在你「分片全下完了，但合并没有跑完就中断了」的情况下，
不用重新搜索下载，直接合成。

用法:
  # 只给分片目录，输出名自动去掉 .parts 后缀
  python merge_parts.py "下载\\花儿与少年·丝路季_20231024.mp4.parts"

  # 自己指定输出
  python merge_parts.py "下载\\xxx.mp4.parts" "下载\\第1集.mp4"

  # 批量：把 下载 目录下所有 .parts 都合并掉
  python merge_parts.py --all "下载"
"""

from __future__ import annotations

import argparse
import os
import re
import sys

CHUNK = 1024 * 1024
PART_RE = re.compile(r"^(\d{6})\.part$")


def list_parts(d):
    items = []
    for fn in os.listdir(d):
        m = PART_RE.match(fn)
        if m:
            items.append((int(m.group(1)), os.path.join(d, fn)))
    items.sort()
    return items


def merge_dir(parts_dir, out_path=None, quiet=False):
    parts_dir = parts_dir.rstrip("\\/")
    if not os.path.isdir(parts_dir):
        raise SystemExit(f"目录不存在: {parts_dir}")

    parts = list_parts(parts_dir)
    if not parts:
        raise SystemExit(f"目录里没有 000000.part 这样的分片: {parts_dir}")

    # 检查连续性
    idxs = [i for i, _ in parts]
    if idxs[0] != 0 or idxs[-1] != len(idxs) - 1:
        missing = sorted(set(range(idxs[-1] + 1)) - set(idxs))
        hint = f"，缺失 {len(missing)} 片（如 {missing[:5]}）" if missing else ""
        raise SystemExit(f"分片不连续：{idxs[0]}..{idxs[-1]}，共 {len(idxs)} 片{hint}")

    if out_path is None:
        base = os.path.basename(parts_dir)
        out_path = os.path.join(os.path.dirname(parts_dir), base[:-6] if base.endswith(".parts") else base + ".mp4")
        if not out_path.lower().endswith(".mp4"):
            out_path += ".mp4"

    total = len(parts)
    init = os.path.join(parts_dir, "init.mp4")
    tmp = out_path + ".tmp"
    written = 0
    step = max(1, total // 20)

    if not quiet:
        print(f"合并 {total} 个分片 -> {out_path}")
    with open(tmp, "wb") as out:
        if os.path.exists(init):
            with open(init, "rb") as f:
                b = f.read()
                out.write(b)
                written += len(b)
        for n, (_i, p) in enumerate(parts, 1):
            with open(p, "rb") as f:
                while True:
                    b = f.read(CHUNK)
                    if not b:
                        break
                    out.write(b)
                    written += len(b)
            if not quiet and (n % step == 0 or n == total):
                pct = n * 100.0 / total
                print(f"\r  合并 {n}/{total}  ({pct:5.1f}%)  {written / 1024 / 1024:7.1f} MB",
                      end="", flush=True)
    if not quiet:
        print()
    os.replace(tmp, out_path)
    size = os.path.getsize(out_path) / 1024 / 1024
    if not quiet:
        print(f"完成: {os.path.abspath(out_path)}  ({size:.1f} MB)")
    return out_path


def main(argv=None):
    p = argparse.ArgumentParser(description="把 *.parts 分片目录合并成 MP4（不重新下载）")
    p.add_argument("parts", help="分片目录路径（名字以 .parts 结尾）")
    p.add_argument("out", nargs="?", default=None, help="输出 mp4 路径（可省略）")
    p.add_argument("--all", action="store_true", help="把该目录下所有 *.parts 子目录都合并")
    p.add_argument("--keep-parts", action="store_true", help="合并后保留分片目录")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if args.all:
        root = args.parts
        if not os.path.isdir(root):
            raise SystemExit(f"目录不存在: {root}")
        targets = [
            os.path.join(root, d)
            for d in os.listdir(root)
            if d.endswith(".parts") and os.path.isdir(os.path.join(root, d))
        ]
        if not targets:
            raise SystemExit(f"{root} 下没有 *.parts 目录")
        ok = 0
        for d in targets:
            try:
                out = merge_dir(d, quiet=args.quiet)
                ok += 1
                if not args.keep_parts:
                    for fn in os.listdir(d):
                        os.remove(os.path.join(d, fn))
                    os.rmdir(d)
                    print(f"  已清理分片目录: {os.path.basename(d)}")
            except SystemExit as e:
                print(f"  跳过 {d}: {e}")
        print(f"共合并 {ok}/{len(targets)} 个")
        return 0

    out = merge_dir(args.parts, args.out, quiet=args.quiet)
    if args.keep_parts:
        print("分片目录已保留")
    else:
        d = args.parts.rstrip("\\/")
        for fn in os.listdir(d):
            os.remove(os.path.join(d, fn))
        os.rmdir(d)
        print(f"已清理分片目录: {os.path.basename(d)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断，分片仍在，重新运行即可。")
        sys.exit(130)
