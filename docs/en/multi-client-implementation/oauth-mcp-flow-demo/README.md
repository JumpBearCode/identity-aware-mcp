# MCP · OAuth Authorization Flow Visualization Teaching App

A **standalone, self-contained OAuth client teaching app** that
walks through the **14-step sequence diagram** of the
[`/mcpproxy` resource-stripping proxy](../implementation-notes-plan-a-mcpproxy-resource-stripping-proxy.md)
**step by step**, using animation to present the **real JSON of every request / response**.

It mimics an "OAuth client that supports static registration (no DCR)" (just like Claude Code / opencode):
using a **static client_id `49af5fc1`** + **PKCE** + **loopback callback**, ultimately obtaining a **real Entra token**.

At the top, you can **switch between two flows for comparison with one click**:

| | 🪄 **MCP proxy** (strips resource, 14 steps) | ⚙️ **MCP direct** (direct to Entra, 11 steps) |
|---|---|---|
| Who uses this path | Claude Code / opencode (**sends** resource) | VS Code (**does not send** resource) |
| PRM `authorization_servers` | Points to **the proxy itself** | Points to **Entra itself** |
| Second discovery document | Proxy's AS metadata | **Entra's openid-configuration** |
| `/authorize` · `/token` | Hits **proxy**, proxy strips resource then forwards to Entra | **Directly** hits Entra, no resource throughout |
| Will it hit `AADSTS9010010` | Client carries resource → needs proxy to strip it to avoid | Does not carry resource → won't hit, no proxy needed |
| Final token / gating | **Exactly the same** (same verifier + OBO + group) | **Exactly the same** |

> Both paths lead to the same destination: the same real Entra token, with identical `aud/scp/oid`. The only difference lies in the "protocol negotiation before token issuance" — **whether a proxy layer is needed to strip `resource`**.

---

> 📄 **Companion Q&A document**: [Q&A-MCP-Authorization-Flow-Discovery-Mechanism-and-redirect_uri-Analysis.md](./qa-mcp-authorization-flow-discovery-mechanism-and-redirect-uri-analysis.md)
> — Explains the 401 mechanism, two rounds of discovery, issuer and well-known probing, and **who actually performs the redirect_uri redirect**.

## Quick Start

```bash
cd docs/en/multi-client-implementation/oauth-mcp-flow-demo
./run.sh                 # Or: python3 server.py
# Open browser at http://localhost:8080
```

- **Zero dependencies**: Uses only Python standard library (`python3` is enough, no pip install).
- **Must be port 8080**: Entra's callback whitelist for client `49af5fc1` only includes
  `http://localhost:8080/callback`, so the app must listen on 8080 for Live login to redirect back.
  (If 8080 is occupied by VS Code / Claude Code, free it up first.)

---

## Two Modes

Switchable at the top:

### 🎬 Demo Playback (default, runnable anytime)
Pure frontend, replays the 14 steps using data **close to real packet captures**. No login required, no server dependency,
ideal for projection-based teaching / screenshots. Use `▶ Play` to auto-advance, or `Next Step / ◀ Previous Step` to step manually; `←/→/Space` also works.

### 🔴 Live Real Call (actually hits your MCP server)
Follow the guide and click three buttons, **every step is a real HTTP call**:

1. **① Real Discovery** — Actually hits `/mcpproxy` to trigger 401, actually GETs two discovery metadata documents (steps 1–4).
2. **② Construct /authorize** — Generates PKCE + state, constructs the authorization request with `resource`,
   and actually captures the **302** returned by the proxy (you will see `resource` stripped, `openid/profile` added — steps 5–6).
3. **③ Open Browser to Login** — Opens a new tab for real Entra login; upon success, Entra 302 redirects back to local
   `:8080/callback`, and the app automatically **exchanges token for real** (steps 8–12) and performs **real `tools/list`** (steps 13–14).
   The obtained real token is **decoded to display claims** (`aud / scp / oid / azp`), proving identity fidelity.

> For security, `access_token / refresh_token / id_token` in the UI are all **truncated and redacted**,
> only displaying decoded non-sensitive claims; the backend **never logs complete tokens**.

---

## 14-Step Mapping (mcpproxy flow, consistent with §3 sequence diagram in the document)

| Step | Swimlane | What Happens |
|---|---|---|
| 1–2 | Client ↔ MCP Endpoint | No token → 401 + `WWW-Authenticate` (pointing to discovery metadata) |
| 3 | Client → Proxy | Protected-Resource Metadata (RFC 9728) → AS points to proxy itself |
| 4 | Client → Proxy | Authorization-Server Metadata (RFC 8414) → **No `registration_endpoint` (no DCR)** |
| 5–6 | Client → Proxy → Entra | `/authorize` (with resource) → Proxy 302 to Entra (**★ strips resource**) |
| 7 | Browser ↔ Entra | Real interactive login (proxy not involved, user's real IP + MFA/CA) |
| 8 | Entra → Client | 302 back to loopback, carrying authorization code (proxy cannot see) |
| 9–12 | Client → Proxy → Entra | `/token` (with resource) → Proxy strips resource and forwards → **Real Entra token** returned as-is |
| 13–14 | Client ↔ MCP Endpoint | Bearer real token → validation + OBO group lookup → gated tool list returned by group |

---

## Directory Structure

```
oauth-mcp-flow-demo/
├── server.py        # Backend: static server + real OAuth client + 14-step capture + /callback (stdlib only)
├── run.sh           # Launch script (includes port 8080 occupancy check)
├── README.md        # This document
└── public/          # Visualization (as required: not placed in src)
    ├── index.html   # Page skeleton
    ├── styles.css   # Dark theme + animations
    ├── steps.js     # 14-step "script": metadata + Demo baked data (single source of truth)
    └── app.js       # Animation engine + SVG sequence diagram + detail panel + Demo/Live control
```

**All visualization is in `public/`, not placed in the project's `src/`.**

---

## What Kind of Client Does It Mimic?

It mimics the type of OAuth client that **complies with the MCP authorization specification and supports static pre-registration** (Claude Code / opencode):

- Follows `WWW-Authenticate` to perform **discovery** (RFC 9728 → RFC 8414);
- Discovers that AS metadata **has no `registration_endpoint`**, thus **does not perform DCR**,
  directly using its own **statically configured `client_id` (`49af5fc1`)**;
- **Public client + PKCE** (no secret);
- Uses **loopback `redirect_uri`** to receive the authorization code;
- Exchanges code at the token endpoint for a token, then calls MCP with Bearer.

The only difference from "direct to Entra" is: it points AS to `/mcpproxy`, and that middle layer does only one thing — **strip `resource`**,
thereby bypassing `AADSTS9010010`. See the implementation notes and bug analysis in the parent `docs/` for details.

---

## FAQ

- **Live stuck at "Waiting for login"**: Confirm the app is running on port 8080; confirm the account you are using is in Entra's
  diagnose / action group (otherwise `tools/list` may be empty, but the flow will still complete).
- **Just want to explain, don't want to log in**: Use Demo mode, the data is close to real packet captures, sufficient for explanation.
- **Change target server**: `MCP_BASE_URL=... python3 server.py` (default points to the deployed ACA instance).