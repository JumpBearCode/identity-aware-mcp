#!/usr/bin/env python3
"""
server.py — 一个“仿静态注册 OAuth 客户端”的教学后端，支持两条流程：

  ● mcpproxy 流程：客户端发 resource → 走 /mcpproxy 代理（代理删 resource 再转发 Entra）。
  ● mcp 流程    ：客户端不发 resource → PRM 直接指向 Entra，客户端与 Entra 直连（像 VS Code）。

它同时是三样东西：
  1. 静态文件服务器：把 public/ 下的可视化页面端出去。
  2. 一个真正的 OAuth public client（静态 client_id 49af5fc1 + PKCE + loopback 回调）。
  3. 两条流程各自 14 / 11 步的“抓包器”：把每一次真实 request/response 采集成前端能渲染的结构。

为什么必须跑在 :8080 —— Entra 里给 client 49af5fc1 注册的 redirect 白名单只有
`http://localhost:8080/callback`，所以本进程必须监听 8080，登录回调才能落回来。

只用 Python 标准库，零 pip 依赖：`python3 server.py` 即可。
对应文档：docs/multi-client-implementation/实现说明-方案A-mcpproxy-resource剥离代理-代码与安全分析.md
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── 常量（与项目配置一致）─────────────────────────────────────────────
BASE = os.environ.get(
    "MCP_BASE_URL",
    "https://dataops-aca-mcp.icyrock-96f978c0.westus2.azurecontainerapps.io",
).rstrip("/")
TENANT = "9ea91fbb-1313-4312-a601-b6d9ab7d4de3"
CLIENT_ID = "49af5fc1-96e6-40c1-b108-cb828cc2a00e"
API_APP_ID = "88de6a37-cf75-40d3-83e8-44c5ccbc0895"
API_SCOPE = f"api://{API_APP_ID}/user_impersonation"
REDIRECT_URI = "http://localhost:8080/callback"

# 代理流程端点
PROXY_URL = f"{BASE}/mcpproxy"
PROXY_PRM = f"{BASE}/.well-known/oauth-protected-resource/mcpproxy"
PROXY_ASM = f"{BASE}/.well-known/oauth-authorization-server/mcpproxy"
PROXY_AUTHORIZE = f"{PROXY_URL}/authorize"
PROXY_TOKEN = f"{PROXY_URL}/token"

# 直连流程端点
MCP_URL = f"{BASE}/mcp"
MCP_PRM = f"{BASE}/.well-known/oauth-protected-resource/mcp"

# Entra（直连流程直接打这些）
ENTRA_ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
ENTRA_OPENID = f"{ENTRA_ISSUER}/.well-known/openid-configuration"
ENTRA_AUTHORIZE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
ENTRA_TOKEN = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
PORT = int(os.environ.get("PORT", "8080"))

# ── 单用户教学场景的内存状态 ─────────────────────────────────────────
RUN = {
    "phase": "idle",       # idle | awaiting_login | done | error
    "flow": "mcpproxy",    # mcpproxy | mcp
    "verifier": None,
    "state": None,
    "steps": {},           # n -> {request?, response?}
    "error": None,
}
LOCK = threading.Lock()

INIT_PAYLOAD = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "oauth-mcp-flow-demo", "version": "1.0.0"}},
}


# ── 小工具 ───────────────────────────────────────────────────────────
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_pkce():
    verifier = b64url(secrets.token_bytes(48))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def redact(tok: str, keep: int = 18) -> str:
    if not isinstance(tok, str) or len(tok) <= keep + 6:
        return tok
    return tok[:keep] + "...<redacted>"


def decode_jwt_claims(token: str) -> dict | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        keep = ("aud", "iss", "azp", "appid", "scp", "roles", "oid", "tid", "name", "preferred_username", "exp")
        return {k: claims[k] for k in keep if k in claims}
    except Exception:
        return None


def http_get(url: str, accept="application/json"):
    req = urllib.request.Request(url, headers={"Accept": accept}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http_get_no_redirect(url: str):
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="GET")
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def http_post_form(url: str, form: dict):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def mcp_post(payload: dict, token: str, session_id: str | None, mcp_url: str, want_response=True):
    """向 streamable-HTTP MCP 端点发一条 JSON-RPC，处理 SSE / JSON 两种响应。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    req = urllib.request.Request(mcp_url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        return {"status": e.code, "session_id": None, "json": _read_json_or_sse(e)}
    sid = r.headers.get("mcp-session-id")
    ctype = r.headers.get("content-type", "")
    if not want_response:
        r.read(); r.close()
        return {"status": r.status, "session_id": sid, "json": None}
    body_json = None
    if "text/event-stream" in ctype:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                try:
                    body_json = json.loads(line[5:].strip())
                except Exception:
                    pass
                break
    else:
        try:
            body_json = json.loads(r.read())
        except Exception:
            body_json = None
    status = r.status
    r.close()
    return {"status": status, "session_id": sid, "json": body_json}


def _read_json_or_sse(resp):
    try:
        return json.loads(resp.read())
    except Exception:
        return None


def parse_url_params(url: str):
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    return base, [[k, v] for k, v in query]


def _pretty(body: bytes) -> str:
    try:
        return json.dumps(json.loads(body), indent=2, ensure_ascii=False)
    except Exception:
        return body.decode("utf-8", "replace")


def _trim_openid(body: bytes) -> str:
    """Entra 的 openid-configuration 有几十个字段，教学只留关键几项。"""
    try:
        full = json.loads(body)
    except Exception:
        return body.decode("utf-8", "replace")
    keys = ["issuer", "authorization_endpoint", "token_endpoint", "jwks_uri",
            "response_types_supported", "response_modes_supported", "scopes_supported",
            "subject_types_supported", "id_token_signing_alg_values_supported"]
    trimmed = {k: full[k] for k in keys if k in full}
    trimmed["//registration_endpoint"] = full.get("registration_endpoint", "字段缺失 → 不宣告 DCR")
    return json.dumps(trimmed, indent=2, ensure_ascii=False)


def _redact_token_obj(tok):
    if not isinstance(tok, dict):
        return tok
    out = dict(tok)
    for k in ("access_token", "refresh_token", "id_token"):
        if k in out:
            out[k] = redact(out[k])
    return out


# ── 步骤 1–4：真实发现（两条流程分叉）───────────────────────────────────
def capture_discovery(flow: str) -> dict:
    mcp_url = PROXY_URL if flow == "mcpproxy" else MCP_URL
    steps = {}
    steps["1"] = {"request": {
        "method": "POST", "url": mcp_url,
        "headers": {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        "body": json.dumps(INIT_PAYLOAD, indent=2),
    }}
    # 2：真实 401（GET 也会 401，带同样的 WWW-Authenticate）
    st, hdrs, body = http_get_no_redirect(mcp_url)
    steps["2"] = {"response": {
        "status": st, "statusText": "Unauthorized" if st == 401 else "",
        "headers": {"content-type": hdrs.get("content-type", ""), "www-authenticate": hdrs.get("www-authenticate", "")},
        "body": _pretty(body),
    }}
    # 3：PRM
    prm = PROXY_PRM if flow == "mcpproxy" else MCP_PRM
    st, hdrs, body = http_get(prm)
    steps["3"] = {
        "request": {"method": "GET", "url": prm, "headers": {"Accept": "application/json"}},
        "response": {"status": st, "statusText": "OK", "headers": {"content-type": hdrs.get("content-type", "")}, "body": _pretty(body)},
    }
    # 4：AS metadata —— 代理流程问代理，直连流程问 Entra
    if flow == "mcpproxy":
        st, hdrs, body = http_get(PROXY_ASM)
        steps["4"] = {
            "request": {"method": "GET", "url": PROXY_ASM, "headers": {"Accept": "application/json"}},
            "response": {"status": st, "statusText": "OK", "headers": {"content-type": hdrs.get("content-type", "")}, "body": _pretty(body)},
        }
    else:
        st, hdrs, body = http_get(ENTRA_OPENID)
        steps["4"] = {
            "request": {"method": "GET", "url": ENTRA_OPENID, "headers": {"Accept": "application/json"}},
            "response": {"status": st, "statusText": "OK", "headers": {"content-type": hdrs.get("content-type", "")}, "body": _trim_openid(body)},
        }
    return steps


# ── 步骤 5(–6)：构造授权请求 ──────────────────────────────────────────
def capture_authorize(flow: str) -> dict:
    verifier, challenge = make_pkce()
    state = b64url(secrets.token_bytes(16))
    with LOCK:
        RUN["flow"] = flow
        RUN["verifier"] = verifier
        RUN["state"] = state
        RUN["phase"] = "awaiting_login"

    if flow == "mcpproxy":
        # 走代理 /authorize（带 resource），并抓代理真实的 302（resource 已删）
        query = [
            ("response_type", "code"), ("client_id", CLIENT_ID), ("redirect_uri", REDIRECT_URI),
            ("scope", f"{API_SCOPE} offline_access"), ("code_challenge", challenge),
            ("code_challenge_method", "S256"), ("state", state), ("resource", PROXY_URL),
        ]
        authorize_url = PROXY_AUTHORIZE + "?" + urllib.parse.urlencode(query)
        step5_query = [([k, v, "stripped"] if k == "resource" else [k, v]) for k, v in query]
        st, hdrs, _ = http_get_no_redirect(authorize_url)
        location = hdrs.get("location", "")
        loc_base, loc_query = parse_url_params(location) if location else ("(no Location header)", [])
        loc_keys = {k for k, _ in loc_query}
        marked = [[k, v, "added"] if k == "scope" else [k, v] for k, v in loc_query]
        steps = {
            "5": {"request": {"method": "GET", "urlBase": PROXY_AUTHORIZE, "query": step5_query, "headers": {}}},
            "6": {"response": {"status": st, "statusText": "Found" if st in (301, 302, 303, 307) else "",
                               "headers": {"location": {"urlBase": loc_base, "query": marked}},
                               "_resource_absent": "resource" not in loc_keys}},
        }
        return {"authorizeUrl": authorize_url, "steps": steps}

    # 直连流程：直接打 Entra /authorize（不发 resource），没有代理 302 这一跳
    query = [
        ("response_type", "code"), ("client_id", CLIENT_ID), ("redirect_uri", REDIRECT_URI),
        ("scope", f"{API_SCOPE} offline_access openid profile"), ("code_challenge", challenge),
        ("code_challenge_method", "S256"), ("state", state),
    ]
    authorize_url = ENTRA_AUTHORIZE + "?" + urllib.parse.urlencode(query)
    steps = {"5": {"request": {"method": "GET", "urlBase": ENTRA_AUTHORIZE, "query": [[k, v] for k, v in query], "headers": {}}}}
    return {"authorizeUrl": authorize_url, "steps": steps}


# ── 登录后：换 token + tools/list（两条流程分叉）─────────────────────────
def capture_after_login(code: str, flow: str) -> None:
    with LOCK:
        verifier = RUN["verifier"]
        state = RUN["state"]
    steps = {}

    if flow == "mcpproxy":
        _proxy_after_login(code, verifier, state, steps)
    else:
        _direct_after_login(code, verifier, state, steps)

    with LOCK:
        RUN["steps"].update(steps)
        RUN["phase"] = "done"


def _proxy_after_login(code, verifier, state, steps):
    steps["8"] = {"response": {"status": 302, "statusText": "Found",
        "headers": {"location": {"urlBase": REDIRECT_URI, "query": [["code", redact(code, 20)], ["state", state]]}}}}
    steps["9"] = {"request": {"method": "POST", "url": PROXY_TOKEN,
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "form": [["grant_type", "authorization_code"], ["code", redact(code, 20)], ["code_verifier", redact(verifier, 16)],
                 ["client_id", CLIENT_ID], ["redirect_uri", REDIRECT_URI], ["resource", PROXY_URL, "stripped"]]}}
    # 真实换 token（打代理 /token；代理内部删 resource 转发 Entra）
    st, hdrs, body = http_post_form(PROXY_TOKEN, {
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "resource": PROXY_URL})
    tok, access, claims = _parse_token(body)
    # 10：代理内部转发到 Entra（我们观察不到，示意；resource 已删）
    steps["10"] = {"request": {"method": "POST", "url": ENTRA_TOKEN,
        "headers": {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        "form": [["grant_type", "authorization_code"], ["code", redact(code, 20)], ["code_verifier", redact(verifier, 16)],
                 ["client_id", CLIENT_ID], ["redirect_uri", REDIRECT_URI]]}}
    tokbody = json.dumps(_redact_token_obj(tok), indent=2, ensure_ascii=False)
    steps["11"] = {"response": {"status": st, "statusText": "OK" if st == 200 else "",
        "headers": {"content-type": hdrs.get("content-type", "application/json")}, "body": tokbody,
        "decoded": {"title": "解开真实 access_token 的声明（未校验签名，教学用）", "claims": claims} if claims else None}}
    steps["12"] = {"response": {"status": st, "statusText": "OK" if st == 200 else "",
        "headers": {"content-type": hdrs.get("content-type", "application/json")}, "body": tokbody}}
    _capture_tools(steps, access, PROXY_URL, req_n="13", res_n="14")


def _direct_after_login(code, verifier, state, steps):
    steps["7"] = {"response": {"status": 302, "statusText": "Found",
        "headers": {"location": {"urlBase": REDIRECT_URI, "query": [["code", redact(code, 20)], ["state", state]]}}}}
    steps["8"] = {"request": {"method": "POST", "url": ENTRA_TOKEN,
        "headers": {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        "form": [["grant_type", "authorization_code"], ["code", redact(code, 20)], ["code_verifier", redact(verifier, 16)],
                 ["client_id", CLIENT_ID], ["redirect_uri", REDIRECT_URI]]}}
    # 真实换 token（直接打 Entra /token，无 resource）
    st, hdrs, body = http_post_form(ENTRA_TOKEN, {
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI})
    tok, access, claims = _parse_token(body)
    steps["9"] = {"response": {"status": st, "statusText": "OK" if st == 200 else "",
        "headers": {"content-type": hdrs.get("content-type", "application/json")},
        "body": json.dumps(_redact_token_obj(tok), indent=2, ensure_ascii=False),
        "decoded": {"title": "解开真实 access_token 的声明（未校验签名，教学用）", "claims": claims} if claims else None}}
    _capture_tools(steps, access, MCP_URL, req_n="10", res_n="11")


def _parse_token(body: bytes):
    try:
        tok = json.loads(body)
    except Exception:
        tok = {"raw": body.decode("utf-8", "replace")}
    access = tok.get("access_token", "") if isinstance(tok, dict) else ""
    claims = decode_jwt_claims(access) if access else None
    return tok, access, claims


def _capture_tools(steps, access, mcp_url, req_n, res_n):
    ok, result, sid = _do_mcp_tools_list(access, mcp_url)
    steps[req_n] = {"request": {"method": "POST", "url": mcp_url,
        "headers": {"Authorization": f"Bearer {redact(access, 18)}", "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream", "mcp-session-id": (sid or "(from initialize)")},
        "body": json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, indent=2)}}
    steps[res_n] = {"response": {"status": 200 if ok else 500, "statusText": "OK" if ok else "Error",
        "headers": {"content-type": "application/json", "mcp-session-id": sid or ""},
        "body": json.dumps(result, indent=2, ensure_ascii=False)}}


def _do_mcp_tools_list(access, mcp_url):
    try:
        init = mcp_post(INIT_PAYLOAD, access, None, mcp_url, want_response=True)
        sid = init.get("session_id")
        if sid:
            mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"}, access, sid, mcp_url, want_response=False)
        res = mcp_post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, access, sid, mcp_url, want_response=True)
        return (res.get("status") == 200 and res.get("json") is not None), (res.get("json") or {"error": "no body"}), sid
    except Exception as e:
        return False, {"error": str(e)}, None


# ── HTTP handler ─────────────────────────────────────────────────────
STATIC = {"/": "index.html", "/index.html": "index.html",
          "/app.js": "app.js", "/steps.js": "steps.js", "/styles.css": "styles.css"}
CTYPE = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8"}


def _flow_of(path_qs: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(path_qs).query)
    flow = (q.get("flow") or ["mcpproxy"])[0]
    return flow if flow in ("mcpproxy", "mcp") else "mcpproxy"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静点；且绝不打印可能含 code/token 的 URL
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, html, code=200):
        data = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in STATIC:
            return self._serve_static(STATIC[path])
        if path == "/api/live/authorize":
            try:
                out = capture_authorize(_flow_of(self.path))
                return self._json({"ok": True, **out})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 200)
        if path == "/api/live/status":
            with LOCK:
                return self._json({"phase": RUN["phase"], "steps": RUN["steps"], "error": RUN["error"]})
        if path == "/callback":
            return self._callback()
        return self._html("<h1>404</h1>", 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/live/discover":
            try:
                flow = _flow_of(self.path)
                with LOCK:
                    RUN.update({"phase": "idle", "flow": flow, "verifier": None, "state": None, "steps": {}, "error": None})
                steps = capture_discovery(flow)
                with LOCK:
                    RUN["steps"].update(steps)
                return self._json({"ok": True, "steps": steps})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 200)
        return self._json({"ok": False, "error": "not found"}, 404)

    def _serve_static(self, name):
        fp = os.path.join(PUBLIC_DIR, name)
        if not os.path.isfile(fp):
            return self._html("<h1>missing " + name + "</h1>", 404)
        with open(fp, "rb") as f:
            data = f.read()
        ext = os.path.splitext(name)[1]
        self.send_response(200)
        self.send_header("Content-Type", CTYPE.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _callback(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = (q.get("code") or [None])[0]
        state = (q.get("state") or [None])[0]
        err = (q.get("error") or [None])[0]
        if err:
            with LOCK:
                RUN["phase"] = "error"; RUN["error"] = f"{err}: {(q.get('error_description') or [''])[0]}"
            return self._html(_result_page(False, RUN["error"]))
        if not code or state != RUN.get("state"):
            with LOCK:
                RUN["phase"] = "error"; RUN["error"] = "state 不匹配或缺少 code"
            return self._html(_result_page(False, "state 不匹配或缺少 code（可能是过期的登录）"))
        try:
            capture_after_login(code, RUN.get("flow", "mcpproxy"))
            return self._html(_result_page(True, None))
        except Exception as e:
            with LOCK:
                RUN["phase"] = "error"; RUN["error"] = str(e)
            return self._html(_result_page(False, str(e)))


def _result_page(ok: bool, msg: str | None) -> str:
    color = "#38d39f" if ok else "#ff6b6b"
    title = "✓ 登录成功，已抓取 token + tools/list" if ok else "✕ 出错了"
    body = "回到教学页面就能看到登录后各步骤的真实数据动画。这个窗口可以关掉了。" if ok else (msg or "")
    return f"""<!doctype html><html lang=zh-CN><head><meta charset=utf-8>
<title>登录回调</title><style>
body{{font-family:-apple-system,system-ui,'PingFang SC',sans-serif;background:#0b0f1a;color:#e6ecf7;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.box{{max-width:440px;text-align:center;padding:34px;border:1px solid #263149;border-radius:16px;background:#151c2c}}
h1{{color:{color};font-size:20px;margin:0 0 12px}} p{{color:#8b98b4;line-height:1.6}}</style></head>
<body><div class=box><h1>{title}</h1><p>{body}</p></div>
<script>try{{setTimeout(function(){{window.close()}},2500)}}catch(e){{}}</script></body></html>"""


def main():
    if not os.path.isdir(PUBLIC_DIR):
        raise SystemExit(f"找不到 public/ 目录：{PUBLIC_DIR}")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("┌───────────────────────────────────────────────────────────────┐")
    print("│  MCP · OAuth 授权流可视化教学 App（mcpproxy ↔ mcp 两条流程）   │")
    print("├───────────────────────────────────────────────────────────────┤")
    print(f"│  打开:   http://localhost:{PORT}")
    print(f"│  目标:   {PROXY_URL}  /  {MCP_URL}")
    print(f"│  回调:   {REDIRECT_URI}  (须在 Entra 白名单里)")
    print("│  停止:   Ctrl-C")
    print("└───────────────────────────────────────────────────────────────┘")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye 👋")
        server.shutdown()


if __name__ == "__main__":
    main()
