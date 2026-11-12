"""Token usage log, usage summaries and API-price estimates, and the Codex
subscription's usage and rate-limit readings (split out of llm_client.py)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import config
import llm_models
from state_io import load_json, save_json


# Token/cost tracking (Build Docs/Archive/Token Efficiency.md) — one JSON-lines log,
# not one file per agent, since "total spend across everything" is the
# top-line question and per-agent is just a filter over the same data.
# Ops state, not agent knowledge, so this lives under OPENCLAVE_HOME/state,
# not in the vault.
USAGE_LOG = config.CONFIG.state_path("usage_log.jsonl")


def log_usage(agent_id: str, provider: str, model_id: str, usage: dict, cost_usd: float | None) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": agent_id,
        "provider": provider,
        "model_id": model_id,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "cost_usd": cost_usd,
    }
    try:
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # usage logging must never break a chat reply


USAGE_WINDOWS = {"7d": 7, "30d": 30, "all": None}


def _read_usage_entries(agent_id: str | None) -> list[dict]:
    if not USAGE_LOG.exists():
        return []
    out = []
    with open(USAGE_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if agent_id is None or entry.get("agent_id") == agent_id:
                out.append(entry)
    return out


def api_equivalent_usd(entry: dict) -> float | None:
    """What a subscription (CLI) call would have cost at the same model's API
    list price. An estimate: cached input is counted at 10% of the input
    price, which is typical but varies by provider."""
    model = llm_models._BY_ID.get(entry.get("model_id"))
    price_in, price_out = llm_models.api_prices(model) if model else (None, None)
    if price_in is None or price_out is None:
        return None
    fresh_in = entry.get("input_tokens", 0) + entry.get("cache_creation_input_tokens", 0)
    cached = entry.get("cache_read_input_tokens", 0)
    return (fresh_in * price_in + cached * price_in * 0.1
            + entry.get("output_tokens", 0) * price_out) / 1_000_000


def usage_summary(agent_id: str | None = None) -> dict:
    """tokens in/out + cost, summed over 7d/30d/all-time windows. agent_id=None
    aggregates across every agent (for the root dashboard's total)."""
    entries = _read_usage_entries(agent_id)
    now = datetime.now(timezone.utc)
    windows = {}
    for name, days in USAGE_WINDOWS.items():
        tokens_in = tokens_out = 0
        cost = 0.0
        has_cost = False
        equiv = 0.0
        has_equiv = False
        for e in entries:
            if days is not None:
                try:
                    ts = datetime.fromisoformat(e["ts"])
                except (KeyError, ValueError):
                    continue
                if (now - ts).days >= days:
                    continue
            tokens_in += e.get("input_tokens", 0) + e.get("cache_creation_input_tokens", 0) + e.get("cache_read_input_tokens", 0)
            tokens_out += e.get("output_tokens", 0)
            if e.get("cost_usd") is not None:
                cost += e["cost_usd"]
                has_cost = True
            else:
                price = api_equivalent_usd(e)
                if price is not None:
                    equiv += price
                    has_equiv = True
        windows[name] = {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost if has_cost else None,
                         "api_equiv_usd": equiv if has_equiv else None}
    return windows


CLI_QUOTA_FILE = config.CONFIG.state_path("cli_quota.json")


def codex_rate_limits_from_log(text: str) -> dict | None:
    """The last rate_limits object in a Codex session log (JSONL)."""
    latest = None
    for line in text.splitlines():
        if '"rate_limits"' in line:
            latest = line
    if latest is None:
        return None
    stack = [json.loads(latest)]
    while stack:  # rate_limits sits a few levels deep; don't hardcode the path
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("rate_limits"), dict):
                return node["rate_limits"]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def codex_usage_from_log(text: str) -> dict | None:
    """Token totals for the whole Codex run: the last token_count event's
    cumulative total_token_usage, in log_usage's field names."""
    latest = None
    for line in text.splitlines():
        if '"token_count"' in line and '"total_token_usage"' in line:
            latest = line
    if latest is None:
        return None
    try:
        total = json.loads(latest)["payload"]["info"]["total_token_usage"]
    except (ValueError, KeyError, TypeError):
        return None
    cached = int(total.get("cached_input_tokens") or 0)
    return {
        "input_tokens": max(0, int(total.get("input_tokens") or 0) - cached),
        "cache_read_input_tokens": cached,
        "output_tokens": int(total.get("output_tokens") or 0),
    }


def _log_codex_usage(agent_id: str, model_id: str, codex_home: Path) -> None:
    """Usage for the Codex route, read from its session logs: the CLI reports
    none otherwise, and Token usage would look empty for every agent on
    Codex. Never raises."""
    try:
        for session in codex_home.glob("sessions/**/*.jsonl"):
            usage = codex_usage_from_log(session.read_text(encoding="utf-8"))
            if usage:
                log_usage(agent_id, "codex-cli", model_id, usage, None)  # subscription: no $ figure
                return
    except Exception:
        pass


def _record_codex_rate_limits(codex_home: Path) -> None:
    """Keep the latest Codex subscription rate-limit reading for the Control
    Panel. Codex logs it in the session file under this call's temporary
    CODEX_HOME, which is deleted right after, so it's read here. Never raises."""
    try:
        found = None
        for session in codex_home.glob("sessions/**/*.jsonl"):
            found = codex_rate_limits_from_log(session.read_text(encoding="utf-8")) or found
        if not found:
            return
        data = load_json(CLI_QUOTA_FILE, {})
        data["codex"] = {**found, "observed_at": datetime.now(timezone.utc).isoformat()}
        save_json(CLI_QUOTA_FILE, data)
    except Exception:
        pass  # quota display is informational; never break a chat reply
