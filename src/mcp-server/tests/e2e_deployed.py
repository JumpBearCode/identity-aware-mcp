#!/usr/bin/env python3
"""End-to-end smoke test against a DEPLOYED Identity-Aware MCP server.

This drives the *real* front door — Entra sign-in -> OBO -> MCP streamable-HTTP
-> diagnose_bash / action_bash -> a live ACA sandbox — so it exercises the whole
stack that unit tests can't: the auth/OBO path, group-scoped tool exposure, the
two-layer lock, sandbox create + FIC `az login` bootstrap, and session-sticky
routing (reuse). It is a *manual* test: it needs an interactive browser sign-in
and a live deployment, so it is not wired into CI.

WHAT IT ASSERTS
  1. Both tools (`diagnose_bash`, `action_bash`) are exposed  -> auth + OBO +
     group membership resolved.
  2. `diagnose_bash` runs bash                                -> cold create +
     bootstrap succeeds (first call), exit code / stdout as expected.
  3. `az account show` works inside the sandbox              -> the FIC
     passwordless `az login` bootstrap actually took.
  4. A marker file written on call #1 is readable on call #3 -> same sandbox was
     reused, i.e. session-sticky routing works (the "hit" path).
  5. `action_bash` runs bash                                 -> the second group
     / its own sandbox works too.

PREREQS
  pip install mcp msal            # client-side SDKs (not in the server image)
  You must sign in as a member of BOTH the diagnose and action Entra groups.

RUN
  python e2e_deployed.py
  # first run prints a device-code URL; open it, enter the code, sign in.
  # the token is cached (~/.cache/aca-mcp-e2e/token_cache.json) so re-runs are
  # silent until it expires.

CONFIG (env overrides; defaults target the current dev deployment)
  MCP_SERVER_URL         full /mcp URL of the deployed server
  AZURE_TENANT_ID        Entra tenant GUID
  MCP_APP_ID             the MCP server's app (client) id -> scope api://<id>/user_impersonation
  MCP_DEVICE_CLIENT_ID   a public client pre-authorized on the MCP API (default: VS Code)

Exit code 0 = all checks passed, 1 = at least one failed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import msal
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# --------------------------------------------------------------------- config
MCP_SERVER_URL = os.environ.get(
    "MCP_SERVER_URL",
    "https://dataops-aca-mcp.icyrock-96f978c0.westus2.azurecontainerapps.io/mcp",
)
TENANT_ID = os.environ.get("AZURE_TENANT_ID", "9ea91fbb-1313-4312-a601-b6d9ab7d4de3")
MCP_APP_ID = os.environ.get("MCP_APP_ID", "88de6a37-cf75-40d3-83e8-44c5ccbc0895")
# VS Code's first-party public client is pre-authorized on the MCP API, so the
# device-code flow needs no consent screen. Override if you register your own.
DEVICE_CLIENT_ID = os.environ.get("MCP_DEVICE_CLIENT_ID", "aebc6443-996d-45c2-90f0-388ff96faa56")
SCOPES = [f"api://{MCP_APP_ID}/user_impersonation"]
CACHE_FILE = os.path.expanduser("~/.cache/aca-mcp-e2e/token_cache.json")

# --------------------------------------------------------------------- checks
_results: list[tuple[bool, str, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    _results.append((passed, name, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


# --------------------------------------------------------------------- auth
def get_token() -> str:
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_FILE):
        cache.deserialize(open(CACHE_FILE).read())
    app = msal.PublicClientApplication(
        DEVICE_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        token_cache=cache,
    )
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])  # silent re-auth
    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise SystemExit(f"device flow init failed: {flow}")
        print("\n" + "=" * 72 + f"\n{flow['message']}\n" + "=" * 72 + "\n", flush=True)
        result = app.acquire_token_by_device_flow(flow)  # blocks on the browser step
    if cache.has_state_changed:
        with open(CACHE_FILE, "w") as f:
            f.write(cache.serialize())
    if "access_token" not in result:
        raise SystemExit(f"sign-in failed: {result.get('error')} - {result.get('error_description')}")
    return result["access_token"]


# --------------------------------------------------------------------- driver
def result_of(res) -> dict:
    """Normalize a CallToolResult into {exit_code, stdout, stderr, truncated}."""
    if getattr(res, "structuredContent", None):
        return res.structuredContent
    for item in res.content:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"exit_code": None, "stdout": text, "stderr": ""}
    return {"exit_code": None, "stdout": "", "stderr": "<no content>"}


async def run() -> None:
    mark1 = f"reuse-{uuid.uuid4().hex}"   # proves session-sticky sandbox reuse
    mark2 = f"action-{uuid.uuid4().hex}"  # proves the action tool/group works
    token = get_token()
    print(f"\nserver: {MCP_SERVER_URL}\n")

    async with streamablehttp_client(MCP_SERVER_URL, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()

            tools = (await s.list_tools()).tools
            names = {t.name for t in tools}
            check("tools exposed (auth + OBO + groups)", {"diagnose_bash", "action_bash"} <= names, f"got {sorted(names)}")

            # action_bash must carry the forced-approval _meta (server-side). Verifies
            # fastmcp passes anthropic/requiresUserInteraction through to the client —
            # this is the signal Claude Code uses to force a human approval every call.
            action_tool = next((t for t in tools if t.name == "action_bash"), None)
            meta = (action_tool.model_dump(by_alias=True).get("_meta") or {}) if action_tool else {}
            check("action_bash forces human approval (_meta)",
                  meta.get("anthropic/requiresUserInteraction") is True, f"_meta={meta}")

            # 1st call: cold create + bootstrap + write a marker file into the sandbox
            r1 = result_of(await s.call_tool("diagnose_bash", {"command": f"echo {mark1} > /tmp/e2e_marker && echo {mark1}"}))
            check("diagnose_bash runs (create+bootstrap)", r1.get("exit_code") == 0 and mark1 in r1.get("stdout", ""), f"exit={r1.get('exit_code')}")

            # bootstrap actually logged in with the FIC service principal
            r2 = result_of(await s.call_tool("diagnose_bash", {"command": "az account show --query name -o tsv"}))
            check("az works in sandbox (bootstrap az login)", r2.get("exit_code") == 0 and r2.get("stdout", "").strip() != "", f"stdout={r2.get('stdout','').strip()!r}")

            # reuse: the marker file survives => same sandbox => session-sticky routing
            r3 = result_of(await s.call_tool("diagnose_bash", {"command": "cat /tmp/e2e_marker"}))
            check("session reuse (sticky routing / hit path)", mark1 in r3.get("stdout", ""), f"exit={r3.get('exit_code')} stdout={r3.get('stdout','').strip()!r}")

            # the other tool / group
            r4 = result_of(await s.call_tool("action_bash", {"command": f"echo {mark2}"}))
            check("action_bash runs (2nd group/sandbox)", r4.get("exit_code") == 0 and mark2 in r4.get("stdout", ""), f"exit={r4.get('exit_code')}")

            # post-exec Layer-2 hygiene (redact.py): a KNOWN-FORMAT secret in
            # action_bash output must be masked before it reaches the client. Uses a
            # FAKE secret, never a real one. Field-name masking is gone — Layer 2 keys
            # off the VALUE FORMAT (here a connection-string AccountKey=<value>).
            secret = f"REDACTME{uuid.uuid4().hex}=="
            cmd5 = ("echo 'DefaultEndpointsProtocol=https;AccountName=foo;AccountKey="
                    + secret + ";EndpointSuffix=core.windows.net'")
            r5 = result_of(await s.call_tool("action_bash", {"command": cmd5}))
            out5 = r5.get("stdout", "")
            check("post-exec redaction masks known-format secrets (action)",
                  secret not in out5 and "«redacted»" in out5, f"stdout={out5.strip()!r}")

            # redaction is ACTION-ONLY: the same shape via diagnose_bash is NOT masked
            # (diagnose relies on the identity boundary — zero data-plane — not on
            # output scrubbing).
            r6 = result_of(await s.call_tool("diagnose_bash", {"command": cmd5}))
            out6 = r6.get("stdout", "")
            check("diagnose_bash does not redact (action-only gate)",
                  secret in out6 and "«redacted»" not in out6, f"stdout={out6.strip()!r}")

    passed = sum(1 for ok, *_ in _results if ok)
    total = len(_results)
    print(f"\n{'=' * 40}\nE2E: {passed}/{total} checks passed")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
