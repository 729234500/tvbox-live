#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox 动态订阅生成器（纯标准库，无第三方依赖）

流程：
  1. 读 sources.json 的候选源 + 用 GitHub API 自动发现新源
  2. 逐个实测：HTTP 状态 / 体积 / JSON 合法性 / sites 数量 / spider 与解析 js 可达
  3. 只把活着的写进 docs/index.json（多仓，电视端固定链接）
  4. 附带生成：index-alt.json（备用镜像）、all.json（全量）、best.json（单仓直连）、
     live.m3u（直播聚合）、status.md + health.json（体检报告）

任何一步失败都不清空已有产物；全部源都挂掉时保留上一版并 exit 1 触发告警。
"""

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CFG = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))

MIRROR = (CFG.get("mirror") or "https://gh-proxy.com/").rstrip("/") + "/"
MIRROR2 = (CFG.get("mirror_backup") or "https://ghfast.top/").rstrip("/") + "/"
MAX_SOURCES = int(CFG.get("max_sources", 10))
MAX_PER_HOST = int(CFG.get("max_per_host", 4))
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 tvbox-live-bot/1.0")
TIMEOUT = 25
MAX_BYTES = 12 * 1024 * 1024
BJT = timezone(timedelta(hours=8))

_VERIFY = ssl.create_default_context()
_NOVERIFY = ssl._create_unverified_context()

GH_HOSTS = ("raw.githubusercontent.com", "github.com", "objects.githubusercontent.com",
            "gitee.com", "cdn.jsdelivr.net")


# ----------------------------------------------------------------------------- http
def ascii_url(u):
    """把含中文等非 ASCII 字符的 URL 转成可发请求的形式。"""
    try:
        u.encode("ascii")
        return u
    except UnicodeEncodeError:
        pass
    p = urllib.parse.urlsplit(u)
    host = p.netloc
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        # 中文域名必须转 punycode，否则底层会抛 ascii 编码错误
        user, at, hostport = host.rpartition("@")
        if ":" in hostport:
            h, sep, port = hostport.rpartition(":")
            host = user + at + h.encode("idna").decode("ascii") + sep + port
        else:
            host = user + at + hostport.encode("idna").decode("ascii")
    safe = "/%:;=+&?@!$'()*,~"
    return urllib.parse.urlunsplit((
        p.scheme, host,
        urllib.parse.quote(p.path, safe=safe),
        urllib.parse.quote(p.query, safe=safe),
        p.fragment))


def split_asset(s):
    """TVBox 的 jar 写法是 '路径;md5;哈希'，拆出真实路径与后缀。"""
    if not isinstance(s, str):
        return s, ""
    i = s.find(";")
    return (s, "") if i < 0 else (s[:i], s[i:])


def rel_join(base, path):
    """相对路径拼接。不用 urljoin：镜像地址里本身含 'https://'，会被它折叠成 'https:/'。"""
    if not path or path.startswith("http"):
        return path
    if path.startswith("//"):
        return urllib.parse.urlsplit(base).scheme + ":" + path
    if ".." in path:
        return urllib.parse.urljoin(base, path)
    d = base.rsplit("/", 1)[0]
    return d + "/" + (path[2:] if path.startswith("./") else path)


def http_get(url, timeout=TIMEOUT, max_bytes=MAX_BYTES, headers=None, retries=1):
    """返回 {status, body, url(最终地址), err, ms}。SSL 证书有问题时自动降级重试。"""
    url = ascii_url(url)
    last = {"status": 0, "body": b"", "url": url, "err": "unknown", "ms": 0}
    for attempt in range(retries + 1):
        for ctx in (_VERIFY, _NOVERIFY):
            t0 = time.time()
            try:
                req = urllib.request.Request(url, method="GET")
                req.add_header("User-Agent", UA)
                req.add_header("Accept", "application/json, text/plain, */*")
                req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
                if "api.github.com" in url and TOKEN:
                    req.add_header("Authorization", "Bearer " + TOKEN)
                for k, v in (headers or {}).items():
                    req.add_header(k, v)
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                    body = r.read(max_bytes)
                    return {"status": r.status, "body": body, "url": r.geturl(),
                            "err": None, "ms": int((time.time() - t0) * 1000)}
            except urllib.error.HTTPError as e:
                return {"status": e.code, "body": b"", "url": url, "err": "HTTP %s" % e.code,
                        "ms": int((time.time() - t0) * 1000)}
            except ssl.SSLError as e:
                last = {"status": 0, "body": b"", "url": url, "err": "SSL: %s" % str(e)[:80],
                        "ms": int((time.time() - t0) * 1000)}
                continue
            except Exception as e:
                last = {"status": 0, "body": b"", "url": url,
                        "err": "%s: %s" % (type(e).__name__, str(e)[:100]),
                        "ms": int((time.time() - t0) * 1000)}
                break
    return last


def decode(body):
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return body.decode(enc)
        except Exception:
            continue
    return body.decode("utf-8", "ignore")


def is_github(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in GH_HOSTS)


def mirrorize(url, which=1):
    """把 github 系地址套上镜像前缀；非 github 原样返回。"""
    if not url or not is_github(url):
        return url
    if url.startswith(MIRROR) or url.startswith(MIRROR2):
        return url
    return (MIRROR if which == 1 else MIRROR2) + url


def host_of(url):
    return urllib.parse.urlparse(url).netloc.lower()


def quota_key(url):
    """限额按「域名 + 仓库」算，避免 github 上一堆不同作者被算成同一家。"""
    host = host_of(url)
    if any(host.endswith(h) for h in ("githubusercontent.com", "github.com",
                                      "cdn.jsdelivr.net", "gitee.com")):
        seg = [s for s in urllib.parse.urlparse(url).path.split("/") if s]
        return host + "/" + "/".join(seg[:2])
    return host


# ----------------------------------------------------------------------------- 校验
def load_cfg(res):
    """从响应里解析出 TVBox 配置对象，返回 (cfg, err)。"""
    if res["status"] != 200:
        return None, res["err"] or ("HTTP %s" % res["status"])
    if not res["body"]:
        return None, "空响应"
    text = decode(res["body"])
    low = text[:600].lower()
    if low.lstrip().startswith("<"):
        return None, "返回的是 HTML（不是配置）"
    try:
        cfg = json.loads(text)
    except Exception:
        # 常见脏数据：结尾多余逗号
        try:
            cfg = json.loads(re.sub(r",\s*([}\]])", r"\1", text))
        except Exception:
            return None, "JSON 解析失败"
    if not isinstance(cfg, dict):
        return None, "不是 JSON 对象"
    return cfg, None


def check_asset(url, base):
    """检查 jar / js 等依赖是否可下载。返回 (ok, note)。"""
    if not url:
        return True, ""
    path, _suffix = split_asset(url)
    full = path if path.startswith("http") else rel_join(base, path)
    res = http_get(full, timeout=20, max_bytes=64 * 1024,
                   headers={"Range": "bytes=0-65535"}, retries=0)
    if res["status"] in (200, 206) and len(res["body"]) > 2000:
        return True, ""
    # 直连失败时试镜像
    if is_github(full):
        res2 = http_get(mirrorize(full), timeout=25, max_bytes=64 * 1024,
                        headers={"Range": "bytes=0-65535"}, retries=0)
        if res2["status"] in (200, 206) and len(res2["body"]) > 2000:
            return True, "依赖需走镜像"
    return False, "依赖下载失败（%s）" % (res["err"] or "HTTP %s" % res["status"])


def analyze(src):
    url = src["url"]
    t0 = time.time()
    out = {
        "name": src.get("name") or host_of(url),
        "url": url,
        "prefer": int(src.get("prefer", 0)),
        "discovered": bool(src.get("discovered")),
        "alive": False, "kind": "?", "sites": 0, "parses": 0, "lives": 0,
        "spider": None, "reason": "", "note": "", "final_url": url,
        "ms": 0, "cfg": None,
    }
    res = http_get(url)
    if res["status"] != 200 and is_github(url):
        res = http_get(mirrorize(url))
        if res["status"] == 200:
            out["note"] = "直连失败，已改走镜像"
    out["ms"] = int((time.time() - t0) * 1000)
    out["final_url"] = res.get("url") or url

    cfg, err = load_cfg(res)
    if cfg is None:
        out["reason"] = err
        return out

    urls = cfg.get("urls")
    sites = cfg.get("sites")

    if isinstance(urls, list) and urls:
        out["kind"] = "multi"
        subs = []
        for it in urls:
            u = it.get("url") if isinstance(it, dict) else it
            if isinstance(u, str) and u.startswith("http"):
                subs.append((it.get("name") if isinstance(it, dict) else None, u))
        if not subs:
            out["reason"] = "多仓里没有有效子地址"
            return out
        out["sites"] = len(subs)
        # 抽查前 3 个子仓
        ok_sub = 0
        for name, u in subs[:3]:
            r = http_get(u)
            if r["status"] != 200 and is_github(u):
                r = http_get(mirrorize(u))
            if r["status"] == 200:
                sub_cfg, _ = load_cfg(r)
                if sub_cfg and (sub_cfg.get("sites") or sub_cfg.get("urls")):
                    ok_sub += 1
        if ok_sub == 0:
            out["reason"] = "子仓全部不可用"
            return out
        out["alive"] = True
        out["note"] = (out["note"] + "；" if out["note"] else "") + "抽查 %d/%d 子仓可用" % (ok_sub, min(3, len(subs)))
        return out

    if isinstance(sites, list) and sites:
        out["kind"] = "single"
        out["sites"] = len(sites)
        out["parses"] = len(cfg.get("parses") or [])
        out["lives"] = len(cfg.get("lives") or [])
        if out["sites"] < 3:
            out["reason"] = "站点数过少（%d）" % out["sites"]
            return out
        spider = cfg.get("spider")
        if isinstance(spider, str) and spider:
            out["spider"] = spider if spider.startswith("http") else rel_join(
                out["final_url"], spider)
            ok, note = check_asset(spider, out["final_url"])
            if not ok:
                out["reason"] = note
                return out
            if note:
                out["note"] = (out["note"] + "；" if out["note"] else "") + note
        # 抽查解析 js（最多 2 个）
        bad_js = 0
        for p in (cfg.get("parses") or [])[:2]:
            u = p.get("url") if isinstance(p, dict) else None
            if isinstance(u, str) and u.endswith(".js"):
                ok, _ = check_asset(u, out["final_url"])
                if not ok:
                    bad_js += 1
        if bad_js:
            out["note"] = (out["note"] + "；" if out["note"] else "") + "%d 个解析 js 不可达" % bad_js
        out["alive"] = True
        out["cfg"] = cfg
        return out

    out["reason"] = "既无 sites 也无 urls"
    return out


# ----------------------------------------------------------------------------- 自动发现
def discover():
    d = CFG.get("discover") or {}
    found = []
    for repo in d.get("repos") or []:
        res = http_get("https://api.github.com/repos/%s/contents/" % repo.strip("/"))
        if res["status"] != 200:
            print("  [发现] %s 列表失败: %s" % (repo, res["err"]))
            continue
        try:
            items = json.loads(decode(res["body"]))
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for f in items:
            if not isinstance(f, dict) or f.get("type") != "file":
                continue
            n = f.get("name") or ""
            if not n.lower().endswith(".json"):
                continue
            if n.lower() in ("package.json", "composer.json", "tsconfig.json", "manifest.json"):
                continue
            if (f.get("size") or 0) > 900_000:
                continue
            u = f.get("download_url")
            if u and u.startswith("http"):
                found.append({"name": "%s/%s" % (repo.split("/")[-1], n), "url": u,
                              "prefer": 10, "discovered": True})

    rx = re.compile(r"https?://raw\.githubusercontent\.com/[A-Za-z0-9_\-\u4e00-\u9fa5./]+\.json")
    for repo in d.get("readmes") or []:
        for branch in ("HEAD", "main", "master"):
            res = http_get("https://raw.githubusercontent.com/%s/%s/README.md" % (repo.strip("/"), branch))
            if res["status"] != 200:
                continue
            txt = decode(res["body"])
            for u in rx.findall(txt):
                found.append({"name": "readme:%s" % u.rsplit("/", 1)[-1], "url": u,
                              "prefer": 5, "discovered": True})
            break

    seen, uniq = set(), []
    for s in found:
        k = s["url"].rstrip("/").split("?")[0]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)
    return uniq


# ----------------------------------------------------------------------------- 直播
def build_live():
    """抓直播源，同时产出：
    - live.m3u  标准 m3u（每条流一行，兼容性好）
    - live.txt  TVBox 原生格式，同一频道多线路用 # 连接，卡了能直接切线路
    """
    m3u_lines, seen_url, src_stat = ["#EXTM3U"], set(), []
    chans, order = {}, []

    for item in CFG.get("live") or []:
        url = item["url"]
        res = http_get(mirrorize(url) if is_github(url) else url, timeout=30)
        if res["status"] != 200:
            res = http_get(url, timeout=30)
        if res["status"] != 200:
            src_stat.append((item["name"], 0, "拉取失败：%s" % (res["err"] or "")))
            continue
        cnt = 0
        pending = None
        for raw in decode(res["body"]).splitlines():
            ln = raw.strip()
            if not ln:
                continue
            if ln.upper().startswith("#EXTINF"):
                pending = ln
                continue
            if ln.startswith("#"):
                continue
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", ln):
                continue
            if ln in seen_url:
                pending = None
                continue
            seen_url.add(ln)
            cnt += 1

            meta = pending or ""
            name = meta.rsplit(",", 1)[-1].strip() if "," in meta else "未命名"
            g = re.search(r'group-title="([^"]*)"', meta)
            group = (g.group(1).strip() if g else "") or "其它"

            m3u_lines.append(meta)
            m3u_lines.append(ln)

            # 频道归并键：去掉括号备注、清晰度后缀、空格，CCTV-1 与 CCTV1 视为同一台。
            # 只做确定性归一，不做模糊匹配，避免把 CCTV-1 和 CCTV-10 混成一个。
            norm = re.sub(r"\s+", "", name)
            norm = re.sub(r"[（(][^）)]*[）)]", "", norm)
            norm = re.sub(r"(?i)(高清|超清|标清|蓝光|FHD|UHD|HD|4K|8K|1080P|720P)$", "", norm)
            norm = re.sub(r"(?i)^(CCTV|CGTN)-?(\d+)$", lambda m: m.group(1).upper() + m.group(2), norm)
            key = norm.lower() or name

            if key not in chans:
                chans[key] = {"name": name, "group": group, "urls": []}
                order.append(key)
            if len(chans[key]["urls"]) < 6:
                chans[key]["urls"].append(ln)
            pending = None
        src_stat.append((item["name"], cnt, "ok" if cnt else "无有效频道"))

    (DOCS / "live.m3u").write_text("\n".join(m3u_lines) + "\n", encoding="utf-8")

    txt, cur = [], None
    for key in order:
        c = chans[key]
        if c["group"] != cur:
            txt.append("%s,#genre#," % c["group"])
            cur = c["group"]
        txt.append("%s,%s" % (c["name"], "#".join(c["urls"])))
    (DOCS / "live.txt").write_text("\n".join(txt) + "\n", encoding="utf-8")

    multi = sum(1 for k in order if len(chans[k]["urls"]) > 1)
    return src_stat, len(seen_url), len(order), multi


# ----------------------------------------------------------------------------- 合并单仓
def build_merged(alive):
    """把「共用同一个 spider jar」的多个源合并成一个大单仓。

    同一个 jar 才能安全合并（站点直接拼接，不依赖 site 级 jar）。
    换源池因此变大：一部片能在合并后的所有站点之间切换。
    """
    groups = {}
    for r in alive:
        if r["kind"] != "single" or not r["cfg"] or not r["spider"]:
            continue
        groups.setdefault(r["spider"], []).append(r)

    def uniq_sites(members):
        """按 api 去重后真正能用的站点数——多份配置常常大面积重叠，只看总和会高估。"""
        seen = set()
        for m in members:
            for s in (m["cfg"].get("sites") or []):
                if isinstance(s, dict):
                    seen.add(s.get("api") or s.get("key"))
        return len(seen)

    best = None
    for sp, members in groups.items():
        if len(members) < 2:
            continue
        u = uniq_sites(members)
        if u < 100:
            continue
        score = (u, max(m["prefer"] for m in members))
        if best is None or score > best[0]:
            best = (score, members)
    if not best:
        return None

    members = best[1]
    members.sort(key=lambda r: -r["prefer"])
    base = members[0]
    cfg = json.loads(json.dumps(base["cfg"], ensure_ascii=False))

    # 统一重建：所有成员的站点都走同一套去重（含基础配置自身的重复项）
    sites, used_keys, used_api = [], set(), set()
    merged_from = [base["name"]]
    for idx, m in enumerate(members):
        before = len(sites)
        for s in (m["cfg"].get("sites") or []):
            if not isinstance(s, dict):
                continue
            api = s.get("api") or ""
            dedup = api or ("key::%s" % s.get("key"))
            if dedup in used_api:
                continue
            used_api.add(dedup)
            ns = dict(s)
            k = ns.get("key") or "site"
            if k in used_keys:
                i = 2
                while "%s-%d" % (k, i) in used_keys:
                    i += 1
                ns["key"] = "%s-%d" % (k, i)
            used_keys.add(ns["key"])
            sites.append(ns)
        if idx and len(sites) > before:
            merged_from.append(m["name"])

    if len(sites) <= len(base["cfg"].get("sites") or []):
        return None

    parses = [dict(p) for p in (cfg.get("parses") or []) if isinstance(p, dict)]
    p_seen = {(p.get("name"), p.get("url")) for p in parses}
    for m in members[1:]:
        for p in (m["cfg"].get("parses") or []):
            if not isinstance(p, dict):
                continue
            sig = (p.get("name"), p.get("url"))
            if sig in p_seen:
                continue
            p_seen.add(sig)
            parses.append(dict(p))

    # 基础配置没有解析池时，从别的存活源借一份（解析 js 与 spider jar 无关，可安全借用）
    if not parses:
        donor = next((r for r in sorted(alive, key=lambda x: -x["prefer"])
                      if r["kind"] == "single" and (r["cfg"].get("parses"))), None)
        if donor:
            for p in donor["cfg"]["parses"]:
                if not isinstance(p, dict):
                    continue
                q = dict(p)
                if isinstance(q.get("url"), str):
                    path, suf = split_asset(q["url"])
                    q["url"] = (mirrorize(path) if path.startswith("http")
                                else mirrorize(rel_join(donor["final_url"], path))) + suf
                parses.append(q)
            print("  解析池借用：%s（%d 个）" % (donor["name"], len(parses)))

    lives = [dict(l) for l in (cfg.get("lives") or []) if isinstance(l, dict)]
    live_urls = {l.get("url") for l in lives}
    for m in members[1:]:
        for l in (m["cfg"].get("lives") or []):
            if not isinstance(l, dict) or l.get("url") in live_urls:
                continue
            lives.append(dict(l))
            live_urls.add(l.get("url"))

    cfg["sites"] = sites
    if parses:
        cfg["parses"] = parses
    if lives:
        cfg["lives"] = lives
    cfg = absolutize(cfg, base["final_url"])
    cfg["name"] = "合并大单仓 · %d 站点" % len(sites)
    (DOCS / "merged.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  merged.json：%d 站点（%s）" % (len(sites), " + ".join(merged_from)))
    return {"sites": len(sites), "from": merged_from}


# ----------------------------------------------------------------------------- best.json
def absolutize(cfg, base):
    """把相对路径的 spider / jar / 解析 js 改成绝对地址并套镜像，便于单独托管。"""
    def fix(u):
        if not isinstance(u, str) or not u or u.startswith("data:"):
            return u
        path, suffix = split_asset(u)
        full = path if path.startswith("http") else rel_join(base, path)
        return mirrorize(full) + suffix

    if isinstance(cfg.get("spider"), str):
        cfg["spider"] = fix(cfg["spider"])
    for s in cfg.get("sites") or []:
        if isinstance(s, dict):
            for k in ("jar", "spider"):
                if s.get(k):
                    s[k] = fix(s[k])
    for p in cfg.get("parses") or []:
        if isinstance(p, dict) and p.get("url"):
            p["url"] = fix(p["url"])
    for k in ("wallpaper", "logo"):
        if isinstance(cfg.get(k), str):
            cfg[k] = fix(cfg[k])
    return cfg


# ----------------------------------------------------------------------------- 主流程
def main():
    DOCS.mkdir(parents=True, exist_ok=True)
    now = datetime.now(BJT)
    stamp = now.strftime("%Y-%m-%d %H:%M")

    cands = list(CFG.get("sources") or [])
    print("[1/4] 发现新源 ...")
    try:
        new = discover()
    except Exception as e:
        print("  发现异常：", e)
        new = []
    known = {c["url"].rstrip("/") for c in cands}
    added = [n for n in new if n["url"].rstrip("/") not in known]
    cap = int((CFG.get("discover") or {}).get("max_new", 6))
    added = added[:cap]
    print("  新发现 %d 个（采用 %d）" % (len(new), len(added)))

    print("[2/4] 实测 %d 个源 ..." % (len(cands) + len(added)))
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(analyze, cands + added))

    alive = [r for r in results if r["alive"]]
    alive.sort(key=lambda r: (-r["prefer"], -r["sites"]))
    print("  存活 %d / %d" % (len(alive), len(results)))

    # 单源去重 + 每域名配额
    picked, per_host, seen_sig = [], {}, set()
    for r in alive:
        h = quota_key(r["url"])
        if per_host.get(h, 0) >= MAX_PER_HOST:
            continue
        sig = "%s|%s" % (r["kind"], r["sites"])
        if r["kind"] == "single":
            sig = "%s|%s|%s" % (r["kind"], r["sites"], r["spider"])
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        per_host[h] = per_host.get(h, 0) + 1
        picked.append(r)
        if len(picked) >= MAX_SOURCES:
            break

    if not picked:
        msg = "全部候选源均不可用，已保留上一版产物（%s）" % stamp
        print("!!", msg)
        (DOCS / "status.md").write_text("# TVBox 订阅体检\n\n> %s\n\n**%s**\n" % (stamp, msg),
                                        encoding="utf-8")
        return 1

    def entry(r, which=1):
        label = r["name"] + ("（%d站点）" % r["sites"] if r["kind"] == "single"
                             else "（%d仓）" % r["sites"])
        return {"url": mirrorize(r["url"], which), "name": label}

    repo = os.environ.get("GITHUB_REPOSITORY") or "OWNER/REPO"
    self_raw = "https://raw.githubusercontent.com/%s/main/docs/merged.json" % repo
    merged = build_merged(alive)
    m_name = "合并大单仓（%d站点）" % merged["sites"] if merged else ""

    def m_entry(which=1):
        return {"url": mirrorize(self_raw, which), "name": m_name}

    head1 = [m_entry(1)] if merged else []
    head2 = [m_entry(2)] if merged else []
    index = {"urls": head1 + [entry(r, 1) for r in picked]}
    index_alt = {"urls": head2 + [entry(r, 2) for r in picked]}
    allcfg = {"urls": head1 + [entry(r, 1) for r in alive]}
    raw_index = {"urls": ([{"url": self_raw, "name": m_name}] if merged else []) +
                          [{"url": r["url"], "name": entry(r)["name"]} for r in picked]}

    (DOCS / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS / "index-alt.json").write_text(json.dumps(index_alt, ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS / "all.json").write_text(json.dumps(allcfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS / "index-direct.json").write_text(json.dumps(raw_index, ensure_ascii=False, indent=2), encoding="utf-8")

    # 单仓直连（实验性）：把 spider / js 改成绝对地址，TV 端不用点选仓
    best = next((r for r in picked if r["kind"] == "single" and r["cfg"]), None)
    if best:
        b = absolutize(json.loads(json.dumps(best["cfg"], ensure_ascii=False)), best["final_url"])
        b["name"] = "TVBox 自动·" + best["name"]
        (DOCS / "best.json").write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  best.json 取自：%s" % best["name"])

    print("[3/4] 生成直播源 ...")
    live_stat, live_cnt, live_ch, live_multi = build_live()

    print("[4/4] 写体检报告 ...")
    health = {
        "updated": now.isoformat(),
        "checked": len(results),
        "alive": len(alive),
        "picked": len(picked),
        "merged": merged or {},
        "live_streams": live_cnt,
        "live_channels": live_ch,
        "live_multi_line": live_multi,
        "sources": [{k: r[k] for k in ("name", "url", "kind", "sites", "parses", "lives",
                                       "alive", "reason", "note", "ms", "discovered")}
                    for r in results],
    }
    (DOCS / "health.json").write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")

    def mark(r):
        return "🟢" if r["alive"] else "🔴"

    rows = sorted(results, key=lambda r: (not r["alive"], -r["prefer"]))
    md = ["# TVBox 订阅体检报告", "",
          "> 最近一次自动检测：**%s（北京时间）**　|　共测 %d 个源，存活 **%d** 个，已收录 **%d** 个"
          % (stamp, len(results), len(alive), len(picked)), "",
          "## 固定链接（电视端只填这一个，永不变）", "",
          "```",
          mirrorize("https://raw.githubusercontent.com/%s/%s/main/docs/index.json"
                    % (os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO").split("/")[0],
                       os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO").split("/")[-1])),
          "```", "",
          "备用镜像：见 README 的「一、电视端填这一个就够了」一节。", "",
          "## 各源状态", "",
          "| 状态 | 名称 | 类型 | 站点 | 解析 | 直播 | 耗时 | 说明 |",
          "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append("| %s | %s | %s | %d | %d | %d | %sms | %s |" % (
            mark(r), r["name"], {"single": "单仓", "multi": "多仓"}.get(r["kind"], "?"),
            r["sites"], r["parses"], r["lives"], r["ms"],
            (r["note"] or r["reason"] or "—").replace("|", "/")))
    md += ["", "## 直播源", "",
           "| 状态 | 名称 | 流数 | 备注 |", "|---|---|---|---|"]
    for name, cnt, note in live_stat:
        md.append("| %s | %s | %d | %s |" % ("🟢" if cnt else "🔴", name, cnt, note))
    md += ["", "合并后共 **%d** 个频道 / %d 条流，其中 **%d** 个频道有备用线路（live.txt 里用 # 连接）。"
           % (live_ch, live_cnt, live_multi)]
    if merged:
        md += ["", "## 合并单仓", "",
               "**%d 站点**，来自 %s（共用同一个 spider jar，合并安全）。"
               % (merged["sites"], " + ".join(merged["from"]))]
    md += ["", "由 GitHub Actions 每日自动生成，请勿手改。"]
    (DOCS / "status.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print("完成：收录 %d 个源，合并仓 %d 站点，直播 %d 频道 / %d 条流"
          % (len(picked), merged["sites"] if merged else 0, live_ch, live_cnt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
