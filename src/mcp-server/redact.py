"""redact.py — post-exec Layer-2 hygiene: mask KNOWN-FORMAT secrets in tool output.

This is the final, deliberately-narrow post-exec gate. See
docs/action-gate-guardrail/从输出脱敏到身份边界-认知收敛与Layer2终稿.md.

  - It is HYGIENE, not a security boundary. A caller who controls the bash command
    can always defeat output scrubbing — hide the command from any scope check
    (`svc=keyvault; az "$svc" secret show …`), rename JSON fields
    (`… | jq '{leak:.value}'`), or re-encode the value (`… | base64`). The real
    boundary is IDENTITY least-privilege: `diagnose` has no data-plane (cannot read
    secrets at all), and privileged reads go through `action` behind human approval.
  - Therefore this runs ONLY on the `action` path (main.py gates it) and does ONLY
    Layer 2: match secrets by their VALUE FORMAT. Format matching is the one thing
    that survives scope-evasion and field-rename — it never looks at the command or
    the field name. The old Layer 1/1b (field-name masking) is gone (a rename beats
    it); the old Layer 3 (entropy) is gone (blind to short secrets, false-positive
    on high-entropy identifiers).
  - What it buys: even for an APPROVED action, account-level long-lived credentials
    (storage account key, SAS, connection-string secret, private key, JWT, and
    common cloud tokens) are not spilled into the transcript / logs / agent context.
    Approval authorizes the OPERATION, not dumping the master key.

Coverage is a curated regex rule-pack — base formats plus popular Azure / cross-cloud
patterns, in the spirit of gitleaks / detect-secrets. It only catches formats we
have a pattern for; `| base64` re-encoding and novel formats still pass. That residual
is accepted and covered by the identity boundary, not here.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid coupling module import to the executor/httpx chain
    from executor import ExecResult

logger = logging.getLogger("dataops-mcp.redact")

MASK = "«redacted»"

# Known-format secret detectors. Each entry: (name, compiled regex, group).
#   group == 0 -> mask the whole match.
#   group == 1 -> keep group(1) (a harmless label like "AccountKey=") and mask the
#                 secret that follows.
# Label-preserving rules come first so a broader whole-match rule doesn't eat the
# label they intend to keep.
_KNOWN: "list[tuple[str, re.Pattern, int]]" = [
    # ---- base formats -------------------------------------------------------
    ("bearer",        re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{10,}"), 1),
    ("sas_sig",       re.compile(r"(?i)(\bsig=)[A-Za-z0-9%_/+-]{10,}"), 1),
    ("conn_secret",   re.compile(r"(?i)(\b(?:AccountKey|SharedAccessKey|Password|pwd)=)[^;\s\"']+"), 1),
    # Azure Function / Logic App URL key: the `?code=<key>` query param.
    ("func_url_code", re.compile(r"(?i)([?&]code=)[A-Za-z0-9._~%+/-]{20,}"), 1),
    ("jwt",           re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), 0),
    ("pem_private",   re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL), 0),
    # Azure storage account key: 64-byte base64 (88 chars ending `==`).
    ("storage_key",   re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{86}=="), 0),
    # ---- Azure --------------------------------------------------------------
    # Function / host key: base64url, 40+ chars, `==`-padded. Heuristic (no strong
    # prefix); acceptable because this path is action-only hygiene where over-masking
    # a stray base64url blob is tolerable. Hex ids / git shas lack `==` and are safe.
    ("azure_func_key", re.compile(r"(?<![A-Za-z0-9_/+-])[A-Za-z0-9_-]{40,}==(?![A-Za-z0-9=])"), 0),
    # Entra (Azure AD) client-secret VALUE — the distinctive `…Q~…` shape, ~40 chars.
    ("azure_ad_secret", re.compile(r"\b[A-Za-z0-9~._-]{3}[0-9A-Za-z]Q~[A-Za-z0-9~._-]{31,34}\b"), 0),
    # ---- popular cross-cloud (prefix-anchored, ~0 false positive) -----------
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{16}\b"), 0),
    ("gcp_api_key",    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("github_token",   re.compile(r"\bgh[posur]_[A-Za-z0-9]{36,}\b"), 0),
    ("slack_token",    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), 0),
    ("openai_key",     re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), 0),
]


def _redact_text(text: str) -> "tuple[str, int]":
    hits = 0
    for _name, pat, grp in _KNOWN:
        def _sub(m, grp=grp):
            nonlocal hits
            hits += 1
            return MASK if grp == 0 else m.group(1) + MASK
        text = pat.sub(_sub, text)
    return text, hits


def redact_result(result: "ExecResult") -> "ExecResult":
    """Mask known-format secrets in a tool result (Layer-2 hygiene only).

    Never blocks, never audits. Call this ONLY on the action path (see main.py); the
    diagnose path has no data-plane, so there is nothing to mask.
    """
    def _one(text: str) -> "tuple[str, int]":
        if not text:
            return text, 0
        return _redact_text(text)

    out, ho = _one(result.stdout)
    err, he = _one(result.stderr)
    hits = ho + he
    if hits:
        # count only, never the values — for FP tuning, NOT an audit record.
        logger.debug("redacted %d known-format secret(s)", hits)
        return dataclasses.replace(result, stdout=out, stderr=err)
    return result
