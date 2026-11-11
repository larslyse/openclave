"""The tool definitions every route offers the model (OpenAI function format),
split out of llm_client.py. tools_for in llm_tools picks each agent's subset."""

import config
import vault


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_backlog",
            "description": "Add a new open item to the agent's backlog, or mark an existing item done/not done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "check", "uncheck"]},
                    "item": {"type": "string", "description": "The backlog item text (for 'add'), or a substring identifying an existing item (for 'check'/'uncheck')."},
                },
                "required": ["action", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan_phase",
            "description": "Update the completion percent and/or status of a phase in the current plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phase": {"type": "string", "description": "Phase name."},
                    "percent": {"type": "integer", "minimum": 0, "maximum": 100},
                    "status": {"type": "string", "enum": ["not-started", "in-progress", "complete"]},
                },
                "required": ["phase", "percent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_mission",
            "description": (
                "Replace your 'mission.md' body — the living baseline of who the user is and what you're "
                "tracking for them (shown to you above as '# Mission'). Use this whenever conversation "
                "reveals something that should update your standing context (a changed constraint, a "
                "new goal, a corrected fact) rather than just living in this session's chat log. Write "
                "the full new body — keep sections that are still true, don't just append. This file "
                "also has a '## Domains' bullet list that other agents see as your expertise tags — "
                "keep that section intact (or update it deliberately, not by accident) when you rewrite "
                "the body. Frontmatter is preserved automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "Full replacement markdown body (no frontmatter)."},
                },
                "required": ["body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_persona",
            "description": (
                "Replace your own persona.md body — who you are and your tone (shown to you above, "
                "before '# Mission'). Use this rarely: only when the user explicitly asks you to change how "
                "you operate, or you've found a genuinely better way to describe your own lane. Don't "
                "use it to expand your own scope unprompted, and mention in your reply that you changed "
                "it so the user notices. Frontmatter is preserved automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "Full replacement markdown body (no frontmatter)."},
                },
                "required": ["body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_feedback",
            "description": (
                "Log one specific correction or confirmation about HOW you interact with the user — not a "
                "domain fact (that's update_mission) and not a full personality rewrite (that's "
                "update_persona), but a short durable rule appended to your own persona.md under "
                "'## Feedback'. Use this whenever the user corrects your approach ('stop doing X', 'don't add "
                "Y') or clearly confirms something worked well without being asked — quiet confirmations "
                "count too, not just corrections. Don't wait to be asked; log it right when it happens."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entry": {"type": "string", "description": "One short, self-contained rule, e.g. 'Give the number first on routine metric checks, add commentary only if asked — the user checks these mid-task and wants the fact, not framing.'"},
                },
                "required": ["entry"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_vault",
            "description": (
                "Keyword-search the owner's whole Obsidian vault for anything not already covered by the "
                "'Retrieved context' section above. Use this whenever the user references something "
                "specific you don't have, asks you to look something up, says they added new info, "
                "or a question needs vault content beyond your mission/plan/backlog. "
                "Every word of a search must match, so keep each alternative to 1-3 words. "
                + vault.search_language_hint()
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords, short and specific; alternatives separated by |."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_vault_file",
            "description": "Read the full contents of one specific vault file using an absolute vault path from your context directory or a qmd:// URI from search_vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute vault file path or qmd:// URI."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_memory",
            "description": (
                "Save a freeform entry in your own memory/ folder — the LOWEST-priority bucket, for "
                "genuinely one-off things that don't need to be in every future context window (a "
                "passing thought, a summary of something you looked into). Not auto-loaded into future "
                "chats — use list_memories / read_memory to pull one back up when it's relevant. "
                "Do NOT use this for a durable protocol/equipment fact that should shape every future "
                "recommendation — that's update_mission. Do NOT use this for a dated qualitative "
                "dated qualitative information — that's log_measurement in a section from your "
                "tracking.md, so it stays in the readable trend record instead of an unindexed folder."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short title — used to generate the note's filename."},
                    "body": {"type": "string", "description": "Note content, markdown."},
                },
                "required": ["title", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": "List your own saved memory entries (title + date), most recent first.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "Read the full content of one of your own memory entries, by id (from list_memories).",
            "parameters": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string"},
                },
                "required": ["entry_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_measurement",
            "description": (
                "Record one dated measurement, activity, or qualitative note in your tracking log. "
                "This is the home "
                "for ANYTHING dated that's worth watching over time, whether it's a number or free text: "
                "if forgetting it by next session would change the advice you give, and it's tied to a "
                "specific date rather than being a standing fact, it belongs here, not in memory/. Call "
                "get_measurements with no metric filter first if you're unsure what metric names are "
                "already in use, so you reuse the same name consistently instead of a near-duplicate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "Short, consistent name for the value or topic being tracked."},
                    "value": {"type": "string", "description": "The recorded value. Leave empty for a note without a single value."},
                    "unit": {"type": "string", "description": "Optional unit for the value."},
                    "note": {"type": "string", "description": "Free-text context for this entry, or the full content for a note-only entry."},
                    "date": {"type": "string", "description": "Optional date (YYYY-MM-DD). Defaults to today if omitted."},
                    "section": {
                        "type": "string",
                        "description": (
                            "Section heading from your tracking.md file. Reuse an existing heading; "
                            "if the file has no sections yet, use 'Progress'."
                        ),
                    },
                },
                "required": ["metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_measurements",
            "description": (
                "Read back entries from one section in your tracking.md, most recent first. Omit metric "
                "to see every metric in that section; pass metric to filter to one trend."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "Optional metric name filter."},
                    "limit": {"type": "integer", "description": "Optional max number of entries to return."},
                    "section": {"type": "string", "description": "A section heading from your tracking.md; defaults to Progress."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_foundation",
            "description": "Record a durable decision basis in optional foundations.md. Use kind=claim for evidence-based knowledge, with source and uncertainty; use kind=operating_rule for agreed rules, with rationale and no confidence score. Read get_foundations first. Do not invent entries or create filler just to populate the file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Topic section, or general."},
                    "foundation": {"type": "string", "description": "Concise claim or agreed operating rule. State limitations or uncertainty where relevant."},
                    "kind": {"type": "string", "enum": ["claim", "operating_rule"]},
                    "reference": {"type": "string", "description": "Source and evidence limitations for a claim; agreement context and rationale for an operating rule."},
                    "confidence": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Optional confidence for claims only. Omit for operating rules."},
                },
                "required": ["topic", "foundation", "kind", "reference"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_foundations",
            "description": "Read back recorded foundations — one topic if given, or every topic concatenated if omitted. Use before add_foundation to avoid duplicates, or when a recommendation needs to cite what it's grounded in.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Optional: one sub-topic section, e.g. 'foot'. Omit to read everything."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_own_dashboard",
            "description": "Read the current HTML of your own dashboard page (what's shown at your own /<agent> URL), so you can edit it incrementally instead of guessing its current state.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_own_dashboard",
            "description": (
                "Replace your own dashboard page with new HTML. This is YOUR page — you decide what's "
                "useful to show given what you're currently working on with the user (plan, backlog, "
                "conversation history, or a different layout entirely). It's a single static file: inline "
                "your own <style> and <script>, no build step or restart needed — changes are live the "
                "next time the page loads. JSON endpoints your own JS can fetch from (replace <agent> "
                "with your own agent id): GET /<agent>/plan, GET /<agent>/backlog, "
                "GET /<agent>/tracking (optional ?metric=&limit= query params), "
                "POST /<agent>/tracking (body: {metric, value?, unit?, note?, date?} — writes straight to "
                "the tracking log with no LLM call, so it's the right target for quick-log buttons: instant "
                "and free, unlike routing a button press through /chat), "
                "GET /<agent>/conversations, GET /<agent>/conversations/{id}, POST /<agent>/chat "
                "(body: {message, conversation_id}). The previous version is automatically backed up "
                "before being overwritten. "
                "Your page runs sandboxed (no access to other agents' pages or endpoints — plain "
                "fetch('/<agent>/...') calls to your OWN endpoints work exactly as shown above, nothing to "
                "change there). localStorage/sessionStorage work but don't persist across a reload; don't "
                "rely on them for anything that needs to survive that. "
                "Shared chat/conversation-list/backlog/kanban/usage/markdown-rendering behavior lives in "
                "/static/dashboard_common.css and /static/dashboard_common.js — link/script-src those two "
                "rather than pasting your own copies (read_own_dashboard shows the current pattern: a small "
                "<style> block with your color variables, the HTML skeleton, then those two includes, then "
                "only your own genuinely custom panels). Keeps every dashboard's shared code fixable in one "
                "place instead of seven."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {"type": "string", "description": "Full HTML document to save as the new dashboard page."},
                },
                "required": ["html"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_note",
            "description": (
                "Create or update a plain note file inside your own folder — for structured, ongoing "
                "content that doesn't fit your other tools (a per-project file, an inventory list, working "
                "notes). Path is relative to your own folder root and must end in .md or .txt. Reserved "
                "paths (dashboard.html, backlog.md, plan.md, mission.md, persona.md, foundations.md, "
                "tracking.md, conversations/, memory/) are refused — those already have dedicated tools. "
                "To update a note that already exists, call read_note first and pass back its version, so "
                "a stale rewrite can't silently clobber a change you haven't seen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside your own folder, e.g. 'projects/motor-control.md'."},
                    "content": {"type": "string", "description": "Full new content of the note."},
                    "expected_version": {"type": "string", "description": "Version string from the last read_note/write_note of this path. Omit only when creating a brand-new file."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "Read one of your own note files by path (from list_notes or a path you already know). Returns a version marker to pass back to write_note when updating it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside your own folder."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "List your own note files created via write_note, most recently modified first.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clave_backlog",
            "description": (
                "Your clave's shared backlog (work that spans its agents). read it, add an item, or (the lead "
                "only, when there is one) check/uncheck an item by part of its text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "add", "check", "uncheck"]},
                    "item": {"type": "string", "description": "Item text for add, or part of it for check/uncheck."},
                    "clave": {"type": "string", "description": "Clave id; only needed if you are in more than one."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_vault_note",
            "description": (
                "Write in the vault folders the owner gave you beyond your own folder (listed in your "
                "instructions; refused anywhere else). mode 'append' adds to an existing note, 'create' makes "
                "a new note in an existing folder. Never deletes. Search first and prefer appending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Vault-relative note path, e.g. 'Area/Blog/Post title.md'."},
                    "content": {"type": "string", "description": "The Markdown to append, or the whole new note."},
                    "mode": {"type": "string", "enum": ["append", "create"]},
                    "change_summary": {"type": "string", "description": "One line: what changed and why."},
                },
                "required": ["path", "content", "mode", "change_summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_pdf",
            "description": (
                "Make a print-ready A4 PDF of a note anywhere in the user's vault, saved next to the note with "
                "the same name (.pdf); it syncs wherever the user's notes sync. Use it when the user asks for a "
                "PDF. Figures stay with their captions, tables and short sections are kept on one page, "
                "headings never end a page. To force a new page, put <div class=\"page-break\"></div> on "
                "its own line in the note (edit the note first if the user wants that); an italic line right "
                "after an image becomes its caption. Notes in configured confidential folders are never exported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "The note: its vault path, a path or qmd:// URI from search_vault, or just its name."},
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "message_agent",
            "description": (
                "Send another OpenClave agent a message: a question in their domain, or data they should "
                "keep (a purchase to log, a measurement, a finding). It lands in their 'From <you>' "
                "conversation, they act on it and answer there, and the user can read it. You don't get the "
                "answer back unless they message you. Put everything they need in the message itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "The recipient's name from the agent roster."},
                    "message": {"type": "string", "description": "The full message, with any data they need."},
                },
                "required": ["agent", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flag_to_maintainer",
            "description": (
                "Send an improvement for the OpenClave app itself to the maintainer: a bug, a broken "
                "or missing tool, or a feature idea. The one deliberate exception to only writing inside your "
                "own folder. Not for research, lookups or domain questions: do those yourself (you have web "
                "search). Returns an item id; use check_maintainer_request with it later if the outcome matters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "A short, specific description of the bug or improvement."},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_maintainer_request",
            "description": "Check the status/result of something previously flagged to the maintainer via flag_to_maintainer, using the id it returned. Status is 'open' (not yet looked at), 'acknowledged' (seen, no result yet), or 'resolved' (result field has the answer).",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "The id returned by flag_to_maintainer."},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_maintainer_inbox_item",
            "description": (
                "Maintainer-only: acknowledge or resolve an inbox item. Use 'acknowledged' once you've seen it "
                "and are working it, 'resolved' once done — pass result with the answer/outcome so the "
                "flagging agent can retrieve it via check_maintainer_request without the user relaying it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "The inbox item's id."},
                    "status": {"type": "string", "enum": ["acknowledged", "resolved"], "description": "New status."},
                    "result": {"type": "string", "description": "Answer/outcome to store, typically set together with status='resolved'."},
                },
                "required": ["item_id"],
            },
        },
    },
]

# The manager's proposals (proposals.py): it asks before it acts.
TOOLS.append({
    "type": "function",
    "function": {
        "name": "propose_to_owner",
        "description": (
            "Manager only. Propose something you want to do, for the user to answer Yes / No / Discuss on your "
            "page; nothing happens until they say yes. kind=vault_note: write something worth keeping into the "
            "main vault (target = the note's vault path from search_vault; mode=append to an existing note, the "
            "default, or create for a new note in an existing folder; content = the exact Markdown to add, "
            "distilled, with a source line; source = the agent file it came from). kind=message_agent: a message "
            "to another agent (agent = its name, content = the whole message). kind=task: anything else you'd do "
            "yourself (content = exactly what); on yes you're told to go ahead in your Audit conversation. Agent "
            "folders and confidential folders are never written."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["vault_note", "message_agent", "task"]},
                "title": {"type": "string", "description": "A few words, shown as the card's title."},
                "why": {"type": "string", "description": "One or two sentences: why the user would want it."},
                "content": {"type": "string", "description": "The note text, the message, or the task."},
                "target": {"type": "string", "description": "vault_note: the note's path inside the vault."},
                "mode": {"type": "string", "enum": ["append", "create"], "description": "vault_note only."},
                "agent": {"type": "string", "description": "message_agent: the recipient's name."},
                "source": {"type": "string", "description": "vault_note: where it came from (agent file path)."},
            },
            "required": ["kind", "title", "why", "content"],
        },
    },
})

# Skills (skills.py): know-how loaded on demand; the prompt lists only names.
TOOLS.append({
    "type": "function",
    "function": {
        "name": "read_skill",
        "description": (
            "Load one of your skills (listed under Skills in your instructions) before doing a job it covers, "
            "then follow it. file= reads another file that comes with the skill."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill's name from the list."},
                "file": {"type": "string", "description": "Optional: a file inside the skill's folder."},
            },
            "required": ["name"],
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "save_skill",
        "description": (
            "Write down how to do a recurring job as one of your own skills, so next time you (or a scheduled "
            "task) can follow it: after a job went well, or when the user explains how they want something done. "
            "Saving under an existing name of yours rewrites it; read it first and keep what still holds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short lowercase name, e.g. 'ha-update'."},
                "description": {"type": "string", "description": "One sentence: what it's for and when to use it."},
                "instructions": {"type": "string", "description": "The steps and rules, in Markdown."},
            },
            "required": ["name", "description", "instructions"],
        },
    },
})

# The agent's own mobile page (custom_pages.py). check_page is read-only so
# it works on Codex too, where the agent writes page.json with its own tools.
TOOLS.append({
    "type": "function",
    "function": {
        "name": "check_page",
        "description": (
            "Your own page in the user's phone app (the button left of your name): shows the page format "
            "and what the user currently sees, with any errors. Call it before building or changing your "
            "page, and again after writing page.json."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "write_page",
        "description": (
            "Save your own page for the user's phone app (page.json in your folder; call check_page first "
            "for the format). It's checked before saving and refused with the reason if a block is wrong. "
            "Build it from your own files so it stays current when you update them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "page": {"type": "object", "description": "{\"title\": ..., \"blocks\": [...]} as check_page describes."},
                "html": {"type": "string", "description": "Optional: full HTML saved as page.html, for an html block."},
            },
            "required": ["page"],
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "restore_page",
        "description": (
            "Put an earlier version of your page back, e.g. when the user says \"revert your page\". "
            "Afterwards tell the user which version is back and ask before changing it again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["phone", "desktop"],
                         "description": "phone = page.json (default); desktop = dashboard.html."},
                "version": {"type": "integer", "description": "From check_page's version list. Leave out for the last "
                            "phone version the user kept (desktop: the one before now)."},
            },
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "finish_onboarding",
        "description": (
            "Onboarding only, and only after the user has confirmed your proposal: saves which optional "
            "tools you get and ends onboarding. Write your mission.md and persona.md first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tools": {"type": "array", "items": {"type": "string"},
                          "description": "Optional tool names the user agreed to. Core tools are always included; don't list them."},
                "tagline": {"type": "string", "description": "Optional improved one-line description for the dashboard (max 60 chars)."},
                "avatar_svg": {"type": "string", "description": "Optional avatar the user agreed to: one small SVG, viewBox 0 0 64 64, plain shapes, under 20 KB."},
                "topics": {"type": "array", "items": {"type": "string"},
                           "description": "Chat topics the user agreed to (usually one per domain, short names). Created empty, in this order."},
            },
            "required": ["tools"],
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "find_voice",
        "description": (
            "Search Fish Audio's voice library for a voice for you to speak with in voice mode. Returns "
            "a few voices with id, description and a sample link. Give the user 2-3 that fit, each with its "
            "sample link so they can listen, and let them pick; then set_voice."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, e.g. 'calm deep male', 'warm british female', 'energetic young'."},
                "language": {"type": "string", "description": "Voice language code, default 'en'."},
            },
            "required": ["query"],
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "set_voice",
        "description": "Use this Fish Audio voice for your voice-mode replies from now on. Only the one the user picked.",
        "parameters": {
            "type": "object",
            "properties": {"voice_id": {"type": "string", "description": "The id from find_voice."}},
            "required": ["voice_id"],
        },
    },
})

TOOLS.append({
    "type": "function",
    "function": {
        "name": "schedule_reminder",
        "description": (
            "Send the user a message later: a push notification to their phone, also posted in your "
            "'Reminders' conversation, where they can reply. For reminders, follow-ups on a future event, "
            "and recurring notes (e.g. a weekly watering note). By default the message is sent exactly as "
            "you write it now, so make it self-contained. With compose=true, message is instead an instruction "
            "you'll follow when it's due (e.g. 'check tomorrow's rain forecast for Oslo; tell the user whether to "
            "water the tomatoes'): you then run once, may only look things up, and your reply is what they get. "
            "Use compose only when the message depends on information you can't have now. "
            f"Times are local time ({config.CONFIG.timezone}); the current "
            "time is in each message's 'Now' line. Schedule only what the user asked for or agreed to. "
            "action='list' shows what's scheduled (with ids); action='cancel' removes one by id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "list", "cancel"]},
                "message": {"type": "string", "description": "add: the text the user receives (under 1000 characters); with compose=true, your instruction for then."},
                "compose": {"type": "boolean", "description": "add: true = write the message when it's due, from the instruction in message."},
                "at": {"type": "string", "description": "add: when, as 'YYYY-MM-DD HH:MM' local time; for a repeat, the first time."},
                "repeat": {"type": "string", "enum": ["daily", "weekly", "monthly"], "description": "add: leave out for a one-off."},
                "reminder_id": {"type": "string", "description": "cancel: the id from add or list."},
            },
            "required": ["action"],
        },
    },
})
