"""rsg-hermes MCP bridge — exposes the rsg-hermes FastAPI surface as MCP tools.

Same shape as the espo-mcp bridge: a single-file FastAPI app speaking MCP
JSON-RPC 2.0 over HTTP at /mcp (and /api/mcp), with /healthz for liveness.
It bakes in no LLM — any agent (the Nous gateway, Claude, etc.) can call it.

It is a THIN PROXY to rsg-hermes-api. All real logic — the router, the
renewal/retention/intake engines, the CRM write-gate — stays in rsg-hermes.
The `hermes_dispatch` tool is the keystone: it forwards a natural-language
command to POST /dispatch, which already returns requires_confirmation=true for
any write, so the human-approval gate is preserved end to end.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("rsg-hermes-mcp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# --- config -----------------------------------------------------------------
# Backend rsg-hermes FastAPI. Default assumes this bridge joins the external
# `hermes-shared` docker network and reaches the api container by name.
HERMES_API_URL = os.environ.get("HERMES_API_URL", "http://rsg-hermes-api:8787").rstrip("/")
# Optional bearer the bridge presents to hermes-api (only if hermes-api enforces one).
# Absent => run anonymously (fine; most hermes-api routes are unauthenticated).
# Present but EMPTY => a misconfiguration, refused at import.
#
# On 2026-07-26 docker-compose.yml declared HERMES_API_TOKEN=${HERMES_API_TOKEN:-}
# while /opt/app/.env never defined it, so this resolved to "". The guard below
# was a bare truthiness test, so an empty value silently omitted the
# Authorization header and every token-gated route (/api/ams/search-insured,
# /api/hermes/book-sync, /api/hermes/tts) answered "invalid or missing bearer
# token" while the configuration looked correct. Failing at startup turns that
# into a five-second diagnosis.
_raw_hermes_token = os.environ.get("HERMES_API_TOKEN")
if _raw_hermes_token is not None and not _raw_hermes_token.strip():
    raise RuntimeError(
        "HERMES_API_TOKEN is set but empty. Unset it to call hermes-api "
        "anonymously, or give it the value hermes-api expects - an empty "
        "token silently disables authentication."
    )
HERMES_API_TOKEN = (_raw_hermes_token or "").strip()
# Intake submissions use a separate shared secret because they are accepted
# asynchronously from trusted automation surfaces such as Onyx and voice tools.
RSG_INTAKE_API_KEY = os.environ.get("RSG_INTAKE_API_KEY", "").strip()
# Optional bearer the BRAIN must present to this bridge (mirrors espo-mcp).
AUTH_TOKEN = os.environ.get("API_SERVER_KEY", "").strip()

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "rsg-hermes-mcp-bridge"
SERVER_VERSION = "1.0.0"
HTTP_TIMEOUT = 45

app = FastAPI(title="rsg-hermes MCP Bridge", docs_url=None, redoc_url=None)


def _check_auth(request: Request) -> bool:
    if not AUTH_TOKEN:
        return True
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() == AUTH_TOKEN
    return False


# --- backend call helper ----------------------------------------------------
def _api(method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
    url = HERMES_API_URL + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if HERMES_API_TOKEN:
        headers["Authorization"] = f"Bearer {HERMES_API_TOKEN}"
    if RSG_INTAKE_API_KEY and path.startswith("/api/intake"):
        headers["X-RSG-API-Key"] = RSG_INTAKE_API_KEY
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        return {"_error": f"HTTP {exc.code} from {path}", "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"Could not reach hermes-api at {path}", "detail": str(exc)}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, indent=2, default=str)


# --- tool catalog -----------------------------------------------------------
def _mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "ping",
            "description": "Liveness check for the rsg-hermes bridge.",
            "inputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "hermes_dispatch",
            "description": (
                "KEYSTONE. Send a natural-language command to the rsg-hermes router "
                "(POST /dispatch). Handles CRM lookups, enrichment, renewals, intake, "
                "documents, commissions — anything the rsg-hermes dispatcher routes. "
                "If the command would write to the CRM, the response has "
                "requires_confirmation=true: relay that to the human, get approval, then "
                "call again with confirm=true. Never set confirm=true on your own."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The natural-language instruction."},
                    "confirm": {
                        "type": "boolean",
                        "description": "Only true after a human approved a write. Default false.",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_renewals",
            "description": "Live Renewals Cockpit: urgency buckets + next-90-day renewals (read-only).",
            "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
        {
            "name": "retention_scan",
            "description": "Retention scan data — quiet quotes and renewal milestones at risk (read-only).",
            "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
        {
            "name": "list_tasks",
            "description": "Open Command Center task cards (e.g. Gretchen's worksheet queue), read-only.",
            "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
        {
            "name": "complete_task",
            "description": "Mark a Command Center task complete (POST /api/command-center/tasks/{id}/complete).",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "Task id to complete."}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "create_task",
            "description": (
                "Create/assign a task under a case via POST /api/tasks. Use to give Gretchen "
                "(or yourself) a to-do: it lands in the Command Center queue, surfaces in the "
                "assignee's daily 8:30am list, and pings the team chat. Assignee + creator "
                "emails are validated against agency_crm_users — use the '.net' addresses "
                "(e.g. gretchen@risksolutionsgroup.net). Idempotent on title within a case: "
                "re-creating the same title is a no-op (created=false)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "case_id": {"type": "string", "description": "Case the task belongs to (required). Use a client's renewal/service case id, or a standing 'general' case for day-planning tasks."},
                    "title": {"type": "string", "description": "Short task title (deduped within the case)."},
                    "description": {"type": "string", "description": "Optional detail / instructions for the assignee."},
                    "assigned_to_email": {"type": "string", "description": "Who does it, e.g. 'gretchen@risksolutionsgroup.net'. Must be an active agency_crm_users email."},
                    "created_by_email": {"type": "string", "description": "Who assigned it (defaults to the service user). Must be a valid agency_crm_users email."},
                    "priority": {"type": "string", "description": "low | medium | high (default medium)."},
                    "due_at": {"type": "string", "description": "Optional ISO due date/time, e.g. '2026-07-24' or '2026-07-24T09:00:00Z'."},
                },
                "required": ["case_id", "title"],
                "additionalProperties": False,
            },
        },
        {
            "name": "create_case",
            "description": (
                "Open a case (agency_crm_cases) via POST /api/cases — the container a task hangs on. "
                "Use this to fill the gap when a client/prospect has no case yet, then attach tasks with "
                "create_task. owner_email is REQUIRED and validated against agency_crm_users (use a '.net' "
                "address, e.g. gretchen@risksolutionsgroup.net). Put the client on insured_name (free text is "
                "fine for a brand-new prospect). Returns the created case incl. its id and case_number."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Case title, e.g. 'Bull Dawg Trucking — fleet remarket'."},
                    "case_type": {"type": "string", "description": "renewal | service | claims | marketing | endorsement | ... (default 'service'). Use 'marketing' for a new-business remarket/quote."},
                    "owner_email": {"type": "string", "description": "Case owner (required). Must be an active agency_crm_users email, e.g. gretchen@risksolutionsgroup.net."},
                    "created_by_email": {"type": "string", "description": "Who opened it (defaults to the service user). Must be a valid agency_crm_users email."},
                    "description": {"type": "string", "description": "Optional case detail / context."},
                    "priority": {"type": "string", "description": "low | medium | high (default medium)."},
                    "insured_name": {"type": "string", "description": "Client/prospect name (free text ok for a new prospect)."},
                    "insured_database_id": {"type": "string", "description": "NowCerts insured GUID, if the AMS record exists."},
                    "policy_number": {"type": "string", "description": "Related policy number, if any."},
                    "due_at": {"type": "string", "description": "Optional ISO due date/time."},
                },
                "required": ["title", "owner_email"],
                "additionalProperties": False,
            },
        },
        {
            "name": "draft_intake",
            "description": "Submit a new-business intake payload to the intake lane (POST /api/intake). Drafts, does not finalize.",
            "inputSchema": {
                "type": "object",
                "properties": {"payload": {"type": "object", "description": "Intake fields object."}},
                "required": ["payload"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_documents",
            "description": "List filed documents (Nextcloud-backed) via GET /api/documents (read-only).",
            "inputSchema": {
                "type": "object",
                "properties": {"folder": {"type": "string", "description": "Optional folder filter."}},
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "save_document",
            "description": "File a document to Nextcloud via POST /api/documents/save.",
            "inputSchema": {
                "type": "object",
                "properties": {"payload": {"type": "object", "description": "Document save fields."}},
                "required": ["payload"],
                "additionalProperties": False,
            },
        },
        {
            "name": "file_to_nextcloud",
            "description": "Upload a binary file (PDF) to the client's Nextcloud folder via POST /api/nextcloud/upload.",
            "inputSchema": {
                "type": "object",
                "properties": {"payload": {"type": "object", "description": "Upload fields: title, account_name, content_base64, content_type (default application/pdf)."}},
                "required": ["payload"],
                "additionalProperties": False,
            },
        },
        {
            "name": "create_client",
            "description": "Create a new EspoCRM Account + Contact and link them via POST /api/crm/create-client.",
            "inputSchema": {
                "type": "object",
                "properties": {"payload": {"type": "object", "description": "Client creation fields: account_name, account_type, website, billing_*, fein, business_entity, industry, sic_code, intel_naics, intel_sic, contact_first_name, contact_last_name, contact_email, contact_phone, contact_title, contact_type, client_type."}},
                "required": ["payload"],
                "additionalProperties": False,
            },
        },
        {
            "name": "sync_health",
            "description": "EspoCRM/NowCerts sync health snapshot via GET /api/hermes/sync-health (read-only).",
            "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
        {
            "name": "list_commissions",
            "description": "Commission ledger rows (expected vs actual, reconciled) via GET /api/commissions (read-only). Use for 'what are we owed', 'commission status', 'unreconciled commissions'.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max rows to return (optional)."},
                    "status": {"type": "string", "description": "Optional status filter, e.g. 'expected', 'reconciled', 'unmatched'."},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "commission_rules",
            "description": "Configured commission rules (carrier/LOB commission percentages) via GET /api/commission-rules (read-only).",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "Max rows to return (optional)."}},
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "carrier_appetite",
            "description": "Carrier appetite reference — which carriers RSG can place a risk with, by line of business, state, and class code (read-only) via GET /api/carriers. Use for 'who writes this?', 'carrier fit for X', 'what carriers do we have for GL in TX'.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "carrier": {"type": "string", "description": "Partial carrier name filter."},
                    "state": {"type": "string", "description": "2-letter state filter, e.g. 'TX'."},
                    "lob": {"type": "string", "description": "Line of business filter (partial), e.g. 'General Liability'."},
                    "naics": {"type": "string", "description": "Exact NAICS code filter."},
                    "limit": {"type": "integer", "description": "Max rows to return (optional)."},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "ams_search_insured",
            "description": "Search the Momentum AMS for existing insureds by name, email, or FEIN (read-only). Use as the search-before-insert gate before ams_create_insured.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Commercial name or 'FirstName LastName'."},
                    "email": {"type": "string"},
                    "fein": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "ams_create_insured",
            "description": "Create/upsert an insured in the Momentum AMS via POST /api/ams/insured. Search-before-insert + DatabaseId upsert. confirm=false (default) returns a dry-run proposal with requires_confirmation=true: relay that to the human, get approval, then call again with confirm=true. Never set confirm=true on your own.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "payload": {"type": "object", "description": "Momentum insured fields: CommercialName, FirstName, LastName, FEIN, AddressLine1, City, State, ZipCode, eMail, Phone, etc. Include DatabaseId to upsert a known record."},
                    "confirm": {"type": "boolean", "description": "Only true after a human approved the write. Default false."},
                    "source": {"type": "string", "description": "Caller id for audit, e.g. 'onyx'."},
                },
                "required": ["payload"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ams_upsert_policy",
            "description": "Create/upsert a policy in the Momentum AMS via POST /api/ams/policy. Search-before-insert by Number+InsuredDatabaseId; upsert by DatabaseId. confirm=false (default) returns a dry-run proposal with requires_confirmation=true: relay to the human, get approval, then call again with confirm=true. Never set confirm=true on your own.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "payload": {"type": "object", "description": "Momentum policy fields: Number, InsuredDatabaseId, EffectiveDate, ExpirationDate, Premium, AgencyCommissionPercent, LineOfBusinessName, etc. Include DatabaseId to upsert a known policy."},
                    "confirm": {"type": "boolean", "description": "Only true after a human approved the write. Default false."},
                    "source": {"type": "string", "description": "Caller id for audit, e.g. 'onyx'."},
                },
                "required": ["payload"],
                "additionalProperties": False,
            },
        },
    ]


# --- tool handlers ----------------------------------------------------------
def _run_ping(args: dict[str, Any]) -> str:
    return f"rsg-hermes bridge reachable. backend={HERMES_API_URL}. echo={args.get('message', 'pong')}"


def _run_hermes_dispatch(args: dict[str, Any]) -> str:
    command = (args.get("command") or "").strip()
    if not command:
        return "Error: 'command' is required."
    body = {"command": command, "confirm": bool(args.get("confirm", False))}
    return _text(_api("POST", "/dispatch", body=body))


def _run_list_renewals(_: dict[str, Any]) -> str:
    return _text(_api("GET", "/api/command-center/renewals"))


def _run_retention_scan(_: dict[str, Any]) -> str:
    return _text(_api("GET", "/api/command-center/retention"))


def _run_list_tasks(_: dict[str, Any]) -> str:
    return _text(_api("GET", "/api/command-center/tasks"))


def _run_complete_task(args: dict[str, Any]) -> str:
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return "Error: 'task_id' is required."
    return _text(_api("POST", f"/api/command-center/tasks/{urllib.parse.quote(task_id)}/complete"))


def _run_create_task(args: dict[str, Any]) -> str:
    case_id = (args.get("case_id") or "").strip()
    title = (args.get("title") or "").strip()
    if not case_id:
        return "Error: 'case_id' is required."
    if not title:
        return "Error: 'title' is required."
    body = {
        "case_id": case_id,
        "title": title,
        "description": args.get("description"),
        "priority": args.get("priority") or "medium",
        "assigned_to_email": args.get("assigned_to_email"),
        "created_by_email": args.get("created_by_email"),
        "due_at": args.get("due_at"),
    }
    return _text(_api("POST", "/api/tasks", body=body))


def _run_create_case(args: dict[str, Any]) -> str:
    title = (args.get("title") or "").strip()
    owner = (args.get("owner_email") or "").strip()
    if not title:
        return "Error: 'title' is required."
    if not owner:
        return "Error: 'owner_email' is required (must be an active agency_crm_users email, e.g. gretchen@risksolutionsgroup.net)."
    body = {
        "title": title,
        "case_type": args.get("case_type") or "service",
        "owner_email": owner,
        "created_by_email": args.get("created_by_email"),
        "description": args.get("description"),
        "priority": args.get("priority") or "medium",
        "insured_name": args.get("insured_name"),
        "insured_database_id": args.get("insured_database_id"),
        "policy_number": args.get("policy_number"),
        "due_at": args.get("due_at"),
    }
    return _text(_api("POST", "/api/cases", body=body))


def _run_draft_intake(args: dict[str, Any]) -> str:
    payload = args.get("payload")
    if not isinstance(payload, dict):
        return "Error: 'payload' object is required."
    return _text(_api("POST", "/api/intake", body=payload))


def _run_list_documents(args: dict[str, Any]) -> str:
    return _text(_api("GET", "/api/documents", params={"folder": args.get("folder")}))


def _run_save_document(args: dict[str, Any]) -> str:
    payload = args.get("payload")
    if not isinstance(payload, dict):
        return "Error: 'payload' object is required."
    return _text(_api("POST", "/api/documents/save", body=payload))


def _run_file_to_nextcloud(args: dict[str, Any]) -> str:
    payload = args.get("payload")
    if not isinstance(payload, dict):
        return "Error: 'payload' object is required."
    return _text(_api("POST", "/api/nextcloud/upload", body=payload))


def _run_create_client(args: dict[str, Any]) -> str:
    payload = args.get("payload")
    if not isinstance(payload, dict):
        return "Error: 'payload' object is required."
    return _text(_api("POST", "/api/crm/create-client", body=payload))


def _run_sync_health(_: dict[str, Any]) -> str:
    return _text(_api("GET", "/api/hermes/sync-health"))


def _run_list_commissions(args: dict[str, Any]) -> str:
    params = {"limit": args.get("limit"), "status": args.get("status")}
    return _text(_api("GET", "/api/commissions", params=params))


def _run_commission_rules(args: dict[str, Any]) -> str:
    return _text(_api("GET", "/api/commission-rules", params={"limit": args.get("limit")}))


def _run_carrier_appetite(args: dict[str, Any]) -> str:
    params = {
        "carrier": args.get("carrier"),
        "state": args.get("state"),
        "lob": args.get("lob"),
        "naics": args.get("naics"),
        "limit": args.get("limit"),
    }
    return _text(_api("GET", "/api/carriers", params=params))


def _run_ams_search_insured(args: dict[str, Any]) -> str:
    params = {"name": args.get("name"), "email": args.get("email"), "fein": args.get("fein")}
    return _text(_api("GET", "/api/ams/search-insured", params=params))


def _run_ams_create_insured(args: dict[str, Any]) -> str:
    payload = args.get("payload")
    if not isinstance(payload, dict):
        return "Error: 'payload' object is required."
    body = {"payload": payload, "confirm": bool(args.get("confirm", False)), "source": args.get("source", "onyx")}
    return _text(_api("POST", "/api/ams/insured", body=body))


def _run_ams_upsert_policy(args: dict[str, Any]) -> str:
    payload = args.get("payload")
    if not isinstance(payload, dict):
        return "Error: 'payload' object is required."
    body = {"payload": payload, "confirm": bool(args.get("confirm", False)), "source": args.get("source", "onyx")}
    return _text(_api("POST", "/api/ams/policy", body=body))


_HANDLERS = {
    "ping": _run_ping,
    "hermes_dispatch": _run_hermes_dispatch,
    "list_renewals": _run_list_renewals,
    "retention_scan": _run_retention_scan,
    "list_tasks": _run_list_tasks,
    "complete_task": _run_complete_task,
    "create_task": _run_create_task,
    "create_case": _run_create_case,
    "draft_intake": _run_draft_intake,
    "list_documents": _run_list_documents,
    "save_document": _run_save_document,
    "file_to_nextcloud": _run_file_to_nextcloud,
    "create_client": _run_create_client,
    "sync_health": _run_sync_health,
    "list_commissions": _run_list_commissions,
    "commission_rules": _run_commission_rules,
    "carrier_appetite": _run_carrier_appetite,
    "ams_search_insured": _run_ams_search_insured,
    "ams_create_insured": _run_ams_create_insured,
    "ams_upsert_policy": _run_ams_upsert_policy,
}


# --- JSON-RPC plumbing (MCP 2024-11-05) -------------------------------------
def _sse(obj: dict) -> Response:
    return Response(content=f"event: message\r\ndata: {json.dumps(obj)}\r\n\r\n", media_type="text/event-stream")

def _result(rid: Any, result: dict[str, Any]) -> Response:
    return _sse({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid: Any, code: int, message: str) -> Response:
    return _sse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "backend": HERMES_API_URL}


@app.post("/mcp")
@app.post("/api/mcp")
async def mcp(request: Request) -> JSONResponse:
    if not _check_auth(request):
        return _error(None, -32001, "Unauthorized")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error(None, -32700, "Parse error")

    method = body.get("method")
    rid = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        return _result(rid, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method in ("notifications/initialized", "initialized"):
        return Response(status_code=202)
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": _mcp_tools()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = _HANDLERS.get(name)
        if handler is None:
            return _result(rid, {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True})
        try:
            text = handler(args)
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            return _result(rid, {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True})
        return _result(rid, {"content": [{"type": "text", "text": text}]})

    return _error(rid, -32601, f"Method not found: {method}")
