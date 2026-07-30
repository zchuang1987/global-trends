# World Signal / 全球热搜采集器

每天采集十个重点市场的热搜，生成：

- 十国每日 Top 10；
- 全球综合 Top 30；
- 正在爆发 Top 20；
- 数据源健康状态和历史快照。

## 默认观察市场

美国、中国、印度、巴西、墨西哥、英国、德国、日本、印度尼西亚、尼日利亚。

九个市场来自 Google Trends 的公开 Trending Now RSS；中国来自 Bilibili 公开热搜。所有数据先在来源内部归一化，避免把搜索量与平台热度原值直接相加。

## 手动更新

在 PowerShell 中运行：

```powershell
.\run_daily.ps1
```

完成后直接打开 `index.html`。页面是单文件自包含仪表盘，不需要启动服务器。

## 云端每日更新

仓库包含 `.github/workflows/daily-trends.yml`：

- 每天新加坡时间 08:07 运行；
- 也可以在 GitHub Actions 页面手动运行；
- 运行 `collector.py` 并验证三类榜单；
- 把 `index.html`、最新数据和历史快照提交回默认分支；
- 使用 GitHub Pages 发布最新仪表盘。

云端首跑成功后，电脑无需开机。若使用私有仓库，GitHub Pages 是否可用取决于账户套餐。

## 修改国家

编辑 `config.json` 中的 `countries`。Google Trends 国家使用两位地区代码，并将 `provider` 设为 `google_trends_rss`。

中国当前使用：

```json
{
  "code": "CN",
  "provider": "bilibili_trending"
}
```

## 多语言合并

`config.json` 的 `aliases` 可添加同一主题的多语言别名。程序还会自动合并高度相似的拼写变体。完全不同语言的实体名称需要加入别名表，避免错误合并。

## 文件说明

- `collector.py`：采集、归一化、聚类、评分和页面生成；
- `config.json`：国家、权重、榜单长度和多语言别名；
- `dashboard.template.html`：仪表盘模板；
- `index.html`：每次运行后生成的最新仪表盘；
- `data/latest.json`：最新结构化数据；
- `data/history/`：历史快照，用于计算爆发速度；
- `run_daily.ps1`：每日自动化入口。

## 失败处理

单一来源暂时失败时，采集器会保留该国家上一次成功数据，并在页面标记为“沿用”。如果从未成功采集过该国家，验证会失败，从而避免发布空榜。

## 评分口径

- 国家榜：各国源内名次 72% + 源内规模百分位 28%；
- 全球榜：在榜国家平均强度 25% + 最高本地强度 20% + 跨国覆盖 40% + 新鲜度 15%；
- 爆发榜：首次运行使用当前强度代理；后续使用综合分变化、名次跃升、新进入榜和跨国扩散。

榜单反映“相对热度信号”，不是跨平台绝对搜索量。
