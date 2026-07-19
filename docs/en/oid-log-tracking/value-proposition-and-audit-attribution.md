# Value Proposition and Audit Attribution — Response to the Feedback "Is This Project Still Valuable in a Read-Only Organization"

> Background: Someone submitted feedback questioning whether this project still has value in a "perfect read-only organization."
> This document records our complete analysis of that feedback, the points we should concede, and the product positioning and audit (oid → log) capabilities that must be completed as a result. This is also the motivation behind the `oid-log-tracking` direction.

---

## 0. Original Feedback (Paraphrased)

User organizations may fall into two scenarios:

1. **Mixed Permissions (Scenario 1)**: Some engineers have Contributor access to certain resources, others have Read-only.
   → Project value is clear: force AI to operate only within a read-only scope.

2. **Perfect Read-Only (Scenario 2)**: All engineers have Read-only only; all write permissions are handled by Service Principals / CI/CD (deployment pipelines).
   → Question: Is this project no longer valuable in this case? Because
   - Users could theoretically run `az` locally using Claude Code;
   - Running locally, Azure logs show the user themselves; using this project, logs show the SP, **losing attribution**.

---

## 1. Conclusion

**This feedback is valid only on a very narrow point, but its entire argument is built on a flawed framework — it understands this product as a "tool to lock AI in a read-only cage."**

If the product's essence were truly just "enforcing read-only," then Scenario 2 would indeed strip it of most of its value. However, the architecture of this project (two tools `diagnose_bash` → Reader SP / `action_bash` → Contributor SP, routing by group, OBO guardrail, sandbox execution) shows it is actually doing something else:

> **Decouple a person's "standing identity" from their "execution identity," and make this boundary policy-driven, auditable, and least-privilege.**

Within this framework, Scenario 2 does not kill the product; instead, it exposes three hidden assumptions in the feedback itself — each of which is untenable.

---

## 2. Three Hidden Assumptions of Scenario 2, Broken Down One by One

### Assumption 1: "All write operations go through CI/CD" — Misses an entire category of operations

CI/CD covers **declarative deployments** (infra-as-code, application releases). But real-world operations include a large category of **interactive day-2 / break-glass operations** that are fundamentally not part of a pipeline:

- Restarting a stuck service/container, temporarily scaling up to handle traffic
- Killing runaway queries during an incident, clearing a backlogged queue, draining a node
- Rerunning a failed job, temporarily toggling a feature flag

These are **write operations**, but they are not "deployments." When an incident happens at 2 AM, no one modifies Terraform and runs a pipeline. How does a "perfect organization" handle these today? Essentially three paths:

- (a) Someone actually holds standing Contributor → The "perfect read-only" premise collapses immediately.
- (b) PIM temporary elevation + local CLI.
- (c) Manually triggering a runbook.

**This project's `action_bash` + Contributor SP + group guardrail + audit is precisely the optimal solution for this path: a governed, just-in-time (JIT), auditable write channel.** The person has zero standing write permissions; all writes go through a policy-narrowed entry point. This is better governed and more auditable than "elevating via PIM and running `az` bare on a local machine."

→ **Scenario 2 does not kill the product; it exposes its own blind spot.**

### Assumption 2: "If a person is read-only, it's safe" — Control plane read-only ≠ Data plane read-only

This is the most technical and impactful point. A person's "read-only" status depends on their assigned role, and **many read-oriented roles include actions like `listKeys` / `listCredentials` — these are reads on the control plane but writes on the data plane**:

| Command | Surface | Actual Consequence |
|---|---|---|
| `az storage account keys list` | "Just reading" | Obtains account key, full read/write on data plane |
| `az cosmosdb keys list` | "Just reading" | Obtains read/write connection string |
| `az acr credential show` | "Just reading" | Obtains registry credentials |
| `az servicebus namespace authorization-rule keys list` | "Just reading" | Obtains a key capable of sending messages |
| `az webapp deployment list-publishing-credentials` | "Just reading" | Obtains publishing credentials |

So a "perfect read-only organization" often has not actually locked down the data plane. This project can assign the Reader SP a **truly minimal role that does not include `listKeys`**, and layer on a command whitelist guardrail — **the SP can have fewer permissions than the person's own "read-only" role.**

→ "AI shouldn't see everything you can see" is a demonstrable selling point here.

### Assumption 3: "Just use Claude Code locally to run `az`" — Ignores the blast radius of AI

"Running locally" = letting an LLM execute commands using **the engineer's full standing identity, on their laptop, with tokens on their disk**. A single prompt injection (reading a malicious issue, a poisoned log) could:

- Use their token to do everything they can do;
- Exfiltrate everything a tenant-level read-only role can access;
- Combined with Assumption 2, directly exfiltrate writable keys.

No sandbox, no command guardrail, no centralized audit, and credentials are persisted to disk.

**This project: Narrow identity + Sandbox (microVM, no laptop access) + Command guardrail + Centralized audit + Credentials never land on disk under ACA path (passwordless FIC).** This value holds **simultaneously in Scenario 1 and Scenario 2**, and is completely orthogonal to whether "the person themselves is read or write" — the feedback completely misses this layer.

---

## 3. Regarding "Logs Show the SP, Losing Attribution" — The Logic Should Be Reversed, But Only If Audit Is Solid

The feedback says: Running locally, Azure logs show the real user; using this project, logs show the SP, losing attribution.
**For write paths, this logic is actually reversed:**

- Person has standing Contributor, runs locally → Logs show the person, **but the cost is they permanently hold broad write permissions** (exactly the risk the organization wants to eliminate).
- Using MCP → Person has **zero standing write permissions**, writes go through a narrow SP + policy entry point, and the application layer records the user→action mapping.

The SP in the native Azure log is **not a bug; it's a signature that "this write was mediated by policy"**; the real user is correlated back via MCP's own audit records. For security-sensitive organizations, "a governed, narrowed entry point + application-layer attribution + minimal standing permissions" is typically more valuable than "pretty native logs."

### ⚠️ But This Rebuttal Only Holds If Application-Layer Audit Is Strong Enough

In the current code, attribution is only a single line in the `src/mcp-server/main.py` middleware:

```python
logger.info("tool call by user_oid=%s tool=%s", oid, context.message.name)
```

It has `who` and `tool`, but **lacks**: the specific command, target resource, execution result, and a correlation ID that can link back to the Azure Activity Log. **If this is not made a first-class citizen, the feedback's point hits home.** This is a capability the product must add, not something that can be argued away — and it is precisely the core problem this directory (`oid-log-tracking`) is meant to solve.

---

## 4. Points to Concede (No Self-Soothing)

- If an organization **truly** meets these conditions: no interactive write needs (even break-glass goes through pipelines) + the person's "read-only" role is also clean on the data plane + no concern about AI/injection blast radius + no need for a centralized "what did AI actually do" audit dashboard — then for **pure read-only** usage, this project does degrade to a relatively thin wrapper. Such organizations exist (small teams, high trust, no compliance requirements). **No product fits every organization, and that is fine.**
- The selling points for pure read-only (sandbox + AI permission narrowing + centralized audit) are weaker and harder to sell than those for the write path. The feedback is correct on this point.

---

## 5. Resulting Strategic Positioning and Action Items

1. **Do not accept the "read-only cage" positioning.** Shift the narrative focus to the write path: **governed JIT write for interactive day-2 operations.** Even a "perfect organization" needs this, and today they awkwardly cobble it together with PIM + local CLI. This is the true foothold in Scenario 2.
2. **Make audit a first-class citizen (the core work item of this directory):**
   `user oid ↔ SP action ↔ specific command ↔ target resource ↔ result ↔ correlation ID linkable to Azure Activity Log`. This directly neutralizes the "attribution" objection.
3. **Focus on AI-specific risks:** prompt injection blast radius, laptop credential exposure, agent permission narrowing. This value applies to both scenarios.
4. **Make the Reader SP strictly less privileged than the person's "read-only" role** (remove `listKeys`-type actions + command whitelist), turning "AI shouldn't see everything you can see" into a demonstrable selling point.

---

## 6. One-Sentence Summary

The feedback identified a weakness in **marketing positioning** (we described ourselves as a read-only tool), but failed to see the **architectural** value (decoupling a person's standing identity from their execution identity, and making this boundary policy-driven and auditable). In the real world, most organizations are Scenario 1, or are "Scenario 2 in name only, but actually have write needs and data plane vulnerabilities." And for this value to truly stand firm, **the auditable correlation chain from oid → specific operation → Azure log** must be completed — that is the reason this direction exists.