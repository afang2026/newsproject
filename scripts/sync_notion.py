#!/usr/bin/env python3
"""
Read feeds.json, ask Claude for a one-sentence "why this matters" take on
each new item, and create a page in the Notion "Reading Desk" database for
it.

Already-synced items are tracked in notion_synced.json (committed to the
repo) so re-runs never create duplicate pages. Processing is capped per run
(MAX_ITEMS_PER_RUN) so a large backlog (e.g. the first run) doesn't fire
hundreds of API calls at once; the rest is picked up on later runs.

Required environment variables:
  ANTHROPIC_API_KEY
  NOTION_TOKEN
  NOTION_DATABASE_ID

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

FEEDS_PATH = "feeds.json"
STATE_PATH = "notion_synced.json"
STATE_RETENTION_DAYS = 10
NOTION_VERSION = "2025-09-03"
ANTHROPIC_VERSION = "2023-06-01"

MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", "40"))
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")


def fatal(message):
    print(f"FATAL: {message}", file=sys.stderr)
    sys.exit(1)


def require_env():
    missing = [
        name
        for name, value in (
            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
            ("NOTION_TOKEN", NOTION_TOKEN),
            ("NOTION_DATABASE_ID", NOTION_DATABASE_ID),
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


def discover_data_source_id():
    """Notion's 2025-09-03 API splits a database into a container + data
    source(s); page creation targets the data source, not the database, so
    this does a one-time lookup rather than assuming the IDs are the same."""
    data = http_json(
        f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}",
        headers=notion_headers(),
    )
    sources = data.get("data_sources") or []
    if not sources:
        fatal("Notion database has no data sources - check NOTION_DATABASE_ID")
    return sources[0]["id"]


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
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 100,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    data = http_json(
        "https://api.anthropic.com/v1/messages",
        method="POST",
        headers=headers,
        payload=payload,
    )
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return text.strip()


def build_notion_properties(item, ai_take):
    properties = {
        "Title": {"title": [{"text": {"content": item.get("title") or "(untitled)"}}]},
        "AI Take": {"rich_text": [{"text": {"content": ai_take[:2000]}}]},
        "Status": {"status": {"name": "New"}},
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


def create_notion_page(data_source_id, item, ai_take):
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
        print("No new items to sync to Notion.")
        save_state(state)
        return 0

    try:
        data_source_id = discover_data_source_id()
    except (urllib.error.URLError, KeyError, ValueError) as e:
        fatal(f"could not resolve Notion data source for database {NOTION_DATABASE_ID}: {e}")

    synced = 0
    for item in to_process:
        label = f"{item.get('source', '')} / {item.get('title', '')[:60]}"
        try:
            ai_take = ask_claude_for_take(item)
        except urllib.error.URLError as e:
            print(f"WARN: Claude summary failed for {label}: {e}")
            continue
        try:
            create_notion_page(data_source_id, item, ai_take)
        except urllib.error.URLError as e:
            print(f"WARN: Notion write failed for {label}: {e}")
            continue
        state[item["id"]] = item.get("published", now.isoformat())
        synced += 1

    save_state(state)
    print(f"Synced {synced}/{len(to_process)} items to Notion.")
    if remaining:
        print(f"{remaining} more pending item(s) queued for the next run (MAX_ITEMS_PER_RUN={MAX_ITEMS_PER_RUN}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
