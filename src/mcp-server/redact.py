"""redact.py — deterministic, in-process masking of secrets in tool output.

Runs on EVERY tool result (diagnose AND action) inside `main.py:_exec`, before the
result leaves the MCP server for the client. It is the "post-exec gate", but there
is no verdict and no approval: the command already ran, the secret already exists in
the output, so the only job is to not surface it. Behaviour is always REDACT — mask
the secret, keep the rest — never BLOCK, never audit (masking is the default path).

Layers, precise-first (see docs/action-gate-guardrail/护栏落地方案-输出脱敏与client强制审批.md §2.1):

  1. JSON-aware key masking — walk `az -o json` output, mask values under
     *unambiguous* sensitive key names (clientSecret, connectionString, accountKey,
     primaryKey, ...). Keys off the field NAME, so it catches arbitrary-valued
     passwords. Bare `value` / `key` are deliberately NOT in the set (they appear on
     non-secret objects: tags {"key","value"}, list wrappers {"value":[...]}); the
     secret-bearing `value` (keyvault secret show, storage keys list) is masked only
     under command scope — precision over recall.
  2. Known-format regex — JWT / PEM / bearer / SAS sig / connection-string
     assignments / 88-char storage key. Each match is a precise span, ~0 FP.
  3. Entropy — OFF by default (REDACT_ENTROPY=1). High false-positive; even when on
     it allowlists GUIDs / hashes so identifiers are never masked. The precise layers
     are the workhorses; entropy is a best-effort net only.

False positives are the only real risk of "redact everything", so 1+2 do the work
and 3 is opt-in. See the doc's FP section for the full rationale.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid coupling module import to the executor/httpx chain
    from executor import ExecResult

logger = logging.getLogger("dataops-mcp.redact")

MASK = "«redacted»"

# --- layer 1: unambiguous sensitive JSON keys --------------------------------
# Deliberately excludes bare `key`, `value`, `token`, `secret`: those show up on
# non-secret objects. The secret-bearing `value`/`key` (keyvault secret show,
# storage keys list) is handled by the command-scoped rules below instead.
_SENSITIVE_KEY = re.compile(
    r"(?i)("
    r"passwd|password|pwd|"
    r"client_?secret|"
    r"(?:primary_?|secondary_?)?connection_?string|"
    r"(?:account|access|primary|secondary|shared_?access|primary_?master|secondary_?master)_?key|"
    r"sas_?token|"
    r"(?:access|refresh)_?token"
    r")$"
)

# --- layer 2: known-format detectors -----------------------------------------
# (name, regex, group-to-mask): group 0 masks the whole match; a >0 group masks
# only the secret part and keeps the label (e.g. "sig=" or "AccountKey=").
_KNOWN = [
    ("jwt",         re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), 0),
    ("pem",         re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL), 0),
    ("bearer",      re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{10,}"), 1),
    ("sas_sig",     re.compile(r"(?i)(\bsig=)[A-Za-z0-9%_/+-]{10,}"), 1),
    ("conn_secret", re.compile(r"(?i)(\b(?:AccountKey|SharedAccessKey|Password|pwd)=)[^;\s\"']+"), 1),
    ("storage_key", re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{86}=="), 0),
]

# --- layer 1b: command-scoped `value`/`key` masking --------------------------
# The whole point of these commands is to reveal a secret in a `value` field.
_CMD_VALUE_SCOPES = (
    re.compile(r"(?i)\bkeyvault\s+(secret|key)\s+(show|download)\b"),
    re.compile(
        r"(?i)\b(storage\s+account|cosmosdb|redis|servicebus|eventhubs|relay|"
        r"cognitiveservices|search|batch|maps|appconfig|acr|signalr|webpubsub|iot)\b"
        r".*\b(keys?\s+list|list[- ]keys|list[- ]connection-strings?|credential|connection-string)\b"
    ),
)

# --- layer 3: entropy allowlist (identifiers that look high-entropy) ----------
_GUID = re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX = re.compile(r"(?i)[0-9a-f]{7,64}")  # git sha / image digest / hex ids
_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
_ENTROPY_MIN = float(os.environ.get("REDACT_ENTROPY_MIN", "4.2"))


def _shannon(s: str) -> float:
    n = len(s)
    if n == 0:
        return 0.0
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def _mask_json(obj, mask_ambiguous: bool):
    """Recursively mask values under sensitive keys. Returns (obj, hit_count)."""
    hits = 0
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = k if isinstance(k, str) else ""
            is_secret = bool(_SENSITIVE_KEY.search(ks)) or (
                mask_ambiguous and ks.lower() in ("value", "key")
            )
            # only scalars are secrets; never nuke a nested list/dict (e.g. {"value":[...]})
            if is_secret and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                out[k] = MASK
                hits += 1
            else:
                out[k], n = _mask_json(v, mask_ambiguous)
                hits += n
        return out, hits
    if isinstance(obj, list):
        out = []
        for item in obj:
            mv, n = _mask_json(item, mask_ambiguous)
            out.append(mv)
            hits += n
        return out, hits
    return obj, hits


def _redact_text(text: str, entropy: bool) -> tuple[str, int]:
    hits = 0
    for _name, pat, grp in _KNOWN:
        def _sub(m, grp=grp):
            nonlocal hits
            hits += 1
            return MASK if grp == 0 else m.group(1) + MASK
        text = pat.sub(_sub, text)
    if entropy:
        def _esub(m):
            nonlocal hits
            tok = m.group(0)
            if _GUID.fullmatch(tok) or _HEX.fullmatch(tok):  # identifier allowlist
                return tok
            if _shannon(tok) >= _ENTROPY_MIN:
                hits += 1
                return MASK
            return tok
        text = _TOKEN.sub(_esub, text)
    return text, hits


def redact_result(result: "ExecResult", command: str = "") -> "ExecResult":
    """Mask secrets in a tool result. Always REDACT; never blocks, never audits."""
    entropy = os.environ.get("REDACT_ENTROPY", "0") == "1"
    scoped = any(p.search(command) for p in _CMD_VALUE_SCOPES)

    def _one(text: str) -> tuple[str, int]:
        if not text:
            return text, 0
        total = 0
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if parsed is not None:
            parsed, n = _mask_json(parsed, mask_ambiguous=scoped)
            total += n
            text = json.dumps(parsed, ensure_ascii=False)
        text, n = _redact_text(text, entropy)
        total += n
        return text, total

    out, ho = _one(result.stdout)
    err, he = _one(result.stderr)
    hits = ho + he
    if hits:
        # count only, never the values — for FP tuning, NOT an audit record.
        logger.debug("redacted %d secret(s) (scoped=%s)", hits, scoped)
        return dataclasses.replace(result, stdout=out, stderr=err)
    return result
