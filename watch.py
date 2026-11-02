#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch.py — 不下载，直接生成网页播放器在线观看

原理:
  采集接口返回的 m3u8 及其分片都带 CORS: *，所以本地 HTML 用 hls.js
  就能直接播放，不需要第三方播放站、不需要下载、无广告。

用法:
  # 交互（推荐，双击 一键观看.bat 效果一样）
  python watch.py

  # 直接生成某部剧的选集播放页并打开
  python watch.py --id 128076

  # 直接播某一集
  python watch.py --id 128076 --ep 12

  # 只想生成文件，不自动打开浏览器
  python watch.py --id 128076 --no-open

生成的网页放在 播放页/<片名>.html，左侧播放器、右侧选集，
播完一集可以点下一集。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import re
import sys
import webbrowser

import mgtv_vip_downloader as core
import wsyzy

OUT_DIR = "播放页"


# ---------------------------------------------------------------- 源探测

_NOISE = re.compile(r"[\s·:：\-_()（）\[\]【】.。,，、的之]")


def _norm(s):
    """归一化片名：去掉空格、标点、「的」这类虚词，让「再见爱人的第五季」能匹配「再见爱人 第五季」。"""
    return _NOISE.sub("", s or "").lower()


def _match(items, kw):
    """在搜索结果里挑最匹配的一部：完全相同 > 前缀 > 包含 > 字符覆盖度。"""
    k = _norm(kw)
    if not k:
        return items[0]
    exact = [x for x in items if _norm(wsyzy.item_name(x)) == k]
    if exact:
        return exact[0]
    pre = [x for x in items if _norm(wsyzy.item_name(x)).startswith(k)]
    if pre:
        return pre[0]
    inc = [x for x in items if k in _norm(wsyzy.item_name(x))]
    if inc:
        return inc[0]
    # 兜底：按共同字符占比挑最像的
    best, best_score = items[0], 0.0
    for x in items:
        n = _norm(wsyzy.item_name(x))
        if not n:
            continue
        score = len(set(k) & set(n)) / max(1, len(set(k)))
        if score > best_score:
            best, best_score = x, score
    return best


def _kw_variants(kw):
    """搜索接口对「的」和空格敏感，准备几档降级关键词。"""
    out = [kw]
    n = _norm(kw)
    if n and n != kw:
        out.append(n)
    for pat in (r"第[一二三四五六七八九十\d]+季", r"第[一二三四五六七八九十\d]+部"):
        m = re.sub(pat, "", kw)
        if m and m != kw and m not in out:
            out.append(m)
    return out


def probe_one(site, kw, sample=3):
    """测某站某片第一集的真实码率（Mbps）。

    码率 = 单片字节 × 8 ÷ 分片秒数，这是唯一靠谱的画质指标。
    只比总大小会误判——时长更长的版本总量大，但未必更清晰。
    """
    for variant in _kw_variants(kw):
        r = _probe_variant(site, variant, kw, sample)
        if r:
            return r
    return None


def _probe_variant(site, search_kw, match_kw, sample=3):
    try:
        lst = wsyzy.search(search_kw, site)
        if not lst:
            return None
        return probe_item(site, _match(lst, match_kw), sample)
    except Exception:
        return None


def probe_item(site, item, sample=3):
    """对某个已确定的搜索结果条目，测它的真实码率。"""
    try:
        name = wsyzy.item_name(item)
        vid = wsyzy.item_id(item)
        vod = wsyzy.detail(vid, site)
        eps = wsyzy.parse_eps(vod or {})
        if not eps:
            return None

        ref = wsyzy.site_cfg(site)["referer"]
        client = core.MgtvClient(referer=ref)
        url = wsyzy.resolve_share(client, eps[0]["url"], ref)
        text = client.get(url, headers={"Referer": ref}).decode("utf-8", "ignore")
        pl = core.parse_m3u8(text, url)
        if pl.variants:
            sub = core.pick_variant(pl, "best")
            text = client.get(sub, headers={"Referer": ref}).decode("utf-8", "ignore")
            pl = core.parse_m3u8(text, sub)
        if not pl.segments:
            return None

        durs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text)]
        avg_dur = (sum(durs) / len(durs)) if durs else 2.0

        sizes = []
        for s in pl.segments[:sample]:
            try:
                sizes.append(len(client.get(s["url"], headers={"Referer": ref})))
            except Exception:
                pass
        if not sizes:
            return None

        avg = sum(sizes) / len(sizes)
        mbps = avg * 8 / avg_dur / 1e6 if avg_dur else 0.0
        total_mb = avg * len(pl.segments) / 1024 / 1024
        return {
            "site": site,
            "label": wsyzy.site_cfg(site).get("label", site),
            "name": name,
            "vid": vid,
            "eps": len(eps),
            "segs": len(pl.segments),
            "avg_kb": avg / 1024,
            "mbps": mbps,
            "total_mb": total_mb,
            "minutes": avg_dur * len(pl.segments) / 60,
            "vod": vod,
        }
    except Exception:
        return None


def probe_all(kw, sites=None, workers=5):
    sites = sites or list(wsyzy.SITES)
    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(lambda s: probe_one(s, kw), sites):
            if r:
                out.append(r)
    # 正片优先（时长 >= 10 分钟），再按码率排——避免把 3 分钟的预告片段排到第一
    out.sort(key=lambda r: (1 if r["minutes"] >= 10 else 0, r["mbps"]), reverse=True)
    return out


def print_probe(results):
    if not results:
        print("所有源都没拿到可用流")
        return
    print(f"{'排名':<4} {'站':<11} {'片名':<18} {'集数':>4} {'码率':>10} {'总量':>9} {'时长':>7}")
    print("-" * 74)
    for i, r in enumerate(results, 1):
        flag = " ★" if i == 1 else ""
        clip = " (片段?)" if r["minutes"] < 10 else ""
        print(f"{i:<4} {r['label']:<11} {r['name'][:16]:<18} {r['eps']:>4} "
              f"{r['mbps']:>7.2f}Mbps {r['total_mb']:>7.0f}MB "
              f"{r['minutes']:>5.0f}分{clip}{flag}")
    print("-" * 74)
    best = results[0]
    gap = ""
    if len(results) > 1 and results[1]["mbps"] > 0:
        gap = f"，比第二名清晰 {best['mbps'] / results[1]['mbps']:.1f} 倍"
    print(f"画质最优：{best['label']}　{best['mbps']:.2f} Mbps{gap}")


def _safe_search(site, kw):
    try:
        return wsyzy.search(kw, site)
    except Exception:
        return []


def search_titles(kw):
    """跨站搜索并按片名归并，返回 [{name, items:[(site, item)]}]。

    这样「明星大侦探」会列出各个季/各个版本供你选，
    而不是直接把某一部按画质顶到第一。
    """
    pairs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        for site, lst in ex.map(lambda s: (s, _safe_search(s, kw)), list(wsyzy.SITES)):
            for x in lst:
                if wsyzy.item_name(x):
                    pairs.append((site, x))

    groups = {}
    for site, x in pairs:
        name = wsyzy.item_name(x)
        key = _norm(name)
        if not key:
            continue
        g = groups.setdefault(key, {"name": name, "items": []})
        if len(name) > len(g["name"]):
            g["name"] = name
        g["items"].append((site, x))

    out = list(groups.values())
    # 多站共有的排前面，其次名字短的（更贴近搜索词）
    out.sort(key=lambda g: (-len(g["items"]), len(g["name"])))
    return out
    print(f"（片名是「{best['name']}」，如果不对，加季数再搜，比如「再见爱人的第五季」）")

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #0f1115; color: #e6e8eb;
    font-family: "Microsoft YaHei", -apple-system, "Segoe UI", sans-serif;
    display: flex; height: 100vh; overflow: hidden;
  }
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #player-box { flex: 1; background: #000; display: flex; align-items: center; justify-content: center; position: relative; }
  video { width: 100%; height: 100%; background: #000; }
  #bar {
    padding: 10px 16px; background: #161a20; border-top: 1px solid #232a33;
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap; font-size: 14px;
  }
  #now { color: #7dd3fc; font-weight: 600; }
  #hint { color: #6b7280; font-size: 12px; }
  #status { color: #f59e0b; font-size: 12px; }
  aside {
    width: 300px; background: #12161b; border-left: 1px solid #232a33;
    display: flex; flex-direction: column; flex-shrink: 0;
  }
  aside h2 {
    margin: 0; padding: 14px 16px; font-size: 15px; font-weight: 600;
    border-bottom: 1px solid #232a33; color: #93c5fd;
  }
  #list { overflow-y: auto; flex: 1; padding: 6px; }
  #list::-webkit-scrollbar { width: 8px; }
  #list::-webkit-scrollbar-thumb { background: #2d3748; border-radius: 4px; }
  .ep {
    padding: 9px 12px; margin: 3px 0; border-radius: 6px; cursor: pointer;
    font-size: 13px; color: #c9d1d9; transition: background .15s;
    display: flex; justify-content: space-between; gap: 8px;
  }
  .ep:hover { background: #1e2630; }
  .ep.active { background: #1d4ed8; color: #fff; font-weight: 600; }
  .ep .n { color: #6b7280; font-size: 12px; }
  .ep.active .n { color: #bfdbfe; }
  @media (max-width: 820px) {
    body { flex-direction: column; }
    aside { width: 100%; height: 38vh; border-left: none; border-top: 1px solid #232a33; }
  }
</style>
</head>
<body>
<div id="main">
  <div id="player-box">
    <video id="v" controls playsinline preload="metadata"></video>
  </div>
  <div id="bar">
    <span id="now">未选择</span>
    <span id="status"></span>
    <span id="hint">← → 切换上一集/下一集　空格 暂停</span>
  </div>
</div>
<aside>
  <h2>__TITLE__　共 __COUNT__ 集</h2>
  <div id="list"></div>
</aside>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"></script>
<script>
const EPISODES = __DATA__;
let START = __START__;
const v = document.getElementById('v');
const listEl = document.getElementById('list');
const nowEl = document.getElementById('now');
const statusEl = document.getElementById('status');
let hls = null;
let cur = -1;

EPISODES.forEach((e, i) => {
  const d = document.createElement('div');
  d.className = 'ep';
  d.dataset.i = i;
  d.innerHTML = '<span>' + e.name + '</span><span class="n">' + (i + 1) + '</span>';
  d.onclick = () => play(i);
  listEl.appendChild(d);
});

function setStatus(t) { statusEl.textContent = t || ''; }

function play(i) {
  if (i < 0 || i >= EPISODES.length) return;
  cur = i;
  const ep = EPISODES[i];
  document.querySelectorAll('.ep').forEach(el => {
    el.classList.toggle('active', parseInt(el.dataset.i, 10) === i);
  });
  const active = document.querySelector('.ep.active');
  if (active) active.scrollIntoView({block: 'nearest'});
  nowEl.textContent = ep.name;
  document.title = ep.name + ' - __TITLE__';
  setStatus('加载中...');

  if (hls) { hls.destroy(); hls = null; }
  v.removeAttribute('src');

  if (window.Hls && window.Hls.isSupported()) {
    hls = new Hls({ maxBufferLength: 30, maxMaxBufferLength: 60 });
    hls.on(Hls.Events.ERROR, (evt, data) => {
      if (data && data.fatal) {
        setStatus('播放出错：' + data.type + '，换一集试试');
      }
    });
    hls.on(Hls.Events.MANIFEST_PARSED, () => { setStatus(''); v.play().catch(() => {}); });
    hls.loadSource(ep.url);
    hls.attachMedia(v);
  } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
    v.src = ep.url;
    v.onloadedmetadata = () => { setStatus(''); v.play().catch(() => {}); };
  } else {
    setStatus('这个浏览器不支持 HLS，请用 Chrome / Edge / Firefox');
  }
}

v.addEventListener('ended', () => { if (cur + 1 < EPISODES.length) play(cur + 1); });
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowRight') { if (cur + 1 < EPISODES.length) play(cur + 1); }
  else if (e.key === 'ArrowLeft') { if (cur > 0) play(cur - 1); }
  else if (e.key === ' ') { e.preventDefault(); v.paused ? v.play() : v.pause(); }
});

if (!window.Hls) setStatus('hls.js 加载失败（网络问题），检查能否访问 cdn.jsdelivr.net');
play(START);
</script>
</body>
</html>
"""


def build_page(vod, eps, start=0, out_dir=OUT_DIR, open_browser=True, tag=""):
    title = (vod.get("vod_name") or "播放").strip()
    os.makedirs(out_dir, exist_ok=True)
    fname = wsyzy.safe_name(title, "play") + (f"_{tag}" if tag else "") + ".html"
    path = os.path.join(out_dir, fname)

    data = [{"name": e["name"], "url": e["url"]} for e in eps]
    doc = (
        HTML_TEMPLATE
        .replace("__TITLE__", html.escape(title))
        .replace("__COUNT__", str(len(eps)))
        .replace("__DATA__", json.dumps(data, ensure_ascii=False))
        .replace("__START__", str(start))
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)

    abs_path = os.path.abspath(path)
    print(f"播放页已生成: {abs_path}")
    print(f"共 {len(eps)} 集，起始第 {start + 1} 集")
    if open_browser:
        webbrowser.open("file:///" + abs_path.replace("\\", "/"))
        print("已用默认浏览器打开")
    return abs_path


def serve_and_open(path, port=8765):
    """本地起 HTTP 服务再播放。

    比 file:// 更保险：部分浏览器对 file:// 页面发起的跨域请求有限制，
    走 http://127.0.0.1 就没有这个问题（已实测可正常播放）。
    """
    import functools
    import http.server
    import socketserver
    import threading
    import time
    import urllib.parse

    root = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)

    httpd = None
    last_err = None
    for p in range(port, port + 12):
        try:
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=root)
            socketserver.TCPServer.allow_reuse_address = True
            httpd = socketserver.TCPServer(("127.0.0.1", p), handler)
            port = p
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            httpd = None
    if httpd is None:
        print(f"本地端口都被占用，改用 file:// 打开（{last_err}）")
        webbrowser.open("file:///" + os.path.abspath(path).replace("\\", "/"))
        return

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/{urllib.parse.quote(name)}"
    print(f"本地播放地址: {url}")
    print("保持这个窗口开着，关掉窗口就停止播放服务。Ctrl+C 退出。")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.shutdown()


def interactive(site=wsyzy.DEFAULT_SITE):
    print("=" * 62)
    print(" 在线观看（两步：先选片，再选画质最高的源）")
    print("=" * 62)
    while True:
        kw = wsyzy._ask(" 输入片名（回车退出）: ")
        if not kw:
            return 0

        # ---- 第一步：选片 ----
        print(f"\n正在搜索「{kw}」...")
        groups = search_titles(kw)
        if not groups:
            print(" 没搜到，换个关键词试试\n")
            continue

        shown = groups[:30]
        print(f" 共 {len(shown)} 个结果（括号里是出现在几个站）：\n")
        for i, g in enumerate(shown, 1):
            print(f"   [{i:>2}] {g['name']}   ({len(g['items'])}站)")
        print()
        pick = wsyzy._ask(f" 选第几个 / 1-{len(shown)}（回车重搜）: ")
        if not pick.isdigit() or not (1 <= int(pick) <= len(shown)):
            continue
        group = shown[int(pick) - 1]
        title = group["name"]

        # ---- 第二步：在选中片名的各源里比画质 ----
        print(f"\n正在对比《{title}》各源的画质 ...")
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            for r in ex.map(lambda si: probe_item(si[0], si[1]), group["items"]):
                if r:
                    results.append(r)
        if not results:
            print(" 这部在各站都没拿到可用流，换别的源试试\n")
            continue

        results.sort(key=lambda r: r["mbps"], reverse=True)
        print()
        print_probe(results)
        print()
        pick2 = wsyzy._ask(
            f" 选哪个源 / 1-{len(results)}（回车=1 最高画质）: ", "1")
        if not pick2.isdigit() or not (1 <= int(pick2) <= len(results)):
            pick2 = "1"
        chosen = results[int(pick2) - 1]

        vod = chosen["vod"]
        eps = wsyzy.parse_eps(vod)
        if not eps:
            print(" 这部没有可播放的集\n")
            continue

        print(f"\n《{vod.get('vod_name')}》 {len(eps)} 集　源：{chosen['label']}")
        print("-" * 62)
        for i in range(min(len(eps), 30)):
            print(f"[{i + 1:>3}] {eps[i]['name']:<16}", end="\n" if (i + 1) % 4 == 0 else "")
        print()
        s = wsyzy._ask(f" 从第几集开始 / 1-{len(eps)}（回车=1）: ", "1")
        start = int(s) - 1 if s.isdigit() and 1 <= int(s) <= len(eps) else 0

        tag = "" if chosen["site"] == wsyzy.DEFAULT_SITE else chosen["site"]
        build_page(vod, eps, start=start, tag=tag)

        again = wsyzy._ask("\n 继续看别的？(y=继续，回车=退出): ")
        if again.lower() != "y":
            return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="不下载，生成网页播放器在线观看",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--id", help="资源 id（用 wsyzy.py --search 查）")
    p.add_argument("--ep", type=int, help="从第 N 集开始（从 1 开始）")
    p.add_argument("--out-dir", default=OUT_DIR, help="播放页输出目录")
    p.add_argument("--no-open", action="store_true", help="只生成文件，不自动打开浏览器")
    p.add_argument("--serve", action="store_true",
                   help="用本地 HTTP 服务播放（file:// 打不开时用这个）")
    p.add_argument("--port", type=int, default=8765, help="--serve 的起始端口")
    p.add_argument("--site", default=wsyzy.DEFAULT_SITE, choices=list(wsyzy.SITES),
                   help="采集站：wsyzy/ffzy/lzi/hongniu/modu")
    p.add_argument("--best", metavar="关键词",
                   help="自动探测所有源，用码率最高的那一条生成播放页（画质最优）")
    args = p.parse_args(argv)

    if args.best:
        print(f"探测所有采集站: {args.best}")
        print("（每站取第一集采样测码率，稍等十几秒）\n")
        results = probe_all(args.best)
        print_probe(results)
        if not results:
            return 1
        best = results[0]
        eps = wsyzy.parse_eps(best["vod"])
        if not eps:
            print("该源没有可分集的剧集列表")
            return 1
        print()
        path = build_page(best["vod"], eps, start=0, out_dir=args.out_dir,
                          open_browser=False if args.serve else (not args.no_open),
                          tag=best["site"])
        if args.serve:
            serve_and_open(path, port=args.port)
        return 0

    if not args.id:
        if wsyzy._is_tty():
            return interactive()
        p.print_help()
        return 0

    vod = wsyzy.detail(args.id, args.site)
    if not vod:
        print(f"id={args.id} 没有数据")
        return 1
    eps = wsyzy.parse_eps(vod)
    if not eps:
        print("没有可播放的集")
        return 1

    start = 0
    if args.ep:
        if not (1 <= args.ep <= len(eps)):
            print(f"--ep 超出范围，共 {len(eps)} 集")
            return 1
        start = args.ep - 1

    path = build_page(vod, eps, start=start, out_dir=args.out_dir,
                      open_browser=False if args.serve else (not args.no_open),
                      tag="" if args.site == wsyzy.DEFAULT_SITE else args.site)
    if args.serve:
        serve_and_open(path, port=args.port)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已退出。")
        sys.exit(130)
