# tvbox-live · TVBox 动态订阅

电视端只填**一个固定链接**，GitHub Actions 每天自动实测、淘汰失效源、补进新源。
源挂了、作者跑路了、换域名了，都在云端处理完，电视上永远不用改配置。

---

## 一、电视端填这一个就够了（多仓，推荐）

TVBox → 设置 → 配置地址，粘贴：

```
https://gh-proxy.com/https://raw.githubusercontent.com/729234500/tvbox-live/main/docs/index.json
```

进去会出现一个仓列表，**第一个是「合并大单仓」**（138 站点，由多个同 jar 的源合并而成，换源池最大），后面是各个独立源，随便挑。**这个地址永远不变。**

**镜像一（备用，gh-proxy 抽风时用）**

```
https://ghfast.top/https://raw.githubusercontent.com/729234500/tvbox-live/main/docs/index-alt.json
```

**镜像二（jsDelivr，CDN 直连）**

```
https://cdn.jsdelivr.net/gh/729234500/tvbox-live@main/docs/index.json
```

> 三个是同一份数据的不同通道。国内电视盒子优先用第一个；哪个能打开用哪个。

**不想每次点选仓？**（实验性）

```
https://gh-proxy.com/https://raw.githubusercontent.com/729234500/tvbox-live/main/docs/best.json
```

自动取当天排名第一的源，并把 jar / 解析 js 改成绝对地址，能独立加载。缺点是只有一家源，那家死了要等下次更新才切换——所以主用多仓那一条。

## 二、直播源（单独一个固定链接，带备用线路）

TVBox → 设置 → 直播源，填 **txt 这个**：

```
https://gh-proxy.com/https://raw.githubusercontent.com/729234500/tvbox-live/main/docs/live.txt
```

同一频道有多条线路时写成 `频道名,线路1#线路2#线路3`，**直播卡了直接在播放界面切线路**，不用换频道。目前 404 个频道里有 84 个带备用线路（卫视普遍 6 条）。

需要标准 m3u 的（部分播放器只认 m3u，但 m3u 无法表达多线路）：

```
https://gh-proxy.com/https://raw.githubusercontent.com/729234500/tvbox-live/main/docs/live.m3u
```

## 三、卡了怎么办：换源 vs 换线路

这两件事不一样，别搞混：

| 操作 | 在哪 | 效果 |
|---|---|---|
| **换线路（换源）** | 播放界面按【线路】/菜单键 | 片名不变，用配置内其它站点重新搜同一部片，进度尽量保留 |
| **换仓** | 设置里切另一个配置 | 整个首页片单都变，当前播放丢失 |

**能换多少条线路，取决于这个配置里有多少站点收录了这部片。** 这就是「合并大单仓」的意义：138 个站点共用一个 spider jar，一部片能在 138 个来源之间轮换。

关于「卡了自动切」——目前做不到，原因很具体：客户端的自动换源判断的是**播放器报错**（连不上、超时、解码失败），而「卡」是还在出画面只是缓冲慢，这种状态不报错，不触发切换。所以：

- 点了播放一直转圈最后报错 → 部分 fork（影视仓、takagen 系）在 **设置 → 播放设置** 里开「播放失败自动换源」能自动跳
- 能播但一直卡 → **只能手动换线路**

比自动换源更有效的是：播放页【设置】把内核从 Exo 换成 IJK（直播尤其明显），或降一档清晰度。

## 四、每天发生了什么

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
把共用同一个 jar 的源合并成 merged.json（换源池变大）
   ↓
合并直播源，同频道多线路写入 live.txt
   ↓
commit 回仓库（本次无变化则不提交）
```

全部源都挂掉时**不会清空**：保留上一版产物并让 Action 失败（GitHub 会发邮件），避免电视端突然变白板。

## 五、体检报告

- 人看：<https://github.com/729234500/tvbox-live/blob/main/docs/status.md>
- 机器看：`docs/health.json`（每个源的站点数、耗时、失败原因）

## 六、怎么改

| 想做的事 | 怎么改 |
|---|---|
| 加一个源 | 编辑 `sources.json` 的 `sources`，填 `name` / `url` / `prefer`（越大越靠前），commit 后自动重跑 |
| 删一个源 | 从 `sources` 里删掉即可 |
| 只留自己信得过的 | 把 `discover.repos` / `discover.readmes` 清空，自动发现就关了 |
| 改更新时间 | 改 `.github/workflows/update.yml` 里的 cron（UTC 时间，北京时间减 8 小时） |
| 改数量上限 | `sources.json` 的 `max_sources` / `max_per_host` |
| 换镜像 | `sources.json` 的 `mirror` / `mirror_backup` |

改完想立刻生效：Actions 页面点 **Run workflow**。

## 七、产物说明

| 文件 | 用途 |
|---|---|
| `docs/index.json` | **主用**，多仓，地址走 gh-proxy 镜像 |
| `docs/merged.json` | 合并大单仓（同 jar 的源合并，换源池最大），已排在 index 第一条 |
| `docs/index-alt.json` | 同 index，地址走 ghfast 镜像 |
| `docs/index-direct.json` | 同 index，地址走 GitHub 原域名（海外/有代理时用） |
| `docs/best.json` | 单仓直连（实验性），spider 已改写为绝对地址 |
| `docs/all.json` | 全量存活源（不套上限，自己挑） |
| `docs/live.txt` | 直播源，**带备用线路**，TVBox 原生格式 |
| `docs/live.m3u` | 直播源标准 m3u（每条流一行） |
| `docs/status.md` / `docs/health.json` | 体检报告 |

## 八、本地跑一遍

```bash
python3 scripts/build.py          # 无第三方依赖，标准库即可
```

## 九、已知限制

- **合并单仓只合并「共用同一个 spider jar」的源**。不同 jar 的站点混在一个配置里需要 site 级 `jar` 字段，部分旧版 TVBox 不支持，会大面积播放失败，所以没有强上。
- **多仓里的子仓地址是别人的**，对方内部若写死 `raw.githubusercontent.com`，在你这网络下可能加载不出来。那不是本仓库的问题，换个仓即可。
- `best.json` 依赖对 spider / js 路径的改写，个别奇葩配置可能改写失败，所以定位为备用。
- 直播频道归并只做确定性归一（去括号备注、去清晰度后缀、`CCTV-1` 与 `CCTV1` 视为同一台），不做模糊匹配，避免把 `CCTV-1` 和 `CCTV-10` 混成一个。所以仍有同名但不同写法的频道不会被合并。
- 自动发现只扫 `sources.json` 里列的仓库，不会全网乱爬（防止误收来路不明的源）。
- GitHub Actions 免费额度对公开仓库不限量，每天一次约 40 秒，不会超额。

## 十、风险提示

这类接口本质是民间聚合，影视内容绝大多数未获授权，且第三方配置可能夹带广告、统计 SDK 甚至跳转。请自行判断使用场景与合规风险；不要在配置了来路不明接口的设备上登录账号或支付。直播请优先用运营商/官方 App 的正规源。

本项目只做「测活 + 聚合」，不托管、不转存任何影视内容。
