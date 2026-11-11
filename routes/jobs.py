"""Background jobs (scheduler.py): the job table and a per-job "Run now"."""
from fastapi import APIRouter, HTTPException
import scheduler

router = APIRouter()


@router.get("/api/jobs")
def api_jobs():
    return {"jobs": scheduler.jobs()}


@router.post("/api/jobs/{job_id}/run")
def api_job_run(job_id: str):
    started, reason = scheduler.run_now(job_id)
    if not started:
        raise HTTPException(status_code=404 if reason == "no such job" else 409, detail=reason)
    return {"started": True}
