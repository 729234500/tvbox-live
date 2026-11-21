#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把外部直播源合并进 build.py 刚产出的 docs/live.txt。

两个来源：
  1. docs/live_fixed.txt —— 这台电视 + 这条宽带上人工实测确认能出画的固定线路，优先级最高。
  2. iptv-api 每天生成的最新 release（result.m3u）—— 每天实测过速，但线路通用、
     常被地域限制或 DNS 污染挡在这张网外面，排第二。

合并是「按来源优先级重新拼接」，不是在上一轮的结果上增量改：
  最终线路 = 固定线路 + iptv-api 线路 + 原有线路，去重后截到 MAX_URLS 条。
这样每天重跑的结果只取决于三个输入的当前内容，不会因为「上次谁被挤掉了」而
来回抖动，跑一次和跑十次结果一致。

为什么不在 sources.json 里直接加 iptv-api：
  它的产物是按每次运行的 tag 发布的，地址每天都变，写不死；只能在构建后跑一步。

失败一律退出 0，绝不影响 build.py 已经产出的内容。
"""

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

REPO = os.environ.get("IPTV_API_REPO", "729234500/iptv-api").strip("/")
ASSET = os.environ.get("IPTV_API_ASSET", "result.m3u")
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
# 调试用：直接指定一个 m3u 地址或本地路径，跳过 release 解析
OVERRIDE = os.environ.get("IPTV_API_M3U", "").strip()

MAX_URLS = 8          # 单频道最多保留几条线路
TIMEOUT = 25
BJT_GROUPS = ("更新时间",)   # 这些分组是 iptv-api 的时间戳伪频道，丢掉

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 tvbox-live-bot/1.0")

_VERIFY = ssl.create_default_context()
_NOVERIFY = ssl._create_unverified_context()


def fetch(url, max_bytes=16 * 1024 * 1024):
    """GET 一个地址，返回 (status, body_bytes, err)。SSL 证书异常时降级重试一次。"""
    last = (0, b"", "unknown")
    for ctx in (_VERIFY, _NOVERIFY):
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "application/json, text/plain, */*")
            if "api.github.com" in url and TOKEN:
                req.add_header("Authorization", "Bearer " + TOKEN)
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                return r.status, r.read(max_bytes), None
        except urllib.error.HTTPError as e:
            return e.code, b"", "HTTP %s" % e.code
        except ssl.SSLError as e:
            last = (0, b"", "SSL: %s" % str(e)[:80])
            continue
        except Exception as e:
            last = (0, b"", "%s: %s" % (type(e).__name__, str(e)[:100]))
            break
    return last


def decode(body):
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return body.decode(enc)
        except Exception:
            continue
    return body.decode("utf-8", "ignore")


def resolve_latest_m3u():
    """在 iptv-api 最近若干次 release 里找带 result.m3u 的那一个，返回 (url, tag, err)。"""
    api = "https://api.github.com/repos/%s/releases?per_page=10" % REPO
    status, body, err = fetch(api)
    if status != 200:
        return None, None, "release 列表取不到：%s" % (err or status)
    try:
        rels = json.loads(decode(body))
    except Exception:
        return None, None, "release 列表不是合法 JSON"
    if not isinstance(rels, list) or not rels:
        return None, None, "还没有任何 release"
    for rel in rels:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        for a in rel.get("assets") or []:
            if isinstance(a, dict) and a.get("name") == ASSET:
                u = a.get("browser_download_url")
                if u:
                    return u, rel.get("tag_name"), None
    return None, None, "近 %d 次 release 里都没有 %s" % (len(rels), ASSET)


def norm_key(name):
    """频道归并键。规则与 build.py 保持一致：去空格/括注/清晰度后缀，CCTV-1 与 CCTV1 归一。"""
    k = re.sub(r"\s+", "", name or "")
    k = re.sub(r"[（(][^）)]*[）)]", "", k)
    k = re.sub(r"(?i)(高清|超清|标清|蓝光|FHD|UHD|HD|4K|8K|1080P|720P)$", "", k)
    k = re.sub(r"(?i)^(CCTV|CGTN)-?(\d+\+?)$", lambda m: m.group(1).upper() + m.group(2), k)
    # 「CCTV-1 综合」「CCTV-1(1080p)」「CCTV1」视为同一路：台标后的中文后缀不是有效区分。
    # 只在 CCTV/CGTN + 数字 这个形态上做，避免误合并其它频道名。
    k = re.sub(r"(?i)^(CCTV|CGTN)-?(\d+\+?)[\u4e00-\u9fff·]*$",
               lambda m: m.group(1).upper() + m.group(2), k)
    return (k.lower() or (name or "")).strip()


def clean_group(g):
    """去掉分组名开头的 emoji / 符号，保持和现有分组风格一致。"""
    g = (g or "").strip()
    g = re.sub(r"^[^0-9A-Za-z\u4e00-\u9fff]+", "", g).strip()
    return g or "其它"


def bad_channel(name, group):
    """iptv-api 里那些不该进电视列表的条目：时间戳伪频道、按更新时间分组的占位。"""
    if any(b in (group or "") for b in BJT_GROUPS):
        return True
    if re.match(r"^\d{4}-\d{2}-\d{2}", name or ""):
        return True
    return not name or name == "未命名"


def parse_m3u(text):
    """解析 m3u，返回 [(频道名, 分组, url)]，保持原有顺序。"""
    out, pending = [], None
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.upper().startswith("#EXTINF"):
            pending = ln
            continue
        if ln.startswith("#"):
            continue
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", ln):
            pending = None
            continue
        meta = pending or ""
        name = meta.rsplit(",", 1)[-1].strip() if "," in meta else "未命名"
        m = re.search(r'group-title="([^"]*)"', meta)
        group = clean_group(m.group(1) if m else "")
        out.append((name, group, ln))
        pending = None
    return out


def parse_live_txt(text):
    """解析 TVBox 原生 live.txt，返回 (顺序分组列表, {分组: {频道名: [urls]}})。"""
    order, groups, cur = [], {}, None
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        head, sep, rest = ln.partition(",")
        if not sep:
            continue
        if rest.strip().startswith("#genre#"):
            g = head.strip() or "其它"
            if g not in groups:
                groups[g] = {}
                order.append(g)
            cur = g
            continue
        if cur is None:
            cur = "其它"
            if cur not in groups:
                groups[cur] = {}
                order.append(cur)
        urls = [u.strip() for u in rest.split("#") if u.strip()]
        if not urls:
            continue
        name = head.strip()
        if name in groups[cur]:
            # 同分组重名不覆盖，避免重写时丢线路
            groups[cur][name].extend(urls)
        else:
            groups[cur][name] = urls
    return order, groups


def bucket(items):
    """[(频道名, 分组, url)] -> {归一化键: {"name","group","urls"}}，同键保持出现顺序并去重。"""
    out = {}
    for name, group, url in items:
        if bad_channel(name, group):
            continue
        key = norm_key(name)
        slot = out.setdefault(key, {"name": name, "group": group, "urls": []})
        if url not in slot["urls"]:
            slot["urls"].append(url)
    return out


def compose(existing_urls, *sources):
    """按来源优先级拼接，去重后截断。"""
    merged, seen = [], set()
    for urls in list(sources) + [existing_urls]:
        for u in urls or []:
            if u in seen:
                continue
            seen.add(u)
            merged.append(u)
    return merged[:MAX_URLS]


def main():
    live_txt = DOCS / "live.txt"
    if not live_txt.exists():
        print("没有 docs/live.txt，跳过（build.py 可能失败了）")
        return 0

    order, groups = parse_live_txt(live_txt.read_text(encoding="utf-8"))

    # ---------- 取固定线路（电视实测过） ----------
    fixed = {}
    fixed_path = DOCS / "live_fixed.txt"
    if fixed_path.exists():
        _, fgroups = parse_live_txt(fixed_path.read_text(encoding="utf-8"))
        fixed = bucket([(name, g, url)
                        for g in fgroups for name, urls in fgroups[g].items()
                        for url in urls])
        print("live_fixed：%d 个频道 / %d 条线路"
              % (len(fixed), sum(len(v["urls"]) for v in fixed.values())))
    else:
        print("没有 docs/live_fixed.txt，跳过固定线路")

    # ---------- 取 iptv-api 当天结果 ----------
    incoming, tag, err = [], None, None
    if OVERRIDE:
        tag = "local"
        if OVERRIDE.startswith("http"):
            status, body, ferr = fetch(OVERRIDE)
            if status == 200:
                incoming = parse_m3u(decode(body))
            else:
                err = "指定的 m3u 拉取失败：%s" % (ferr or status)
        elif Path(OVERRIDE).exists():
            incoming = parse_m3u(Path(OVERRIDE).read_text(encoding="utf-8", errors="ignore"))
        else:
            err = "指定的 m3u 不存在：%s" % OVERRIDE
    else:
        src, tag, rerr = resolve_latest_m3u()
        if not src:
            err = "解析最新 release 失败：%s" % rerr
        else:
            status, body, ferr = fetch(src)
            if status == 200:
                incoming = parse_m3u(decode(body))
            else:
                err = "下载 %s 失败：%s" % (src, ferr or status)
    if err:
        print("!! %s（只用现有线路 + 固定线路）" % err)

    fresh = bucket(incoming)
    print("iptv-api（%s）：%d 个频道 / %d 条线路"
          % (tag or "-", len(fresh), sum(len(v["urls"]) for v in fresh.values())))

    # ---------- 重组每个频道 ----------
    by_key = {}                       # 归一化键 -> (分组, 原标题)
    for g in order:
        for name in groups[g]:
            by_key.setdefault(norm_key(name), (g, name))

    stats = {"filled": 0, "added": 0, "empty": 0}
    for key, slot in list(fixed.items()) + [(k, v) for k, v in fresh.items() if k not in fixed]:
        if key in by_key:
            g, name = by_key[key]
            old = groups[g][name]
            new = compose(old, fixed.get(key, {}).get("urls"), fresh.get(key, {}).get("urls"))
            if new != old:
                groups[g][name] = new
                stats["filled"] += 1
            continue

        # 现有列表里没有这个频道，用来源的分组新建
        g = slot["group"]
        if g not in groups:
            groups[g] = {}
            order.append(g)
        groups[g][slot["name"]] = compose([], fixed.get(key, {}).get("urls"),
                                          fresh.get(key, {}).get("urls"))
        by_key[key] = (g, slot["name"])
        stats["added"] += 1

    if not stats["filled"] and not stats["added"] and not err:
        print("没有可新增或改动的线路，live.txt 保持原样")
        return 0

    lines = []
    for g in order:
        if not groups[g]:
            continue
        lines.append("%s,#genre#," % g)
        for name, urls in groups[g].items():
            if urls:
                lines.append("%s,%s" % (name, "#".join(urls[:MAX_URLS])))
    new_text = "\n".join(lines) + "\n"
    if new_text == live_txt.read_text(encoding="utf-8"):
        print("live.txt 内容无变化")
        return 0
    live_txt.write_text(new_text, encoding="utf-8")

    # live.m3u 同步追加（保留原有 EXTINF 元数据，只补新的流）
    m3u_path = DOCS / "live.m3u"
    if m3u_path.exists():
        old = m3u_path.read_text(encoding="utf-8")
        old_urls = set()
        for ln in old.splitlines():
            ln = ln.strip()
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", ln):
                old_urls.add(ln)
        extra = []
        for g in order:
            for name, urls in groups[g].items():
                for url in urls:
                    if url in old_urls:
                        continue
                    old_urls.add(url)
                    extra.append('#EXTINF:-1 group-title="%s",%s' % (g, name))
                    extra.append(url)
        if extra:
            with m3u_path.open("a", encoding="utf-8") as f:
                f.write("\n" + "\n".join(extra) + "\n")

    total = sum(len(u) for g in order for u in groups[g].values())
    print("合并完成：%d 个频道被补充/重排，%d 个新频道；现共 %d 分组 / %d 频道 / %d 条流"
          % (stats["filled"], stats["added"], len(order),
             sum(len(groups[g]) for g in order), total))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        # 兜底：任何意外都不该拖垮当天的构建
        print("!! 合并直播源时出错，已忽略：%s: %s" % (type(e).__name__, e))
        raise SystemExit(0)
