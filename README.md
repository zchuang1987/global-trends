# Global Trends Collector / 全球渠道热搜

每天采集一次九个渠道的公开趋势信号，生成：

- 全球综合热搜：渠道内名次归一化后，聚合同一话题；跨渠道出现会加权。
- 正在爆发榜：和上一次采集比较名次、指数与渠道覆盖。
- 各渠道榜单：保留原标题、原始链接和平台指标。
- 数据源健康：明确区分官方公开源、代理来源、缓存和待配置来源。

## 九个渠道与数据源

| 渠道 | 默认来源 | 默认状态 | 说明 |
|---|---|---:|---|
| Google | Google Trends Trending Now RSS | 官方公开源 | 内部合并 9 个代表性区域，页面不再按国家展示 |
| TikTok | TikTok Creative Center | 官方页面，经 Jina 读取代理 | 未登录页面通常只公开榜首样本 |
| X / Twitter | Trends24 Worldwide | 公开代理 | 配置 `X_BEARER_TOKEN` 后自动切换到 X 官方 API Worldwide |
| Reddit | `r/popular` Hot Atom Feed | 官方公共 Feed | 热门帖子榜，不是关键词热搜 |
| YouTube | YouTube Data API `mostPopular` | API 或公开转发代理 | 配置 `YOUTUBE_API_KEY` 后使用官方 API；否则尝试 Trending2Day 转发 |
| Instagram | 用户配置的数据源 | 待配置 | Instagram 没有公开的全站排名热搜 API，不用其他平台内容冒充 |
| Bilibili | B站公开搜索热词接口 | 官方公开端点 | B站热搜关键词 |
| 百度 | 百度实时热搜榜页面 | 官方公开页面 | 解析页面内公开热榜数据 |
| 微博 | 微博 `hot_band` 网页接口 | 官方公开端点 | 微博实时热搜 |

## 本地运行

Windows PowerShell：

```powershell
.\run_daily.ps1
```

或直接：

```powershell
python collector.py
python collector.py --validate-only
```

生成文件：

- `index.html`：可离线打开的完整仪表盘。
- `data/latest.json`：最新结构化数据。
- `data/history/*.json`：每次采集快照。

## 可选密钥

密钥不是启动采集器的必要条件，但会提高渠道质量：

```text
X_BEARER_TOKEN          X API 的 Bearer Token
YOUTUBE_API_KEY         YouTube Data API v3 Key
INSTAGRAM_TRENDS_URL    合规 Instagram 数据服务的 JSON 地址
INSTAGRAM_TRENDS_TOKEN  上述服务需要时使用的 Bearer Token
```

`INSTAGRAM_TRENDS_URL` 返回格式可以是数组，也可以放在 `items`、`data` 或 `trends` 字段中。每项至少需要 `title`，可选 `url`、`metric`、`metric_text` 和 `image`。

## 云端每日运行

`.github/workflows/daily-trends.yml` 在 GitHub Actions 中每天 **08:07（Asia/Singapore）** 运行：

1. 采集九个渠道。
2. 校验数据质量。
3. 提交最新 JSON 与历史快照。
4. 发布到 GitHub Pages。

可选密钥请放在仓库的 **Settings → Secrets and variables → Actions → New repository secret**，不要写进代码。

也可以在仓库的 **Actions → Collect channel trends → Run workflow** 手动执行。

## 排名口径

- 渠道分：68% 名次、22% 公开指标、10% 多区域覆盖。
- 综合榜：以同一话题的最强渠道分为基础，跨渠道同时出现获得额外分。
- 爆发榜：比较上次采集；首次出现记为 `NEW`，上升记为 `↑N`。
- 不同平台没有统一的绝对搜索量，因此综合分是跨渠道注意力指数，不等同于真实搜索次数。

## 已知边界

- 平台可能改变页面结构、风控或公开范围；失败时会保留上次成功数据并显示“沿用上次”。
- X 官方趋势 API 为付费接口；未提供令牌时使用 Trends24，页面会明确标注。
- YouTube 已从单一 Trending 页转向分类 Charts；本项目采用 Data API 的 `mostPopular` 区域榜合并作为每日热门视频信号。
- Instagram 不提供公开的全站排名热搜，因此默认不会生成虚假的 Instagram 榜。
