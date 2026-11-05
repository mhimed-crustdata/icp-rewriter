"""Optional integrations: Slack incoming webhook, Notion page, and Claude (Anthropic).

All are off unless their env var is set, and all respect --dry-run.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, List, Optional

NOTION_VERSION = "2022-06-28"


def send_slack(text: str, dry_run: bool = False, webhook_url: Optional[str] = None) -> str:
    """Post `text` to a Slack incoming webhook. Returns what happened."""
    url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if dry_run:
        return "dry-run: Slack message not sent"
    if not url:
        return "SLACK_WEBHOOK_URL not set: Slack message skipped"
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
    return "Slack message sent"


def _text_block(kind: str, text: str) -> Dict[str, Any]:
    return {"object": "block", "type": kind,
            kind: {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}}


def notion_blocks(markdown: str, targets: List[Dict[str, Any]], max_targets: int = 60) -> List[Dict[str, Any]]:
    """Turn the ICP markdown and the target list into Notion blocks (Notion allows 100 per request)."""
    blocks: List[Dict[str, Any]] = []
    for line in markdown.splitlines():
        if not line.strip() or line.startswith("|---"):
            continue
        if line.startswith("## "):
            blocks.append(_text_block("heading_2", line[3:]))
        elif line.startswith("# "):
            continue  # the page title already carries it
        elif line.startswith("- "):
            blocks.append(_text_block("bulleted_list_item", line[2:].replace("**", "")))
        else:
            blocks.append(_text_block("paragraph", line))
    if targets:
        blocks.append(_text_block("heading_2", "Target list"))
        for t in targets[:max_targets]:
            bits = [t.get("name") or t.get("domain"), t.get("domain"), f"{t.get('employees')} people" if t.get("employees") else "",
                    t.get("stage"), t.get("industry"), t.get("country")]
            blocks.append(_text_block("bulleted_list_item", " | ".join(b for b in bits if b)))
    return blocks[:100]


def send_notion(title: str, markdown: str, targets: List[Dict[str, Any]], dry_run: bool = False,
                token: Optional[str] = None, parent_page_id: Optional[str] = None) -> str:
    """Create a Notion page under NOTION_PARENT_PAGE_ID holding the ICP and the target list."""
    token = token or os.environ.get("NOTION_TOKEN", "")
    parent = parent_page_id or os.environ.get("NOTION_PARENT_PAGE_ID", "")
    if dry_run:
        return "dry-run: Notion page not created"
    if not token or not parent:
        return "NOTION_TOKEN / NOTION_PARENT_PAGE_ID not set: Notion page skipped"
    body = {"parent": {"page_id": parent},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
            "children": notion_blocks(markdown, targets)}
    req = urllib.request.Request("https://api.notion.com/v1/pages", data=json.dumps(body).encode(), method="POST",
                                 headers={"authorization": f"Bearer {token}", "notion-version": NOTION_VERSION,
                                          "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
    return "Notion page created"


def claude_available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def ask_claude(prompt: str, max_tokens: int = 2000, client=None) -> Optional[str]:
    """One short Claude call. Returns None when Claude is not configured or declines.

    Needs `pip install anthropic` and ANTHROPIC_API_KEY. Model: ANTHROPIC_MODEL
    (default claude-opus-5-5). Uses low effort because the task is a short note.
    """
    if client is None:
        if not claude_available():
            return None
        import anthropic
        client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"),
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        return None
    text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
    return text.strip() or None
