"""Model catalog + per-agent selection — the one place that declares which
LLMs agents can use.

Adding a model means adding one entry here. Selection is persisted to
OPENCLAVE_HOME/state/agent_models.json (not the vault — this is ops config, not agent knowledge,
same reasoning as color/tagline living in agents.py rather than a vault file)
and read by llm_client at call time.
"""
import json
import re
from dataclasses import dataclass
from datetime import date

from config import CONFIG
from state_io import load_json, save_json

SELECTION_FILE = CONFIG.state_path("agent_models.json")


@dataclass(frozen=True)
class ModelOption:
    id: str          # selection key — persisted in agent_models.json and used as the dashboard <select> value
    provider: str     # "xai" | "anthropic" | "claude-cli" | "codex-cli" — picks which client llm_client uses
    display_name: str
    tier: str         # short human note for the dashboard, e.g. "default", "heavy coding"
    # The literal model string sent to the provider. None means "same as id" —
    # true for every entry except the paired grok-4.6 variants below, which
    # need two distinct selection ids (fast vs. default effort) mapped to one
    # real xAI model.
    api_model_id: str | None = None
    # xai only, forwarded to the API as-is (e.g. "low"/"high"). Measured live
    # 2026-09-19 against grok-4.6 with Rocky's real system prompt: "low" cut a
    # real query from ~15s to ~5s avg with no loss of coherence. Left unset
    # for every other xai entry (untested there).
    reasoning_effort: str | None = None
    # Deliberately False for every entry until each provider's chat_client
    # function actually sends image content (tracked separately from the
    # attachment storage/API plumbing this field ships alongside) — attaching
    # an image is accepted before then, but chat_service rejects sending with
    # one rather than silently dropping it, per Mobile Build Requirements
    # §10: "A filename in the prompt is not image understanding."
    supports_vision: bool = False
    api_input_usd: float | None = None  # per 1M tokens; informational for CLI-backed models
    api_output_usd: float | None = None
    # Relative speed within its route, for grouping the model pickers
    # (API fast / API slow / CLI fast / CLI slow). Measured 2026-09-24 with a
    # trivial "reply ok" call: Sonnet 5 2.0s, Grok 4.6 Fast 5.4s, Codex GPT-5.6
    # models 5.8-8.7s, claude-cli Sonnet 4.6 17.9s. The rest (Grok 4.3/4.6/4.7,
    # Opus 5/5.5, Opus 4.6, GPT-6 Astra) are estimates from effort and model size.
    speed: str = "fast"
    auto_added: str | None = None  # date, for models added by model_updater.py
    # "YYYY-MM": the provider's release month (xAI and Anthropic list it).
    # OpenAI's Codex list has no dates, so for those it's the month the model
    # was added to OpenClave (released_is_added).
    released: str | None = None

    @property
    def route(self) -> str:
        return "API" if self.provider in ("xai", "anthropic") else "CLI"


CATALOG: list[ModelOption] = [
    ModelOption(id="grok-4.3", released="2026-04", provider="xai", display_name="Grok 4.3", supports_vision=True, tier="balanced xAI"),
    ModelOption(
        id="grok-4.6", released="2026-08", speed="slow", provider="xai", display_name="Grok 4.6", supports_vision=True, tier="flagship xAI, slow (~15s/reply)",
        reasoning_effort="high",
    ),
    ModelOption(
        id="grok-4.6-fast", released="2026-08", api_model_id="grok-4.6", provider="xai", display_name="Grok 4.6 Fast", supports_vision=True,
        tier="flagship xAI, fast (~5s/reply)", reasoning_effort="low",
    ),
    ModelOption(
        id="grok-4.7-low", released="2026-09", api_model_id="grok-4.7", provider="xai", display_name="Grok 4.7 Low", supports_vision=True,
        tier="new xAI; latency unmeasured", reasoning_effort="low", api_input_usd=2, api_output_usd=6,
    ),
    ModelOption(
        id="claude-sonnet-5", released="2026-06", provider="anthropic", display_name="Claude Sonnet 5", tier="complex ops",
        supports_vision=True, api_input_usd=2, api_output_usd=10,
    ),
    ModelOption(
        id="claude-opus-5", released="2026-07", speed="slow", provider="anthropic", display_name="Claude Opus 5", tier="heavy coding",
        supports_vision=True, api_input_usd=5, api_output_usd=25,
    ),
    # Verified 2026-10-02 (Rick's backlog): id, $4/$20, 1M context and vision
    # from Anthropic's model reference; live "reply OK" 2.2s over the API.
    # Thinking can't be disabled and forced tool_choice is rejected; this
    # client sends neither. Successor to Opus 5 at a lower price.
    ModelOption(
        id="claude-opus-5-5", released="2026-09", provider="anthropic", display_name="Claude Opus 5.5", tier="current flagship, cheaper than Opus 5",
        supports_vision=True, api_input_usd=4, api_output_usd=20,
    ),
    ModelOption(
        id="claude-sonnet-4-6", released="2026-02", speed="slow", provider="claude-cli", display_name="Claude Sonnet 4.6", supports_vision=True, tier="default"
    ),
    ModelOption(
        id="claude-opus-4-6", released="2026-02", speed="slow", provider="claude-cli", display_name="Claude Opus 4.6", supports_vision=True, tier="heavy coding"
    ),
    # Same model on the Claude subscription: 4.7s bare `claude -p` 2026-10-02.
    # Supports agent connections like every claude-cli model (R2's HA).
    ModelOption(
        id="claude-opus-5-5-cli", released="2026-09", api_model_id="claude-opus-5-5", speed="slow", provider="claude-cli",
        display_name="Claude Opus 5.5 (subscription)", supports_vision=True, tier="current flagship, heavy work", api_input_usd=4, api_output_usd=20,
    ),
    ModelOption(
        id="gpt-5.6-terra", released="2026-09", provider="codex-cli", display_name="GPT-5.6 Terra", tier="balanced", supports_vision=True,
        api_input_usd=2, api_output_usd=12,
    ),
    # Sol/Luna are real, current-generation siblings of Terra — confirmed via
    # `codex debug models` (2026-09-20), not guessed: Sol defaults to low
    # reasoning effort ("reliable agentic workhorse for everyday tasks"),
    # Luna to medium ("fast and affordable agentic coding model"). Codex's
    # subscription path already forces reasoning_effort="low" for every model
    # regardless of this field (see _chat_codex_cli), so the distinction that
    # actually reaches the API today is model choice, not this field.
    ModelOption(
        id="gpt-5.6-sol", released="2026-09", provider="codex-cli", display_name="GPT-5.6 Sol", tier="reliable, quick", supports_vision=True,
        api_input_usd=4, api_output_usd=20,
    ),
    ModelOption(
        id="gpt-5.6-luna", released="2026-09", provider="codex-cli", display_name="GPT-5.6 Luna", tier="fast, affordable", supports_vision=True,
        api_input_usd=.2, api_output_usd=1.2,
    ),
    # Found by the Control Panel's new-model check (model_discovery.py) and added
    # 2026-09-24 after a live test: both work over the Codex route at the same
    # speed as Terra (5.2-5.8s for a trivial reply). Descriptions and image
    # support from `codex debug models`; API prices not published there.
    ModelOption(
        id="gpt-6-sol", released="2026-09", provider="codex-cli", display_name="GPT-6 Sol", tier="workhorse for coding and everyday work",
        supports_vision=True,
    ),
    ModelOption(
        id="gpt-6-luna", released="2026-09", provider="codex-cli", display_name="GPT-6 Luna", tier="fast and affordable, easier tasks",
        supports_vision=True,
    ),
    ModelOption(
        id="gpt-6-astra", released="2026-09", speed="slow", provider="codex-cli", display_name="GPT-6 Astra", tier="heavy coding", supports_vision=True
    ),
]

BUILTIN = list(CATALOG)

# Models model_updater.py added after a test reply, and models it hid from
# the pickers (state in OPENCLAVE_HOME/state/auto_models.json). CATALOG and _BY_ID
# below are updated in place by apply_auto, so every importer sees them.
AUTO_FILE = CONFIG.state_path("auto_models.json")
HIDDEN: set[str] = set()
PROVIDER_OLDER: set[str] = set()  # catalog ids the provider itself calls older (model_updater)


def _read_auto() -> dict:
    data = load_json(AUTO_FILE, {})
    return data if isinstance(data, dict) else {}


def apply_auto(state: dict | None = None) -> None:
    state = _read_auto() if state is None else state
    fields = set(ModelOption.__dataclass_fields__)
    added = []
    for raw in state.get("added", []):
        try:
            model = ModelOption(**{k: v for k, v in raw.items() if k in fields})
        except TypeError:
            continue
        if model.id not in {m.id for m in BUILTIN}:
            added.append(model)
    CATALOG[:] = BUILTIN + added
    _BY_ID.clear()
    _BY_ID.update({m.id: m for m in CATALOG})
    HIDDEN.clear()
    HIDDEN.update(h["id"] for h in state.get("hidden", []))
    PROVIDER_OLDER.clear()
    PROVIDER_OLDER.update(state.get("provider_older", []))


def in_use() -> set[str]:
    """Models an agent (or the out-of-credits fallback) is set to."""
    used = set(_load_selections().values())
    try:
        settings = json.loads(CONFIG.state_path("service_settings.json").read_text())
        if settings.get("fallback_model"):
            used.add(settings["fallback_model"])
    except (OSError, ValueError):
        pass
    return used


def picker_models() -> list[ModelOption]:
    """What the model pickers show: hidden models only while in use."""
    used = in_use()
    return [m for m in CATALOG if m.id not in HIDDEN or m.id in used]


# API list prices (USD per 1M tokens, input/output) by the provider's model
# id, for entries that don't carry their own (CLI models, auto-added ones).
# Checked 2026-10-12: xAI from its /v1/language-models API, Anthropic from
# platform.claude.com/docs/en/about-claude/pricing, OpenAI from
# developers.openai.com/api/docs/pricing. A model on a subscription (CLI)
# route is priced at the same model's API list price.
LIST_PRICES: dict[str, tuple[float, float]] = {
    "grok-4.3": (1.25, 2.5), "grok-4.5": (2, 6), "grok-4.6": (2, 6), "grok-4.7": (2, 6),
    "claude-haiku-5-5": (0.1, 0.5), "claude-sonnet-4-6": (3, 15), "claude-sonnet-5": (2, 10),
    "claude-sonnet-5-5": (2, 10), "claude-opus-4-6": (5, 25), "claude-opus-5": (5, 25),
    "claude-opus-5-5": (4, 20), "claude-fable-5": (10, 50), "claude-fable-5-1": (10, 50),
    "gpt-5.6-luna": (0.2, 1.2), "gpt-5.6-terra": (2, 12), "gpt-5.6-sol": (4, 20),
    "gpt-6-luna": (0.1, 0.5), "gpt-6-sol": (2, 10), "gpt-6.1-sol": (2, 10), "gpt-6-astra": (10, 50),
}


def api_prices(model: ModelOption) -> tuple[float | None, float | None]:
    """(input, output) USD per 1M tokens: the entry's own, else the list price."""
    if model.api_input_usd is not None and model.api_output_usd is not None:
        return model.api_input_usd, model.api_output_usd
    return LIST_PRICES.get(model.api_model_id or model.id, (None, None))


# Cost tiers for the model pickers (2026-10-12), by API output price
# per 1M tokens: low <= $4, mid > $4 and <= $20, high > $20. Colours are
# green/amber/violet, never red (red reads as danger); the $ label carries
# the tier for colourblind viewers.
COST_TIERS = {"low": ("$", "#22C55E"), "mid": ("$$", "#F59E0B"), "high": ("$$$", "#A855F7")}


def cost_tier(model: ModelOption) -> str | None:
    """"low" / "mid" / "high", or None when the price is unknown."""
    out = api_prices(model)[1]
    if out is None:
        return None
    return "low" if out <= 4 else "mid" if out <= 20 else "high"


# Picker/overview order: API fast, API slow, CLI fast, CLI slow.
GROUPS = ("API · fast", "API · slow", "CLI · fast", "CLI · slow")


def group(model: ModelOption) -> str:
    return f"{model.route} · {model.speed}"


# ── the model picker's columns (2026-11-04) ─────────────────────────────
VENDORS = {"xai": "xAI", "anthropic": "Anthropic", "claude-cli": "Anthropic", "codex-cli": "OpenAI"}
NAME_MAX = 16
OLD_AFTER_MONTHS = 6


def vendor(model: ModelOption) -> str:
    return VENDORS.get(model.provider, model.provider)


def short_name(model: ModelOption) -> str:
    """The name without the vendor word or "(subscription)" (the Vendor and
    Route columns say those), at most NAME_MAX characters."""
    name = re.sub(r"\s*\(subscription\)\s*$", "", model.display_name)
    name = re.sub(r"^Claude\s+", "", name)
    return name if len(name) <= NAME_MAX else name[:NAME_MAX - 1].rstrip() + "…"


def released_is_added(model: ModelOption) -> bool:
    return model.provider == "codex-cli"


def _family(model: ModelOption) -> tuple[str, str]:
    """(route, name without version numbers): "Claude Opus 5.5" -> Opus."""
    bare = re.sub(r"\s*\(subscription\)\s*$", "", model.display_name)
    return model.route, " ".join(re.sub(r"\d+(?:\.\d+)*", " ", bare).replace("-", " ").split()).lower()


def _version(model: ModelOption) -> tuple:
    m = re.search(r"\d+(?:\.\d+)*", model.display_name)
    return tuple(int(x) for x in m.group(0).split(".")) if m else ()


def _months_ago(released: str | None, today: date | None = None) -> int | None:
    if not released or not re.fullmatch(r"\d{4}-\d{2}", released):
        return None
    today = today or date.today()
    y, m = map(int, released.split("-"))
    return (today.year - y) * 12 + today.month - m


def old_reason(model: ModelOption, models: list[ModelOption] | None = None, today: date | None = None) -> str | None:
    """Why a model counts as old, or None: a newer version of the same model
    on the same route is in the list, the provider calls it older, or it was
    released more than OLD_AFTER_MONTHS ago."""
    models = CATALOG if models is None else models
    fam, ver = _family(model), _version(model)
    newer = [m for m in models if m.id != model.id and _family(m) == fam and _version(m) > ver]
    if newer:
        return f"{max(newer, key=_version).display_name} is newer"
    if model.id in PROVIDER_OLDER:
        return "the provider lists it as an older model"
    age = _months_ago(model.released, today)
    if age is not None and age > OLD_AFTER_MONTHS and not released_is_added(model):
        return f"released {age} months ago"
    return None


DEFAULT_MODEL_ID = "claude-sonnet-4-6"

_BY_ID = {m.id: m for m in CATALOG}


def display_name(model_id: str | None) -> str | None:
    """None for unknown ids (e.g. a model removed from the catalog)."""
    m = _BY_ID.get(model_id) if model_id else None
    return m.display_name if m else None


def get(model_id: str) -> ModelOption:
    return _BY_ID.get(model_id, _BY_ID[DEFAULT_MODEL_ID])


def _load_selections() -> dict[str, str]:
    data = load_json(SELECTION_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_selections(data: dict[str, str]) -> None:
    save_json(SELECTION_FILE, data)


def selected_model_id(agent_id: str) -> str:
    return _load_selections().get(agent_id, DEFAULT_MODEL_ID)


def set_selected_model(agent_id: str, model_id: str) -> None:
    if model_id not in _BY_ID:
        raise ValueError(f"unknown model id: {model_id}")
    data = _load_selections()
    data[agent_id] = model_id
    _save_selections(data)


apply_auto()
