#!/usr/bin/env python
"""Collect ten-market daily trends and build a self-contained dashboard.

The collector intentionally uses public, read-only endpoints:
* Google Trends "Trending Now" RSS for nine markets.
* Bilibili's public trending search endpoint for China.

It has no third-party Python dependencies and preserves the last successful
country result if a source is temporarily unavailable.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
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
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/126 Safari/537.36 "
    "WorldSignalCollector/1.0"
)


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


def fetch_bytes(url: str, timeout: int, retries: int) -> bytes:
    last_error: Exception | None = None
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/json, text/xml;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        },
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"Failed after {retries} attempts: {last_error}")


def parse_traffic(text: str | None) -> int:
    if not text:
        return 0
    compact = text.strip().upper().replace(",", "").replace("+", "")
    match = re.search(r"([\d.]+)\s*([KMB]?)", compact)
    if not match:
        return 0
    value = float(match.group(1))
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(value * multiplier.get(match.group(2), 1))


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_pubdate(text: str | None, fallback: datetime) -> datetime:
    if not text:
        return fallback
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return fallback


def normalize_title(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = "".join(
        char
        for char in value
        if unicodedata.category(char)[0] not in {"P", "S"} or char in {"+", "#"}
    )
    value = re.sub(r"\s+", " ", value).strip()
    return value


def topic_id(canonical: str) -> str:
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def token_similarity(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def find_text(node: ET.Element | None, tag: str) -> str:
    if node is None:
        return ""
    value = node.findtext(tag)
    return (value or "").strip()


def parse_google_trends(country: dict[str, Any], raw: bytes, now: datetime) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    items: list[dict[str, Any]] = []
    for rank, node in enumerate(root.findall("./channel/item"), start=1):
        title = find_text(node, "title")
        if not title:
            continue
        traffic_text = find_text(node, f"{{{HT_NS}}}approx_traffic")
        published = parse_pubdate(find_text(node, "pubDate"), now)
        news_node = node.find(f"{{{HT_NS}}}news_item")
        news_title = find_text(news_node, f"{{{HT_NS}}}news_item_title")
        news_url = find_text(news_node, f"{{{HT_NS}}}news_item_url")
        news_source = find_text(news_node, f"{{{HT_NS}}}news_item_source")
        picture = find_text(node, f"{{{HT_NS}}}picture")
        query = urllib.parse.quote_plus(title)
        items.append(
            {
                "title": title,
                "canonical": normalize_title(title),
                "country_code": country["code"],
                "rank": rank,
                "traffic_text": traffic_text,
                "traffic": parse_traffic(traffic_text),
                "published_at": iso_utc(published),
                "age_hours": max(0.0, (now - published).total_seconds() / 3600),
                "source": "Google Trends",
                "source_type": "search",
                "source_url": f"https://trends.google.com/trends/explore?geo={country['code']}&q={query}",
                "news_title": news_title,
                "news_url": news_url,
                "news_source": news_source,
                "image": picture,
            }
        )
    return items


def parse_bilibili(country: dict[str, Any], raw: bytes, now: datetime) -> list[dict[str, Any]]:
    payload = json.loads(raw.decode("utf-8"))
    entries = payload.get("data", {}).get("trending", {}).get("list", [])
    items: list[dict[str, Any]] = []
    for rank, entry in enumerate(entries, start=1):
        title = (entry.get("show_name") or entry.get("keyword") or "").strip()
        keyword = (entry.get("keyword") or title).strip()
        if not title:
            continue
        query = urllib.parse.quote_plus(keyword)
        items.append(
            {
                "title": title,
                "canonical": normalize_title(title),
                "country_code": country["code"],
                "rank": rank,
                "traffic_text": "平台未公开",
                "traffic": 0,
                "published_at": iso_utc(now),
                "age_hours": 0.0,
                "source": "Bilibili 热搜",
                "source_type": "platform_trend",
                "source_url": f"https://search.bilibili.com/all?keyword={query}",
                "news_title": "",
                "news_url": "",
                "news_source": "",
                "image": entry.get("icon") or "",
            }
        )
    return items


def collect_country(
    country: dict[str, Any],
    timeout: int,
    retries: int,
    now: datetime,
) -> tuple[str, list[dict[str, Any]], str | None]:
    provider = country["provider"]
    try:
        if provider == "google_trends_rss":
            url = f"https://trends.google.com/trending/rss?geo={country['code']}"
            raw = fetch_bytes(url, timeout, retries)
            return country["code"], parse_google_trends(country, raw, now), None
        if provider == "bilibili_trending":
            url = "https://api.bilibili.com/x/web-interface/search/square?limit=50"
            raw = fetch_bytes(url, timeout, retries)
            return country["code"], parse_bilibili(country, raw, now), None
        raise ValueError(f"Unknown provider: {provider}")
    except Exception as exc:  # source failures are represented in dashboard health
        return country["code"], [], f"{type(exc).__name__}: {exc}"


def apply_local_scores(items: list[dict[str, Any]]) -> None:
    if not items:
        return
    max_traffic = max(item["traffic"] for item in items)
    denominator = max(1, len(items) - 1)
    for item in items:
        rank_score = 1 - ((item["rank"] - 1) / denominator)
        traffic_score = (
            math.log1p(item["traffic"]) / math.log1p(max_traffic)
            if max_traffic > 0 and item["traffic"] > 0
            else rank_score
        )
        item["local_score"] = round(100 * (0.72 * rank_score + 0.28 * traffic_score), 1)


def build_alias_lookup(config: dict[str, Any]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for preferred, aliases in config.get("aliases", {}).items():
        canonical = normalize_title(preferred)
        lookup[canonical] = canonical
        for alias in aliases:
            lookup[normalize_title(alias)] = canonical
    return lookup


def canonical_for_cluster(
    value: str,
    alias_lookup: dict[str, str],
    existing: list[str],
) -> str:
    normalized = alias_lookup.get(value, value)
    if normalized in existing:
        return normalized
    if len(normalized) < 5:
        return normalized
    for candidate in existing:
        if abs(len(candidate) - len(normalized)) > max(8, len(normalized) // 2):
            continue
        sequence = SequenceMatcher(None, candidate, normalized).ratio()
        overlap = token_similarity(candidate, normalized)
        contained = (
            min(len(candidate), len(normalized)) >= 5
            and (candidate in normalized or normalized in candidate)
        )
        if contained or sequence >= 0.94 or (overlap >= 0.82 and min(len(candidate), len(normalized)) >= 8):
            return candidate
    return normalized


def build_global_topics(
    country_lists: list[dict[str, Any]],
    config: dict[str, Any],
    previous: dict[str, Any],
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    countries_by_code = {country["code"]: country for country in config["countries"]}
    total_weight = sum(float(country.get("weight", 1)) for country in config["countries"])
    alias_lookup = build_alias_lookup(config)
    clusters: dict[str, list[dict[str, Any]]] = {}
    canonical_order: list[str] = []

    for country_list in country_lists:
        for item in country_list["items"]:
            canonical = canonical_for_cluster(item["canonical"], alias_lookup, canonical_order)
            if canonical not in clusters:
                clusters[canonical] = []
                canonical_order.append(canonical)
            clusters[canonical].append(item)

    topics: list[dict[str, Any]] = []
    for canonical, members in clusters.items():
        best_by_country: dict[str, dict[str, Any]] = {}
        for member in members:
            code = member["country_code"]
            if code not in best_by_country or member["local_score"] > best_by_country[code]["local_score"]:
                best_by_country[code] = member

        weighted_scores = []
        country_weight = 0.0
        for code, member in best_by_country.items():
            weight = float(countries_by_code[code].get("weight", 1))
            weighted_scores.append(member["local_score"] * weight)
            country_weight += weight
        average_score = sum(weighted_scores) / max(country_weight, 0.001)
        best_score = max(member["local_score"] for member in best_by_country.values())
        coverage = country_weight / max(total_weight, 0.001)
        breadth = min(
            1.0,
            math.log1p(len(best_by_country)) / math.log1p(5),
        )
        freshness = max(math.exp(-member["age_hours"] / 30) for member in members)
        global_score = 100 * (
            0.25 * (average_score / 100)
            + 0.20 * (best_score / 100)
            + 0.40 * breadth
            + 0.15 * freshness
        )
        lead = max(
            members,
            key=lambda item: (
                item["traffic"],
                item["local_score"],
                -item["rank"],
            ),
        )
        countries = [
            {
                "code": code,
                "flag": countries_by_code[code]["flag"],
                "name_zh": countries_by_code[code]["name_zh"],
                "rank": member["rank"],
                "local_score": member["local_score"],
                "title": member["title"],
                "source": member["source"],
                "source_url": member["source_url"],
            }
            for code, member in sorted(
                best_by_country.items(),
                key=lambda pair: pair[1]["rank"],
            )
        ]
        topics.append(
            {
                "id": topic_id(canonical),
                "canonical": canonical,
                "title": lead["title"],
                "global_score": round(global_score, 1),
                "coverage": round(coverage * 100, 1),
                "country_count": len(best_by_country),
                "countries": countries,
                "best_local_score": round(best_score, 1),
                "traffic": max(member["traffic"] for member in members),
                "traffic_text": lead["traffic_text"],
                "freshness": round(freshness * 100, 1),
                "source": lead["source"],
                "source_url": lead["source_url"],
                "news_title": lead["news_title"],
                "news_url": lead["news_url"],
                "news_source": lead["news_source"],
                "image": lead["image"],
            }
        )

    topics.sort(key=lambda item: (item["global_score"], item["country_count"], item["traffic"]), reverse=True)
    previous_topics = {
        item["id"]: item for item in previous.get("global_topics", [])
    }
    previous_ranks = {
        item["id"]: index
        for index, item in enumerate(previous.get("global_topics", []), start=1)
    }
    comparison_mode = "较上次更新" if previous_topics else "冷启动代理"

    for rank, topic in enumerate(topics, start=1):
        topic["rank"] = rank
        prior = previous_topics.get(topic["id"])
        prior_rank = previous_ranks.get(topic["id"])
        topic["previous_rank"] = prior_rank
        topic["is_new"] = prior is None
        topic["rank_change"] = (prior_rank - rank) if prior_rank is not None else None
        topic["score_change"] = (
            round(topic["global_score"] - prior["global_score"], 1) if prior else None
        )

        if not previous_topics:
            breakout = (
                0.60 * topic["global_score"]
                + 0.25 * topic["freshness"]
                + 0.15 * topic["best_local_score"]
            )
            reason = "首日基线：综合当前强度与新鲜度"
        else:
            score_delta = max(0.0, topic["global_score"] - (prior or {}).get("global_score", 0))
            delta_signal = min(1.0, score_delta / 30)
            rank_gain = max(0, (prior_rank - rank) if prior_rank is not None else 20)
            rank_signal = min(1.0, rank_gain / 20)
            novelty_signal = 1.0 if prior is None else 0.0
            prior_coverage = (prior or {}).get("coverage", 0)
            coverage_signal = min(1.0, max(0.0, topic["coverage"] - prior_coverage) / 30)
            breakout = 100 * (
                0.35 * delta_signal
                + 0.20 * rank_signal
                + 0.20 * novelty_signal
                + 0.10 * coverage_signal
                + 0.15 * (topic["global_score"] / 100)
            )
            reasons = []
            if prior is None:
                reasons.append("新进入榜")
            if rank_gain > 0 and prior_rank is not None:
                reasons.append(f"上升 {rank_gain} 位")
            if score_delta >= 1:
                reasons.append(f"热度 +{score_delta:.1f}")
            if topic["country_count"] >= 2:
                reasons.append(f"扩散至 {topic['country_count']} 国")
            reason = " · ".join(reasons) or "当前强度领先"
        topic["breakout_score"] = round(min(100, breakout), 1)
        topic["breakout_reason"] = reason

    breakout_topics = sorted(
        topics,
        key=lambda item: (item["breakout_score"], item["global_score"]),
        reverse=True,
    )
    for rank, topic in enumerate(breakout_topics, start=1):
        topic["breakout_rank"] = rank
    return topics, breakout_topics, comparison_mode


def previous_country_items(previous: dict[str, Any], code: str) -> list[dict[str, Any]]:
    for country in previous.get("country_lists", []):
        if country.get("code") == code:
            return country.get("items", [])
    return []


def build_payload(config: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    previous = load_json(LATEST_PATH, {})
    timeout = int(config.get("request_timeout_seconds", 25))
    retries = int(config.get("request_retries", 3))
    top_n_country = int(config.get("top_n_country", 10))
    results: dict[str, tuple[list[dict[str, Any]], str | None]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(config["countries"]))) as executor:
        futures = {
            executor.submit(collect_country, country, timeout, retries, now): country
            for country in config["countries"]
        }
        for future in concurrent.futures.as_completed(futures):
            code, items, error = future.result()
            results[code] = (items, error)

    country_lists: list[dict[str, Any]] = []
    health_sources: list[dict[str, Any]] = []
    for country in config["countries"]:
        items, error = results.get(country["code"], ([], "No result returned"))
        stale = False
        if not items:
            cached = previous_country_items(previous, country["code"])
            if cached:
                items = cached
                stale = True
        apply_local_scores(items)
        visible_items = items[:top_n_country]
        status = "stale" if stale else ("error" if error else "ok")
        country_lists.append(
            {
                "code": country["code"],
                "name_zh": country["name_zh"],
                "name_en": country["name_en"],
                "region": country["region"],
                "flag": country["flag"],
                "provider": country["provider"],
                "status": status,
                "items": visible_items,
            }
        )
        health_sources.append(
            {
                "code": country["code"],
                "status": status,
                "item_count": len(visible_items),
                "message": (
                    "使用上次成功数据" if stale else (error or "采集成功")
                ),
            }
        )

    global_topics, breakout_topics, comparison_mode = build_global_topics(
        country_lists, config, previous, now
    )
    top_global = int(config.get("top_n_global", 30))
    top_breakout = int(config.get("top_n_breakout", 20))
    ok_count = sum(source["status"] == "ok" for source in health_sources)
    stale_count = sum(source["status"] == "stale" for source in health_sources)
    error_count = sum(source["status"] == "error" for source in health_sources)

    return {
        "schema_version": 1,
        "title": config.get("dashboard_title", "World Signal / 全球热搜"),
        "generated_at": iso_utc(now),
        "timezone": config.get("timezone", "Asia/Singapore"),
        "market_count": len(country_lists),
        "topic_count": sum(len(country["items"]) for country in country_lists),
        "country_lists": country_lists,
        "global_topics": global_topics[:top_global],
        "breakout_topics": breakout_topics[:top_breakout],
        "comparison_mode": comparison_mode,
        "health": {
            "status": "healthy" if error_count == 0 and stale_count == 0 else "partial",
            "ok": ok_count,
            "stale": stale_count,
            "error": error_count,
            "sources": health_sources,
        },
        "methodology": {
            "country_ranking": "各国源内名次 72% + 源内规模百分位 28%",
            "global_ranking": "在榜国家平均强度 25% + 最高本地强度 20% + 跨国覆盖 40% + 新鲜度 15%",
            "breakout_ranking": (
                "首次运行使用当前强度代理；此后使用综合分变化、名次跃升、新进入榜和跨国扩散"
            ),
            "scope_note": (
                "Google Trends 提供九国搜索趋势；中国使用 Bilibili 公开热搜。"
                "所有数值先在来源内部归一化，避免把不同平台原始量直接相加。"
            ),
        },
    }


def render_dashboard(payload: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    serialized = serialized.replace("</script", "<\\/script")
    return template.replace("__TRENDS_DATA__", serialized)


def validate_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if len(payload.get("country_lists", [])) != 10:
        errors.append("Expected exactly 10 country lists")
    if not payload.get("global_topics"):
        errors.append("Global ranking is empty")
    if not payload.get("breakout_topics"):
        errors.append("Breakout ranking is empty")
    for country in payload.get("country_lists", []):
        if not country.get("items"):
            errors.append(f"{country.get('code')}: country list is empty")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect global daily trends")
    parser.add_argument("--validate-only", action="store_true", help="Validate current latest.json")
    args = parser.parse_args()

    if args.validate_only:
        payload = load_json(LATEST_PATH, {})
        errors = validate_payload(payload)
        if errors:
            print("\n".join(f"ERROR: {error}" for error in errors), file=sys.stderr)
            return 1
        print(
            f"OK: {len(payload['country_lists'])} countries, "
            f"{len(payload['global_topics'])} global topics, "
            f"{len(payload['breakout_topics'])} breakout topics"
        )
        return 0

    config = load_json(CONFIG_PATH, {})
    if not config.get("countries"):
        print("ERROR: config.json has no countries", file=sys.stderr)
        return 1

    payload = build_payload(config)
    errors = validate_payload(payload)
    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors), file=sys.stderr)
        return 1

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    atomic_write(HISTORY_DIR / f"{timestamp}.json", serialized)
    atomic_write(LATEST_PATH, serialized)
    atomic_write(INDEX_PATH, render_dashboard(payload))
    print(
        json.dumps(
            {
                "status": payload["health"]["status"],
                "generated_at": payload["generated_at"],
                "countries": payload["market_count"],
                "country_topics": payload["topic_count"],
                "global_topics": len(payload["global_topics"]),
                "breakout_topics": len(payload["breakout_topics"]),
                "dashboard": str(INDEX_PATH),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
