#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wsyzy.py — 从公开影视采集接口（MacCMS 标准协议）取 m3u8 并下载

为什么用它:
  解析站（jx.*）全是 iframe 套壳，流地址靠 JS 动态拉，还经常跨境超时。
  这个接口是标准 MacCMS 采集协议，detail 返回的 vod_play_url 里
  直接就是 m3u8 直链，不经过任何播放器，不需要会员，不受 iframe 限制。

用法:
  # 1) 搜片
  python wsyzy.py --search "再见爱人"

  # 2) 看某部片子有哪些集（--id 从第 1 步结果里抄）
  python wsyzy.py --id 128076

  # 3) 下某一集（集号从第 2 步列表里看，从 1 开始）
  python wsyzy.py --id 128076 --ep 5 --out-dir ./下载 --quality 1080

  # 4) 下全部
  python wsyzy.py --id 128076 --all --out-dir ./下载

  # 5) 一步到位：搜索+取第一条+下第一集
  python wsyzy.py --search "甜蜜的任务" --auto --ep 1

依赖: requests 或标准库 urllib（脚本自带回退）；AES 用 pycryptodome 或 cryptography。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse

import mgtv_vip_downloader as core

SITE = "https://wsyzy.cc"
API = "https://api.wsyzy.net/api.php/provide/vod/"
REFERER = SITE + "/"

# 多个采集站：不同站采的片源不同，画质和更新速度也不同。
# search/detail 的 URL 模板里 {kw}=关键词、{vid}=资源 id。
SITES = {
    "wsyzy": {
        "label": "无损云(默认)",
        "search": "https://wsyzy.cc/index.php/ajax/suggest?mid=1&wd={kw}",
        "detail": "https://api.wsyzy.net/api.php/provide/vod/?ac=detail&ids={vid}",
        "referer": "https://wsyzy.cc/",
    },
    "ffzy": {
        "label": "非凡",
        "search": "https://cj.ffzyapi.com/api.php/provide/vod/?ac=detail&wd={kw}",
        "detail": "https://cj.ffzyapi.com/api.php/provide/vod/?ac=detail&ids={vid}",
        "referer": "https://cj.ffzyapi.com/",
    },
    "lzi": {
        "label": "量子",
        "search": "https://cj.lziapi.com/api.php/provide/vod/?ac=detail&wd={kw}",
        "detail": "https://cj.lziapi.com/api.php/provide/vod/?ac=detail&ids={vid}",
        "referer": "https://cj.lziapi.com/",
    },
    "hongniu": {
        "label": "红牛",
        "search": "https://www.hongniuzy2.com/api.php/provide/vod/?ac=detail&wd={kw}",
        "detail": "https://www.hongniuzy2.com/api.php/provide/vod/?ac=detail&ids={vid}",
        "referer": "https://www.hongniuzy2.com/",
    },
    "modu": {
        "label": "魔都",
        "search": "https://www.mdzyapi.com/api.php/provide/vod/?ac=detail&wd={kw}",
        "detail": "https://www.mdzyapi.com/api.php/provide/vod/?ac=detail&ids={vid}",
        "referer": "https://www.mdzyapi.com/",
    },
}
DEFAULT_SITE = "wsyzy"

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def item_id(x):
    """wsyzy 的 suggest 返回 id，标准 MacCMS 返回 vod_id。"""
    return x.get("id") or x.get("vod_id")


def item_name(x):
    return x.get("name") or x.get("vod_name") or ""


def site_cfg(site):
    return SITES.get(site or DEFAULT_SITE, SITES[DEFAULT_SITE])


def _hdr_extra(referer=None):
    return {"Referer": referer or REFERER, "User-Agent": core.DEFAULT_UA}


def get_json(url, timeout=20, retries=3, referer=None):
    last = None
    for i in range(retries):
        try:
            raw = core.http_get(url, _hdr_extra(referer), timeout=timeout)
            return json.loads(raw.decode("utf-8", "ignore"))
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"接口请求失败: {url} -> {last}")


def search(kw, site=DEFAULT_SITE):
    cfg = site_cfg(site)
    url = cfg["search"].format(kw=urllib.parse.quote(kw))
    return (get_json(url, referer=cfg["referer"]).get("list") or [])


def detail(vid, site=DEFAULT_SITE):
    cfg = site_cfg(site)
    url = cfg["detail"].format(vid=vid)
    lst = (get_json(url, referer=cfg["referer"]).get("list") or [])
    return lst[0] if lst else None


def parse_eps(vod):
    """vod_play_url 形如  名称$url#名称$url ，多播放源用 $$$ 分隔，取第一个源。"""
    pu = vod.get("vod_play_url") or ""
    first = pu.split("$$$")[0]
    eps = []
    for item in first.split("#"):
        item = item.strip()
        if not item:
            continue
        if "$" in item:
            name, url = item.split("$", 1)
        else:
            name, url = "", item
        url = url.strip()
        if url.startswith("http"):
            eps.append({"name": name.strip() or f"第{len(eps) + 1}集", "url": url})
    return eps


def safe_name(s, fallback="output"):
    s = _ILLEGAL.sub("_", (s or "").strip()).strip(". ")
    return s or fallback


_SHARE_M3U8 = re.compile(r'''const\s+url\s*=\s*["']([^"']+?\.m3u8[^"']*)["']''')


def resolve_share(client, url, referer=None):
    """有些站给的是 /share/xxx 播放页，里面藏着 const url = "/xxx.m3u8?sign=..."。"""
    if ".m3u8" in url:
        return url
    try:
        t = client.get(url, headers={"Referer": referer or REFERER}).decode("utf-8", "ignore")
    except Exception:
        return url
    m = _SHARE_M3U8.search(t)
    if m:
        return urllib.parse.urljoin(url, m.group(1))
    return url


def resolve_download_url(client, m3u8_url, quality="best"):
    m3u8_url = resolve_share(client, m3u8_url)
    text = client.get(m3u8_url, headers={"Referer": REFERER}).decode("utf-8", "ignore")
    pl = core.parse_m3u8(text, m3u8_url)
    if pl.variants:
        sub = core.pick_variant(pl, quality)
        text = client.get(sub, headers={"Referer": REFERER}).decode("utf-8", "ignore")
        pl = core.parse_m3u8(text, sub)
    return pl


def download_ep(client, ep, out_path, quality="best", workers=8):
    pl = resolve_download_url(client, ep["url"], quality)
    if not pl.segments:
        raise RuntimeError(f"没有解析到分片: {ep['url']}")
    core.Downloader(client, out_path, workers=workers).run(pl)
    return out_path


def cmd_search(args):
    print(f"搜索: {args.search}")
    lst = search(args.search, args.site)
    if not lst:
        print("没搜到。换个关键词（片名少带后缀，比如去掉「第几季」）。")
        return 1
    print(f"找到 {len(lst)} 条：")
    print(f"{'ID':<10} 片名")
    print("-" * 60)
    for x in lst:
        print(f"{item_id(x):<10} {item_name(x)}")
    print("-" * 60)
    print(f'下集请看: python wsyzy.py --id {item_id(lst[0])} --site {args.site}')
    return 0


def cmd_list(args):
    vod = detail(args.id, args.site)
    if not vod:
        print(f"id={args.id} 没有数据")
        return 1
    eps = parse_eps(vod)
    print(f"片名: {vod.get('vod_name')}")
    print(f"年份: {vod.get('vod_year')}   类型: {vod.get('type_name')}   更新: {vod.get('vod_time')}")
    print(f"备注: {vod.get('vod_remarks')}")
    print(f"共 {len(eps)} 个源：")
    print("-" * 70)
    for i, e in enumerate(eps, 1):
        print(f"[{i:>4}] {e['name']:<18} {e['url'][:70]}")
    print("-" * 70)
    print(f'下载: python wsyzy.py --id {args.id} --ep 1 --out-dir ./下载')
    return 0


def download_targets(vod, targets, out_dir, quality="best", workers=8, site=DEFAULT_SITE):
    """targets: [(集号, ep_dict), ...]"""
    title = safe_name(vod.get("vod_name"), "output")
    os.makedirs(out_dir, exist_ok=True)
    client = core.MgtvClient(referer=site_cfg(site)["referer"])
    print(f"\n片名: {vod.get('vod_name')}")
    print(f"待下载 {len(targets)} 集   输出目录: {os.path.abspath(out_dir)}")
    print("=" * 70)

    ok, fail = 0, 0
    for n, (idx, ep) in enumerate(targets, 1):
        name = safe_name(f"{title}_{ep['name']}", f"{title}_{idx}")
        out = os.path.join(out_dir, name + ".mp4")
        if os.path.exists(out) and os.path.getsize(out) > 1024 * 1024:
            print(f"[{idx}] 已存在，跳过")
            ok += 1
            continue
        print(f"\n[{idx}] {ep['name']}  ({n}/{len(targets)})")
        try:
            download_ep(client, ep, out, quality=quality, workers=workers)
            size = os.path.getsize(out) / 1024 / 1024
            print(f"[{idx}] 完成 {size:.1f} MB")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[{idx}] 失败: {type(e).__name__}: {e}")
            fail += 1
    print("=" * 70)
    print(f"成功 {ok}，失败 {fail}")
    if ok:
        print(f"文件在: {os.path.abspath(out_dir)}")
    return ok, fail


def _ask(prompt, default=""):
    try:
        s = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已退出。")
        raise SystemExit(0)
    return s or default


def _is_tty():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def wizard():
    """无参数时的交互向导：搜片 → 选片 → 选集 → 下载。"""
    print("=" * 62)
    print(" 影视资源搜索下载")
    print("=" * 62)
    print(" 提示：片名少带后缀，'再见爱人' 比 '再见爱人的第五季' 更容易搜到")
    print("-" * 62)

    while True:
        kw = _ask(" 输入片名（直接回车退出）: ")
        if not kw:
            print("已退出。")
            return 0

        print(f"\n正在搜索「{kw}」...")
        try:
            lst = search(kw)
        except Exception as e:  # noqa: BLE001
            print(f" 搜索失败（网络？）: {type(e).__name__}: {e}\n")
            continue
        if not lst:
            print(" 没搜到，换个关键词再试\n")
            continue

        print(f" 找到 {len(lst)} 条：")
        for i, x in enumerate(lst, 1):
            print(f"   [{i:>2}] {item_name(x)}")
        print()

        pick = _ask(f" 选第几个 / 1-{len(lst)}（回车重新搜）: ")
        if not pick.isdigit() or not (1 <= int(pick) <= len(lst)):
            continue

        vid = item_id(lst[int(pick) - 1])
        print("\n读取剧集列表...")
        try:
            vod = detail(vid)
        except Exception as e:  # noqa: BLE001
            print(f" 读取失败: {e}\n")
            continue
        if not vod:
            print(" 没有数据\n")
            continue

        eps = parse_eps(vod)
        if not eps:
            print(" 这部没有可下载的集\n")
            continue

        print(f"\n《{vod.get('vod_name')}》  {len(eps)} 集   更新: {vod.get('vod_time')}")
        print("-" * 62)
        show = min(len(eps), 60)
        for i in range(show):
            line = f"[{i + 1:>3}] {eps[i]['name']:<16}"
            if (i + 1) % 3 == 0:
                print("   " + line)
            else:
                print("   " + line, end="")
        if show % 3:
            print()
        if len(eps) > show:
            print(f"   ... 共 {len(eps)} 集，全部下载请输 a")
        print("-" * 62)

        sel = _ask(f" 下第几集 / 1-{len(eps)}（a=全部，回车返回）: ")
        if not sel:
            continue

        if sel.lower() == "a":
            targets = list(enumerate(eps, 1))
        elif sel.isdigit() and 1 <= int(sel) <= len(eps):
            targets = [(int(sel), eps[int(sel) - 1])]
        else:
            print(" 输入无效\n")
            continue

        q = _ask(" 画质 best/1080/720（回车=best）: ", "best")
        out_dir = _ask(" 保存目录（回车=当前目录下的 下载）: ", "下载")
        w = _ask(" 并发数（回车=8；嫌慢可试 12-16，卡住就降到 4）: ", "8")
        try:
            workers = max(1, int(w))
        except ValueError:
            workers = 8
        try:
            download_targets(vod, targets, out_dir, quality=q, workers=workers)
        except Exception as e:  # noqa: BLE001
            print(f" 下载出错: {type(e).__name__}: {e}")
        print()
        again = _ask(" 继续下别的？(y=继续，回车=退出): ")
        if again.lower() != "y":
            return 0


def cmd_download(args):
    vod = detail(args.id, args.site)
    if not vod:
        print(f"id={args.id} 没有数据")
        return 1
    eps = parse_eps(vod)
    if not eps:
        print("没有解析到可下载的集")
        return 1

    if args.all:
        targets = list(enumerate(eps, 1))
    else:
        if not args.ep:
            print("要指定 --ep N 或 --all")
            return 1
        if not (1 <= args.ep <= len(eps)):
            print(f"--ep 超出范围，共 {len(eps)} 集")
            return 1
        targets = [(args.ep, eps[args.ep - 1])]

    ok, fail = download_targets(
        vod, targets, args.out_dir or ".", quality=args.quality,
        workers=args.workers, site=args.site,
    )
    return 0 if fail == 0 else 2


def main(argv=None):
    p = argparse.ArgumentParser(
        description="公开影视采集接口 → m3u8 → 下载（不依赖解析站）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--search", metavar="关键词", help="按片名搜索，返回 id 列表")
    p.add_argument("--id", metavar="ID", help="资源 id（--search 的结果里抄）")
    p.add_argument("--ep", type=int, help="下载第 N 集（从 1 开始）")
    p.add_argument("--all", action="store_true", help="下载全部集")
    p.add_argument("--auto", action="store_true", help="配合 --search：自动取第一条结果")
    p.add_argument("--out-dir", default="", help="输出目录")
    p.add_argument("--quality", default="best", help="best / worst / 1080 / 720")
    p.add_argument("--workers", type=int, default=8, help="分片并发数（4-16 最佳，过高会被 CDN 限速）")
    p.add_argument("--site", default=DEFAULT_SITE, choices=list(SITES),
                   help="采集站：wsyzy/ffzy/lzi/hongniu/modu（不同站片源画质不同）")
    args = p.parse_args(argv)

    if args.search and not args.id:
        lst = search(args.search, args.site)
        if args.auto and lst:
            args.id = str(item_id(lst[0]))
            print(f"自动选择: {item_name(lst[0])} (id={args.id})")
        elif args.ep or args.all:
            # 搜索后直接下载
            if not lst:
                print("没搜到")
                return 1
            args.id = str(item_id(lst[0]))
            print(f"自动选择: {item_name(lst[0])} (id={args.id})")
        else:
            return cmd_search(args)

    if not args.id:
        if not args.search and _is_tty():
            return wizard()
        p.print_help()
        return 0

    if args.ep or args.all:
        return cmd_download(args)
    return cmd_list(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。分片保留在 *.parts 目录，重跑可续传。")
        sys.exit(130)
