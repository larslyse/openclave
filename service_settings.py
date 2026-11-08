"""Service-wide tunables set from the Control Panel.

Ops state, not agent knowledge, so it lives in OPENCLAVE_HOME/state,
not in the vault. Read fresh on every call so a Control Panel
change applies to the next message with no restart.
"""
import json
import re

import llm_models
from config import CONFIG

SETTINGS_FILE = CONFIG.state_path("service_settings.json")

# Automatic note lookup before each text message (retrieval.build_context):
#   off      — none; the agent still has search_vault when it decides it needs it
#   keywords — search only the 2 rarest words of the message (fast, focused)
#   full     — search with the whole message (the original behaviour; 5s+ on
#              long/voice-transcribed messages, measured 2026-09-24)
AUTO_LOOKUP_MODES = ("off", "keywords", "full")

# Opt-in: when the chosen model is out of credits or at its usage limit,
# retry once on this model and say so in the reply. "" = off.
# Digest push of attention.py's findings: "off", "daily" or "weekly"
# (Mondays), at digest_time in the configured timezone. Off by default: it's a notification
# The owner opts into (2026-09-27).
DIGEST_MODES = ("off", "daily", "weekly")
# Weekly review (2026-11-04): what the Monday cleanup (cleanup.py) and the
# manager's audit (audit.py) do, ticked in the Control Panel. Every item only
# suggests; nothing changes until the owner says yes. (key, label, part)
WEEKLY_REVIEW = (
    ("archive_stale", "Suggest archiving chats nobody has used for a while", "cleanup"),
    ("delete_empty", "Suggest deleting chats that never got going", "cleanup"),
    ("rename_unclear", "Suggest clearer names for vaguely named chats", "cleanup"),
    ("flag_agents", "Flag agents nobody talks to, or with too many open chats", "cleanup"),
    ("vault_notes", "Propose notes worth keeping in your main vault", "audit"),
    ("nudge_agents", "Propose nudges for agents that are stuck or stale", "audit"),
    ("tasks", "Propose other fixes and tasks the manager would do", "audit"),
    ("merge_agents", "Suggest merging agents whose domains overlap", "audit"),
    ("remove_old_models", "Suggest removing old models", "audit"),
    ("add_new_models", "Suggest adding new models", "audit"),
)
REVIEW_DEFAULTS = {key: True for key, _, _ in WEEKLY_REVIEW}
DEFAULTS = {"auto_lookup": "full", "fallback_model": "", "digest": "off", "digest_time": "07:30",
            "weekly_review": REVIEW_DEFAULTS, "multiple_claves": False,
            "active_clave": "", "sort_conversations": "newest", "sort_agent_tabs": "manual"}
# Control Panel → Sorting. Conversations: chat lists on desktop and phone.
# Agent tabs: the agent page's tab strip; "manual" is the dragged order
# (agent_order.py, shared with the dashboard), "recent" puts the agent with
# the latest message first.
SORT_CHOICES = {"sort_conversations": ("newest", "alphabetical"), "sort_agent_tabs": ("manual", "recent")}


def load() -> dict:
    try:
        stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    settings = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    review = settings["weekly_review"] if isinstance(settings["weekly_review"], dict) else {}
    settings["weekly_review"] = {k: bool(review.get(k, v)) for k, v in REVIEW_DEFAULTS.items()}
    return settings


def review_on(key: str) -> bool:
    """Is this Weekly review item ticked?"""
    return load()["weekly_review"].get(key, True)


def update(changes: dict) -> dict:
    unknown = set(changes) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    if "auto_lookup" in changes and changes["auto_lookup"] not in AUTO_LOOKUP_MODES:
        raise ValueError(f"auto_lookup must be one of {', '.join(AUTO_LOOKUP_MODES)}")
    if "digest" in changes and changes["digest"] not in DIGEST_MODES:
        raise ValueError(f"digest must be one of {', '.join(DIGEST_MODES)}")
    if "digest_time" in changes and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(changes["digest_time"])):
        raise ValueError("digest_time must be HH:MM")
    if changes.get("fallback_model") and changes["fallback_model"] not in {m.id for m in llm_models.CATALOG}:
        raise ValueError(f"unknown model: {changes['fallback_model']}")
    for key, choices in SORT_CHOICES.items():
        if key in changes and changes[key] not in choices:
            raise ValueError(f"{key} must be one of {', '.join(choices)}")
    if "active_clave" in changes and not isinstance(changes["active_clave"], str):
        raise ValueError("active_clave must be a clave id")
    if "multiple_claves" in changes and not isinstance(changes["multiple_claves"], bool):
        raise ValueError("multiple_claves must be true or false")
    if "weekly_review" in changes:
        review = changes["weekly_review"]
        if not isinstance(review, dict) or set(review) - set(REVIEW_DEFAULTS) or \
                not all(isinstance(v, bool) for v in review.values()):
            raise ValueError("weekly_review: tick boxes by name, true or false")
        changes = {**changes, "weekly_review": {**load()["weekly_review"], **review}}
    settings = {**load(), **changes}
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SETTINGS_FILE)
    return settings
