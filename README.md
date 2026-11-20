# tvbox-live · TVBox 动态订阅

电视端只填**一个固定链接**，GitHub Actions 每天自动实测、淘汰失效源、补进新源。
源挂了、作者跑路了、换域名了，都在云端处理完，电视上永远不用改配置。

---

## 一、电视端填这一个就够了（多仓，推荐）

TVBox → 设置 → 配置地址，粘贴：

```
https://gh-proxy.com/https://raw.githubusercontent.com/729234500/tvbox-live/main/docs/index.json
```

进首页后会出现一个「仓」列表，挨个点开看哪个顺眼，随便换。**这个地址永远不变。**

**镜像一（备用，gh-proxy 抽风时用）**

```
https://ghfast.top/https://raw.githubusercontent.com/729234500/tvbox-live/main/docs/index-alt.json
```

**镜像二（jsDelivr，CDN 直连）**

```
https://cdn.jsdelivr.net/gh/729234500/tvbox-live@main/docs/index.json
```

> 三个是同一份数据的不同通道。国内电视盒子优先用第一个；哪个能打开用哪个。

**不想每次点选仓？**（实验性，直接用某一条配置，不用选）

```
https://gh-proxy.com/https://raw.githubusercontent.com/729234500/tvbox-live/main/docs/best.json
```

它会自动取当天排名第一的源，并把 jar / 解析 js 的地址改写成绝对地址，所以能独立加载。
缺点是它只有一家源，那家死了要等下次自动更新才切换——所以主用多仓那一条。

## 二、直播源（单独一个固定链接）

TVBox → 设置 → 直播源 / 我的-直播，填：

```
https://gh-proxy.com/https://raw.githubusercontent.com/729234500/tvbox-live/main/docs/live.m3u
```

每天把 fanmingming、iptv-org 等几个仓库的频道合并去重（当前约 600 个频道，含央视/卫视）。

## 三、每天发生了什么

```
03:30 定时触发
   ↓
读 sources.json 候选清单 + GitHub API 自动发现新源（扫仓库 json、扒 README 里的 raw 链接）
   ↓
并发实测每个源
   · HTTP 200 且不是 HTML 报错页
   · JSON 可解析（容忍多余逗号）
   · sites / urls 数量够
   · spider jar 能真下下来（自动处理 `路径;md5;哈希` 写法）
   · 解析用到的 js 可达
   ↓
只把「活着」的写进 docs/index.json，按 prefer 排序、每仓库限 4 条、总数上限 10
   ↓
commit 回仓库（本次无变化则不提交）
```

全部源都挂掉时**不会清空**：保留上一版产物并让 Action 失败（GitHub 会给你发邮件），避免电视端突然变白板。

## 四、体检报告

- 人看：<https://github.com/729234500/tvbox-live/blob/main/docs/status.md>
- 机器看：`docs/health.json`（每个源的站点数、耗时、失败原因）

状态含义：🟢 可用 / 🔴 当天实测不可用（通常第二天会被自动剔除或恢复）。

## 五、怎么改

| 想做的事 | 怎么改 |
|---|---|
| 加一个源 | 编辑 `sources.json` 的 `sources`，填 `name` / `url` / `prefer`（越大越靠前），commit 后自动重跑 |
| 删一个源 | 从 `sources` 里删掉即可 |
| 只留自己信得过的 | 把 `discover.repos` / `discover.readmes` 清空，自动发现就关了 |
| 改更新时间 | 改 `.github/workflows/update.yml` 里的 cron（UTC 时间，北京时间减 8 小时） |
| 改数量上限 | `sources.json` 的 `max_sources` / `max_per_host` |
| 换镜像 | `sources.json` 的 `mirror` / `mirror_backup` |

改完想立刻生效：Actions 页面点 **Run workflow**。

## 六、产物说明

| 文件 | 用途 |
|---|---|
| `docs/index.json` | **主用**，多仓，地址走 gh-proxy 镜像 |
| `docs/index-alt.json` | 同上，地址走 ghfast 镜像 |
| `docs/index-direct.json` | 同上，地址走 GitHub 原域名（海外/有代理时用） |
| `docs/best.json` | 单仓直连（实验性），spider 已改写为绝对地址 |
| `docs/all.json` | 全量存活源（不套上限，自己挑） |
| `docs/live.m3u` | 直播源聚合 |
| `docs/status.md` / `docs/health.json` | 体检报告 |

## 七、本地跑一遍

```bash
python3 scripts/build.py          # 无第三方依赖，标准库即可
```

## 八、已知限制

- **多仓里的子仓地址是别人的**，那些子仓内部若写死 `raw.githubusercontent.com`，在你网络下可能加载不出来。这是对方配置的问题，本仓库管不到；换一个仓试试。
- `best.json` 依赖对 spider / js 路径的改写，个别奇葩配置可能改写失败，所以定位为备用。
- 自动发现只扫 `sources.json` 里列的仓库，不会全网乱爬（防止误收来路不明的源）。
- GitHub Actions 免费额度对公开仓库不限量，每天一次几十秒，不会超额。

## 九、风险提示

这类接口本质是民间聚合，影视内容绝大多数未获授权，且第三方配置可能夹带广告、统计 SDK 甚至跳转。请自行判断使用场景与合规风险；不要在配置了来路不明接口的设备上登录账号或支付。直播请优先用运营商/官方 App 的正规源。

本项目只做「测活 + 聚合」，不托管、不转存任何影视内容。
