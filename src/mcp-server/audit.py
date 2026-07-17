"""audit.py — the single place that emits the authoritative per-tool-call audit
event, and mints the correlation id that ties a native Azure log line back to it.

`main.py` / `executor.py` only ever call four things from here:

    cid = new_correlation_id()                       # one per tool call
    ip  = client_ip()                                # the user's real IP (ingress only)
    ua  = build_user_agent(cid, oid)                 # injected as AZURE_HTTP_USER_AGENT (layer 2)
    await get_audit_sink().record(AuditEvent(...))   # the authoritative row (layer 1)

Everything else — Log Analytics ingestion, credential, stdout fallback,
never-raise semantics, the fastmcp request detail behind client_ip — is hidden
here. Swapping the sink (Log Analytics <-> stdout) is an env change, not a code
change. See docs/oid-log-tracking/.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("dataops-mcp.audit")


# --- interface 1: correlation id (one per tool call) -------------------------
def new_correlation_id() -> str:
    """hex (no dashes) => safe as a User-Agent token and easy to extract in KQL."""
    return uuid.uuid4().hex


# --- interface 2: the UA injection token (layer 2) ---------------------------
def build_user_agent(correlation_id: str, oid: str | None = None) -> str:
    """String appended to AZURE_HTTP_USER_AGENT.

    Default is GUID only (see selection doc §7). Set AUDIT_UA_INCLUDE_OID=1 to
    also stamp the raw oid (opt-in); the GUID stays the authoritative join key
    either way.
    """
    tok = f"mcp/{correlation_id}"
    if oid and os.environ.get("AUDIT_UA_INCLUDE_OID", "0") == "1":
        tok += f";oid/{oid}"
    return tok


# --- interface 3: the user's real IP (a layer-1 field) -----------------------
def client_ip() -> str | None:
    """First hop of the ingress request's X-Forwarded-For = the user's real IP.

    The user's IP only exists at the MCP ingress (downstream everything is the
    sandbox egress). The exact fastmcp dependency symbol can vary by version
    (get_http_headers / get_http_request); if we can't read it we degrade to
    None — never fail a tool call over a missing IP. Keeping this detail here
    means main.py never touches the HTTP layer.
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers() or {}
        xff = headers.get("x-forwarded-for")
        return xff.split(",")[0].strip() if xff else None
    except Exception:
        return None


# --- interface 4: the audit event + sink -------------------------------------
@dataclass
class AuditEvent:
    correlation_id: str
    tool: str
    group: str | None
    user_oid: str | None = None
    user_upn: str | None = None
    client_ip: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    command: str | None = None
    explanation: str | None = None
    sp_appid: str | None = None
    exit_code: int | None = None
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_row(self) -> dict:
        # Column names must match the DCR stream / table schema (provisioning).
        # TimeGenerated is the required column for a custom Log Analytics table.
        d = asdict(self)
        d["TimeGenerated"] = d.pop("ts")
        return d


class AuditSink:
    """A sink records one AuditEvent. Contract: never raise, never block a tool."""

    async def record(self, event: AuditEvent) -> None:  # pragma: no cover
        ...


class StdoutAuditSink(AuditSink):
    """Fallback for local dev / when no DCR is configured: a structured JSON line.

    Replaces the old free-text `logger.info("tool call by ...")` with one machine
    -parseable audit record.
    """

    async def record(self, event: AuditEvent) -> None:
        try:
            logger.info("AUDIT %s", json.dumps(event.to_row(), ensure_ascii=False))
        except Exception as e:  # never let audit break a tool call
            logger.warning("audit stdout failed: %s", e)


class LogAnalyticsAuditSink(AuditSink):
    """Ships to Log Analytics via the Logs Ingestion API + a Data Collection Rule.

    The azure-monitor-ingestion import is lazy so the local (stdout) path never
    needs the package installed.
    """

    def __init__(self, endpoint: str, rule_id: str, stream: str):
        from azure.identity.aio import DefaultAzureCredential
        from azure.monitor.ingestion.aio import LogsIngestionClient

        self._stream = stream
        self._rule_id = rule_id
        self._cred = DefaultAzureCredential()  # = the MCP app's managed identity
        self._client = LogsIngestionClient(endpoint, self._cred)

    async def record(self, event: AuditEvent) -> None:
        # Audit must never block or fail a tool call: bound it and swallow. If we
        # ever need stronger guarantees, add a local WAL / retry behind this.
        try:
            await asyncio.wait_for(
                self._client.upload(self._rule_id, self._stream, [event.to_row()]),
                timeout=float(os.environ.get("AUDIT_TIMEOUT", "5")),
            )
        except Exception as e:
            logger.warning("audit ingestion failed (%s); cid=%s", e, event.correlation_id)


_sink: AuditSink | None = None


def get_audit_sink() -> AuditSink:
    """Singleton. Log Analytics when AUDIT_DCR_* is configured, else stdout."""
    global _sink
    if _sink is None:
        endpoint = os.environ.get("AUDIT_DCR_ENDPOINT")
        rule_id = os.environ.get("AUDIT_DCR_RULE_ID")
        stream = os.environ.get("AUDIT_STREAM_NAME", "Custom-MCPAudit_CL")
        if endpoint and rule_id:
            _sink = LogAnalyticsAuditSink(endpoint, rule_id, stream)
            logger.info("audit sink: Log Analytics (%s)", stream)
        else:
            _sink = StdoutAuditSink()
            logger.info("audit sink: stdout (AUDIT_DCR_* not set)")
    return _sink
