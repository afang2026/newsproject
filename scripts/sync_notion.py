#!/usr/bin/env python3
"""
Read feeds.json and, for any new items, create a fresh page under the
"Various Scans" Notion page named "Scan at <time> (Chicago time)" containing
an AI-written executive summary plus a database of that run's articles.

Already-synced items are tracked in notion_synced.json (committed to the
repo) so re-runs never re-process the same article. Processing is capped per
run (MAX_ITEMS_PER_RUN) so a large backlog doesn't fire hundreds of API calls
at once; the rest is picked up on later runs.

Each item's title is also checked against the titles from the immediately
preceding scan only (notion_last_scan_titles.json, not full history): an
exact/near-exact title is dropped from this scan entirely (it's already
covered), a loosely-similar title is kept but relabeled
"[SIMILAR] <new title> | <old title>" so the overlap is visible in Notion.
This is plain text similarity (difflib), not an extra AI call.

Required environment variables:
  ANTHROPIC_API_KEY
  NOTION_TOKEN
  NOTION_SCANS_DATA_SOURCE_ID   - the "Various scans" database (inline in
                                  the Reading Desk page); each run adds one
                                  row/page to it. That database's title
                                  property happens to be named "Name"
                                  (Notion's own default) - SCANS_TITLE_PROP
                                  below assumes that; update it if you ever
                                  rename that column in Notion.

Exit codes:
  0 - success, even if some individual items failed
  1 - missing required env vars, or feeds.json is missing/malformed (fatal)
"""

import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FEEDS_PATH = "feeds.json"
STATE_PATH = "notion_synced.json"
LAST_SCAN_TITLES_PATH = "notion_last_scan_titles.json"
STATE_RETENTION_DAYS = 10
NOTION_VERSION = "2025-09-03"
ANTHROPIC_VERSION = "2023-06-01"
CHICAGO = ZoneInfo("America/Chicago")

# Title-similarity thresholds (difflib ratio on normalized titles).
EXACT_TITLE_THRESHOLD = 0.97  # treat near-identical strings as "same", not just ==
SIMILAR_TITLE_THRESHOLD = 0.6

MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", "150"))
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_SCANS_DATA_SOURCE_ID = os.environ.get("NOTION_SCANS_DATA_SOURCE_ID")
SCANS_TITLE_PROP = os.environ.get("SCANS_TITLE_PROP", "Name")

SOURCE_COLORS = {
    "The Atlantic": "blue",
    "The Economist": "red",
    "The New York Times": "gray",
    "The Wall Street Journal": "orange",
    "The Washington Post": "purple",
}


def fatal(message):
    print(f"FATAL: {message}", file=sys.stderr)
    sys.exit(1)


def require_env():
    missing = [
        name
        for name, value in (
            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
            ("NOTION_TOKEN", NOTION_TOKEN),
            ("NOTION_SCANS_DATA_SOURCE_ID", NOTION_SCANS_DATA_SOURCE_ID),
        )
        if not value
    ]
    if missing:
        fatal(f"missing required environment variable(s): {', '.join(missing)}")


def load_feeds(path=FEEDS_PATH):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fatal(f"could not load {path}: {e}")
    items = data.get("items")
    if not isinstance(items, list):
        fatal(f"{path} has no top-level 'items' array")
    return items


def load_state(path=STATE_PATH):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: could not read {path}, starting fresh: {e}")
        return {}


def save_state(state, path=STATE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def prune_state(state, now):
    cutoff = now - timedelta(days=STATE_RETENTION_DAYS)
    kept = {}
    for item_id, published in state.items():
        try:
            if datetime.fromisoformat(published) >= cutoff:
                kept[item_id] = published
        except (TypeError, ValueError):
            continue
    return kept


def load_last_scan_titles(path=LAST_SCAN_TITLES_PATH):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: could not read {path}, treating as empty: {e}")
        return []


def save_last_scan_titles(titles, path=LAST_SCAN_TITLES_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(titles, f, indent=2)
        f.write("\n")


_NORMALIZE_RE = re.compile(r"[^a-z0-9 ]+")


def normalize_title(title):
    lowered = (title or "").lower()
    stripped = _NORMALIZE_RE.sub(" ", lowered)
    return " ".join(stripped.split())


def classify_against_last_scan(title, last_titles_normalized):
    """Compare one title against last scan's titles only (not full history).
    Returns ("exact" | "similar" | "none", matched_original_title_or_None)."""
    norm = normalize_title(title)
    best_ratio = 0.0
    best_original = None
    for original, other_norm in last_titles_normalized:
        ratio = difflib.SequenceMatcher(None, norm, other_norm).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_original = original
    if best_ratio >= EXACT_TITLE_THRESHOLD:
        return "exact", best_original
    if best_ratio >= SIMILAR_TITLE_THRESHOLD:
        return "similar", best_original
    return "none", None


def http_json(url, method="GET", headers=None, payload=None, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def anthropic_headers():
    return {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def ask_claude(prompt, max_tokens):
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = http_json(
        "https://api.anthropic.com/v1/messages",
        method="POST",
        headers=anthropic_headers(),
        payload=payload,
    )
    blocks = data.get("content") or []
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def ask_claude_for_take(item):
    prompt = (
        "You help a student triage a daily news feed. Given only a headline "
        "and its RSS summary (never the full article), write ONE sentence "
        "(max ~30 words) on why this might be worth reading, or what it "
        "connects to. Be concrete, not generic. Do not use hedging phrases "
        "like 'this article discusses'.\n\n"
        f"Source: {item.get('source', '')}\n"
        f"Title: {item.get('title', '')}\n"
        f"Summary: {item.get('summary', '')}\n"
    )
    return ask_claude(prompt, max_tokens=100)


def ask_claude_for_executive_summary(processed):
    lines = [
        f"- [{it.get('source', '')}] {it.get('title', '')}: {take}"
        for it, take in processed
        if take
    ]
    prompt = (
        "You are producing a short executive summary for a personal "
        "news-reading dashboard. Below are articles from one scan, each "
        "with source, title, and a one-line take. Write a synthesized "
        "executive summary (3-5 sentences of prose, no markdown headers) "
        "that groups related stories by theme, calls out what's most worth "
        "prioritizing and why, and can note what's safe to skip. Be "
        "specific - reference actual story topics, not generic phrases.\n\n"
        + "\n".join(lines)
    )
    return ask_claude(prompt, max_tokens=400)


def chicago_scan_title(now_utc):
    local = now_utc.astimezone(CHICAGO)
    time_part = local.strftime("%-I:%M %p") if os.name != "nt" else local.strftime("%I:%M %p").lstrip("0")
    date_part = local.strftime("%b %-d") if os.name != "nt" else local.strftime("%b %d").replace(" 0", " ")
    return f"Scan at {time_part}, {date_part} (Chicago time)"


def create_scan_page(title):
    payload = {
        "parent": {"type": "data_source_id", "data_source_id": NOTION_SCANS_DATA_SOURCE_ID},
        "icon": {"type": "emoji", "emoji": "📰"},
        "properties": {SCANS_TITLE_PROP: {"title": [{"type": "text", "text": {"content": title}}]}},
    }
    data = http_json(
        "https://api.notion.com/v1/pages",
        method="POST",
        headers=notion_headers(),
        payload=payload,
    )
    return data["id"]


def append_summary_blocks(page_id, summary_text):
    children = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": (summary_text or "(no summary)")[:2000]}}],
                "icon": {"type": "emoji", "emoji": "💡"},
            },
        },
        {"object": "block", "type": "divider", "divider": {}},
    ]
    http_json(
        f"https://api.notion.com/v1/blocks/{page_id}/children",
        method="PATCH",
        headers=notion_headers(),
        payload={"children": children},
    )


def create_scan_database(scan_page_id):
    source_options = [{"name": name, "color": color} for name, color in SOURCE_COLORS.items()]
    payload = {
        "parent": {"type": "page_id", "page_id": scan_page_id},
        "title": [{"type": "text", "text": {"content": "Articles"}}],
        "properties": {
            "Title": {"title": {}},
            "Source": {"select": {"options": source_options}},
            "Section": {"rich_text": {}},
            "Link": {"url": {}},
            "Published": {"date": {}},
            "AI Take": {"rich_text": {}},
            "Status": {
                "select": {
                    "options": [
                        {"name": "New", "color": "blue"},
                        {"name": "Read", "color": "green"},
                        {"name": "Skipped", "color": "gray"},
                    ]
                }
            },
            "RSS ID": {"rich_text": {}},
        },
    }
    data = http_json(
        "https://api.notion.com/v1/databases",
        method="POST",
        headers=notion_headers(),
        payload=payload,
    )
    sources = data.get("data_sources") or []
    if not sources:
        fatal("Notion did not return a data source for the newly created database")
    return sources[0]["id"]


def build_notion_properties(item, ai_take, display_title):
    properties = {
        "Title": {"title": [{"text": {"content": display_title or item.get("title") or "(untitled)"}}]},
        "AI Take": {"rich_text": [{"text": {"content": (ai_take or "")[:2000]}}]},
        "Status": {"select": {"name": "New"}},
        "RSS ID": {"rich_text": [{"text": {"content": item["id"]}}]},
    }
    if item.get("link"):
        properties["Link"] = {"url": item["link"]}
    if item.get("source"):
        properties["Source"] = {"select": {"name": item["source"]}}
    if item.get("section"):
        properties["Section"] = {"rich_text": [{"text": {"content": item["section"][:2000]}}]}
    if item.get("published"):
        properties["Published"] = {"date": {"start": item["published"]}}
    return properties


def create_notion_row(data_source_id, item, ai_take, display_title):
    payload = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": build_notion_properties(item, ai_take, display_title),
    }
    http_json(
        "https://api.notion.com/v1/pages",
        method="POST",
        headers=notion_headers(),
        payload=payload,
    )


def main():
    require_env()
    items = load_feeds()
    now = datetime.now(timezone.utc)
    state = prune_state(load_state(), now)

    pending = [it for it in items if it.get("id") and it["id"] not in state]
    pending.sort(key=lambda it: it.get("published", ""))  # oldest first

    to_process = pending[:MAX_ITEMS_PER_RUN]
    remaining = len(pending) - len(to_process)

    if not to_process:
        print("No new items this run - skipping scan page creation.")
        save_state(state)
        return 0

    last_scan_titles = load_last_scan_titles()
    last_scan_titles_normalized = [(t, normalize_title(t)) for t in last_scan_titles]

    processed = []  # list of (item, ai_take, display_title) for items included in this scan
    exact_dupes = 0
    for item in to_process:
        title = item.get("title", "")
        verdict, matched = classify_against_last_scan(title, last_scan_titles_normalized)
        if verdict == "exact":
            # Already covered by the immediately preceding scan - skip the
            # Claude call and the Notion row entirely, but still mark it
            # synced so it doesn't keep getting re-checked every run.
            state[item["id"]] = item.get("published", now.isoformat())
            exact_dupes += 1
            continue

        label = f"{item.get('source', '')} / {title[:60]}"
        try:
            ai_take = ask_claude_for_take(item)
        except urllib.error.URLError as e:
            print(f"WARN: Claude summary failed for {label}: {e}")
            continue

        display_title = f"[SIMILAR] {title} | {matched}" if verdict == "similar" else title
        processed.append((item, ai_take, display_title))

    if exact_dupes:
        print(f"Skipped {exact_dupes} item(s) with a title matching the previous scan.")

    if not processed:
        print("WARN: nothing left to sync this run (all duplicates or failed takes).")
        save_state(state)
        return 0

    try:
        exec_summary = ask_claude_for_executive_summary([(it, take) for it, take, _ in processed])
    except urllib.error.URLError as e:
        print(f"WARN: executive summary failed, continuing without one: {e}")
        exec_summary = ""

    scan_title = chicago_scan_title(now)
    try:
        scan_page_id = create_scan_page(scan_title)
        append_summary_blocks(scan_page_id, exec_summary)
        data_source_id = create_scan_database(scan_page_id)
    except urllib.error.URLError as e:
        fatal(f"could not create scan page/database in Notion: {e}")

    synced = 0
    new_last_scan_titles = []
    for item, ai_take, display_title in processed:
        label = f"{item.get('source', '')} / {item.get('title', '')[:60]}"
        try:
            create_notion_row(data_source_id, item, ai_take, display_title)
        except urllib.error.URLError as e:
            print(f"WARN: Notion row write failed for {label}: {e}")
            continue
        state[item["id"]] = item.get("published", now.isoformat())
        new_last_scan_titles.append(item.get("title", ""))
        synced += 1

    save_state(state)
    save_last_scan_titles(new_last_scan_titles)
    print(f"Created '{scan_title}' with {synced}/{len(processed)} articles.")
    if remaining:
        print(f"{remaining} more pending item(s) queued for the next run (MAX_ITEMS_PER_RUN={MAX_ITEMS_PER_RUN}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
