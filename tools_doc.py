"""Regenerates the configured tools overview note from llm_client.TOOLS.

Tool definitions only change with a code change, and code only takes effect
after a restart, so writing the note at startup means it always describes the
tools actually running. Written only when the content changes, so vault
backups don't see a new version on every restart. The output lives in
OPENCLAVE_HOME unless the user configures another path.
"""
import json
from datetime import date

import agent_tools
import llm_client
import vault

DOC_PATH = vault.CONFIG.tools_doc_path
UPDATED_PLACEHOLDER = "{updated}"


def _size(tool: dict) -> int:
    return len(json.dumps(tool))


def _set(name: str) -> str:
    if name == agent_tools.ONBOARDING_TOOL:
        return "onboarding only"
    if name in agent_tools.MANAGER_TOOLS:
        return "manager only (always on)"
    if name in agent_tools.MAINTAINER_ONLY:
        return "optional (maintainer only)"
    return "core" if name in agent_tools.CORE_TOOLS else "optional"


def render(tools: list[dict]) -> str:
    """Note text with an {updated} placeholder for the date, so a change of
    day alone never counts as a content change."""
    tools = sorted(tools, key=_size, reverse=True)
    total = sum(_size(t) for t in tools)
    out = [f"""---
Name: Tools overview
Type: Reference
Description: Every tool the OpenClave agents get, with full description text and size, generated from llm_client.TOOLS.
tags:
  - openclave
  - tokens
Related:
  - "[[Architecture]]"
  - "[[System prompt size]]"
Updated: {UPDATED_PLACEHOLDER}
---

# Tools overview

{len(tools)} tools, {total:,} characters of definitions in total (roughly {total // 4:,} tokens). Each agent gets the **core** tools plus the **optional** ones chosen for it (Control Panel → Agents → Tools); an agent with no saved choice gets all optional tools. "Size" is the full definition as sent: description plus parameter descriptions. Sorted largest first.

Generated automatically from `llm_tool_defs.py` (`TOOLS`) each time the OpenClave service starts (`tools_doc.py`). Edits made here are overwritten; change the tool definitions in code instead.

## Summary

| # | Tool | Set | Size (chars) | Description | Parameters |
|---|---|---|---|---|---|
"""]
    for i, t in enumerate(tools, 1):
        props = t.get("parameters", {}).get("properties", {})
        out.append(f"| {i} | [[#{t['name']}]] | {_set(t['name'])} | {_size(t):,} | {len(t['description']):,} | {len(props)} |\n")
    out.append("\n## Tools\n")
    for t in tools:
        params = t.get("parameters", {})
        props, required = params.get("properties", {}), set(params.get("required", []))
        description = t["description"].replace("\n", "\n> ")
        out.append(f"\n### {t['name']}\n\n**Size:** {_size(t):,} chars total "
                   f"({len(t['description']):,} description)\n\n> {description}\n")
        if props:
            out.append("\n| Parameter | Required | Description |\n|---|---|---|\n")
            for name, spec in props.items():
                text = (spec.get("description") or "").replace("|", "\\|").replace("\n", " ")
                out.append(f"| `{name}` | {'yes' if name in required else ''} | {text} |\n")
    return "".join(out)


def write_if_changed() -> bool:
    content = render([t["function"] for t in llm_client.TOOLS])
    try:
        existing = DOC_PATH.read_text(encoding="utf-8")
    except OSError:
        existing = None
    if existing is not None:
        # Compare with the stored date masked out: only real tool changes rewrite.
        stored_date = next((line[len("Updated: "):] for line in existing.splitlines()
                            if line.startswith("Updated: ")), None)
        if stored_date and existing == content.replace(UPDATED_PLACEHOLDER, stored_date):
            return False
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(content.replace(UPDATED_PLACEHOLDER, str(date.today())), encoding="utf-8")
    return True
