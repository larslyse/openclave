"""Stdio MCP server giving R2 direct access to Home Assistant's full REST/config
API — states, services, templates, and config-entry options flows (helper/group
membership editing) — not just the narrow voice-assistant-scoped "assist" tool
HA's own native MCP Server integration can expose the assist endpoint
alongside this server through the agent's ``agent.json`` MCP configuration.

R2-only, spawned fresh per chat call like mcp_vault_server.py. The base URL and
API token are injected via env vars at spawn time (see llm_client.py) and never
appear in the tool schema the model sees — the model picks method/path/body,
this process attaches auth.
"""
import asyncio
import itertools
import json
import os
import urllib.error
import urllib.request

import websockets

import ha_guard
from mcp.server.mcpserver import MCPServer

HA_BASE_URL = os.environ.get("HA_BASE_URL", "http://homeassistant.local:8123")
HA_WS_URL = HA_BASE_URL.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
HA_API_TOKEN = os.environ["HA_API_TOKEN"]
_ws_id_counter = itertools.count(1)

mcp = MCPServer("openclave-ha")


@mcp.tool()
def ha_api(method: str, path: str, body: dict | None = None) -> str:
    """Call the Home Assistant REST/config API directly. Full admin access —
    not just read/control, this can also edit config (groups, helpers).
    Reads and everyday control run at once; config changes, deletions,
    restarts, locks/alarms and scripts/automations need the user's yes first
    (the call is refused with instructions until they give it).

    method: GET, POST, PUT, or DELETE.
    path: must start with /api/. Common ones:
      - GET /api/states or /api/states/<entity_id> — read entity state/attributes
      - POST /api/services/<domain>/<service> with body {"entity_id": ...} — call
        a service (e.g. domain=light, service=turn_on)
      - POST /api/template with body {"template": "<jinja2>"} — read-only Jinja2
        queries, e.g. "{{ area_entities('Kontor') }}", "{{ config_entry_id('light.x') }}",
        "{{ states('light.x') }}" — use this to look up entity IDs / areas /
        config-entry relationships before making a change, rather than guessing.
      - GET /api/config/config_entries/entry — list all config entries (filter
        client-side by 'domain', e.g. 'group' for helper groups)
      - POST /api/config/config_entries/options/flow with body {"handler": "<entry_id>"}
        — start an options-flow edit (e.g. for a light/helper group). Returns a
        flow_id and the current field values under each field's
        description.suggested_value — read those first, don't guess the schema.
      - POST /api/config/config_entries/options/flow/<flow_id> with the FULL
        updated field set (it's a full replace of that step's fields, not a
        merge — include every field from the schema, not just the one changing)
        — completes the edit. A response of type "create_entry" means it saved.

    body: JSON-serializable dict for POST/PUT, or None for GET/DELETE.
    """
    if not path.startswith("/api/"):
        return "Error: path must start with /api/"
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "DELETE"):
        return f"Error: unsupported method {method}"

    if os.environ.get(ha_guard.READ_ONLY_ENV) == "1" and not ha_guard.is_read_rest(method, path):
        return ha_guard.READ_ONLY_REFUSAL
    rest_call = {"api": "rest", "method": method, "path": path, "body": body}
    if ha_guard.needs_yes_rest(method, path) and not ha_guard.preapproved(rest_call, os.environ.get(ha_guard.PREAPPROVED_ENV)):
        refusal = ha_guard.check(rest_call, f"{method} {path}")
        if refusal:
            return refusal

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        HA_BASE_URL + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {HA_API_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode() or "(empty response — likely succeeded)"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()[:2000]}"
    except Exception as e:
        return f"Error: {e}"


async def _ws_call(message: dict) -> dict:
    async with websockets.connect(HA_WS_URL, open_timeout=10) as ws:
        auth_req = json.loads(await ws.recv())
        if auth_req.get("type") != "auth_required":
            return {"error": f"unexpected handshake: {auth_req}"}
        await ws.send(json.dumps({"type": "auth", "access_token": HA_API_TOKEN}))
        auth_result = json.loads(await ws.recv())
        if auth_result.get("type") != "auth_ok":
            return {"error": f"HA WS auth failed: {auth_result}"}
        msg = dict(message)
        msg["id"] = next(_ws_id_counter)
        await ws.send(json.dumps(msg))
        return json.loads(await ws.recv())


@mcp.tool()
def ha_ws(message: dict) -> str:
    """Call Home Assistant's WebSocket admin API directly — the same API HA's
    own frontend uses, so it covers everything ha_api (REST) can't reach:
    entity registry removal/edits, device registry removal, and
    integration-specific commands like ZHA's device management. REST's
    /api/config/entity_registry/... doesn't exist (confirmed 404) — this is
    the only real path for those.

    message: the WS command dict. Omit "id" (added automatically) and don't
    include "type": "auth" (handled automatically) — just the command itself.
    Common ones:
      {"type": "config/entity_registry/remove", "entity_id": "light.x"}
      {"type": "config/entity_registry/get", "entity_id": "light.x"}
      {"type": "config/device_registry/remove_config_entry",
       "device_id": "...", "config_entry_id": "..."}
      {"type": "zha/devices"} — list ZHA devices (to find ieee for removal)
      {"type": "zha/device/remove", "ieee": "..."}

    Use ha_api's POST /api/template first to resolve IDs you don't already
    have (e.g. "{{ device_id('light.x') }}").

    Deletions here are real and immediate — confirm with the user before calling
    a remove/delete command, same as you would before any other destructive
    change.
    """
    if os.environ.get(ha_guard.READ_ONLY_ENV) == "1" and not ha_guard.is_read_ws(message):
        return ha_guard.READ_ONLY_REFUSAL
    if ha_guard.needs_yes_ws(message) and not ha_guard.preapproved({"api": "ws", "message": message},
                                                                   os.environ.get(ha_guard.PREAPPROVED_ENV)):
        refusal = ha_guard.check({"api": "ws", "message": message}, f"WS {message.get('type')}")
        if refusal:
            return refusal
    try:
        result = asyncio.run(_ws_call(message))
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(result)


if __name__ == "__main__":
    mcp.run(transport="stdio")
