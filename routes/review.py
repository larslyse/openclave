"""Upkeep: the maintainer inbox, attention digest, weekly cleanup, the manager's proposals, weekly review and audit."""
from fastapi import APIRouter, HTTPException
import service_settings
import attention
import cleanup
import vault
import chat_jobs
import proposals
import audit
import agents as agents_module
from agents import AGENTS
from schemas import (
    CleanupAction, UnarchiveRequest, InboxItem, InboxResponse, UpdateInboxItemRequest,
)

router = APIRouter()


@router.get("/api/maintainer/inbox", response_model=InboxResponse)
def maintainer_inbox(status: str | None = None):
    return InboxResponse(items=[InboxItem(**i) for i in vault.read_maintainer_inbox(status)])


@router.get("/api/maintainer/inbox/{item_id}", response_model=InboxItem)
def maintainer_inbox_item(item_id: str):
    item = vault.get_maintainer_inbox_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="inbox item not found")
    return InboxItem(**item)


@router.post("/api/maintainer/inbox/{item_id}", response_model=InboxItem)
def update_maintainer_inbox_item(item_id: str, req: UpdateInboxItemRequest):
    # Deterministic, no LLM call — same reasoning as POST /<agent>/tracking:
    # a dashboard acknowledge/resolve button shouldn't need a chat round trip.
    try:
        updated = vault.update_maintainer_inbox_item(item_id, req.status, req.result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=404, detail="inbox item not found")
    return InboxItem(**vault.get_maintainer_inbox_item(item_id))


@router.get("/api/attention")
def api_attention():
    """Per-agent findings (attention.py) plus the digest's last run."""
    results = attention.check_all()
    return {"agents": results, "needs_you": attention.needs_you(results), "digest": attention.state()}


@router.post("/api/attention/digest-test")
def api_attention_digest_test():
    """Send the digest now, even if empty, to check it arrives."""
    return attention.send_digest(force=True)


# ── Weekly cleanup (cleanup.py): Rick suggests, the owner applies ──
@router.get("/api/cleanup")
def api_cleanup():
    return cleanup.report()


@router.post("/api/cleanup/check")
def api_cleanup_check():
    """Re-check now (no message from Rick, no push)."""
    cleanup.generate()
    return cleanup.report()


@router.post("/api/cleanup/apply")
def api_cleanup_apply(req: CleanupAction):
    try:
        return cleanup.apply(req.id, req.title)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except chat_jobs.AgentBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/cleanup/dismiss")
def api_cleanup_dismiss(req: CleanupAction):
    if not cleanup.dismiss(req.id):
        raise HTTPException(status_code=404, detail="that suggestion is no longer there")
    return {"ok": True}


# ── The manager's proposals (proposals.py) and weekly audit (audit.py) ──
@router.get("/api/proposals")
def api_proposals():
    return {"proposals": proposals.list_open(), "manager": agents_module.manager_id()}


@router.post("/api/proposals/{item_id}/approve")
def api_proposal_approve(item_id: str, req: dict | None = None):
    content = (req or {}).get("content")
    try:
        return proposals.approve(item_id, content if isinstance(content, str) else None)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except chat_jobs.AgentBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/proposals/{item_id}/decline")
def api_proposal_decline(item_id: str, req: dict | None = None):
    note = (req or {}).get("note")
    try:
        proposals.decline(item_id, note if isinstance(note, str) else None)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True}


@router.post("/api/proposals/{item_id}/discuss")
def api_proposal_discuss(item_id: str):
    try:
        where = proposals.discuss(item_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not where["agent_id"]:
        raise HTTPException(status_code=409, detail="there's no manager to discuss it with")
    vault.ensure_conversation(where["agent_id"], where["conversation_id"], "Audit")
    return where


# Weekly review (Control Panel): what the Monday cleanup and the manager's
# audit do. Ticks save through /api/settings {"weekly_review": {key: bool}}.
@router.get("/api/weekly-review")
def api_weekly_review():
    on = service_settings.load()["weekly_review"]
    manager = agents_module.manager_id()
    return {"items": [{"key": k, "label": label, "part": part, "on": on[k]}
                      for k, label, part in service_settings.WEEKLY_REVIEW],
            "manager": AGENTS[manager].name if manager in AGENTS else None, "manager_id": manager,
            "last_week": cleanup._load().get("last_week")}


@router.post("/api/weekly-review/run")
def api_weekly_review_run():
    """Run it now (cleanup, then the audit) instead of waiting for Monday.
    Waits for the cleanup, so the page can show its result straight away; the
    audit is the manager's own chat run and only gets queued here."""
    return {"started": True, **cleanup.run_weekly()}


@router.post("/api/audit/run")
def api_audit_run():
    result = audit.run()
    if not result.get("queued"):
        raise HTTPException(status_code=409, detail=result.get("reason") or "could not start the audit")
    return result


@router.post("/api/cleanup/unarchive")
def api_cleanup_unarchive(req: UnarchiveRequest):
    if req.agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="unknown agent")
    try:
        if not vault.unarchive_conversation(req.agent_id, req.conversation_id):
            raise HTTPException(status_code=404, detail="no such archived conversation")
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}
