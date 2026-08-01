#!/usr/bin/env python3
"""RSS-only college student-council news collector.

The collector fetches syndication feeds, not article pages. It therefore does
not bypass robots.txt, paywalls, logins, or anti-bot controls.
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_PATH = ROOT / "data" / "news.json"
USER_AGENT = "StudentCouncilNewsMonitor/1.0 (+GitHub Actions; RSS metadata only)"
KST = timezone(timedelta(hours=9))


def clean(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_image(value: str | None, item: ET.Element) -> str:
    for tag in (
        "{http://search.yahoo.com/mrss/}content",
        "{http://search.yahoo.com/mrss/}thumbnail",
        "enclosure",
    ):
        node = item.find(tag)
        if node is not None and node.get("url", "").startswith(("http://", "https://")):
            return node.get("url", "")
    match = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)', value or "", re.I)
    return html.unescape(match.group(1)) if match else ""


def parse_date(value: str | None) -> datetime:
    if value:
        try:
            dt = email.utils.parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    return datetime.now(timezone.utc)


def text_of(node: ET.Element, *names: str) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text
    return ""


def unwrap_google_url(url: str) -> str:
    # Google News RSS links remain valid redirects. We intentionally do not
    # resolve them because that would request the publisher's article page.
    return url.strip()


def fetch_xml(url: str) -> ET.Element:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml;q=0.9"})
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = response.read(5_000_000)
    return ET.fromstring(payload)


def parse_rss(root: ET.Element, source_name: str, matched_query: str = "") -> list[dict]:
    rows: list[dict] = []
    items = root.findall("./channel/item")
    if not items and root.tag.endswith("rss"):
        items = root.findall(".//item")
    for item in items:
        source_node = item.find("source")
        link = text_of(item, "link", "guid").strip()
        raw_summary = text_of(item, "description", "{http://purl.org/rss/1.0/modules/content/}encoded")
        source = clean(source_node.text if source_node is not None else source_name)
        title = clean(text_of(item, "title"))
        # Google News appends " - publisher" to titles. Remove only the exact
        # source suffix so an official RSS copy can be deduplicated by headline.
        if source_name == "Google News RSS" and source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].rstrip()
        rows.append({
            "title": title,
            "url": link,
            "summary": clean(raw_summary),
            "image_url": extract_image(raw_summary, item),
            "source": source,
            "published_at": parse_date(text_of(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date")).isoformat(),
            "matched_query": matched_query,
            "feed": source_name,
        })
    return rows


def fetch_google_feed(query: str) -> list[dict]:
    params = urllib.parse.urlencode({"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    url = f"https://news.google.com/rss/search?{params}"
    return parse_rss(fetch_xml(url), "Google News RSS", query)


def fetch_publisher_feed(source: dict) -> list[dict]:
    # Official publisher RSS items normally contain the publisher's own article URL.
    rows = parse_rss(fetch_xml(source["url"]), source["name"])
    university = source.get("university")
    for row in rows:
        if university:
            row["image_url"] = source.get("logo_url", "")
            row["thumbnail_type"] = "university_logo"
            row["publisher_group"] = "university_press"
            row["universities"] = [university]
        else:
            row["publisher_group"] = "official_media"
    return rows


def google_search_fallback(item: dict) -> str:
    title = re.sub(r"\s+-\s+[^-]+$", "", item["title"]).strip()
    query = f'"{title}" {item.get("source", "")}'.strip()
    return "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})


def resolve_google_article(url: str) -> str:
    """Resolve a Google News RSS id without requesting the publisher page."""
    article_id = urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        page = response.read(1_000_000).decode("utf-8", "ignore")
    match = re.search(
        rf'data-n-a-id="{re.escape(article_id)}" data-n-a-ts="([^"]+)" data-n-a-sg="([^"]+)"',
        page,
    )
    if not match:
        raise ValueError("Google News resolution attributes missing")
    timestamp, signature = match.groups()
    request_value = [
        "garturlreq",
        [["ko", "KR", ["FINANCE_TOP_INDICES", "WEB_TEST_1_0_0"], None, None, 1, 1, "KR:ko", None, 180, None, None, None, None, None, 0, None, None, [1608992183, 723341000]], "ko", "KR", 1, [2, 3, 4, 8], 1, 0, "655000234", 0, 0, None, 0],
        article_id,
        int(timestamp),
        signature,
    ]
    envelope = [[["Fbv4je", json.dumps(request_value, ensure_ascii=False, separators=(",", ":")), None, "generic"]]]
    body = urllib.parse.urlencode({"f.req": json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))}).encode()
    endpoint = "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je"
    batch_request = urllib.request.Request(endpoint, data=body, headers={
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    })
    with urllib.request.urlopen(batch_request, timeout=25) as response:
        result = response.read(100_000).decode("utf-8", "ignore")
    outer = json.loads(result[result.find("[["):])
    inner = json.loads(outer[0][2])
    resolved = inner[1]
    if not resolved.startswith(("http://", "https://")) or "news.google.com" in urllib.parse.urlparse(resolved).netloc:
        raise ValueError("invalid resolved article URL")
    return resolved


def accepted(item: dict, config: dict) -> bool:
    haystack = f"{item['title']} {item['summary']}".lower()
    if any(word.lower() in haystack for word in config["exclude_any"]):
        return False
    has_core = any(word.lower() in haystack for word in config["required_any"])
    has_context = any(word.lower() in haystack for word in config["university_context"])
    # The feed itself supplies the university context for a campus newspaper.
    return has_core and (has_context or item.get("publisher_group") == "university_press")


def detect_universities(item: dict, config: dict) -> list[str]:
    """Tag articles so one university filter spans campus and national media."""
    detected = list(item.get("universities", []))
    haystack = f"{item['title']} {item['summary']}".lower()
    for university, aliases in config.get("university_aliases", {}).items():
        if university not in detected and any(alias.lower() in haystack for alias in aliases):
            detected.append(university)
    return detected


def category(item: dict, config: dict) -> str:
    haystack = f"{item['title']} {item['summary']}".lower()
    scores = {
        name: sum(1 for word in words if word.lower() in haystack)
        for name, words in config["category_rules"].items()
    }
    winner, score = max(scores.items(), key=lambda pair: pair[1])
    return winner if score else "기타"


def identity(item: dict) -> str:
    # Only completely identical, whitespace-cleaned headlines are duplicates.
    # Punctuation or wording differences remain separate articles.
    exact_title = clean(item["title"])
    return hashlib.sha256(exact_title.encode("utf-8")).hexdigest()[:20]


def load_existing() -> list[dict]:
    try:
        items = json.loads(DATA_PATH.read_text(encoding="utf-8")).get("items", [])
        # Migrate old Google intermediary links so deployed pages never hang.
        for item in items:
            if "news.google.com" in urllib.parse.urlparse(item.get("url", "")).netloc:
                item["url"] = google_search_fallback(item)
                item["link_type"] = "search_fallback"
        return items
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = {item["id"]: item for item in load_existing() if item.get("id")}
    failures = []
    resolution_budget = int(config.get("max_google_resolutions_per_run", 8))
    for index, query in enumerate(config["queries"]):
        try:
            for item in fetch_google_feed(query):
                item["publisher_group"] = "official_media"
                item["universities"] = detect_universities(item, config)
                if accepted(item, config):
                    item["id"] = identity(item)
                    item["category"] = category(item, config)
                    item["collected_at"] = datetime.now(timezone.utc).isoformat()
                    current = merged.get(item["id"])
                    if current:
                        if not current.get("image_url") and item.get("image_url"):
                            current["image_url"] = item["image_url"]
                        if current.get("link_type") == "search_fallback" and resolution_budget > 0:
                            resolution_budget -= 1
                            try:
                                current["url"] = resolve_google_article(item["url"])
                                current["link_type"] = "publisher_direct"
                            except Exception:
                                pass
                        continue
                    if resolution_budget > 0:
                        resolution_budget -= 1
                    else:
                        item["url"] = google_search_fallback(item)
                        item["link_type"] = "search_fallback"
                        merged[item["id"]] = item
                        continue
                    try:
                        item["url"] = resolve_google_article(item["url"])
                        item["link_type"] = "publisher_direct"
                    except Exception:
                        item["url"] = google_search_fallback(item)
                        item["link_type"] = "search_fallback"
                    merged[item["id"]] = item
        except Exception as exc:  # keep other feeds usable when one request fails
            failures.append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
        if index + 1 < len(config["queries"]):
            time.sleep(1.2)

    for source in config.get("rss_sources", []):
        try:
            for item in fetch_publisher_feed(source):
                item["universities"] = detect_universities(item, config)
                if item["title"] and item["url"] and accepted(item, config):
                    item["id"] = identity(item)
                    item["category"] = category(item, config)
                    item["collected_at"] = datetime.now(timezone.utc).isoformat()
                    item["link_type"] = "publisher_direct"
                    # Prefer an official publisher RSS record over a Google record
                    # with the same normalized title, because its link is direct.
                    merged[item["id"]] = item
        except Exception as exc:
            failures.append({"source": source["name"], "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(1.2)

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(config["retention_days"]))
    items = [item for item in merged.values() if datetime.fromisoformat(item["published_at"]) >= cutoff]
    items.sort(key=lambda item: item["published_at"], reverse=True)
    items = items[: int(config["max_items"])]
    output = {
        "updated_at": datetime.now(KST).isoformat(),
        "item_count": len(items),
        "collection_policy": "RSS metadata only; article pages are not crawled",
        "universities": list(config.get("university_aliases", {}).keys()),
        "failures": failures,
        "items": items,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"saved {len(items)} items ({len(failures)} query failures)")
    total_feeds = len(config["queries"]) + len(config.get("rss_sources", []))
    if failures and len(failures) == total_feeds:
        raise SystemExit("all feed requests failed")


if __name__ == "__main__":
    main()
