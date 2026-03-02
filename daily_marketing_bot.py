import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


KEYWORDS = [
    "brand",
    "branding",
    "marketing",
    "advertising",
    "campaign",
    "consumer",
    "cmo",
    "media buy",
    "creative strategy",
    "performance marketing",
]

SOURCE_CONFIGS = [
    {"name": "Reuters", "domain": "reuters.com", "weight": 4},
    {"name": "Bloomberg", "domain": "bloomberg.com", "weight": 3},
    {"name": "Financial Times", "domain": "ft.com", "weight": 3},
    {"name": "Wall Street Journal", "domain": "wsj.com", "weight": 3},
    {"name": "Ad Age", "domain": "adage.com", "weight": 5},
    {"name": "Campaign", "domain": "campaignlive.com", "weight": 5},
    {"name": "Marketing Week", "domain": "marketingweek.com", "weight": 5},
    {"name": "The Drum", "domain": "thedrum.com", "weight": 5},
    {"name": "Digiday", "domain": "digiday.com", "weight": 5},
    {"name": "Harvard Business Review", "domain": "hbr.org", "weight": 4},
]

DEFAULT_USER_AGENT = "feishu-marketing-daily/1.0 (+local)"
DEFAULT_LOOKBACK_HOURS = 30
DEFAULT_MAX_ITEMS = 10
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


@dataclass
class FeedSource:
    name: str
    url: str
    weight: int


def build_google_news_rss(domain: str) -> str:
    query = (
        f"site:{domain} "
        "(brand OR branding OR marketing OR advertising OR campaign OR cmo OR consumer)"
    )
    encoded_query = urllib.parse.quote_plus(query)
    return (
        "https://news.google.com/rss/search"
        f"?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    )


def build_sources() -> list[FeedSource]:
    sources = []
    for conf in SOURCE_CONFIGS:
        sources.append(
            FeedSource(
                name=conf["name"],
                url=build_google_news_rss(conf["domain"]),
                weight=conf["weight"],
            )
        )
    return sources


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sent_links": [], "last_run_date": ""}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"sent_links": [], "last_run_date": ""}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_text(url: str, user_agent: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    filtered = [(k, v) for (k, v) in q if not k.lower().startswith("utm_")]
    query = urllib.parse.urlencode(filtered, doseq=True)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def extract_items(feed_xml: str, source_name: str, source_weight: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError:
        return items

    channel = root.find("channel")
    if channel is not None:
        for item in channel.findall("item"):
            items.append(
                {
                    "title": strip_html(item.findtext("title") or ""),
                    "link": canonical_url(item.findtext("link") or ""),
                    "summary": strip_html(item.findtext("description") or ""),
                    "published": parse_datetime(item.findtext("pubDate") or ""),
                    "source": source_name,
                    "source_weight": source_weight,
                }
            )
        return items

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("atom:entry", ns):
        link = ""
        link_el = entry.find("atom:link", ns)
        if link_el is not None:
            link = link_el.attrib.get("href", "")
        items.append(
            {
                "title": strip_html(entry.findtext("atom:title", default="", namespaces=ns)),
                "link": canonical_url(link),
                "summary": strip_html(
                    entry.findtext("atom:summary", default="", namespaces=ns)
                    or entry.findtext("atom:content", default="", namespaces=ns)
                ),
                "published": parse_datetime(
                    entry.findtext("atom:updated", default="", namespaces=ns)
                    or entry.findtext("atom:published", default="", namespaces=ns)
                ),
                "source": source_name,
                "source_weight": source_weight,
            }
        )
    return items


def score_item(item: dict[str, Any], now_utc: datetime) -> int:
    text = normalize_text(f"{item.get('title', '')} {item.get('summary', '')}")
    keyword_hits = 0
    for kw in KEYWORDS:
        if kw in text:
            keyword_hits += 1

    freshness = 0
    published = item.get("published")
    if isinstance(published, datetime):
        age_hours = (now_utc - published).total_seconds() / 3600.0
        if age_hours <= 6:
            freshness = 3
        elif age_hours <= 12:
            freshness = 2
        elif age_hours <= 24:
            freshness = 1

    return keyword_hits + int(item.get("source_weight", 0)) + freshness


def collect_news(sources: list[FeedSource], lookback_hours: int, user_agent: str) -> list[dict[str, Any]]:
    now_utc = datetime.now(timezone.utc)
    min_dt = now_utc - timedelta(hours=lookback_hours)
    all_items: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for src in sources:
        try:
            feed_text = fetch_text(src.url, user_agent=user_agent)
        except Exception as exc:
            print(f"[warn] feed fetch failed: {src.name} ({exc})")
            continue

        for item in extract_items(feed_text, src.name, src.weight):
            link = item.get("link", "")
            title = item.get("title", "").strip()
            if not link or not title:
                continue
            pub = item.get("published")
            if isinstance(pub, datetime) and pub < min_dt:
                continue

            dedup_key = normalize_text(f"{title}|{link}")
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            item["score"] = score_item(item, now_utc)
            all_items.append(item)

    all_items.sort(
        key=lambda x: (
            int(x.get("score", 0)),
            x.get("published") or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return all_items


def summarize_item(item: dict[str, Any], max_len: int = 180) -> str:
    summary = strip_html(item.get("summary", ""))
    if not summary:
        return "No summary provided by source."
    if len(summary) <= max_len:
        return summary
    return summary[: max_len - 3].rstrip() + "..."


def parse_json_array_from_text(text: str) -> list[dict[str, Any]]:
    content = (text or "").strip()
    if not content:
        return []

    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)

    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        pass

    start = content.find("[")
    end = content.rfind("]")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(content[start : end + 1])
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def deepseek_enhance_items(items: list[dict[str, Any]], model: str = DEFAULT_DEEPSEEK_MODEL) -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key or not items:
        return

    compact_items = []
    for idx, item in enumerate(items, 1):
        compact_items.append(
            {
                "idx": idx,
                "source": item.get("source", ""),
                "title": item.get("title", ""),
                "summary": summarize_item(item, max_len=280),
            }
        )

    prompt = (
        "你是资深品牌营销分析师。请基于以下英文资讯，输出简体中文解读。\n"
        "只返回 JSON 数组，每个元素包含字段：idx, zh_title, key_points, impact。\n"
        "约束：\n"
        "- zh_title: 中文标题。\n"
        "- 事件：简要概括发生事件。\n"
        "- key_points: 2-3条要点数组，每条。\n"
        "- impact: 影响意义，1-2句，<=80字，聚焦品牌/营销层面的决策启发。\n"
        "- 不要输出数组以外任何文字。\n"
        f"items={json.dumps(compact_items, ensure_ascii=False)}"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你输出严格有效的 JSON，不要 markdown。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }

    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="ignore"))

        content = body["choices"][0]["message"]["content"].strip()
        parsed = parse_json_array_from_text(content)

        by_idx: dict[int, dict[str, Any]] = {}
        for row in parsed:
            try:
                row_idx = int(row.get("idx"))
            except Exception:
                continue
            by_idx[row_idx] = row

        for idx, item in enumerate(items, 1):
            row = by_idx.get(idx, {})
            zh_title = str(row.get("zh_title", "")).strip()
            impact = str(row.get("impact", "")).strip()
            points = row.get("key_points", [])

            cleaned_points: list[str] = []
            if isinstance(points, list):
                cleaned_points = [str(p).strip() for p in points if str(p).strip()]

            if zh_title:
                item["zh_title"] = zh_title
            if cleaned_points:
                item["zh_points"] = cleaned_points[:3]
            if impact:
                item["impact"] = impact

    except Exception as exc:
        print(f"[warn] deepseek enhance skipped: {exc}")


def build_feishu_payload(items: list[dict[str, Any]], report_date: str) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []

    if not items:
        rows.append(
            [
                {
                    "tag": "text",
                    "text": "今日未检索到符合条件的新动态（可放宽关键词或扩大时间窗口）。",
                }
            ]
        )
    else:
        for idx, item in enumerate(items, 1):
            title = item.get("zh_title") or item["title"].strip()
            source = item["source"]
            published = item.get("published")
            published_text = (
                published.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                if isinstance(published, datetime)
                else "unknown time"
            )

            points = item.get("zh_points")
            if isinstance(points, list) and points:
                points_text = "\n".join([f"- {p}" for p in points[:3]])
            else:
                points_text = f"- {summarize_item(item)}"

            impact = (
                item.get("impact")
                or "该动态可能影响品牌预算配置、渠道策略或创意方向，建议结合业务目标快速评估。"
            )

            line = (
                f"{idx}. [{source}] {title}\n"
                f"摘要要点：\n{points_text}\n"
                f"影响意义：{impact}\n"
                f"时间：{published_text}"
            )
            rows.append(
                [
                    {"tag": "text", "text": line + "\n"},
                    {"tag": "a", "text": "查看原文", "href": item["link"]},
                ]
            )

    return {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": f"品牌营销日报 | {report_date}",
                    "content": rows,
                }
            }
        },
    }


def sign_feishu_payload(payload: dict[str, Any], secret: str) -> dict[str, Any]:
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    sign = base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    ).decode("utf-8")
    payload = dict(payload)
    payload["timestamp"] = timestamp
    payload["sign"] = sign
    return payload


def post_to_feishu(webhook_url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return True, body
    except Exception as exc:
        return False, str(exc)


def resolve_timezone(tz_name: str):
    if ZoneInfo is None:
        print("[warn] zoneinfo unavailable, fallback to UTC+8.")
        return timezone(timedelta(hours=8))
    try:
        return ZoneInfo(tz_name)
    except Exception:
        print(f"[warn] invalid timezone '{tz_name}', fallback to Asia/Shanghai.")
        return ZoneInfo("Asia/Shanghai")


def run_once(args: argparse.Namespace, state: dict[str, Any]) -> int:
    sources = build_sources()
    results = collect_news(sources, lookback_hours=args.lookback_hours, user_agent=args.user_agent)

    sent_links = set(state.get("sent_links", []))
    new_items = [it for it in results if it["link"] not in sent_links]
    picked = new_items[: args.max_items]

    deepseek_enhance_items(picked, model=args.deepseek_model)

    print(f"[info] collected={len(results)}, new={len(new_items)}, picked={len(picked)}")
    for idx, item in enumerate(picked, 1):
        shown_title = item.get("zh_title") or item["title"]
        print(f"{idx}. [{item['source']}] {shown_title}")
        print(f"   {item['link']}")

    if args.dry_run:
        return 0

    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("[error] FEISHU_WEBHOOK_URL is empty. Use --dry-run to test collection.")
        return 1

    tz = resolve_timezone(args.timezone)
    report_date = datetime.now(tz).strftime("%Y-%m-%d")
    payload = build_feishu_payload(picked, report_date=report_date)

    secret = os.environ.get("FEISHU_SECRET", "").strip()
    if secret:
        payload = sign_feishu_payload(payload, secret)

    ok, detail = post_to_feishu(webhook_url, payload)
    if not ok:
        print(f"[error] feishu push failed: {detail}")
        return 1

    print(f"[info] feishu push success: {detail}")
    for it in picked:
        sent_links.add(it["link"])
    state["sent_links"] = list(sent_links)[-5000:]
    state["last_run_date"] = report_date
    return 0


def daemon_loop(args: argparse.Namespace, state_path: Path) -> int:
    tz = resolve_timezone(args.timezone)
    target_hhmm = args.daily_time.strip()

    while True:
        state = load_state(state_path)
        now_local = datetime.now(tz)
        today = now_local.strftime("%Y-%m-%d")
        last_run_date = state.get("last_run_date", "")
        now_hhmm = now_local.strftime("%H:%M")

        should_run = now_hhmm == target_hhmm and (args.force or last_run_date != today)
        if should_run:
            code = run_once(args, state)
            save_state(state_path, state)
            if code != 0:
                print("[warn] run failed in daemon mode, retrying next minute.")
            time.sleep(65)
            continue

        time.sleep(20)


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    default_state = script_dir / "data" / "state.json"

    parser = argparse.ArgumentParser(
        description="Daily Feishu push for brand/marketing news from major English media."
    )
    parser.add_argument("--daemon", action="store_true", help="Run forever and push at --daily-time.")
    parser.add_argument("--daily-time", default="09:00", help="Daily push time, format HH:MM.")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="IANA timezone, e.g. Asia/Shanghai.")
    parser.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--deepseek-model", default=DEFAULT_DEEPSEEK_MODEL)
    parser.add_argument("--state-path", default=str(default_state))
    parser.add_argument("--dry-run", action="store_true", help="Fetch and rank only; do not push.")
    parser.add_argument("--force", action="store_true", help="Run even if already pushed today.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_path = Path(args.state_path)

    if args.daemon:
        print(
            f"[info] daemon started. timezone={args.timezone}, daily_time={args.daily_time}, "
            f"lookback_hours={args.lookback_hours}, max_items={args.max_items}"
        )
        return daemon_loop(args, state_path)

    state = load_state(state_path)
    code = run_once(args, state)
    if code == 0:
        save_state(state_path, state)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
