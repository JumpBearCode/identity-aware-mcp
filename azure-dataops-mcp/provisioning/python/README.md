# Python route — full walkthrough

End-to-end setup for the local DataOps MCP stack using the Python SDK
(`msgraph-sdk`). This is the recommended route: it prints every ID/secret as it
goes and is easy to debug. The Bicep route in `../bicep/` produces the same
`.env`; pick one.

Everything below runs from this directory (`provisioning/python/`) unless noted.

---

## 0. Prereqs

- `az login` as a user with these Entra roles:
  - **Application Administrator** — create app registrations + service principals
  - **Group Administrator** — create the two AD security groups
  - (Application Administrator is *also* enough to grant the OBO admin consent in
    step 1 — those are low-privilege, user-consentable scopes. Only high-privilege
    Graph scopes would need Privileged Role Admin / Global Admin.)
- `uv` installed (for the Python deps).
- One env var:

```bash
export AZURE_TENANT_ID=<your tenant id>
az login --tenant $AZURE_TENANT_ID
```

---

## 1. Provision the identities + groups

```bash
uv sync
uv run python provision.py
```

This talks to Microsoft Graph and creates, in your tenant:

| Resource | What it is | Auth |
|---|---|---|
| Entra App `DataOps MCP Server` (+ SP) | The protected resource. Exposes the `api://<mcp>/user_impersonation` scope; VS Code is pre-authorized for it. Requests delegated Graph `User.Read email offline_access openid profile`, and the script **grants admin consent** for those (tenant-wide), so OBO works with no per-user prompt. | client secret |
| AD Group `mcp-diagnose-users` | Membership = "may call the diagnose tool". | — |
| AD Group `mcp-action-admins` | Membership = "may call the action tool" (write). | — |
| Entra App `dataops-diagnose-sp` (+ SP) | Identity the **diagnose-worker** container runs as. | client secret |
| Entra App `dataops-action-sp` (+ SP) | Identity the **action-worker** container runs as. | client secret |

> **No Azure RBAC is assigned.** The two worker SPs exist but can't touch any
> resource until you grant them access in step 3.

On success the script writes `../../.env` (refuses to overwrite an existing one)
with all the IDs + secrets that `docker-compose.yml` consumes, and prints the two
group IDs you'll need in step 2.

**What is OBO, and why the admin consent?**
The client (VS Code) signs the user in and gets a token *for the MCP server*
(`user_impersonation`). The server can't reuse that token to call Graph — it's
audienced to the server, not Graph. **On-Behalf-Of** is the exchange: the server
trades the incoming user token for a *new* token to Graph that still carries the
user's identity. That's how the server looks up **the user's** group membership
(to decide which tools they can call) as the user, not as an app. The admin
consent in step 1 is what lets that exchange succeed silently.

---

## 2. Add users to the AD groups

Group membership is what authorizes a signed-in user to call a tool. Add
yourself (and others) using the group IDs the script printed:

```bash
# your own object id:
az ad signed-in-user show --query id -o tsv

az ad group member add --group <DIAGNOSE_GROUP_ID> --member-id <user-object-id>
az ad group member add --group <ACTION_GROUP_ID>   --member-id <user-object-id>
```

- In `mcp-diagnose-users` → can call the diagnose tool.
- In `mcp-action-admins` → can call the action (write) tool too.

Membership changes can take a minute to propagate into freshly issued tokens.

---

## 3. Grant the worker SPs Azure access

Provisioning deliberately leaves the workers with zero permissions. Decide the
scope (a resource group is the usual choice — avoid subscription-wide) and assign
roles to each worker SP. Example:

```bash
SCOPE="/subscriptions/<sub>/resourceGroups/<rg>"

# diagnose-worker: read-only
az role assignment create --assignee <DIAGNOSE_SP_CLIENT_ID> \
  --role Reader --scope "$SCOPE"

# action-worker: write
az role assignment create --assignee <ACTION_SP_CLIENT_ID> \
  --role Contributor --scope "$SCOPE"
```

The worker client IDs are in `.env` (`DIAGNOSE_SP_CLIENT_ID`,
`ACTION_SP_CLIENT_ID`). This is the Azure-execution boundary: whatever a worker
can do is exactly this RBAC — the user's identity never reaches Azure, only the
worker's SP does.

---

## 4. Run the stack

```bash
cd ../..            # repo root, where .env and docker-compose.yml live
docker compose up --build
```

Three containers come up:

- **mcp-server** (`localhost:8080`) — validates the user's JWT, does the OBO
  group lookup, and routes the call to a worker. Holds no Azure data-plane rights.
- **diagnose-worker** — runs as `diagnose-sp`, read-only `az` commands.
- **action-worker** — runs as `action-sp`, write `az` commands (gated by an
  approval hook).

Each service reads its identity from `.env`; the workers get
`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET` for their SP.

---

## 5. Wire up VS Code

`.vscode/mcp.json`:

```json
{ "servers": { "azure-dataops": { "url": "http://localhost:8080/mcp" } } }
```

VS Code is already pre-authorized on the server app, so signing in won't show a
consent screen. After sign-in, the tools you see depend on your group membership
from step 2.

---

## Runtime flow (what happens per request)

```
VS Code ──Entra OAuth (PKCE)──► gets token for api://<mcp>/user_impersonation
   │ calls tool with that token
   ▼
mcp-server : verify JWT → OBO-exchange to Graph → read user's groups
           : in mcp-diagnose-users? in mcp-action-admins? → allow/deny tool
           │ if allowed, forward command to the right worker
           ▼
diagnose-worker / action-worker : run `az` as their own SP (RBAC from step 3)
```

Two identities, two purposes: the **user identity** drives authorization/audit/
tool visibility; the **worker SP** is the fixed Azure execution boundary.

---

## Tear down

No destroy script — in the Entra portal delete the three app registrations and
two groups. The OBO oauth2 grant disappears automatically when the MCP server SP
is deleted. Remove the worker RBAC with `az role assignment delete` (or it
cascades when the worker SPs are deleted).
