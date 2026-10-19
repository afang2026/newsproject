#!/usr/bin/env python3
"""
Fetch RSS/Atom feeds listed in feeds.config.json and write feeds.json for
the Reading Desk static site.

Exit codes:
  0 - success, even if some or all individual feeds failed to fetch
  1 - feeds.config.json is missing or not valid JSON (fatal, worth failing CI on)
"""

import calendar
import hashlib
import html
import json
import os
import re
import socket
import sys
from datetime import datetime, timedelta, timezone

import feedparser

# feedparser has no timeout kwarg of its own; this bounds the underlying
# socket operations it (and urllib) perform.
socket.setdefaulttimeout(15)

USER_AGENT = "ReadingDeskFeedBot/1.0 (+https://github.com/afang2026/newsproject)"

CONFIG_PATH = "feeds.config.json"
OUTPUT_PATH = "feeds.json"
MAX_SUMMARY_CHARS = 400
MAX_AGE_DAYS = 7

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def load_config(path):
    """Load and validate feeds.config.json. Raises on missing/malformed file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    feeds = data.get("feeds")
    if not isinstance(feeds, list):
        raise ValueError('feeds.config.json must contain a top-level "feeds" array')
    return feeds


def load_existing_items(path):
    """Best-effort load of a previously-written feeds.json. Never fatal."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items")
        return items if isinstance(items, list) else []
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: could not read existing {path}, starting fresh: {e}")
        return []


def clean_summary(raw):
    """Strip HTML tags/entities, collapse whitespace, truncate to ~400 chars."""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS].rstrip() + "…"
    return text


def stable_id(entry, link):
    """Stable sha1-based id from the entry's guid/id, else its link."""
    basis = entry.get("id") or entry.get("guid") or link or ""
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def entry_published_iso(entry, fallback_dt):
    """ISO-8601 UTC string from entry.published_parsed/updated_parsed, else fallback."""
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct:
        try:
            ts = calendar.timegm(struct)
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    return fallback_dt.isoformat()


def fetch_feed(feed_cfg, run_time):
    """Fetch and normalize one feed's entries. Returns a list (possibly empty)."""
    source = feed_cfg.get("source", "")
    section = feed_cfg.get("section", "")
    url = feed_cfg.get("url", "")
    label = f"{source} / {section} ({url})"

    try:
        parsed = feedparser.parse(url, agent=USER_AGENT)
    except Exception as e:  # noqa: BLE001 - one bad feed must never kill the run
        print(f"WARN: {label} returned 0 items or failed: {e}")
        return []

    status = getattr(parsed, "status", None)
    if status is not None and not (200 <= status < 300):
        print(f"WARN: {label} returned 0 items or failed: HTTP {status}")
        return []

    if parsed.bozo and not parsed.entries:
        reason = parsed.get("bozo_exception", "unknown parse error")
        print(f"WARN: {label} returned 0 items or failed: {reason}")
        return []

    if not parsed.entries:
        print(f"WARN: {label} returned 0 items or failed: no entries in feed")
        return []

    items = []
    for entry in parsed.entries:
        link = entry.get("link", "") or ""
        summary_raw = entry.get("summary") or entry.get("description") or ""
        items.append(
            {
                "id": stable_id(entry, link),
                "source": source,
                "section": section,
                "title": (entry.get("title") or "").strip(),
                "summary": clean_summary(summary_raw),
                "link": link,
                "published": entry_published_iso(entry, run_time),
                "source_url": url,
            }
        )
    return items


def merge_items(existing_items, fresh_items):
    """Dedupe by id; on conflict, the item with the newer 'published' wins."""
    by_id = {}
    for item in existing_items + fresh_items:
        item_id = item.get("id")
        if not item_id:
            continue
        current = by_id.get(item_id)
        if current is None or item.get("published", "") > current.get("published", ""):
            by_id[item_id] = item
    return list(by_id.values())


def main():
    try:
        feeds = load_config(CONFIG_PATH)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"FATAL: could not load {CONFIG_PATH}: {e}", file=sys.stderr)
        return 1

    run_time = datetime.now(timezone.utc)

    fresh_items = []
    for feed_cfg in feeds:
        fresh_items.extend(fetch_feed(feed_cfg, run_time))

    existing_items = load_existing_items(OUTPUT_PATH)
    merged = merge_items(existing_items, fresh_items)

    cutoff = run_time - timedelta(days=MAX_AGE_DAYS)

    def is_recent(item):
        try:
            return datetime.fromisoformat(item["published"]) >= cutoff
        except (KeyError, ValueError, TypeError):
            return False

    merged = [item for item in merged if is_recent(item)]
    merged.sort(key=lambda item: item.get("published", ""), reverse=True)

    output = {
        "generatedAt": run_time.isoformat(),
        "items": merged,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Wrote {len(merged)} items to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
