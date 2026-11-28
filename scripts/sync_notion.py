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

Separately, each run's surviving articles are also checked against a
persistent "Storylines" database (storylines.json tracks it locally, kept to
a rolling STORYLINE_WINDOW_DAYS window). One AI call per run decides, for
each article, whether it continues an existing storyline or starts a new
one; either way it writes a short note on how the article relates to - and,
where relevant, how different outlets compare on - that storyline. This is
how "related developments across sources and over time" get linked, as
opposed to the single-scan executive summary above.

Required environment variables:
  ANTHROPIC_API_KEY
  NOTION_TOKEN
  NOTION_SCANS_DATA_SOURCE_ID       - the "Various scans" database (inline in
                                      the Reading Desk page); each run adds
                                      one row/page to it. That database's
                                      title property happens to be named
                                      "Name" (Notion's own default) -
                                      SCANS_TITLE_PROP below assumes that;
                                      update it if you ever rename that
                                      column in Notion.
  NOTION_STORYLINES_DATA_SOURCE_ID  - the "Storylines" database (created by
                                      this project directly via the Notion
                                      API, so its property names are fixed:
                                      Title/Summary/Last Updated/Article
                                      Count/Status).

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
STORYLINES_STATE_PATH = "storylines.json"
STATE_RETENTION_DAYS = 10
STORYLINE_WINDOW_DAYS = 30  # storylines idle longer than this stop being offered as a match
MAX_STORYLINES_IN_CONTEXT = 50  # keeps the classification prompt bounded even with many active storylines
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
NOTION_STORYLINES_DATA_SOURCE_ID = os.environ.get("NOTION_STORYLINES_DATA_SOURCE_ID")
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
            ("NOTION_STORYLINES_DATA_SOURCE_ID", NOTION_STORYLINES_DATA_SOURCE_ID),
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


def load_storylines_state(path=STORYLINES_STATE_PATH):
    """Returns {notion_page_id: {"title", "summary", "last_updated", "article_count"}}."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: could not read {path}, starting fresh: {e}")
        return {}


def save_storylines_state(state, path=STORYLINES_STATE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def prune_storylines_state(state, now):
    """Drops storylines idle longer than STORYLINE_WINDOW_DAYS from local
    tracking. Their Notion pages aren't deleted - they just stop being
    offered as a match, so a later related article starts a fresh storyline
    instead of reviving an old one."""
    cutoff = now - timedelta(days=STORYLINE_WINDOW_DAYS)
    kept = {}
    for page_id, info in state.items():
        try:
            if datetime.fromisoformat(info["last_updated"]) >= cutoff:
                kept[page_id] = info
        except (TypeError, ValueError, KeyError):
            continue
    return kept


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


def describe_error(e):
    """urllib's default str(HTTPError) is just 'HTTP Error 400: Bad Request'
    with no detail; Notion's actual response body explains what's wrong."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return f"{e} - {body[:500]}"
    return str(e)


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


def ask_claude(prompt, max_tokens, label="request"):
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        # Haiku 5.5 defaults to "medium" extended-thinking effort, which can
        # burn the entire max_tokens budget on invisible reasoning before
        # writing anything visible (confirmed live: stop_reason="max_tokens"
        # with block_types=["thinking"] and zero text, on a one-sentence
        # task). None of these prompts need chain-of-thought, so turn it off.
        "thinking": {"type": "disabled"},
    }
    data = http_json(
        "https://api.anthropic.com/v1/messages",
        method="POST",
        headers=anthropic_headers(),
        payload=payload,
    )
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    if not text:
        # No HTTP error here - the call succeeded but produced no visible
        # text (e.g. truncated by max_tokens before any text block, or a
        # content-block shape we don't expect). Log enough to diagnose
        # instead of silently returning "" with no trace of why.
        print(
            f"WARN: Claude {label} returned no text "
            f"(stop_reason={data.get('stop_reason')!r}, "
            f"block_types={[b.get('type') for b in blocks]!r})"
        )
    return text


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
    return ask_claude(prompt, max_tokens=200, label=f"take ({item.get('title', '')[:40]!r})")


#   Haiku 5.5 is $0.10/M input + $0.50/M output tokens (under 100K-token
#   prompts). Even at the ceiling below (4000 output tokens) plus ~150
#   one-line-take input tokens each from up to MAX_ITEMS_PER_RUN articles,
#   one run's total cost is well under a cent - nowhere near $1. The ceiling
#   exists so a pathological number of articles can't runaway the request,
#   not because cost is actually at risk.
EXEC_SUMMARY_BASE_TOKENS = 500
EXEC_SUMMARY_TOKENS_PER_ITEM = 15
EXEC_SUMMARY_MAX_TOKENS_CEILING = 4000


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
        "executive summary (a few sentences to a short paragraph, more if "
        "there are many unrelated stories to group - prose, no markdown "
        "headers) that groups related stories by theme, calls out what's "
        "most worth prioritizing and why, and can note what's safe to skip. "
        "Be specific - reference actual story topics, not generic phrases.\n\n"
        + "\n".join(lines)
    )
    max_tokens = min(
        EXEC_SUMMARY_MAX_TOKENS_CEILING,
        EXEC_SUMMARY_BASE_TOKENS + EXEC_SUMMARY_TOKENS_PER_ITEM * len(lines),
    )
    return ask_claude(prompt, max_tokens=max_tokens, label="executive summary")


STORYLINE_BASE_TOKENS = 400
STORYLINE_TOKENS_PER_ITEM = 90  # each assignment is a small JSON object plus a 1-2 sentence note
STORYLINE_MAX_TOKENS_CEILING = 4000


def _extract_json_object(text):
    """Claude is asked to return only JSON, but may still wrap it in prose or
    a markdown code fence; pull out the first {...} block rather than
    requiring an exact match."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def ask_claude_for_storyline_assignments(processed, active_storylines):
    """For each article in `processed` (list of (item, ai_take, display_title)),
    decide whether it continues one of `active_storylines` (list of
    (page_id, info) tuples) or starts a new one, and write a short note on
    how it relates to that storyline - including cross-source comparison
    when other articles in this batch cover the same thing.

    Returns a list of dicts: {"article": int, "match": "existing"|"new",
    "storyline": int (if existing), "title"/"summary" (if new), "note": str}.
    Returns [] if the model's output can't be parsed - callers should treat
    that as "skip storyline linking this run", not "everything is new".
    """
    storyline_lines = [
        f"{i}. {info['title']}: {info['summary']}" for i, (_, info) in enumerate(active_storylines)
    ] or ["(none yet - every article below will start a new storyline)"]
    article_lines = [
        f"{i}. [{it.get('source', '')}] {it.get('title', '')}: {take}"
        for i, (it, take, _) in enumerate(processed)
    ]
    prompt = (
        "You track ongoing news storylines for a personal reading dashboard. "
        "Below is a list of EXISTING STORYLINES (index, title, current summary) "
        "and a list of NEW ARTICLES from the latest scan (index, source, title, "
        "one-line take).\n\n"
        "For each new article, decide:\n"
        "- \"existing\": it's a genuine continuation of one existing storyline "
        "(the same ongoing situation/story, not just the same broad topic). "
        "Give that storyline's index and a 1-2 sentence note on what's new "
        "here, how it relates to the storyline's last known state, and - if "
        "other articles in this batch cover the same development - how "
        "sources compare.\n"
        "- \"new\": it doesn't continue any existing storyline. Give a short "
        "title (under 8 words), a 1-sentence summary of the storyline, and a "
        "1-sentence opening note.\n"
        "Default to \"new\" whenever there's no genuine existing match - almost "
        "any hard-news or policy/economics/politics article plausibly gets "
        "follow-up coverage later, so it's worth seeding a storyline for even "
        "on its first appearance. Only omit an article entirely (no \"new\" "
        "either) if it's inherently one-off with no realistic follow-up, e.g. "
        "an obituary, an awards announcement, or a single human-interest "
        "feature - that should be rare, not the default.\n\n"
        "Respond with ONLY a JSON object, no markdown fences, no commentary, "
        "in exactly this shape:\n"
        '{"assignments": [{"article": 0, "match": "existing", "storyline": 2, '
        '"note": "..."}, {"article": 1, "match": "new", "title": "...", '
        '"summary": "...", "note": "..."}]}\n\n'
        "EXISTING STORYLINES:\n" + "\n".join(storyline_lines) + "\n\n"
        "NEW ARTICLES:\n" + "\n".join(article_lines)
    )
    max_tokens = min(
        STORYLINE_MAX_TOKENS_CEILING,
        STORYLINE_BASE_TOKENS + STORYLINE_TOKENS_PER_ITEM * (len(active_storylines) + len(processed)),
    )
    text = ask_claude(prompt, max_tokens=max_tokens, label="storyline assignments")
    if not text:
        return []
    parsed = _extract_json_object(text)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("assignments"), list):
        print(f"WARN: storyline assignments response wasn't the expected JSON shape: {text[:300]!r}")
        return []
    return parsed["assignments"]


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


def create_storyline_page(title, summary, now_iso):
    payload = {
        "parent": {"type": "data_source_id", "data_source_id": NOTION_STORYLINES_DATA_SOURCE_ID},
        "icon": {"type": "emoji", "emoji": "🧵"},
        "properties": {
            "Title": {"title": [{"text": {"content": title[:2000]}}]},
            "Summary": {"rich_text": [{"text": {"content": summary[:2000]}}]},
            "Last Updated": {"date": {"start": now_iso}},
            "Article Count": {"number": 1},
            "Status": {"select": {"name": "Active"}},
        },
    }
    data = http_json(
        "https://api.notion.com/v1/pages",
        method="POST",
        headers=notion_headers(),
        payload=payload,
    )
    return data["id"]


def update_storyline_properties(page_id, summary, now_iso, article_count):
    payload = {
        "properties": {
            "Summary": {"rich_text": [{"text": {"content": summary[:2000]}}]},
            "Last Updated": {"date": {"start": now_iso}},
            "Article Count": {"number": article_count},
            "Status": {"select": {"name": "Active"}},
        }
    }
    http_json(
        f"https://api.notion.com/v1/pages/{page_id}",
        method="PATCH",
        headers=notion_headers(),
        payload=payload,
    )


def append_storyline_update(page_id, now_utc, item, note):
    local = now_utc.astimezone(CHICAGO)
    stamp = local.strftime("%-I:%M %p, %b %-d") if os.name != "nt" else local.strftime("%I:%M %p, %b %d")
    title = item.get("title", "(untitled)")
    source = item.get("source", "")
    link = item.get("link")
    text_runs = [{"type": "text", "text": {"content": f"{stamp} — {note}\n"}}]
    citation = {"type": "text", "text": {"content": f"{title} ({source})"}}
    if link:
        citation["text"]["link"] = {"url": link}
    text_runs.append(citation)
    children = [{"object": "block", "type": "callout", "callout": {"rich_text": text_runs, "icon": {"type": "emoji", "emoji": "🔗"}}}]
    http_json(
        f"https://api.notion.com/v1/blocks/{page_id}/children",
        method="PATCH",
        headers=notion_headers(),
        payload={"children": children},
    )


def create_scan_database(scan_page_id):
    # 2025-09-03 API: a NEW database's custom property schema must be nested
    # under initial_data_source.properties, NOT a top-level "properties" key
    # - a top-level "properties" is silently ignored, leaving Notion's bare
    # default single "Name" title column (this bit us once already).
    source_options = [{"name": name, "color": color} for name, color in SOURCE_COLORS.items()]
    payload = {
        "parent": {"type": "page_id", "page_id": scan_page_id},
        "title": [{"type": "text", "text": {"content": "Articles"}}],
        "initial_data_source": {
            "properties": {
                "Title": {"title": {}},
                "Source": {"select": {"options": source_options}},
                "Section": {"rich_text": {}},
                "Link": {"url": {}},
                "Published": {"date": {}},
                "AI Take": {"rich_text": {}},
                "Storyline": {"rich_text": {}},
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
            }
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


def build_notion_properties(item, ai_take, display_title, storyline_title=None):
    properties = {
        "Title": {"title": [{"text": {"content": display_title or item.get("title") or "(untitled)"}}]},
        "AI Take": {"rich_text": [{"text": {"content": (ai_take or "")[:2000]}}]},
        "Status": {"select": {"name": "New"}},
        "RSS ID": {"rich_text": [{"text": {"content": item["id"]}}]},
    }
    if storyline_title:
        properties["Storyline"] = {"rich_text": [{"text": {"content": storyline_title[:2000]}}]}
    if item.get("link"):
        properties["Link"] = {"url": item["link"]}
    if item.get("source"):
        properties["Source"] = {"select": {"name": item["source"]}}
    if item.get("section"):
        properties["Section"] = {"rich_text": [{"text": {"content": item["section"][:2000]}}]}
    if item.get("published"):
        properties["Published"] = {"date": {"start": item["published"]}}
    return properties


def create_notion_row(data_source_id, item, ai_take, display_title, storyline_title=None):
    payload = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": build_notion_properties(item, ai_take, display_title, storyline_title),
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
            print(f"WARN: Claude summary failed for {label}: {describe_error(e)}")
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
        print(f"WARN: executive summary failed, continuing without one: {describe_error(e)}")
        exec_summary = ""

    storylines_state = prune_storylines_state(load_storylines_state(), now)
    now_iso = now.isoformat()
    # Most-recently-updated first, capped so the classification prompt stays bounded.
    active_storylines = sorted(storylines_state.items(), key=lambda kv: kv[1]["last_updated"], reverse=True)
    active_storylines = active_storylines[:MAX_STORYLINES_IN_CONTEXT]

    try:
        assignments = ask_claude_for_storyline_assignments(processed, active_storylines)
    except urllib.error.URLError as e:
        print(f"WARN: storyline classification failed, continuing without storyline links: {describe_error(e)}")
        assignments = []

    storyline_titles = [None] * len(processed)  # aligned with `processed`, filled in below
    for assignment in assignments:
        try:
            idx = int(assignment["article"])
            item, ai_take, _ = processed[idx]
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        note = assignment.get("note", "")
        if assignment.get("match") == "existing":
            try:
                storyline_idx = int(assignment["storyline"])
                page_id, info = active_storylines[storyline_idx]
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            try:
                append_storyline_update(page_id, now, item, note)
                info["article_count"] = info.get("article_count", 1) + 1
                info["last_updated"] = now_iso
                update_storyline_properties(page_id, info.get("summary", ""), now_iso, info["article_count"])
            except urllib.error.URLError as e:
                print(f"WARN: storyline update failed for {info.get('title', '')[:60]!r}: {describe_error(e)}")
                continue
            storylines_state[page_id] = info
            storyline_titles[idx] = info.get("title")
        elif assignment.get("match") == "new":
            new_title = (assignment.get("title") or "").strip()
            new_summary = (assignment.get("summary") or "").strip()
            if not new_title:
                continue
            try:
                page_id = create_storyline_page(new_title, new_summary, now_iso)
                append_storyline_update(page_id, now, item, note)
            except urllib.error.URLError as e:
                print(f"WARN: could not create storyline {new_title[:60]!r}: {describe_error(e)}")
                continue
            storylines_state[page_id] = {
                "title": new_title,
                "summary": new_summary,
                "last_updated": now_iso,
                "article_count": 1,
            }
            storyline_titles[idx] = new_title

    save_storylines_state(storylines_state)

    scan_title = chicago_scan_title(now)
    try:
        scan_page_id = create_scan_page(scan_title)
        append_summary_blocks(scan_page_id, exec_summary)
        data_source_id = create_scan_database(scan_page_id)
    except urllib.error.URLError as e:
        fatal(f"could not create scan page/database in Notion: {describe_error(e)}")

    synced = 0
    new_last_scan_titles = []
    for (item, ai_take, display_title), storyline_title in zip(processed, storyline_titles):
        label = f"{item.get('source', '')} / {item.get('title', '')[:60]}"
        try:
            create_notion_row(data_source_id, item, ai_take, display_title, storyline_title)
        except urllib.error.URLError as e:
            print(f"WARN: Notion row write failed for {label}: {describe_error(e)}")
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
