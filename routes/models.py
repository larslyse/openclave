"""Models: the picker, choosing an agent's model, model checks and updates, credits, quota and usage."""
import json
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException
import model_discovery
import model_updater
import provider_credits
import llm_client
import llm_models
import connections
import vault
import setup_check
from agents import AGENTS
from schemas import AgentUsage, AllUsageResponse, ModelOption, SetModelRequest, UsageWindow

router = APIRouter()


def _model_option(m, agent_id: str | None = None, times: dict | None = None,
                  shown: list | None = None) -> ModelOption:
    price_in, price_out = llm_models.api_prices(m)
    t = (times or {}).get(m.id) or {}
    cost = llm_models.cost_tier(m)
    label, color = llm_models.COST_TIERS.get(cost, ("", ""))
    return ModelOption(
        id=m.id, provider=m.provider, display_name=m.display_name, tier=m.tier,
        supports_vision=m.supports_vision, api_input_usd=price_in, api_output_usd=price_out,
        route=m.route, speed=m.speed, group=llm_models.group(m),
        supports=connections.supported(agent_id, m.id) if agent_id else [],
        cost_tier=cost, cost_label=label, cost_color=color,
        vendor=llm_models.vendor(m), short_name=llm_models.short_name(m), released=m.released,
        released_is_added=llm_models.released_is_added(m), old=llm_models.old_reason(m, shown),
        latency_ms=t.get("average_ms"), latency_samples=t.get("samples", 0),
    )


def _picker_options(agent_id: str | None = None) -> list[ModelOption]:
    shown = llm_models.picker_models()
    times = api_model_response_times()
    ready = setup_check.ready_providers()
    return [_model_option(m, agent_id, times, shown).model_copy(update={"ready": m.provider in ready}) for m in shown]


def _set_model(agent_id: str, req: SetModelRequest) -> dict:
    """The one way a model gets chosen (every picker ends here). No blocking
    warning: pickers tag models that support the agent's connections, and the
    agent itself says so if it's on one that doesn't (connections.py)."""
    if req.model_id not in {m.id for m in llm_models.CATALOG}:
        raise HTTPException(status_code=400, detail="unknown model id")
    llm_models.set_selected_model(agent_id, req.model_id)
    return {"agent_id": agent_id, "model_id": req.model_id, "loses": connections.lost(agent_id, req.model_id)}


@router.get("/api/usage", response_model=AllUsageResponse)
def api_usage():
    total = llm_client.usage_summary(None)
    by_agent = [
        AgentUsage(agent_id=agent_id, windows={k: UsageWindow(**v) for k, v in llm_client.usage_summary(agent_id).items()})
        for agent_id in AGENTS
    ]
    return AllUsageResponse(total={k: UsageWindow(**v) for k, v in total.items()}, by_agent=by_agent)


@router.get("/api/model-check")
def api_model_check():
    """Cached new-model report; kicks off a background refresh when over a day old."""
    result = model_discovery.cached()
    refreshing = model_discovery.is_stale(result) and model_discovery.refresh_in_background()
    return {"result": result, "refreshing": refreshing}


@router.post("/api/model-check")
def api_model_check_now():
    return {"result": model_discovery.check(), "refreshing": False}


# Model updates (model_updater.py): automatic adds/hides, reviewed here.
@router.get("/api/model-updates")
def api_model_updates():
    return model_updater.summary()


@router.post("/api/model-updates/run")
def api_model_updates_run():
    return {"started": model_updater.run_in_background()}


@router.post("/api/model-updates/enabled")
def api_model_updates_enabled(req: dict):
    model_updater.set_enabled(bool(req.get("on")))
    return model_updater.summary()


@router.post("/api/model-updates/{action}")
def api_model_updates_review(action: str, req: dict):
    if action not in ("remove", "restore"):
        raise HTTPException(status_code=404, detail="unknown action")
    try:
        getattr(model_updater, action)(str(req.get("id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return model_updater.summary()


@router.get("/api/credits")
def api_credits():
    return provider_credits.get()


@router.get("/api/quota")
def api_quota():
    """Subscription quota for the Control Panel. Codex: last reading captured
    from an OpenClave call (llm_client._record_codex_rate_limits), else the
    newest interactive Codex session log (same subscription). The Claude CLI
    reports no quota at all."""
    codex = None
    try:
        codex = json.loads(llm_client.CLI_QUOTA_FILE.read_text(encoding="utf-8")).get("codex")
    except (OSError, ValueError):
        pass
    if codex is None:
        sessions = sorted(Path.home().glob(".codex/sessions/*/*/*/*.jsonl"), key=lambda p: p.stat().st_mtime)
        if sessions:
            try:
                found = llm_client.codex_rate_limits_from_log(sessions[-1].read_text(encoding="utf-8"))
            except (OSError, ValueError):
                found = None
            if found:  # observed when that session last wrote, not now
                codex = {**found, "observed_at": datetime.fromtimestamp(sessions[-1].stat().st_mtime, timezone.utc).isoformat()}
    return {"codex": codex, "claude_cli": None}


@router.get("/api/model-response-times")
def api_model_response_times():
    """Average recent reply time per model across all agents, weighted by samples."""
    totals: dict[str, list[int]] = {}
    for agent_id in AGENTS:
        for model_id, t in vault.read_response_time_averages(agent_id).items():
            acc = totals.setdefault(model_id, [0, 0])
            acc[0] += t["average_ms"] * t["samples"]
            acc[1] += t["samples"]
    return {m: {"average_ms": round(total / n), "samples": n} for m, (total, n) in totals.items() if n}


@router.get("/api/models", response_model=list[ModelOption])
def api_models(agent: str | None = None):
    """?agent=<id> tags the models that support that agent's connections."""
    agent_id = agent if agent in AGENTS else None
    return _picker_options(agent_id)


@router.post("/api/agents/{agent_id}/model")
def api_set_agent_model(agent_id: str, req: SetModelRequest):
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="agent not found")
    return _set_model(agent_id, req)
