#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vip_resolver.py — 用公开解析接口把芒果TV VIP 页面转成可下载的 m3u8

原理:
  非会员账号拿不到完整 m3u8（服务端鉴权）。第三方的「VIP视频解析」服务
  用自己的会员账号把流代理出来，你请求它、它回一个带 m3u8 的播放页。
  本脚本并发探测一批解析接口，从返回的 HTML/JS 里抠出 m3u8 并验证
  （只认真 #EXTM3U 开头的，过滤掉假地址），找到后可直接调下载器。

用法:
  # 1) 只探测，打印可用的 m3u8
  python vip_resolver.py --url "https://www.mgtv.com/b/887305/24691407.html"

  # 2) 探测到直接下载
  python vip_resolver.py --url "https://www.mgtv.com/b/887305/24691407.html" --download
  python vip_resolver.py --url "..." --download --out 第01集.mp4 --quality 1080

  # 3) 指定/追加解析接口（接口挂了就换，格式：名字=URL模板，用 {u} 占位）
  python vip_resolver.py --url "..." --parser "mysite=https://xxx.com/jx/?url={u}"

  # 4) 看看内置了哪些接口
  python vip_resolver.py --list

注意:
  * 解析接口的存活率一直在变，脚本会标出每个接口的结果，挂了就自己补新的。
  * 部分接口返回的是一个中间播放页，脚本会自动跟一层 iframe。
  * m3u8 通常几分钟内有效，探测到就立刻下载。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import re
import sys
import urllib.parse

import mgtv_vip_downloader as core

# --------------------------------------------------------------- parser list

# (名字, 接口模板, 是否对视频地址做 URL 编码)
# 前 8 条取自 scriptcat「AI视频解析」v1.2 的默认线路（在维护中）；
# 后面几条是历史备用，失效了用 --parser 自己补。
PARSERS = [
    ("im1907",      "https://im1907.top?jx={u}",            False),
    ("xymp4",       "https://jx.xymp4.cc/?url={u}",         True),
    ("2s0",         "https://jx.2s0.cn/player/?url={u}",    True),
    ("xmflv",       "https://jx.xmflv.com/?url={u}",        True),
    ("789jiexi",    "https://jx.789jiexi.com/?url={u}",     True),
    ("hls-one",     "https://jx.hls.one/?url={u}",          True),
    ("txnp",        "https://bfq.txnp.cn/player?url={u}",   True),
    ("fongmi",      "https://json.fongmi.cc/web?url={u}",   True),
    # --- 备用 ---
    ("playerjy",    "https://jx.playerjy.com/?url={u}",     True),
    ("aidouer",     "https://jx.aidouer.net/?url={u}",      True),
    ("yemu",        "https://www.yemu.xyz/?url={u}",        True),
    ("mini-fun",    "https://jx.mini-fun.cn/?url={u}",      True),
    ("17kyun",      "https://jx.17kyun.com/?url={u}",       True),
]

M3U8_RE = re.compile(r"""https?://[^\s"'<>\\()]+?\.m3u8[^\s"'<>\\()]*""", re.I)
SRC_RE = re.compile(r"""(?:src|source|url|playurl|video)\s*[:=]\s*["'](https?://[^"']+)["']""", re.I)
IFRAME_RE = re.compile(r"""<iframe[^>]+src=["']([^"']+)["']""", re.I)


def fetch_text(url, timeout=10, referer="https://www.mgtv.com/"):
    raw = core.http_get(
        url,
        {
            "User-Agent": core.DEFAULT_UA,
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,*/*",
        },
        timeout=timeout,
    )
    return raw.decode("utf-8", "ignore")


def ping_parser(name, tpl, timeout=5):
    """测本地到接口域名的 TCP 443 连通性 + DNS 解析，用来区分“连不上”和“解析失败”。"""
    import socket
    import time as _t
    from urllib.parse import urlparse

    probe = tpl.replace("{u}", "")
    try:
        host = urlparse(probe).hostname
    except Exception:
        host = None
    if not host:
        return name, "-", "无法解析模板", 0.0

    try:
        ip = socket.gethostbyname(host)
    except Exception as e:
        return name, host, f"DNS失败({type(e).__name__})", 0.0

    t0 = _t.time()
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            ms = (_t.time() - t0) * 1000
            return name, host, f"通 {ip}", ms
    except Exception as e:
        ms = (_t.time() - t0) * 1000
        return name, host, f"{type(e).__name__}", ms


def verify_m3u8(url, timeout=8):
    """真 m3u8 必须能取到 #EXTM3U 开头的文本。"""
    try:
        raw = core.http_get(
            url,
            {"User-Agent": core.DEFAULT_UA, "Referer": "https://www.mgtv.com/"},
            timeout=timeout,
        )
    except Exception:
        return False
    head = raw[:256].lstrip(b"\xef\xbb\xbf \r\n\t")
    return head.startswith(b"#EXTM3U")


def dig_candidates(text):
    """从播放页里挖候选地址，优先级：m3u8 直链 > 播放器 src。"""
    cands = []
    for m in M3U8_RE.findall(text):
        if m not in cands:
            cands.append(m)
    if not cands:
        for m in SRC_RE.findall(text):
            if ".m3u8" in m and m not in cands:
                cands.append(m)
    return cands


def try_parser(name, tpl, page_url, encode=True, verbose=True):
    u = urllib.parse.quote(page_url, safe="") if encode else page_url
    target = tpl.format(u=u)
    try:
        text = fetch_text(target)
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"  [{name:<12}] 连不上        {type(e).__name__}")
        return name, "unreachable", None

    cands = dig_candidates(text)

    # 跟一层 iframe：有些接口返回中间播放页
    if not cands:
        for ifr in IFRAME_RE.findall(text)[:2]:
            ifr_url = urllib.parse.urljoin(target, ifr)
            try:
                cands += dig_candidates(fetch_text(ifr_url, referer=target))
            except Exception:
                pass

    if not cands:
        if verbose:
            print(f"  [{name:<12}] 无 m3u8       ({len(text)} 字节)")
        return name, "no-m3u8", None

    for c in cands:
        if verify_m3u8(c):
            if verbose:
                print(f"  [{name:<12}] ✅ 可用        {c[:110]}")
            return name, "ok", c

    if verbose:
        print(f"  [{name:<12}] 有地址但无效  {cands[0][:90]}")
    return name, "invalid", None


def resolve(page_url, parsers, workers=8):
    print(f"目标页面: {page_url}")
    print(f"并发探测 {len(parsers)} 个解析接口 ...")
    print("-" * 70)
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(try_parser, n, t, page_url, enc) for n, t, enc in parsers]
        for f in concurrent.futures.as_completed(futs):
            name, status, url = f.result()
            if status == "ok":
                found.append((name, url))
    print("-" * 70)
    return found


def main(argv=None):
    p = argparse.ArgumentParser(
        description="芒果TV VIP 页面 → 可用 m3u8（第三方解析接口探测）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--url", help="芒果TV 视频页地址，例如 https://www.mgtv.com/b/887305/24691407.html")
    p.add_argument("--download", action="store_true", help="找到后直接下载")
    p.add_argument("--out", default="output.mp4", help="下载输出文件名")
    p.add_argument("--quality", default="best", help="best / worst / 1080 / 720")
    p.add_argument("--workers", type=int, default=8, help="并发探测数")
    p.add_argument("--parser", action="append", default=[],
                   metavar="名=URL模板", help="追加解析接口，可多次")
    p.add_argument("--list", action="store_true", help="列出内置接口")
    p.add_argument("--links", action="store_true",
                   help="只生成各接口的解析链接（复制到浏览器里逐个试，最实用）")
    p.add_argument("--ping", action="store_true",
                   help="只测本地到各接口域名的连通性（定位“全超时”是网络还是解析失败）")
    args = p.parse_args(argv)

    if args.list:
        print("内置解析接口（第三列=是否对视频地址做 URL 编码）：")
        for n, t, enc in PARSERS:
            print(f"  {n:<12} {'编码' if enc else '原始':<4} {t}")
        return 0

    if not args.url and not args.ping:
        p.print_help()
        return 0

    parsers = list(PARSERS)
    for item in args.parser:
        if "=" in item:
            n, t = item.split("=", 1)
            parsers.append((n.strip(), t.strip(), True))

    if args.links:
        print(f"视频页: {args.url}")
        print("把下面的链接逐个粘到浏览器打开，能出画面的就是活的：")
        print("-" * 78)
        for n, t, enc in parsers:
            u = urllib.parse.quote(args.url, safe="") if enc else args.url
            print(f"{n:<12} {t.format(u=u)}")
        print("-" * 78)
        print("能播之后，按 F12 → Network 过滤 m3u8 复制地址，再：")
        print('  python mgtv_vip_downloader.py --m3u8 "地址" --out 剧集.mp4')
        return 0

    if args.ping:
        print("测本地到各解析接口的连通性（TCP 443）：")
        print("-" * 78)
        print(f"{'接口':<12} {'域名':<28} {'状态':<20} {'耗时'}")
        print("-" * 78)
        ok = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futs = [ex.submit(ping_parser, n, t) for n, t, _e in parsers]
            for f in concurrent.futures.as_completed(futs):
                name, host, status, ms = f.result()
                if status.startswith("通"):
                    ok += 1
                print(f"{name:<12} {host or '-':<28} {status:<20} {ms:.0f} ms")
        print("-" * 78)
        print(f"可连通 {ok}/{len(parsers)} 个")
        if ok == 0:
            print("\n全部连不上 = 你的网络到这些域名不通，不是线路不支持芒果。")
            print("  ① 换 DNS 再试：223.5.5.5 / 119.29.29.29")
            print("  ② 该站可能需要代理，挂上代理后重跑本命令确认")
            print("  ③ 全网都挂了就去搜新的解析站，用 --parser 加进来")
        elif ok < len(parsers):
            print("\n有一部分通 = 网络没问题，挑“通”的那几条浏览器打开试。")
            print("  页面能开但一直转圈，就是该线路不支持芒果或该集，换线路。")
        else:
            print("\n全部可连通 = 网络完全正常，问题出在解析侧（不支持芒果/该集）。")
        return 0

    found = resolve(args.url, parsers, workers=max(1, args.workers))

    if not found:
        print("\n所有接口都没拿到可用 m3u8。可能原因：")
        print("  1) 接口集体失效 —— 搜「VIP视频解析接口」找新的，用 --parser 加进来")
        print("  2) 芒果独播内容解析站覆盖不到 —— 换资源站/磁力直接下")
        print("  3) 页面地址要的是视频页（/b/ 或 /h/），不是首页或列表页")
        print("\n自测一个接口是否活着：")
        print(f'  python vip_resolver.py --url "{args.url}" --parser "test=https://新接口/?url={{u}}"')
        return 1

    best = found[0][1]
    print(f"\n可用接口 {len(found)} 个，选用：{found[0][0]}")
    print(f"m3u8: {best[:160]}{'...' if len(best) > 160 else ''}")
    print("\n手动下载：")
    print(f'  python mgtv_vip_downloader.py --m3u8 "{best}" --out {args.out}')

    if args.download:
        print("\n开始下载 ...")
        client = core.MgtvClient()
        text = client.get(best, headers={"Referer": "https://www.mgtv.com/"}).decode("utf-8", "ignore")
        pl = core.parse_m3u8(text, best)
        if pl.variants:
            sub = core.pick_variant(pl, args.quality)
            print("master 列表，选中:", sub[:120])
            text = client.get(sub, headers={"Referer": "https://www.mgtv.com/"}).decode("utf-8", "ignore")
            pl = core.parse_m3u8(text, sub)
        out = args.out
        if not out.lower().endswith((".mp4", ".ts")):
            out += ".mp4"
        core.Downloader(client, out, workers=8).run(pl)
        print("完成 ->", out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
