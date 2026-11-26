#!/usr/bin/env python3
"""
Read feeds.json and, for any new items, create a fresh page under the
"Various Scans" Notion page named "Scan at <time> (Chicago time)" containing
an AI-written executive summary plus a database of that run's articles.

Already-synced items are tracked in notion_synced.json (committed to the
repo) so re-runs never re-process the same article. Processing is capped per
run (MAX_ITEMS_PER_RUN) so a large backlog doesn't fire hundreds of API calls
at once; the rest is picked up on later runs.

Required environment variables:
  ANTHROPIC_API_KEY
  NOTION_TOKEN
  NOTION_SCANS_PARENT_PAGE_ID   - the "Various Scans" page, new scan pages
                                  are created as its children

Exit codes:
  0 - success, even if some individual items failed
  1 - missing required env vars, or feeds.json is missing/malformed (fatal)
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FEEDS_PATH = "feeds.json"
STATE_PATH = "notion_synced.json"
STATE_RETENTION_DAYS = 10
NOTION_VERSION = "2025-09-03"
ANTHROPIC_VERSION = "2023-06-01"
CHICAGO = ZoneInfo("America/Chicago")

MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", "40"))
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_SCANS_PARENT_PAGE_ID = os.environ.get("NOTION_SCANS_PARENT_PAGE_ID")

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
            ("NOTION_SCANS_PARENT_PAGE_ID", NOTION_SCANS_PARENT_PAGE_ID),
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
        "parent": {"type": "page_id", "page_id": NOTION_SCANS_PARENT_PAGE_ID},
        "icon": {"type": "emoji", "emoji": "📰"},
        "properties": {"title": [{"type": "text", "text": {"content": title}}]},
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


def build_notion_properties(item, ai_take):
    properties = {
        "Title": {"title": [{"text": {"content": item.get("title") or "(untitled)"}}]},
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


def create_notion_row(data_source_id, item, ai_take):
    payload = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": build_notion_properties(item, ai_take),
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

    processed = []  # list of (item, ai_take) for items we got a take for
    for item in to_process:
        label = f"{item.get('source', '')} / {item.get('title', '')[:60]}"
        try:
            ai_take = ask_claude_for_take(item)
        except urllib.error.URLError as e:
            print(f"WARN: Claude summary failed for {label}: {e}")
            continue
        processed.append((item, ai_take))

    if not processed:
        print("WARN: no items got a usable Claude take this run; nothing to sync.")
        save_state(state)
        return 0

    try:
        exec_summary = ask_claude_for_executive_summary(processed)
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
    for item, ai_take in processed:
        label = f"{item.get('source', '')} / {item.get('title', '')[:60]}"
        try:
            create_notion_row(data_source_id, item, ai_take)
        except urllib.error.URLError as e:
            print(f"WARN: Notion row write failed for {label}: {e}")
            continue
        state[item["id"]] = item.get("published", now.isoformat())
        synced += 1

    save_state(state)
    print(f"Created '{scan_title}' with {synced}/{len(processed)} articles.")
    if remaining:
        print(f"{remaining} more pending item(s) queued for the next run (MAX_ITEMS_PER_RUN={MAX_ITEMS_PER_RUN}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
