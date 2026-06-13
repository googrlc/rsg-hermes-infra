"""Standalone, model-agnostic MCP bridge, deployable to Railway.

This service is the read + propose layer of the staging architecture. It exposes
read tools (EspoCRM / Supabase) and a propose tool that writes to a Supabase
staging table — it intentionally has NO live-write tool. Any agent (Claude,
Hermes, or other) may call it; the bridge bakes in no specific LLM.

It keeps MCP server entries in a YAML config file and exposes:
- /mcp (alias for list/probe)
- /api/mcp (dashboard compatibility)
- /healthz (liveness)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("mcp-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# Config path: prefer the generic var; fall back to the legacy HERMES_CONFIG_PATH
# so existing deployments keep working without an env change.
CONFIG_PATH = Path(
    os.environ.get("MCP_BRIDGE_CONFIG_PATH")
    or os.environ.get("HERMES_CONFIG_PATH")
    or "/data/config.yaml"
)
AUTH_TOKEN = os.environ.get("API_SERVER_KEY", "")
KNOWN_CATEGORIES = ["All", "Connected", "Failed", "Disabled"]
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "rsg-espocrm-mcp-bridge"
SERVER_VERSION = "1.1.0"

app = FastAPI(title="MCP Bridge", docs_url=None, redoc_url=None)


def _check_auth(request: Request) -> bool:
    if not AUTH_TOKEN:
        return True
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() == AUTH_TOKEN
    return False


def _read_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return yaml.safe_load(CONFIG_PATH.read_text()) or {}


def _write_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.safe_dump(config, default_flow_style=False, sort_keys=False))


def _get_servers(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("mcp_servers", {})
    return dict(raw) if isinstance(raw, dict) else {}


def _normalize(name: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        entry = {"command": str(entry)} if entry else {}
    enabled = entry.get("enabled", True) if "enabled" in entry else not entry.get("disabled", False)
    transport = entry.get("transport", "stdio" if entry.get("command") else "sse")
    return {
        "id": name,
        "name": name,
        "enabled": enabled,
        "command": entry.get("command"),
        "args": entry.get("args", []),
        "env": entry.get("env", {}),
        "url": entry.get("url"),
        "headers": entry.get("headers", {}),
        "transportType": transport,
        "status": "unknown",
    }


def _to_entry(body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if body.get("transportType"):
        out["transport"] = body["transportType"]
    for key in ("command", "args", "env", "url", "headers"):
        if body.get(key):
            out[key] = body[key]
    if "enabled" in body:
        out["enabled"] = body["enabled"]
    if body.get("authType") and body["authType"] != "none":
        auth: dict[str, Any] = {"type": body["authType"]}
        if body.get("bearerToken"):
            auth["token"] = body["bearerToken"]
        if body.get("oauth"):
            auth["oauth"] = body["oauth"]
        out["auth"] = auth
    if body.get("toolMode") and body["toolMode"] != "all":
        out["tool_mode"] = body["toolMode"]
    if body.get("includeTools"):
        out["include_tools"] = body["includeTools"]
    if body.get("excludeTools"):
        out["exclude_tools"] = body["excludeTools"]
    return out


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _jsonrpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "ping",
            "description": "Health check tool that confirms bridge availability.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Optional ping message."}
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "espocrm_get_current_user",
            "description": "Fetch current EspoCRM API user via /api/v1/App/user.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "search_accounts",
            "description": "Search EspoCRM Accounts by name (read-only). Returns id/name/website/phone.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Name substring to search for."},
                    "limit": {"type": "integer", "description": "Max rows (1-50, default 10)."},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_account",
            "description": "Fetch a single EspoCRM Account by id (read-only).",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "EspoCRM Account id."}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "propose_crm_change",
            "description": (
                "Propose a CRM write (upsert/enrich/update) to the staging table — does NOT "
                "touch live records. A human approves and a deterministic committer applies it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "enum": ["Account", "Contact", "Policy", "Opportunity"]},
                    "op": {"type": "string", "enum": ["upsert", "enrich", "update"]},
                    "matchKey": {"type": "string", "description": "Dedup key, e.g. momentum_client_id."},
                    "espocrmId": {"type": "string", "description": "Target record id if already known."},
                    "before": {"type": "object", "description": "Snapshot of current values."},
                    "after": {"type": "object", "description": "Proposed field values."},
                    "rationale": {"type": "string"},
                    "confidence": {"type": "number", "description": "0..1."},
                    "source": {"type": "string", "description": "Enrichment source / origin."},
                },
                "required": ["entity", "after"],
                "additionalProperties": False,
            },
        },
        {
            "name": "query_commission_ledger",
            "description": "Read rows from the canonical commission_ledger (Supabase, read-only).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max rows (1-200, default 20)."},
                    "reconciliationStatus": {"type": "string"},
                    "espocrmPolicyId": {"type": "string"},
                    "auditStatus": {"type": "string", "description": "e.g. carrier_unmatched, tier_ambiguous."},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "record_commission_parity",
            "description": (
                "Write parity-reconciliation rows to commission_parity_report (Supabase). Used by the "
                "cutover gate that compares CRM Commission records vs platform commission_ledger."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "runId": {"type": "string", "description": "UUID grouping one reconciliation run."},
                    "rows": {
                        "type": "array",
                        "description": "Parity rows (bucket, cause, match_key, *_commission, delta, details...).",
                        "items": {"type": "object"},
                    },
                },
                "required": ["runId", "rows"],
                "additionalProperties": False,
            },
        },
    ]


def _run_ping(arguments: dict[str, Any]) -> str:
    message = arguments.get("message", "pong")
    return f"Bridge reachable: {message}"


def _run_espocrm_get_current_user() -> str:
    base_url = os.environ.get("ESPOCRM_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("ESPOCRM_API_KEY", "").strip()
    if not base_url:
        return "Missing ESPOCRM_BASE_URL."
    if not api_key:
        return "Missing ESPOCRM_API_KEY."

    req = urllib.request.Request(
        url=f"{base_url}/api/v1/App/user",
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return f"EspoCRM request failed with HTTP {exc.code}."
    except urllib.error.URLError as exc:
        return f"EspoCRM request failed: {exc.reason}"


# ---------------------------------------------------------------------------
# HTTP helpers — EspoCRM REST + Supabase PostgREST (urllib, no extra deps)
# ---------------------------------------------------------------------------


def _espocrm_request(
    method: str, path: str, params: Any = None, body: Any = None
) -> tuple[Any, str | None]:
    base = os.environ.get("ESPOCRM_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("ESPOCRM_API_KEY", "").strip()
    if not base:
        return None, "Missing ESPOCRM_BASE_URL."
    if not api_key:
        return None, "Missing ESPOCRM_API_KEY."
    url = f"{base}/api/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url=url,
        data=data,
        method=method,
        headers={"X-Api-Key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return (json.loads(raw) if raw else {}), None
    except urllib.error.HTTPError as exc:
        return None, f"EspoCRM HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"EspoCRM request failed: {exc.reason}"


def _supabase_request(
    method: str, path: str, params: Any = None, body: Any = None
) -> tuple[Any, str | None]:
    base = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not base:
        return None, "Missing SUPABASE_URL."
    if not key:
        return None, "Missing SUPABASE_SERVICE_KEY."
    url = f"{base}/rest/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url=url,
        data=data,
        method=method,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=representation",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return (json.loads(raw) if raw else []), None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8")[:200] if hasattr(exc, "read") else ""
        return None, f"Supabase HTTP {exc.code}: {detail}"
    except urllib.error.URLError as exc:
        return None, f"Supabase request failed: {exc.reason}"


# ---------------------------------------------------------------------------
# Tool implementations — READ (EspoCRM/Supabase) + PROPOSE (write staging).
# The bridge intentionally exposes NO live-write tool. Agents propose to staging;
# a separate privileged committer applies approved rows. See COMMISSION-WORKSPACE-DESIGN.md.
# ---------------------------------------------------------------------------


def _run_search_accounts(arguments: dict[str, Any]) -> str:
    query = str(arguments.get("query", "")).strip()
    limit = max(1, min(int(arguments.get("limit", 10) or 10), 50))
    # Espo v8 needs bracket-encoded where (JSON where is ignored).
    params: list[tuple[str, Any]] = [("maxSize", limit), ("select", "id,name,website,phoneNumber")]
    if query:
        params += [
            ("where[0][type]", "contains"),
            ("where[0][attribute]", "name"),
            ("where[0][value]", query),
        ]
    data, err = _espocrm_request("GET", "Account", params=params)
    if err:
        return f"search_accounts failed: {err}"
    rows = data.get("list", []) if isinstance(data, dict) else []
    return json.dumps({"total": data.get("total", len(rows)), "list": rows})


def _run_get_account(arguments: dict[str, Any]) -> str:
    account_id = str(arguments.get("id", "")).strip()
    if not account_id:
        return "get_account: missing id."
    data, err = _espocrm_request("GET", f"Account/{account_id}")
    if err:
        return f"get_account failed: {err}"
    return json.dumps(data)


def _run_propose_crm_change(arguments: dict[str, Any]) -> str:
    entity = arguments.get("entity")
    if entity not in ("Account", "Contact", "Policy", "Opportunity"):
        return "propose_crm_change: entity must be Account|Contact|Policy|Opportunity."
    row = {
        "entity": entity,
        "op": arguments.get("op", "upsert"),
        "match_key": arguments.get("matchKey"),
        "espocrm_id": arguments.get("espocrmId"),
        "before": arguments.get("before") or {},
        "after": arguments.get("after") or {},
        "rationale": arguments.get("rationale"),
        "confidence": arguments.get("confidence"),
        "source": arguments.get("source"),
        # LLM-agnostic: any agent (claude / hermes / other) labels itself here.
        "proposed_by": arguments.get("proposedBy", "agent"),
    }
    data, err = _supabase_request("POST", "crm_change_proposals", body=row)
    if err:
        return f"propose_crm_change failed: {err}"
    pid = data[0]["id"] if isinstance(data, list) and data else "?"
    return f"Proposed {entity} {row['op']} → crm_change_proposals id={pid}, status=pending."


def _run_query_commission_ledger(arguments: dict[str, Any]) -> str:
    limit = max(1, min(int(arguments.get("limit", 20) or 20), 200))
    params: dict[str, Any] = {"select": "*", "limit": str(limit), "order": "created_at.desc"}
    if arguments.get("reconciliationStatus"):
        params["reconciliation_status"] = f"eq.{arguments['reconciliationStatus']}"
    if arguments.get("espocrmPolicyId"):
        params["espocrm_policy_id"] = f"eq.{arguments['espocrmPolicyId']}"
    if arguments.get("auditStatus"):
        params["audit_status"] = f"eq.{arguments['auditStatus']}"
    data, err = _supabase_request("GET", "commission_ledger", params=params)
    if err:
        return f"query_commission_ledger failed: {err}"
    return json.dumps(data)


def _run_record_commission_parity(arguments: dict[str, Any]) -> str:
    run_id = arguments.get("runId")
    rows = arguments.get("rows") or []
    if not run_id:
        return "record_commission_parity: missing runId."
    if not isinstance(rows, list) or not rows:
        return "record_commission_parity: rows must be a non-empty array."
    for r in rows:
        r["run_id"] = run_id
    data, err = _supabase_request("POST", "commission_parity_report", body=rows)
    if err:
        return f"record_commission_parity failed: {err}"
    n = len(data) if isinstance(data, list) else 0
    return f"Recorded {n} parity rows → commission_parity_report run_id={run_id}."


def _discover_payload() -> dict[str, Any]:
    """Return a valid MCP 2024-11-05 HTTP transport discovery payload.

    The response mirrors the ``initialize`` result shape plus a ``tools``
    array (equivalent to a ``tools/list`` result) so that MCP clients can
    discover capabilities and available tools in a single request.
    """
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "tools": _mcp_tools(),
    }


def _server_list_payload() -> dict[str, Any]:
    config = _read_config()
    servers = _get_servers(config)
    items = [_normalize(name, entry) for name, entry in servers.items() if entry is not None]
    return {"servers": items, "total": len(items), "categories": KNOWN_CATEGORIES}


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/mcp")
async def list_mcp_via_short_path(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return JSONResponse(_discover_payload())


@app.get("/mcp/discover")
async def mcp_discover_get(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return JSONResponse(_discover_payload())


@app.post("/mcp/discover")
async def mcp_discover_post(request: Request):
    return await mcp_discover_get(request)


@app.post("/mcp")
async def mcp_jsonrpc(request: Request):
    if not _check_auth(request):
        return _jsonrpc_error(None, -32001, "Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return _jsonrpc_error(None, -32700, "Invalid JSON payload")

    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if not method:
        return _jsonrpc_error(request_id, -32600, "Missing method")

    if method == "initialize":
        requested_version = params.get("protocolVersion", MCP_PROTOCOL_VERSION)
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "notifications/initialized":
        return JSONResponse(status_code=202, content=None)

    if method == "tools/list":
        return _jsonrpc_result(request_id, {"tools": _mcp_tools()})

    # Compatibility alias for clients that probe with a non-standard discover method.
    if method == "discover":
        return _jsonrpc_result(request_id, _discover_payload())

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}

        if tool_name == "ping":
            text = _run_ping(arguments)
            return _jsonrpc_result(request_id, {"content": [{"type": "text", "text": text}]})

        if tool_name == "espocrm_get_current_user":
            text = _run_espocrm_get_current_user()
            return _jsonrpc_result(request_id, {"content": [{"type": "text", "text": text}]})

        tool_runners = {
            "search_accounts": _run_search_accounts,
            "get_account": _run_get_account,
            "propose_crm_change": _run_propose_crm_change,
            "query_commission_ledger": _run_query_commission_ledger,
            "record_commission_parity": _run_record_commission_parity,
        }
        runner = tool_runners.get(tool_name)
        if runner is not None:
            text = runner(arguments)
            return _jsonrpc_result(request_id, {"content": [{"type": "text", "text": text}]})

        return _jsonrpc_error(request_id, -32602, f"Unknown tool: {tool_name}")

    return _jsonrpc_error(request_id, -32601, f"Method not found: {method}")


@app.get("/api/mcp")
async def list_mcp(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return JSONResponse(_server_list_payload())


@app.post("/api/mcp")
async def add_mcp(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Missing server name"}, status_code=400)
    config = _read_config()
    if "mcp_servers" not in config:
        config["mcp_servers"] = {}
    entry = _to_entry(body)
    config["mcp_servers"][name] = entry
    _write_config(config)
    log.info("Added MCP server: %s", name)
    return JSONResponse({"ok": True, "server": _normalize(name, entry)})


@app.put("/api/mcp/{name}")
async def update_mcp(name: str, request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    body = await request.json()
    config = _read_config()
    servers = _get_servers(config)
    if name not in servers:
        return JSONResponse({"ok": False, "error": f"MCP server not found: {name}"}, status_code=404)
    existing = servers[name] if isinstance(servers[name], dict) else {}
    for key in ("command", "args", "env", "url", "headers", "enabled"):
        if key in body:
            existing[key] = body[key]
    if "transportType" in body:
        existing["transport"] = body["transportType"]
    config["mcp_servers"][name] = existing
    _write_config(config)
    log.info("Updated MCP server: %s", name)
    return JSONResponse({"ok": True, "server": _normalize(name, existing)})


@app.delete("/api/mcp/{name}")
async def delete_mcp(name: str, request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    config = _read_config()
    servers = _get_servers(config)
    if name not in servers:
        return JSONResponse({"ok": False, "error": f"MCP server not found: {name}"}, status_code=404)
    del config["mcp_servers"][name]
    _write_config(config)
    log.info("Deleted MCP server: %s", name)
    return JSONResponse({"ok": True})


@app.api_route("/api/mcp/test", methods=["POST"])
async def test_mcp(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return JSONResponse(
        {
            "ok": False,
            "error": "Live MCP server testing requires a newer agent-dashboard build.",
        },
        status_code=501,
    )


@app.api_route("/api/mcp/discover", methods=["POST"])
async def discover_mcp(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return JSONResponse(
        {
            "ok": False,
            "error": "MCP discovery requires a newer agent-dashboard build.",
        },
        status_code=501,
    )


@app.api_route("/api/mcp/presets", methods=["GET"])
async def mcp_presets(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return JSONResponse({"presets": [], "total": 0})


@app.api_route("/api/mcp/hub-sources", methods=["GET"])
async def mcp_hub_sources(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return JSONResponse({"sources": [], "total": 0})


@app.api_route("/api/mcp/hub-search", methods=["GET"])
async def mcp_hub_search(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return JSONResponse({"results": [], "total": 0})


@app.api_route("/api/mcp/configure", methods=["POST"])
async def mcp_configure(request: Request):
    return await add_mcp(request)


@app.get("/api/mcp/{name}/logs")
async def mcp_logs(name: str, request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return JSONResponse({"logs": [], "name": name})
