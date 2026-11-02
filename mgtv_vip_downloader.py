#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mgtv_vip_downloader.py

芒果TV (mgtv.com) 视频流抓取 / 下载工具

功能:
  1. 模式A  --m3u8 URL      : 直接下载浏览器里抓到的 m3u8（最稳，推荐）
  2. 模式B  --vid VID       : 走 PC web 播放接口自动取流（需带登录 cookie + tk2）
  3. master playlist 自动选最高码率
  4. AES-128 / AES-128-CBC 分片解密（EXT-X-KEY / EXT-X-MAP / IV / 序号派生）
  5. 多线程分片下载 + 分片级断点续传 + 失败重试
  6. ts / fMP4 自动合并，输出 MP4

依赖:
  pip install requests
  pip install pycryptodome     # 或者 pip install cryptography

用法示例:
  # 1) 最简单：F12 网络面板里找到 .m3u8，整条 URL 复制过来
  python mgtv_vip_downloader.py --m3u8 "https://.../xxx.m3u8?..." --out 剧集01.mp4

  # 2) 走接口：cookie 必须有（VIP 账号登录态），tk2 从浏览器请求头里复制
  python mgtv_vip_downloader.py --vid 1234567 --cookie-file cookie.txt \
         --tk2 "XXXXXX" --out 剧集01.mp4

  # 3) 画质选择
  python mgtv_vip_downloader.py --m3u8 "URL" --quality 1080
  python mgtv_vip_downloader.py --m3u8 "URL" --quality worst

说明:
  * VIP 内容能否取到，取决于你传进去的 cookie / 播放地址本身是否有权限，
    本工具只做「解析 m3u8 → 下载 → 解密 → 合并」这一段。
  * 芒果的 key 服务会校验 Referer / UA，脚本默认带上了，必要时用
    --referer / --ua 覆盖。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

try:
    # 可选依赖：装了 requests 就用它，没装自动回退到标准库 urllib（不影响运行）
    import requests  # type: ignore
    _HAS_REQUESTS = True
except Exception:
    requests = None
    _HAS_REQUESTS = False

# 排查用：设置 MGTV_FORCE_URLLIB=1 可强制走标准库分支
if os.environ.get("MGTV_FORCE_URLLIB"):
    requests = None
    _HAS_REQUESTS = False

import urllib.request
import urllib.error
import gzip
import io
import zlib

# ---------------------------------------------------------------- AES backend

_AES_BACKEND = None
try:
    # 可选依赖：pycryptodome 优先，缺失时回退 cryptography（两者装任一即可）
    from Crypto.Cipher import AES as _PD_AES  # type: ignore
    _AES_BACKEND = "pycryptodome"
except Exception:
    try:
        # 可选依赖：仅当 pycryptodome 缺失时才走这里（静态检查器看不到也属正常）
        from cryptography.hazmat.primitives.ciphers import (  # type: ignore
            Cipher as _CrCipher,
            algorithms as _CrAlgos,
            modes as _CrModes,
        )
        _AES_BACKEND = "cryptography"
    except Exception:
        _AES_BACKEND = None


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-CBC 解密并去掉 PKCS#7 padding。"""
    if _AES_BACKEND == "pycryptodome":
        out = _PD_AES.new(key, _PD_AES.MODE_CBC, iv).decrypt(data)
    elif _AES_BACKEND == "cryptography":
        dec = _CrCipher(_CrAlgos.AES(key), _CrModes.CBC(iv)).decryptor()
        out = dec.update(data) + dec.finalize()
    else:
        raise RuntimeError(
            "缺少 AES 库，请先安装： pip install pycryptodome  或  pip install cryptography"
        )
    if out and 1 <= out[-1] <= 16:
        pad = out[-1]
        if out[-pad:] == bytes([pad]) * pad:
            out = out[:-pad]
    return out


# ------------------------------------------------------------------- constants

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_REFERER = "https://www.mgtv.com/"
API_INFO = "https://pcweb.api.mgtv.com/video/info"
API_PLAYER = "https://pcweb.api.mgtv.com/player/video"
API_CKEY = "https://pcweb.api.mgtv.com/player/getCkey"

CHUNK = 1024 * 256


def _setup_console():
    """终端编码自适应。

    终端本身能显示中文（CP936 / UTF-8）就原样输出；只有 stdout 是纯 ASCII
    （典型情况是被重定向到管道/只读输出面板）时才切到 UTF-8。
    之前无条件 reconfigure('utf-8') 会让 VS Code 终端按 GBK 解码，反而全是乱码。
    """
    if sys.platform != "win32":
        return
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if enc in ("", "ascii", "us-ascii", "ansi_x3.4-1968"):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_setup_console()


def log(*a):
    print("[mgtv]", *a, flush=True)


def detect_system_proxy():
    """读 Windows 系统代理设置；返回 'host:port' 或 None。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            key.Close()
        if enable and server:
            return str(server)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------- http io

def http_get(url: str, headers: dict | None = None, timeout=20, proxy: str = "") -> bytes:
    """统一 HTTP GET：有 requests 用 requests，否则用标准库 urllib。"""
    headers = dict(headers or {})

    if _HAS_REQUESTS:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = requests.get(url, headers=headers, timeout=timeout, proxies=proxies)
        r.raise_for_status()
        return r.content

    req = urllib.request.Request(url, headers=headers, method="GET")
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            try:
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            except Exception:
                pass
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except Exception:
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception:
                pass
    return raw


# ---------------------------------------------------------------------- client

class MgtvClient:
    def __init__(self, cookie="", referer=DEFAULT_REFERER, ua=DEFAULT_UA,
                 proxies=None, timeout=20, retries=3):
        self.cookie = cookie.strip()
        self.referer = referer
        self.ua = ua
        self.proxies = proxies
        self.proxy = ""
        if isinstance(proxies, dict):
            self.proxy = proxies.get("https") or proxies.get("http") or ""
        self.timeout = timeout
        self.retries = retries
        self.suuid = str(uuid.uuid4()).replace("-", "")
        self._tls = threading.local()
        # 直连失败时自动回退用的系统代理（Clash 等）。未显式指定 proxies 才启用。
        self._sys_proxy = None
        if not self.proxies:
            sp = detect_system_proxy()
            if sp:
                self._sys_proxy = sp if "://" in sp else f"http://{sp}"
        self._use_proxy = False
        self._proxy_lock = threading.Lock()

    # --- session（关键：每线程一个 Session，复用 TCP/TLS 连接）----------------
    def _session(self):
        s = getattr(self._tls, "session", None)
        if s is None:
            s = requests.Session()
            # 不读取系统代理环境变量：下载走直连最快。
            # 需要代理时显式传 proxies / --proxy，避免被 TUN/全局代理无端拖慢。
            s.trust_env = False
            s.headers.update(self._headers())
            if self.proxies:
                s.proxies.update(self.proxies)
            # 连接池开大，避免并发时排队新建连接
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=128, pool_maxsize=128, max_retries=0, pool_block=False
            )
            s.mount("http://", adapter)
            s.mount("https://", adapter)
            self._tls.session = s
        return s

    # --- request -----------------------------------------------------------
    def _headers(self, extra=None):
        h = {
            "User-Agent": self.ua,
            "Referer": self.referer,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        if self.cookie:
            h["Cookie"] = self.cookie
        if extra:
            h.update(extra)
        return h

    def _current_proxy(self):
        if self.proxies:
            return self.proxies
        if self._use_proxy and self._sys_proxy:
            return {"http": self._sys_proxy, "https": self._sys_proxy}
        return None

    def get(self, url, headers=None, stream=False, expect_json=False):
        last = None
        for i in range(self.retries):
            try:
                if _HAS_REQUESTS:
                    r = self._session().get(url, headers=headers or {},
                                            timeout=self.timeout,
                                            proxies=self._current_proxy())
                    r.raise_for_status()
                    raw = r.content
                else:
                    px = self._current_proxy() or {}
                    raw = http_get(url, self._headers(headers),
                                   timeout=self.timeout,
                                   proxy=px.get("https") or px.get("http") or "")
                if expect_json:
                    return json.loads(raw.decode("utf-8", "ignore"))
                return raw
            except Exception as e:  # noqa: BLE001
                last = e
                # 直连失败且存在系统代理（Clash 之类）→ 全局切到代理再试
                if (not self.proxies and not self._use_proxy and self._sys_proxy):
                    with self._proxy_lock:
                        if not self._use_proxy:
                            self._use_proxy = True
                            log(f"直连失败（{type(e).__name__}），自动切换系统代理 {self._sys_proxy}")
                    continue
                time.sleep(0.4 * (i + 1))
        raise RuntimeError(f"请求失败 {url} -> {last}")

    # --- API ---------------------------------------------------------------
    def video_info(self, vid: str) -> dict:
        """基础信息：标题、时长、集数等。"""
        data = self.get(f"{API_INFO}?vid={vid}", expect_json=True)
        return data.get("data", {})

    def get_ckey(self, vid: str) -> str:
        """尝试从 ckey 接口拿一个播放令牌（不保证所有版本都开放）。"""
        try:
            data = self.get(
                f"{API_CKEY}?video_id={vid}&suuid={self.suuid}",
                expect_json=True,
            )
            return (data.get("data") or {}).get("ckey", "") or (data.get("ckey") or "")
        except Exception:
            return ""

    def build_tk2(self, vid: str, ckey: str = "") -> str:
        """tk2 = base64(suuid_ckey_timestamp)，PC web 播放接口的签名参数。"""
        ts = str(int(time.time()))
        raw = f"{self.suuid}_{ckey}_{ts}"
        return base64.b64encode(raw.encode()).decode()

    def play_info(self, vid: str, tk2: str = "") -> dict:
        """播放信息，返回 stream 列表（含 m3u8 地址）。"""
        tk2 = tk2 or self.build_tk2(vid, self.get_ckey(vid))
        url = f"{API_PLAYER}?video_id={vid}&suuid={self.suuid}&tk2={tk2}"
        data = self.get(url, expect_json=True)
        body = data.get("data") or {}
        streams = body.get("stream") or []
        if not streams:
            for key in ("stream_domain", "info"):
                pass
            log("播放接口未返回 stream，可能缺少有效 cookie / tk2 已过期。")
            log("返回体片段：", json.dumps(data, ensure_ascii=False)[:400])
        return body


# ------------------------------------------------------------------ m3u8 parse

KEY_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:(.*)")


def _attrs(line: str) -> dict:
    out = {}
    for k, v in KEY_ATTR_RE.findall(line):
        v = v.strip().strip('"')
        out[k] = v
    return out


class Playlist:
    def __init__(self):
        self.variants = []      # [(bandwidth, resolution, url)]
        self.segments = []      # [{'url':..., 'key':..., 'iv':..., 'seq': int}]
        self.map_url = None
        self.key_uri = None
        self.key_iv = None
        self.key_method = None
        self.media_sequence = 0


def parse_m3u8(text: str, base_url: str) -> Playlist:
    pl = Playlist()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # master playlist
    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        pending = None
        for ln in lines:
            if ln.startswith("#EXT-X-STREAM-INF"):
                m = STREAM_INF_RE.match(ln)
                info = _attrs(m.group(1)) if m else {}
                bw = int(info.get("BANDWIDTH") or info.get("AVERAGE-BANDWIDTH") or 0)
                res = info.get("RESOLUTION", "")
                pending = (bw, res)
            elif ln.startswith("#"):
                continue
            elif pending:
                pl.variants.append((pending[0], pending[1], urljoin(base_url, ln)))
                pending = None
        return pl

    # media playlist
    seq = 0
    cur_key = None
    pending_range = None
    for ln in lines:
        if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            pl.media_sequence = int(ln.split(":", 1)[1])
            seq = pl.media_sequence
        elif ln.startswith("#EXT-X-KEY:"):
            a = _attrs(ln[len("#EXT-X-KEY:"):])
            method = (a.get("METHOD") or "NONE").upper()
            if method == "NONE":
                cur_key = None
            else:
                cur_key = {
                    "method": method,
                    "uri": urljoin(base_url, a["URI"]) if a.get("URI") else None,
                    "iv": a.get("IV"),
                }
                pl.key_method = method
                pl.key_uri = cur_key["uri"]
                pl.key_iv = cur_key["iv"]
        elif ln.startswith("#EXT-X-MAP:"):
            a = _attrs(ln[len("#EXT-X-MAP:"):])
            if a.get("URI"):
                pl.map_url = urljoin(base_url, a["URI"])
        elif ln.startswith("#EXT-X-BYTERANGE:"):
            pending_range = ln.split(":", 1)[1]
        elif not ln.startswith("#"):
            pl.segments.append({
                "url": urljoin(base_url, ln),
                "key": dict(cur_key) if cur_key else None,
                "seq": seq,
                "range": pending_range,
            })
            pending_range = None
            seq += 1
    return pl


def _iv_bytes(iv_attr, seq: int):
    if iv_attr:
        v = iv_attr[2:] if iv_attr.lower().startswith("0x") else iv_attr
        v = v.zfill(32)
        return bytes.fromhex(v)
    return seq.to_bytes(16, "big")


def _key_bytes(raw: bytes) -> bytes:
    """key 服务可能返回 16 字节裸 key，也可能返回 32 字符 hex。"""
    if len(raw) == 32:
        try:
            return bytes.fromhex(raw.decode())
        except Exception:
            pass
    return raw[:16]


# ------------------------------------------------------------------- downloader

class Downloader:
    def __init__(self, client: MgtvClient, out_path: str, workers=8,
                 keep_parts=False, quiet=False):
        self.client = client
        self.out_path = out_path
        self.workers = max(1, workers)
        self.keep_parts = keep_parts
        self.quiet = quiet
        self.parts_dir = out_path + ".parts"
        self.lock = threading.Lock()
        self.done = 0
        self.total = 0
        self.bytes = 0
        self.t0 = 0.0
        self._key_cache = {}

    # --- helpers -----------------------------------------------------------
    def _log(self, *a):
        if not self.quiet:
            log(*a)

    def _get_key(self, uri):
        if uri in self._key_cache:
            return self._key_cache[uri]
        raw = self.client.get(uri, headers={"Referer": self.client.referer})
        key = _key_bytes(raw)
        self._key_cache[uri] = key
        return key

    def _fetch_segment(self, seg, idx: int, init_data=None):
        path = os.path.join(self.parts_dir, f"{idx:06d}.part")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with self.lock:
                self.done += 1
            return idx, path

        headers = {}
        if seg.get("range"):
            try:
                start, length = seg["range"].split("@")[0], seg["range"].split("@")[0]
                n = int(seg["range"].split("@")[0])
                headers["Range"] = f"bytes=0-{n - 1}"
            except Exception:
                pass

        data = self.client.get(seg["url"], headers=headers)
        if init_data and idx == 0 and not seg.get("range"):
            pass

        k = seg.get("key")
        if k and k.get("method", "").upper().startswith("AES"):
            key = self._get_key(k["uri"])
            iv = _iv_bytes(k.get("iv"), seg["seq"])
            if len(key) not in (16, 24, 32):
                raise RuntimeError(f"key 长度异常 {len(key)}: {k['uri']}")
            data = aes_cbc_decrypt(key, iv, data)

        with open(path, "wb") as f:
            f.write(data)
        with self.lock:
            self.done += 1
            self.bytes += len(data)
            if not self.quiet and (self.done % 20 == 0 or self.done == self.total):
                elapsed = max(0.001, time.time() - self.t0)
                pct = self.done * 100.0 / max(1, self.total)
                speed = self.bytes / elapsed / 1024 / 1024
                remain = (self.total - self.done) / max(0.001, self.done / elapsed)
                eta = f"{int(remain // 60):02d}:{int(remain % 60):02d}"
                print(f"\r[mgtv] 分片 {self.done}/{self.total}  ({pct:5.1f}%)  "
                      f"{speed:5.2f} MB/s  ETA {eta}",
                      end="", flush=True)
        return idx, path

    # --- main --------------------------------------------------------------
    def run(self, pl: Playlist):
        if not pl.segments:
            raise RuntimeError("m3u8 里没有解析到分片，URL 可能失效或需要重新鉴权。")

        os.makedirs(self.parts_dir, exist_ok=True)
        self.total = len(pl.segments)

        # 断点续传：先把已完成分片的字节数计入，速度统计才准
        resumed = 0
        for i in range(self.total):
            p = os.path.join(self.parts_dir, f"{i:06d}.part")
            if os.path.exists(p):
                self.bytes += os.path.getsize(p)
                resumed += 1
        self.t0 = time.time()

        init_data = None
        if pl.map_url:
            self._log("下载 init segment:", pl.map_url)
            init_data = self.client.get(pl.map_url)
            with open(os.path.join(self.parts_dir, "init.mp4"), "wb") as f:
                f.write(init_data)

        self._log(f"分片数 {self.total}，并发 {self.workers}"
                  + (f"，加密 {pl.key_method}" if pl.key_method else "，未加密")
                  + (f"，续传跳过 {resumed} 片" if resumed else ""))

        sp = detect_system_proxy()
        if sp and not getattr(self.client, "proxies", None):
            self._log(f"检测到系统代理 {sp}：优先直连，若直连不通会自动改走它。")

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = [ex.submit(self._fetch_segment, s, i, init_data)
                    for i, s in enumerate(pl.segments)]
            for f in as_completed(futs):
                f.result()
        if not self.quiet:
            print()

        self._merge()
        if not self.keep_parts:
            for fn in os.listdir(self.parts_dir):
                os.remove(os.path.join(self.parts_dir, fn))
            os.rmdir(self.parts_dir)
        self._log("完成 ->", self.out_path)

    def _merge(self):
        tmp = self.out_path + ".tmp"
        written = 0
        step = max(1, self.total // 20)
        if not self.quiet:
            print(f"[mgtv] 正在合并 {self.total} 个分片，请勿关闭窗口 ...", flush=True)
        with open(tmp, "wb") as out:
            init = os.path.join(self.parts_dir, "init.mp4")
            if os.path.exists(init):
                with open(init, "rb") as f:
                    b = f.read()
                    out.write(b)
                    written += len(b)
            for i in range(self.total):
                p = os.path.join(self.parts_dir, f"{i:06d}.part")
                if not os.path.exists(p):
                    raise RuntimeError(f"缺少分片 {p}")
                with open(p, "rb") as f:
                    while True:
                        b = f.read(CHUNK * 4)
                        if not b:
                            break
                        out.write(b)
                        written += len(b)
                if not self.quiet and ((i + 1) % step == 0 or i + 1 == self.total):
                    pct = (i + 1) * 100.0 / self.total
                    print(f"\r[mgtv] 合并 {i + 1}/{self.total}  ({pct:5.1f}%)  "
                          f"{written / 1024 / 1024:7.1f} MB", end="", flush=True)
        if not self.quiet:
            print()
        os.replace(tmp, self.out_path)


# ---------------------------------------------------------------- entry helpers

def pick_variant(pl: Playlist, quality: str) -> str:
    if not pl.variants:
        return ""
    vs = pl.variants[:]
    if quality in ("best", "max", ""):
        vs.sort(key=lambda x: x[0], reverse=True)
        return vs[0][2]
    if quality in ("worst", "min"):
        vs.sort(key=lambda x: x[0])
        return vs[0][2]
    # 按分辨率关键字匹配，例如 1080 / 720
    for bw, res, url in vs:
        if quality in res:
            return url
    vs.sort(key=lambda x: x[0], reverse=True)
    log(f"未匹配到画质 '{quality}'，退回最高码率 {vs[0][1] or vs[0][0]}")
    return vs[0][2]


def load_cookie(args) -> str:
    if args.cookie:
        return args.cookie
    if args.cookie_file and os.path.exists(args.cookie_file):
        with open(args.cookie_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def resolve_m3u8(client: MgtvClient, args) -> str:
    if args.m3u8:
        return args.m3u8.strip()

    if not args.vid:
        raise SystemExit("需要 --m3u8 或 --vid 之一")

    body = client.play_info(args.vid, args.tk2 or "")
    streams = body.get("stream") or []
    if not streams:
        raise SystemExit(
            "播放接口没有返回流。\n"
            "  排查方向：1) 确认 --cookie 是已登录且有效的；2) tk2 从浏览器那次成功播放的\n"
            "  请求里复制；3) 直接用 --m3u8 模式，F12 里复制 m3u8 整条 URL 最省事。"
        )
    # stream 项里通常有 url / name / 清晰度
    cands = []
    for s in streams:
        u = s.get("url") or ""
        if ".m3u8" in u:
            name = s.get("name") or s.get("profile_name") or ""
            cands.append((name, u))
    if not cands:
        raise SystemExit("stream 里没有 m3u8 地址：" + json.dumps(streams, ensure_ascii=False)[:300])

    chosen = cands[0][1]
    for name, u in cands:
        if args.quality in name:
            chosen = u
            break
    else:
        if args.quality in ("best", "max", ""):
            chosen = cands[0][1]
        elif args.quality in ("worst", "min"):
            chosen = cands[-1][1]
    return chosen


def _banner():
    aes = _AES_BACKEND or "缺失(加密流会失败)"
    http = "requests" if _HAS_REQUESTS else "urllib(标准库)"
    print("=" * 62)
    print(" 芒果TV 视频下载器")
    print("=" * 62)
    print(f" 运行环境 : HTTP={http}   AES={aes}")
    print(f" Python   : {sys.version.split()[0]}   {sys.executable}")
    print(f" 当前目录 : {os.getcwd()}")
    print("-" * 62)


def _ask(prompt, default=""):
    """读取一行；Ctrl+C / Ctrl+D 视为退出，不当成异常堆栈。"""
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


def print_usage_card():
    """非交互环境（只读输出面板 / 管道重定向）里给出可直接复制的命令。"""
    _banner()
    print(" 当前不是可输入的终端（Code Runner 的“输出”面板只读）。")
    print(" 三种可行做法：")
    print()
    print(" ① 改用集成终端：Ctrl+` 打开终端，然后粘贴：")
    print('    python mgtv_vip_downloader.py --m3u8 "这里换成你的m3u8链接" --out 剧集01.mp4')
    print()
    print(" ② Code Runner 设置里勾选 Run In Terminal，再点运行即可交互输入：")
    print('    "code-runner.runInTerminal": true')
    print()
    print(" ③ 想走 vid 接口：")
    print('    python mgtv_vip_downloader.py --vid 1234567 --cookie-file cookie.txt --tk2 "xxx"')
    print()
    print(" 拿 m3u8 的方法：浏览器 F12 → Network → 过滤 m3u8 → 右键 Copy link address")
    print("=" * 62)


def run_wizard(args):
    """不带参数直接运行时进入交互向导。"""
    _banner()
    print(" 1) 我有 m3u8 链接    ← 推荐，浏览器 F12 → Network → 过滤 m3u8 复制整条")
    print(" 2) 我有视频 vid      ← 需要已登录 cookie，接口签名会变")
    print(" q) 退出")
    print("-" * 62)

    choice = _ask(" 请选择 [1]: ", "1")
    if choice.lower() in ("q", "quit", "exit"):
        raise SystemExit(0)

    if choice == "2":
        args.vid = _ask(" 视频 vid : ")
        if not args.vid:
            raise SystemExit("没填 vid，已退出。")
        ck = _ask(" cookie 文件路径（可留空）: ")
        if ck:
            args.cookie_file = ck
        tk2 = _ask(" tk2（可留空）: ")
        if tk2:
            args.tk2 = tk2
        args.out = _ask(" 输出文件名 [output.mp4]: ", "output.mp4")
        args.quality = _ask(" 画质 best/worst/1080 [best]: ", "best")
        return args

    args.m3u8 = _ask(" 粘贴 m3u8 链接 : ")
    if not args.m3u8:
        raise SystemExit("没填链接，已退出。")
    args.out = _ask(" 输出文件名 [output.mp4]: ", "output.mp4")
    args.quality = _ask(" 画质 best/worst/1080 [best]: ", "best")
    w = _ask(" 并发分片数 [8]: ", "8")
    try:
        args.workers = max(1, int(w))
    except ValueError:
        args.workers = 8
    return args


def main(argv=None):
    p = argparse.ArgumentParser(
        description="芒果TV m3u8 流抓取/下载（支持 AES-128 解密与断点续传）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--m3u8", help="直接指定 m3u8 URL（推荐，最稳）")
    p.add_argument("--vid", help="视频 vid，走播放接口自动取流")
    p.add_argument("--tk2", default="", help="播放接口签名参数，可从浏览器请求头复制")
    p.add_argument("--cookie", default="", help="Cookie 字符串（需登录态）")
    p.add_argument("--cookie-file", default="", help="从文件读取 Cookie")
    p.add_argument("--referer", default=DEFAULT_REFERER)
    p.add_argument("--ua", default=DEFAULT_UA)
    p.add_argument("--proxy", default="", help="例如 http://127.0.0.1:7890")
    p.add_argument("--out", default="output.mp4", help="输出文件名")
    p.add_argument("--quality", default="best", help="best / worst / 1080 / 720 ...")
    p.add_argument("--workers", type=int, default=8, help="分片并发数（4-16 最佳，过高会被 CDN 限速）")
    p.add_argument("--keep-parts", action="store_true", help="保留分片目录")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if not args.m3u8 and not args.vid:
        if _is_tty():
            run_wizard(args)
        else:
            print_usage_card()
            return 0

    if _AES_BACKEND is None:
        log("警告：pycryptodome / cryptography 都没装，AES 加密流会解密失败。")

    proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else None

    client = MgtvClient(
        cookie=load_cookie(args),
        referer=args.referer,
        ua=args.ua,
        proxies=proxies,
    )

    m3u8_url = resolve_m3u8(client, args)
    log("m3u8:", m3u8_url[:160] + ("..." if len(m3u8_url) > 160 else ""))

    text = client.get(m3u8_url, headers={"Referer": args.referer}).decode("utf-8", "ignore")
    pl = parse_m3u8(text, m3u8_url)

    if pl.variants:
        log(f"master playlist，候选 {len(pl.variants)} 路：")
        for bw, res, _u in sorted(pl.variants, key=lambda x: -x[0]):
            log(f"   {res or '?':>10}  {bw // 1000:>6} kbps")
        sub_url = pick_variant(pl, args.quality)
        log("选定:", sub_url[:160])
        text = client.get(sub_url, headers={"Referer": args.referer}).decode("utf-8", "ignore")
        pl = parse_m3u8(text, sub_url)

    out = args.out
    if not os.path.splitext(out)[1]:
        out += ".mp4"
    out_dir = os.path.dirname(os.path.abspath(out))
    os.makedirs(out_dir, exist_ok=True)

    Downloader(client, out, workers=args.workers,
               keep_parts=args.keep_parts, quiet=args.quiet).run(pl)
    size = os.path.getsize(out) / 1024 / 1024
    log(f"文件大小 {size:.1f} MB")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[mgtv] 已中断，分片保留在 *.parts 目录，重跑同一命令可续传。")
        sys.exit(130)
