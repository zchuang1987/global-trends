#!/usr/bin/env python3
"""Collect daily trend signals by platform channel and build the dashboard.

The collector has no third-party Python dependencies. Public sources are used
where a platform exposes them; proxy and credential-dependent sources are
explicitly identified in the generated dataset and dashboard.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
LATEST_PATH = DATA_DIR / "latest.json"
CONFIG_PATH = ROOT / "config.json"
TEMPLATE_PATH = ROOT / "dashboard.template.html"
INDEX_PATH = ROOT / "index.html"

HT_NS = "https://trends.google.com/trending/rss"
ATOM_NS = "http://www.w3.org/2005/Atom"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/126 Safari/537.36 "
    "WorldSignalCollector/2.0"
)


class NeedsConfiguration(RuntimeError):
    """Raised when a channel needs a user-supplied credential or provider."""


@dataclass
class ChannelResult:
    code: str
    items: list[dict[str, Any]]
    mode: str
    note: str = ""
    error: str = ""


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(text: str | None, fallback: datetime) -> datetime:
    if not text:
        return fallback
    try:
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return fallback


def parse_number(text: str | int | float | None) -> int:
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        return int(text)
    compact = str(text).strip().upper().replace(",", "").replace("+", "")
    match = re.search(r"([\d.]+)\s*([KMB]?)", compact)
    if not match:
        return 0
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(float(match.group(1)) * multiplier.get(match.group(2), 1))


def normalize_title(text: str) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(text)).casefold()
    value = "".join(
        char
        for char in value
        if unicodedata.category(char)[0] not in {"P", "S"} or char == "+"
    )
    return re.sub(r"\s+", " ", value).strip()


def decode_json_string(text: str) -> str:
    try:
        return json.loads(f'"{text}"')
    except json.JSONDecodeError:
        return text


def topic_id(canonical: str) -> str:
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def fetch_bytes(
    url: str,
    timeout: int,
    retries: int,
    headers: dict[str, str] | None = None,
) -> bytes:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/rss+xml, text/plain, text/html, */*",
        "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.7",
    }
    request_headers.update(headers or {})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"Failed after {retries} attempts: {last_error}")


def fetch_json(
    url: str,
    timeout: int,
    retries: int,
    headers: dict[str, str] | None = None,
) -> Any:
    return json.loads(fetch_bytes(url, timeout, retries, headers).decode("utf-8", "ignore"))


def base_item(
    title: str,
    channel: dict[str, Any],
    rank: int,
    now: datetime,
    source_url: str,
    metric_text: str = "",
    metric: int = 0,
    published_at: datetime | None = None,
    **extra: Any,
) -> dict[str, Any]:
    published = published_at or now
    return {
        "title": title.strip(),
        "canonical": normalize_title(title),
        "channel_code": channel["code"],
        "channel_name": channel["name"],
        "rank": rank,
        "metric_text": metric_text,
        "metric": metric,
        "published_at": iso_utc(published),
        "age_hours": round(max(0.0, (now - published).total_seconds() / 3600), 2),
        "source_url": source_url,
        **extra,
    }


def parse_google_rss(
    raw: bytes,
    channel: dict[str, Any],
    region: str,
    now: datetime,
) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    items: list[dict[str, Any]] = []
    for rank, node in enumerate(root.findall("./channel/item"), start=1):
        title = (node.findtext("title") or "").strip()
        if not title:
            continue
        traffic_text = (node.findtext(f"{{{HT_NS}}}approx_traffic") or "").strip()
        published = parse_datetime(node.findtext("pubDate"), now)
        picture = (node.findtext(f"{{{HT_NS}}}picture") or "").strip()
        query = urllib.parse.quote_plus(title)
        items.append(
            base_item(
                title,
                channel,
                rank,
                now,
                f"https://trends.google.com/trends/explore?geo={region}&q={query}",
                traffic_text,
                parse_number(traffic_text),
                published,
                region=region,
                image=picture,
            )
        )
    return items


def collect_google(
    channel: dict[str, Any],
    config: dict[str, Any],
    timeout: int,
    retries: int,
    now: datetime,
) -> ChannelResult:
    regions = config.get("google_regions", ["US"])

    def fetch_region(region: str) -> list[dict[str, Any]]:
        url = f"https://trends.google.com/trending/rss?geo={region}"
        return parse_google_rss(fetch_bytes(url, timeout, retries), channel, region, now)

    gathered: list[dict[str, Any]] = []
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(regions))) as pool:
        futures = {pool.submit(fetch_region, region): region for region in regions}
        for future in concurrent.futures.as_completed(futures):
            region = futures[future]
            try:
                gathered.extend(future.result())
            except Exception as exc:
                failures.append(f"{region}: {exc}")

    merged: dict[str, dict[str, Any]] = {}
    for item in gathered:
        key = item["canonical"]
        if not key:
            continue
        if key not in merged:
            merged[key] = {**item, "regions": [item["region"]], "region_count": 1}
        else:
            target = merged[key]
            if item["region"] not in target["regions"]:
                target["regions"].append(item["region"])
            target["region_count"] = len(target["regions"])
            target["rank"] = min(target["rank"], item["rank"])
            if item["metric"] > target["metric"]:
                target["metric"] = item["metric"]
                target["metric_text"] = item["metric_text"]
                target["source_url"] = item["source_url"]

    ranked = sorted(
        merged.values(),
        key=lambda item: (-item["region_count"], item["rank"], -item["metric"]),
    )
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    note = f"合并 {len(regions) - len(failures)}/{len(regions)} 个 Google Trends 区域榜"
    if failures:
        note += f"；{len(failures)} 个区域暂时失败"
    return ChannelResult(channel["code"], ranked, "official", note=note)


def collect_tiktok(
    channel: dict[str, Any],
    _config: dict[str, Any],
    timeout: int,
    retries: int,
    now: datetime,
) -> ChannelResult:
    official = (
        "https://ads.tiktok.com/business/creativecenter/hashtag/statistics/pc/en"
        "?countryCode=US&period=7"
    )
    mirror = f"https://r.jina.ai/{official}"
    text = fetch_bytes(mirror, timeout, retries).decode("utf-8", "ignore")
    pattern = re.compile(
        r"(?m)^(\d+)\s*\n\s*\n#([^\n]+)\s*\n\s*\n([^\n]+)"
        r"\s*\n\s*\n([\d.]+[KMB]?)\s+Posts"
        r"\s*\n\s*\n([\d.]+[KMB]?)\s+Views"
    )
    items: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        rank = int(match.group(1))
        tag = f"#{match.group(2).strip()}"
        posts_text = match.group(4)
        views_text = match.group(5)
        query = urllib.parse.quote(match.group(2).strip())
        items.append(
            base_item(
                tag,
                channel,
                rank,
                now,
                f"https://www.tiktok.com/tag/{query}",
                f"{posts_text} posts · {views_text} views",
                parse_number(views_text),
                category=match.group(3).strip(),
            )
        )
    if not items:
        raise RuntimeError("TikTok Creative Center returned no public hashtag rows")
    return ChannelResult(
        channel["code"],
        items,
        "official_via_fetch_proxy",
        note="TikTok Creative Center 7 日热门标签；未登录页面只公开榜首样本",
    )


def collect_x(
    channel: dict[str, Any],
    _config: dict[str, Any],
    timeout: int,
    retries: int,
    now: datetime,
) -> ChannelResult:
    bearer = os.getenv("X_BEARER_TOKEN", "").strip()
    if bearer:
        url = "https://api.x.com/2/trends/by/woeid/1?max_trends=50"
        payload = fetch_json(url, timeout, retries, {"Authorization": f"Bearer {bearer}"})
        rows = payload.get("data", [])
        items = []
        for rank, row in enumerate(rows, start=1):
            title = (row.get("trend_name") or row.get("name") or "").strip()
            if not title:
                continue
            count = parse_number(row.get("tweet_count"))
            items.append(
                base_item(
                    title,
                    channel,
                    rank,
                    now,
                    f"https://x.com/search?q={urllib.parse.quote_plus(title)}",
                    f"{count:,} posts" if count else "",
                    count,
                )
            )
        return ChannelResult(channel["code"], items, "official_api", note="X API Worldwide（WOEID 1）")

    text = fetch_bytes("https://trends24.in/", timeout, retries).decode("utf-8", "ignore")
    match = re.search(
        r"<ol[^>]*class=trend-card__list[^>]*>(.*?)</ol>",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise RuntimeError("Trends24 Worldwide list was not found")
    items = []
    link_pattern = re.compile(
        r'<a[^>]+href="([^"]+)"[^>]*class=trend-link[^>]*>(.*?)</a>'
        r'.*?<span[^>]*class=tweet-count[^>]*data-count="([^"]*)"',
        re.IGNORECASE | re.DOTALL,
    )
    for rank, row in enumerate(link_pattern.finditer(match.group(1)), start=1):
        title = html.unescape(re.sub(r"<[^>]+>", "", row.group(2))).strip()
        if title:
            count = parse_number(row.group(3))
            items.append(
                base_item(
                    title,
                    channel,
                    rank,
                    now,
                    html.unescape(row.group(1)),
                    f"{count:,} posts" if count else "",
                    count,
                )
            )
    if not items:
        raise RuntimeError("Trends24 Worldwide list contained no trends")
    return ChannelResult(
        channel["code"],
        items,
        "public_proxy",
        note="X 未配置付费 API 令牌；当前使用 Trends24 Worldwide 公开代理",
    )


def collect_reddit(
    channel: dict[str, Any],
    _config: dict[str, Any],
    timeout: int,
    retries: int,
    now: datetime,
) -> ChannelResult:
    url = "https://www.reddit.com/r/popular/hot/.rss"
    root = ET.fromstring(fetch_bytes(url, timeout, retries))
    namespace = {"a": ATOM_NS}
    items: list[dict[str, Any]] = []
    for rank, entry in enumerate(root.findall("a:entry", namespace), start=1):
        title = (entry.findtext("a:title", default="", namespaces=namespace) or "").strip()
        link_node = entry.find("a:link", namespace)
        link = link_node.attrib.get("href", "") if link_node is not None else ""
        updated = parse_datetime(
            entry.findtext("a:updated", default="", namespaces=namespace),
            now,
        )
        category = entry.find("a:category", namespace)
        subreddit = category.attrib.get("term", "") if category is not None else ""
        if title:
            items.append(
                base_item(
                    title,
                    channel,
                    rank,
                    now,
                    link,
                    subreddit,
                    0,
                    updated,
                    subreddit=subreddit,
                )
            )
    return ChannelResult(
        channel["code"],
        items,
        "official_public_feed",
        note="Reddit r/popular 的 Hot 公共 Atom feed；属于热门帖子榜，不是关键词热搜",
    )


def parse_youtube_rows(
    rows: list[dict[str, Any]],
    channel: dict[str, Any],
    region: str,
    now: datetime,
) -> list[dict[str, Any]]:
    items = []
    for rank, row in enumerate(rows, start=1):
        snippet = row.get("snippet", {})
        video_id = row.get("id", "")
        if isinstance(video_id, dict):
            video_id = video_id.get("videoId", "")
        title = (snippet.get("title") or "").strip()
        if not title or not video_id:
            continue
        views = parse_number(row.get("statistics", {}).get("viewCount"))
        thumbs = snippet.get("thumbnails", {})
        image = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
        published = parse_datetime(snippet.get("publishedAt"), now)
        items.append(
            base_item(
                title,
                channel,
                rank,
                now,
                f"https://www.youtube.com/watch?v={video_id}",
                f"{views:,} views" if views else "",
                views,
                published,
                region=region,
                video_id=video_id,
                channel_title=snippet.get("channelTitle", ""),
                image=image,
            )
        )
    return items


def collect_youtube(
    channel: dict[str, Any],
    config: dict[str, Any],
    timeout: int,
    retries: int,
    now: datetime,
) -> ChannelResult:
    regions = config.get("youtube_regions", ["US"])
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()

    def fetch_region(region: str) -> list[dict[str, Any]]:
        params = {
            "part": "snippet,contentDetails,statistics,status",
            "chart": "mostPopular",
            "maxResults": "24",
            "regionCode": region,
        }
        if api_key:
            params["key"] = api_key
            url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params)
            payload = fetch_json(url, timeout, retries)
        else:
            url = "https://trending2day.com/proxy.php?" + urllib.parse.urlencode(params)
            payload = fetch_json(
                url,
                timeout,
                retries,
                {"Referer": "https://trending2day.com/"},
            )
        return parse_youtube_rows(payload.get("items", []), channel, region, now)

    gathered: list[dict[str, Any]] = []
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(regions))) as pool:
        futures = {pool.submit(fetch_region, region): region for region in regions}
        for future in concurrent.futures.as_completed(futures):
            region = futures[future]
            try:
                gathered.extend(future.result())
            except Exception as exc:
                failures.append(f"{region}: {exc}")
    if not gathered:
        raise RuntimeError("All YouTube regions failed: " + " | ".join(failures[:3]))

    merged: dict[str, dict[str, Any]] = {}
    for item in gathered:
        key = item["video_id"]
        if key not in merged:
            merged[key] = {**item, "regions": [item["region"]], "region_count": 1}
        else:
            target = merged[key]
            if item["region"] not in target["regions"]:
                target["regions"].append(item["region"])
            target["region_count"] = len(target["regions"])
            target["rank"] = min(target["rank"], item["rank"])
            target["metric"] = max(target["metric"], item["metric"])
            target["metric_text"] = f"{target['metric']:,} views"
    ranked = sorted(
        merged.values(),
        key=lambda item: (-item["region_count"], item["rank"], -item["metric"]),
    )
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    mode = "official_api" if api_key else "public_api_proxy"
    note = (
        f"合并 {len(regions) - len(failures)}/{len(regions)} 个 YouTube mostPopular 区域榜；"
        + ("使用 YouTube Data API" if api_key else "无 API key，使用 Trending2Day 转发的 YouTube Data API")
    )
    return ChannelResult(channel["code"], ranked, mode, note=note)


def collect_instagram(
    channel: dict[str, Any],
    _config: dict[str, Any],
    timeout: int,
    retries: int,
    now: datetime,
) -> ChannelResult:
    provider_url = os.getenv("INSTAGRAM_TRENDS_URL", "").strip()
    if not provider_url:
        raise NeedsConfiguration(
            "Instagram 没有公开的全站排名热搜 API；如购买合规数据源，可设置 "
            "INSTAGRAM_TRENDS_URL（返回 title/url/metric 的 JSON 列表）"
        )
    headers: dict[str, str] = {}
    token = os.getenv("INSTAGRAM_TRENDS_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = fetch_json(provider_url, timeout, retries, headers)
    rows = payload if isinstance(payload, list) else (
        payload.get("items") or payload.get("data") or payload.get("trends") or []
    )
    items: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        if isinstance(row, str):
            row = {"title": row}
        title = str(row.get("title") or row.get("name") or row.get("hashtag") or "").strip()
        if not title:
            continue
        if row.get("hashtag") and not title.startswith("#"):
            title = f"#{title}"
        items.append(
            base_item(
                title,
                channel,
                rank,
                now,
                row.get("url") or f"https://www.instagram.com/explore/search/keyword/?q={urllib.parse.quote_plus(title)}",
                str(row.get("metric_text") or ""),
                parse_number(row.get("metric") or row.get("views") or row.get("posts")),
                image=row.get("image") or "",
            )
        )
    if not items:
        raise RuntimeError("Configured Instagram provider returned no trend rows")
    return ChannelResult(channel["code"], items, "configured_provider", note="用户配置的 Instagram 数据源")


def collect_bilibili(
    channel: dict[str, Any],
    _config: dict[str, Any],
    timeout: int,
    retries: int,
    now: datetime,
) -> ChannelResult:
    url = "https://api.bilibili.com/x/web-interface/search/square?limit=50"
    payload = fetch_json(url, timeout, retries)
    rows = payload.get("data", {}).get("trending", {}).get("list", [])
    items = []
    for rank, row in enumerate(rows, start=1):
        title = (row.get("show_name") or row.get("keyword") or "").strip()
        keyword = (row.get("keyword") or title).strip()
        if title:
            items.append(
                base_item(
                    title,
                    channel,
                    rank,
                    now,
                    f"https://search.bilibili.com/all?keyword={urllib.parse.quote_plus(keyword)}",
                    "",
                    0,
                    image=row.get("icon") or "",
                )
            )
    return ChannelResult(channel["code"], items, "official_public_endpoint", note="B站公开搜索热词接口")


def collect_baidu(
    channel: dict[str, Any],
    _config: dict[str, Any],
    timeout: int,
    retries: int,
    now: datetime,
) -> ChannelResult:
    url = "https://top.baidu.com/board?tab=realtime"
    text = fetch_bytes(url, timeout, retries).decode("utf-8", "ignore")
    pattern = re.compile(
        r'"hotScore":"?(\d+)"?.{0,900}?"query":"((?:\\.|[^"])*)"'
        r'.{0,900}?"rawUrl":"((?:\\.|[^"])*)"',
        re.DOTALL,
    )
    items = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        title = decode_json_string(match.group(2)).strip()
        if not title or title in seen:
            continue
        seen.add(title)
        score = parse_number(match.group(1))
        source_url = decode_json_string(match.group(3))
        items.append(
            base_item(
                title,
                channel,
                len(items) + 1,
                now,
                source_url,
                f"{score:,} 热度",
                score,
            )
        )
    if not items:
        raise RuntimeError("Baidu hot-board payload was not found")
    return ChannelResult(channel["code"], items, "official_public_page", note="百度实时热搜榜公开页面")


def collect_weibo(
    channel: dict[str, Any],
    _config: dict[str, Any],
    timeout: int,
    retries: int,
    now: datetime,
) -> ChannelResult:
    url = "https://weibo.com/ajax/statuses/hot_band"
    payload = fetch_json(
        url,
        timeout,
        retries,
        {"Referer": "https://weibo.com/", "Accept": "application/json"},
    )
    rows = payload.get("data", {}).get("band_list", [])
    items = []
    for row in sorted(rows, key=lambda value: value.get("realpos", 999)):
        title = (row.get("word") or row.get("note") or "").strip()
        if not title:
            continue
        rank = parse_number(row.get("realpos")) or len(items) + 1
        score = parse_number(row.get("num"))
        items.append(
            base_item(
                title,
                channel,
                rank,
                now,
                f"https://s.weibo.com/weibo?q={urllib.parse.quote_plus(title)}",
                f"{score:,} 热度" if score else row.get("label_name", ""),
                score,
                category=row.get("category", ""),
                label=row.get("label_name", ""),
            )
        )
    if not items:
        raise RuntimeError("Weibo hot-band API returned no rows")
    return ChannelResult(channel["code"], items, "official_public_endpoint", note="微博公开网页使用的 hot-band 接口")


COLLECTORS = {
    "google_trends": collect_google,
    "tiktok_creative_center": collect_tiktok,
    "x_trends": collect_x,
    "reddit_popular": collect_reddit,
    "youtube_popular": collect_youtube,
    "instagram_trends": collect_instagram,
    "bilibili_trending": collect_bilibili,
    "baidu_hot": collect_baidu,
    "weibo_hot": collect_weibo,
}


def collect_channel(
    channel: dict[str, Any],
    config: dict[str, Any],
    timeout: int,
    retries: int,
    now: datetime,
) -> ChannelResult:
    provider = channel["provider"]
    try:
        collector = COLLECTORS[provider]
        return collector(channel, config, timeout, retries, now)
    except NeedsConfiguration as exc:
        return ChannelResult(channel["code"], [], "needs_configuration", note=str(exc))
    except Exception as exc:
        return ChannelResult(
            channel["code"],
            [],
            "error",
            error=f"{type(exc).__name__}: {exc}",
        )


def apply_local_scores(items: list[dict[str, Any]]) -> None:
    if not items:
        return
    max_metric = max(item.get("metric", 0) for item in items)
    denominator = max(1, len(items) - 1)
    for item in items:
        rank_score = 1 - ((item["rank"] - 1) / denominator)
        metric = item.get("metric", 0)
        metric_score = (
            math.log1p(metric) / math.log1p(max_metric)
            if max_metric > 0 and metric > 0
            else rank_score
        )
        breadth_score = min(1.0, item.get("region_count", 1) / 3)
        item["local_score"] = round(
            100 * (0.68 * rank_score + 0.22 * metric_score + 0.10 * breadth_score),
            1,
        )


def build_alias_lookup(config: dict[str, Any]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for preferred, aliases in config.get("aliases", {}).items():
        canonical = normalize_title(preferred)
        lookup[canonical] = canonical
        for alias in aliases:
            lookup[normalize_title(alias)] = canonical
    return lookup


def fuzzy_match(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) < 8:
        return False
    if not (left.isascii() and right.isascii()):
        return False
    short, long = sorted((left, right), key=len)
    generic = {
        "official trailer",
        "official video",
        "music video",
        "trailer",
        "video",
        "lyrics",
    }
    if (
        len(short) >= 7
        and short not in generic
        and re.search(rf"\b{re.escape(short)}\b", long)
    ):
        return True
    left_tokens, right_tokens = set(left.split()), set(right.split())
    overlap = (
        len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        if left_tokens and right_tokens
        else 0
    )
    return overlap >= 0.75 and SequenceMatcher(None, left, right).ratio() >= 0.93


def build_global_topics(
    channel_items: dict[str, list[dict[str, Any]]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    alias_lookup = build_alias_lookup(config)
    channels_by_code = {channel["code"]: channel for channel in config["channels"]}
    clusters: list[dict[str, Any]] = []
    for channel in config["channels"]:
        for item in channel_items.get(channel["code"], []):
            canonical = alias_lookup.get(item["canonical"], item["canonical"])
            if not canonical:
                continue
            cluster = next(
                (
                    candidate
                    for candidate in clusters
                    if candidate["canonical"] == canonical
                    or fuzzy_match(candidate["canonical"], canonical)
                ),
                None,
            )
            if cluster is None:
                cluster = {"canonical": canonical, "signals": []}
                clusters.append(cluster)
            cluster["signals"].append(item)

    topics: list[dict[str, Any]] = []
    for cluster in clusters:
        signals = cluster["signals"]
        channel_codes = sorted({signal["channel_code"] for signal in signals})
        representative = max(
            signals,
            key=lambda signal: (
                signal.get("local_score", 0)
                * channels_by_code[signal["channel_code"]].get("weight", 1),
                signal.get("metric", 0),
            ),
        )
        weighted_scores = [
            signal.get("local_score", 0)
            * channels_by_code[signal["channel_code"]].get("weight", 1)
            for signal in signals
        ]
        raw_score = max(weighted_scores) + 13 * (len(channel_codes) - 1)
        topics.append(
            {
                "id": topic_id(cluster["canonical"]),
                "canonical": cluster["canonical"],
                "title": representative["title"],
                "raw_score": round(raw_score, 2),
                "channel_count": len(channel_codes),
                "channels": channel_codes,
                "signals": [
                    {
                        "channel_code": signal["channel_code"],
                        "channel_name": signal["channel_name"],
                        "rank": signal["rank"],
                        "metric_text": signal.get("metric_text", ""),
                        "source_url": signal.get("source_url", ""),
                    }
                    for signal in sorted(
                        signals,
                        key=lambda signal: (-signal.get("local_score", 0), signal["rank"]),
                    )
                ],
            }
        )

    topics.sort(key=lambda topic: (-topic["channel_count"], -topic["raw_score"], topic["title"]))
    max_raw = max((topic["raw_score"] for topic in topics), default=1)
    for rank, topic in enumerate(topics, start=1):
        topic["rank"] = rank
        topic["score"] = round(100 * topic["raw_score"] / max_raw, 1)
        del topic["raw_score"]
    return topics


def build_breakouts(
    topics: list[dict[str, Any]],
    previous: dict[str, Any],
) -> list[dict[str, Any]]:
    previous_topics = previous.get("rankings", {}).get("global", [])
    previous_lookup = {
        topic.get("canonical") or normalize_title(topic.get("title", "")): topic
        for topic in previous_topics
    }
    breakouts = []
    for topic in topics:
        prior = previous_lookup.get(topic["canonical"])
        if prior:
            rise = int(prior.get("rank", topic["rank"])) - topic["rank"]
            score_delta = topic["score"] - float(prior.get("score", topic["score"]))
            coverage_delta = topic["channel_count"] - int(prior.get("channel_count", 1))
            breakout_score = 4 * max(0, rise) + 10 * max(0, coverage_delta) + max(0, score_delta)
            if breakout_score <= 0:
                continue
            movement = f"↑{rise}" if rise > 0 else "加速"
        else:
            breakout_score = topic["score"] * 0.62 + 8 * max(0, topic["channel_count"] - 1)
            rise = None
            score_delta = None
            coverage_delta = None
            movement = "NEW"
        breakouts.append(
            {
                **topic,
                "breakout_score": round(breakout_score, 1),
                "movement": movement,
                "rise": rise,
                "score_delta": score_delta,
                "coverage_delta": coverage_delta,
            }
        )
    breakouts.sort(key=lambda topic: (-topic["breakout_score"], topic["rank"]))
    for rank, topic in enumerate(breakouts, start=1):
        topic["breakout_rank"] = rank
    return breakouts


def build_dataset(config: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    timeout = int(config.get("request_timeout_seconds", 35))
    retries = int(config.get("request_retries", 2))
    top_n = int(config.get("top_n_channel", 20))
    previous_v2 = previous if previous.get("schema_version") == 2 else {}
    previous_channels = previous_v2.get("channel_items", {})

    results: dict[str, ChannelResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(config["channels"])) as pool:
        futures = {
            pool.submit(collect_channel, channel, config, timeout, retries, now): channel
            for channel in config["channels"]
        }
        for future in concurrent.futures.as_completed(futures):
            channel = futures[future]
            results[channel["code"]] = future.result()

    channel_items: dict[str, list[dict[str, Any]]] = {}
    health: list[dict[str, Any]] = []
    for channel in config["channels"]:
        result = results[channel["code"]]
        items = result.items
        status = "ok"
        mode = result.mode
        note = result.note
        error = result.error
        if not items and result.mode == "needs_configuration":
            status = "needs_configuration"
        elif not items:
            cached = previous_channels.get(channel["code"], [])
            if cached:
                items = cached
                status = "stale"
                mode = "cached"
                note = "本次采集失败，沿用上次成功数据"
            else:
                status = "error"
        items = sorted(items, key=lambda item: item.get("rank", 999))[:top_n]
        for rank, item in enumerate(items, start=1):
            item["rank"] = rank
        apply_local_scores(items)
        channel_items[channel["code"]] = items
        health.append(
            {
                "code": channel["code"],
                "name": channel["name"],
                "label": channel["label"],
                "mark": channel["mark"],
                "official": channel.get("official", False),
                "source_url": channel["source_url"],
                "status": status,
                "mode": mode,
                "item_count": len(items),
                "note": note,
                "error": error,
            }
        )

    global_topics = build_global_topics(channel_items, config)
    breakouts = build_breakouts(global_topics, previous_v2)
    active = sum(1 for row in health if row["status"] in {"ok", "stale"})
    return {
        "schema_version": 2,
        "generated_at": iso_utc(now),
        "timezone": config.get("timezone", "Asia/Singapore"),
        "title": config.get("dashboard_title", "World Signal"),
        "summary": {
            "channels_total": len(config["channels"]),
            "channels_active": active,
            "signals_total": sum(len(items) for items in channel_items.values()),
            "cross_channel_topics": sum(1 for topic in global_topics if topic["channel_count"] > 1),
        },
        "health": health,
        "channel_items": channel_items,
        "rankings": {
            "global": global_topics[: int(config.get("top_n_global", 30))],
            "breakout": breakouts[: int(config.get("top_n_breakout", 20))],
        },
        "methodology": {
            "global": "先把每个渠道名次归一化，再对同名/高相似话题聚合；跨渠道出现会获得额外权重。",
            "breakout": "与上一次采集比较名次、综合分和渠道覆盖；首次出现的话题标记为 NEW。",
            "caveat": "不同平台并不存在统一的“全球热搜”口径；综合榜是跨渠道信号指数，不等于绝对搜索量。",
        },
    }


def render_dashboard(dataset: dict[str, Any]) -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(dataset, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    if "__DATA_JSON__" not in template:
        raise RuntimeError("dashboard.template.html is missing __DATA_JSON__")
    atomic_write(INDEX_PATH, template.replace("__DATA_JSON__", payload))


def validate_dataset(dataset: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if dataset.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    health = dataset.get("health", [])
    if len(health) != 9:
        errors.append(f"expected 9 channel health rows, got {len(health)}")
    active = sum(1 for row in health if row.get("status") in {"ok", "stale"})
    if active < 5:
        errors.append(f"only {active} channels are active; at least 5 are required")
    if not dataset.get("rankings", {}).get("global"):
        errors.append("global ranking is empty")
    for code, items in dataset.get("channel_items", {}).items():
        ranks = [item.get("rank") for item in items]
        if ranks != list(range(1, len(items) + 1)):
            errors.append(f"{code}: ranks are not contiguous")
    return errors


def write_outputs(dataset: dict[str, Any], keep_history: bool) -> Path | None:
    serialized = json.dumps(dataset, ensure_ascii=False, indent=2) + "\n"
    atomic_write(LATEST_PATH, serialized)
    history_path: Path | None = None
    if keep_history:
        stamp = dataset["generated_at"].replace("-", "").replace(":", "").replace("T", "-").replace("Z", "Z")
        history_path = HISTORY_DIR / f"{stamp}.json"
        atomic_write(history_path, serialized)
    render_dashboard(dataset)
    return history_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect platform-channel daily trends")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--no-history", action="store_true")
    args = parser.parse_args()

    if args.validate_only:
        dataset = load_json(LATEST_PATH, {})
        errors = validate_dataset(dataset)
        if errors:
            print("Validation failed:", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print(
            "Validation passed: "
            f"{dataset['summary']['channels_active']}/{dataset['summary']['channels_total']} channels active, "
            f"{dataset['summary']['signals_total']} signals."
        )
        return 0

    config = load_json(CONFIG_PATH, {})
    if not config.get("channels"):
        print("config.json has no channels", file=sys.stderr)
        return 1
    previous = load_json(LATEST_PATH, {})
    dataset = build_dataset(config, previous)
    errors = validate_dataset(dataset)
    if errors:
        for error in errors:
            print(f"Validation error: {error}", file=sys.stderr)
        return 1
    history_path = write_outputs(dataset, not args.no_history)
    print(
        f"Collected {dataset['summary']['signals_total']} signals from "
        f"{dataset['summary']['channels_active']}/{dataset['summary']['channels_total']} channels."
    )
    for row in dataset["health"]:
        suffix = f" — {row['note']}" if row["note"] else ""
        if row["error"]:
            suffix += f" — {row['error']}"
        print(f"[{row['status'].upper():>19}] {row['name']}: {row['item_count']} items{suffix}")
    print(f"Dashboard: {INDEX_PATH}")
    print(f"Latest data: {LATEST_PATH}")
    if history_path:
        print(f"History: {history_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
