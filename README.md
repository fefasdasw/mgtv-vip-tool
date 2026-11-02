# mgtv-vip-tool

芒果TV / 影视资源 在线播放与下载工具。

从公开影视采集接口取 m3u8 流，支持**在线播放**（本地网页播放器，自动挑最高码率源）和**下载**（多线程分片 + AES-128 解密 + 断点续传 + 自动合并）。

> ⚠️ 仅供技术学习与个人研究。请勿用于商业用途，请支持正版。

---

## 功能

- **在线观看**：生成带 hls.js 的本地播放器页面，无广告、可选集、自动连播，不消耗硬盘
- **多源比画质**：跨采集站搜索，实测每条流的真实码率（Mbps），自动选最清晰的那条
- **下载成 MP4**：多线程分片下载、AES-128/CBC 解密、分片级断点续传、自动合并
- **断点续传**：中断后重跑同一条命令，已完成分片自动跳过
- **分片修复**：下载完成但合并中断时，可单独把 `*.parts` 目录合并成 MP4
- **零强制依赖**：无 `requests` 时自动回退标准库 `urllib`；AES 支持 pycryptodome / cryptography 任一
- **自动代理回退**：直连失败时自动切换到系统代理（Clash 等）

## 环境要求

- Python 3.9+
- Windows / macOS / Linux

可选依赖（装了更好，不装也能跑）：

```bash
pip install -r requirements.txt
```

即 `requests`（HTTP 后端）+ `pycryptodome`（AES 后端，也可用 `cryptography` 代替）。

## 快速开始

```bash
git clone https://github.com/<your-name>/mgtv-vip-tool.git
cd mgtv-vip-tool
```

### Windows 双击即用

| 文件 | 作用 |
|---|---|
| `一键观看.bat` | 搜片 → 选片 → 比画质 → 网页播放 |
| `一键下载.bat` | 搜片 → 选片 → 选集 → 下载 MP4 |

### 命令行

```bash
# 在线观看：自动对比所有源的画质，用最清晰的那条
python watch.py --best "片名"

# 交互式（推荐）
python watch.py

# 下载某一集
python wsyzy.py --search "片名"
python wsyzy.py --id 128076 --ep 12 --out-dir ./下载

# 下载全集
python wsyzy.py --id 128076 --all --out-dir ./下载
```

## 命令参考

### `watch.py` — 在线观看

| 参数 | 说明 |
|---|---|
| `--best 关键词` | 自动探测所有源，用码率最高的生成播放页 |
| `--id ID` | 指定资源 id 生成播放页 |
| `--ep N` | 从第 N 集开始 |
| `--site 站点` | 指定采集站（`wsyzy`/`ffzy`/`lzi`/`hongniu`/`modu`） |
| `--serve` | 用本地 HTTP 服务播放（`file://` 打不开时用） |
| `--no-open` | 只生成文件，不自动打开浏览器 |

生成的播放页在 `播放页/<片名>.html`，双击即可播放。

### `wsyzy.py` — 搜索与下载

| 参数 | 说明 |
|---|---|
| `--search 关键词` | 搜索，返回 id 列表 |
| `--id ID` | 资源 id |
| `--ep N` | 下载第 N 集 |
| `--all` | 下载全部 |
| `--out-dir DIR` | 输出目录 |
| `--quality` | `best` / `worst` / `1080` / `720` |
| `--workers N` | 分片并发数（默认 8，4–16 最佳） |
| `--site 站点` | 指定采集站 |

### `merge_parts.py` — 合并残留分片

下载完成但合并阶段被中断时，直接合并现成的 `*.parts` 目录，不用重新下载：

```bash
python merge_parts.py "下载/xxx.mp4.parts"
python merge_parts.py --all "下载"
```

### `mgtv_vip_downloader.py` — 通用 m3u8 下载器

手上有 m3u8 直链时可以直接用：

```bash
python mgtv_vip_downloader.py --m3u8 "https://.../index.m3u8" --out 视频.mp4
```

| 参数 | 说明 |
|---|---|
| `--m3u8 URL` | 直接指定 m3u8 |
| `--vid ID` + `--cookie` | 走芒果官方播放接口 |
| `--proxy` | 显式指定代理（如 `http://127.0.0.1:7890`） |
| `--workers` | 分片并发数 |
| `--keep-parts` | 保留分片目录 |

### `vip_resolver.py` — 解析接口探测

```bash
python vip_resolver.py --url "视频页地址" --links   # 生成各解析站的播放链接
python vip_resolver.py --ping                      # 测本地到各接口的连通性
```

### `selftest_mgtv.py` — 自测

本地起 HTTP 服务，构造 AES 加密的 m3u8 分片，验证「解析 → 下载 → 解密 → 合并」全链路：

```bash
python selftest_mgtv.py
# SELFTEST_OK  ts=5  mb=0.04  variants=2
```

## 工作原理

```
                 ┌─────────────┐
  搜索关键词 ──▶ │ 采集站 API  │ ──▶ vod_play_url（含每集 m3u8 或 share 页）
                 └─────────────┘
                        │
                        ▼
                 resolve_share()  有些站给的是 /share/xxx 页面，
                        │          需从中提取 const url = "xxx.m3u8?sign=..."
                        ▼
              ┌──────────────────┐
              │   m3u8 解析器     │  master/variants 自动展开、EXT-X-KEY、
              │                  │  EXT-X-MAP、EXTINF 时长
              └──────────────────┘
                   │          │
        在线播放 ──┘          └── 下载
             │                      │
      hls.js + 本地 HTML     多线程分片 + AES-128-CBC 解密
      （CORS: * 可直接拉流）   → 断点续传 → 顺序合并 → MP4
```

**画质是怎么判断的**：码率 = 单片字节 × 8 ÷ 分片时长。跨站对比取最大值。同一部片在不同源能差 4–5 倍，所以自动测比固定用某个站更靠谱。

## 常见问题

**Q：打开播放页黑屏 / 一直转圈？**
用本地服务器方式：`python watch.py --id <id> --serve`。部分浏览器对 `file://` 页面的跨域请求有限制。

**Q：下载到 100% 后卡住？**
那是合并阶段（搬运 GB 级数据，耗时几十秒）。等它跑完，会显示 `合并 x/y`。如果已被中断，用 `merge_parts.py` 直接合并残留分片。

**Q：速度慢？**
1. 并发调到 8（实测最优，更高反而被 CDN 限速）
2. 该 CDN 可能在海外，程序会自动切换系统代理
3. 换一部片或换个源试试

**Q：所有源都搜不到？**
片名少带后缀。`再见爱人` 比 `再见爱人的第五季` 更容易搜到，搜到后再从列表里选季数。

**Q：下载完的文件播放器不认？**
HLS 分片拼接后本质是 MPEG-TS 流，把扩展名改成 `.ts` 即可；或用 ffmpeg 转封装：
```bash
ffmpeg -i 视频.mp4 -c copy -bsf:a aac_adtstoasc 输出.mp4
```

## 画质的现实边界

采集站是统一转码流水线，综艺类普遍在 1.7–1.8 Mbps，电影类运气好到 2.2 Mbps。本工具能保证**你拿到的是所有可用源里最清晰的一条**，但无法突破源码率上限。想要更高画质只能走官方会员或高码率资源站。

## 免责声明

本项目仅用于**技术学习与研究**（HLS 协议解析、AES 解密、多线程下载、播放器实现）。

- 不含任何视频内容，所有资源来自第三方公开接口
- 请勿用于商业用途或二次分发
- 请支持正版，尊重版权方权益
- 使用本工具产生的任何后果由使用者自行承担

## License

[MIT](LICENSE)
