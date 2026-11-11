"""Web Push: the app's public key, subscriptions and a test notification."""
from fastapi import APIRouter, HTTPException
import web_push

router = APIRouter()


@router.get("/api/push/key")
def api_push_key():
    return {"key": web_push.public_key()}


@router.post("/api/push/subscribe")
def api_push_subscribe(req: dict):
    try:
        web_push.add_subscription(req.get("subscription") or {}, str(req.get("label") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"devices": len(web_push.subscriptions())}


@router.post("/api/push/unsubscribe")
def api_push_unsubscribe(req: dict):
    web_push.remove_subscription(str(req.get("endpoint") or ""))
    return {"devices": len(web_push.subscriptions())}


@router.get("/api/push/status")
def api_push_status():
    return {"devices": [{"label": s.get("label", ""), "added": s.get("added", "")} for s in web_push.subscriptions()]}


@router.post("/api/push/test")
def api_push_test():
    delivered = web_push.send_all("OpenClave", "Test notification: reminders will arrive like this.",
                                  url="/mobile/", tag="openclave-test")
    return {"delivered": delivered, "devices": len(web_push.subscriptions())}
