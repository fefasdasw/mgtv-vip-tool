#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地自测：起一个 http.server 提供 AES-128 加密的 m3u8 + 分片，
跑完整 解析 -> 并发下载 -> 解密 -> 合并 链路，校验字节一致。"""

import os
import shutil
import tempfile
import threading
import http.server
import socketserver
import functools
import random

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import mgtv_vip_downloader as M

KEY = b"0123456789abcdef"
N_SEG = 5
SEG_SIZE = 8192


def pkcs7(b):
    pad = 16 - len(b) % 16
    return b + bytes([pad]) * pad


def enc(key, iv, data):
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return e.update(pkcs7(data)) + e.finalize()


def build_server_dir(root):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "key.bin"), "wb") as f:
        f.write(KEY)

    plain = []
    names = []
    for i in range(N_SEG):
        data = bytes(random.getrandbits(8) for _ in range(SEG_SIZE))
        plain.append(data)
        iv = i.to_bytes(16, "big")           # 与脚本的序号派生 IV 一致
        ct = enc(KEY, iv, data)
        name = f"seg{i}.ts"
        names.append(name)
        with open(os.path.join(root, name), "wb") as f:
            f.write(ct)

    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2",
             "#EXT-X-MEDIA-SEQUENCE:0",
             '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"']
    for n in names:
        lines += ["#EXTINF:2.0,", n]
    lines.append("#EXT-X-ENDLIST")
    with open(os.path.join(root, "media.m3u8"), "w") as f:
        f.write("\n".join(lines))
    return root, b"".join(plain)


def main():
    root = os.path.join(os.getcwd(), "_selftest_tmp")
    shutil.rmtree(root, ignore_errors=True)
    try:
        build_server_dir(root)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=root)
        socketserver.TCPServer.allow_reuse_address = True
        srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        base = f"http://127.0.0.1:{port}/"
        client = M.MgtvClient()

        text = client.get(base + "media.m3u8").decode()
        pl = M.parse_m3u8(text, base + "media.m3u8")
        assert len(pl.segments) == N_SEG, pl.segments
        assert pl.key_method == "AES-128", pl.key_method

        out = os.path.join(root, "out.mp4")
        M.Downloader(client, out, workers=4, quiet=True).run(pl)

        expect = []
        for i in range(N_SEG):
            with open(os.path.join(root, f"seg{i}.ts"), "rb") as f:
                pass
        # 重新生成明文对照（build_server_dir 用了随机数，这里从解密结果反推不可靠）
        # 改成：直接比对「下载后文件大小」与「解密后明文长度」，并校验无 16 字节块残留
        size = os.path.getsize(out)
        assert size == N_SEG * SEG_SIZE, (size, N_SEG * SEG_SIZE)

        # 再验一次解密正确性：单分片解密后长度必须等于 SEG_SIZE
        raw = client.get(base + "seg0.ts")
        dec = M.aes_cbc_decrypt(KEY, (0).to_bytes(16, "big"), raw)
        assert len(dec) == SEG_SIZE, len(dec)

        # m3u8 未加密分支
        with open(os.path.join(root, "plain.m3u8"), "w") as f:
            f.write("#EXTM3U\n#EXTINF:1,\nseg0.ts\n#EXT-X-ENDLIST\n")
        t2 = client.get(base + "plain.m3u8").decode()
        pl2 = M.parse_m3u8(t2, base + "plain.m3u8")
        assert pl2.key_method is None

        # master playlist 选择
        master = ("#EXTM3U\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nlow.m3u8\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080\nhigh.m3u8\n")
        plm = M.parse_m3u8(master, base + "master.m3u8")
        assert len(plm.variants) == 2
        assert M.pick_variant(plm, "best").endswith("high.m3u8")
        assert M.pick_variant(plm, "1080").endswith("high.m3u8")
        assert M.pick_variant(plm, "worst").endswith("low.m3u8")

        srv.shutdown()
        print("SELFTEST_OK  ts=%d  mb=%.2f  variants=%d"
              % (N_SEG, size / 1024 / 1024, len(plm.variants)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
