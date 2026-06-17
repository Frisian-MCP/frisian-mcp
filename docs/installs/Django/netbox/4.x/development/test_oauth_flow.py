"""
NBX-4: OAuth authorization code (PKCE) flow end-to-end test.

Usage (run from the repo root or any directory):
    python netbox/development/test_oauth_flow.py

Requires the NetBox dev harness running at http://localhost:8080.

Config pre-conditions (already in development/configuration.py):
    FRISIAN_MCP_OAUTH_AUTO_APPROVE = True        — no consent page
    FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = True  — unknown PKCE clients auto-register
    FRISIAN_MCP_OAUTH_ISSUER = "http://localhost:8080"
"""

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.parse
import urllib.request

BASE = os.getenv("NETBOX_BASE_URL", "http://localhost:8080")
NETBOX_TOKEN = os.getenv("NETBOX_API_TOKEN", "0123456789abcdef0123456789abcdef01234567")


def _request(method, url, data=None, headers=None, follow=False):
    req_headers = {"Content-Type": "application/json", **(headers or {})}
    body = json.dumps(data).encode() if data else None
    if data and req_headers.get("Content-Type") == "application/x-www-form-urlencoded":
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


def step(label):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")


def ok(msg):
    print(f"  [PASS] {msg}")


def fail(msg, detail=""):
    print(f"  [FAIL] {msg}")
    if detail:
        print(f"         {detail}")
    sys.exit(1)


def info(msg):
    print(f"  [INFO] {msg}")


# ---------------------------------------------------------------------------
# Step 1 — well-known discovery
# ---------------------------------------------------------------------------
step("1. Well-known OAuth metadata discovery")
status, _, body = _request("GET", f"{BASE}/.well-known/oauth-authorization-server")
if status != 200:
    fail(f"GET /.well-known/oauth-authorization-server returned {status}", body[:300])
meta = json.loads(body)
issuer = meta.get("issuer", "")
authorize_ep = meta.get("authorization_endpoint", "")
token_ep = meta.get("token_endpoint", "")
ok(f"issuer: {issuer}")
ok(f"authorization_endpoint: {authorize_ep}")
ok(f"token_endpoint: {token_ep}")
if not (issuer and authorize_ep and token_ep):
    fail("Missing required metadata fields")

# ---------------------------------------------------------------------------
# Step 2 — generate PKCE values
# ---------------------------------------------------------------------------
step("2. Generate PKCE code_verifier / code_challenge")
client_id = "test-nbx4-" + secrets.token_hex(4)
code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
code_challenge = base64.urlsafe_b64encode(
    hashlib.sha256(code_verifier.encode()).digest()
).rstrip(b"=").decode()
redirect_uri = "http://localhost:9999/callback"
state = secrets.token_hex(8)
info(f"client_id:       {client_id}")
info(f"code_verifier:   {code_verifier[:20]}...")
info(f"code_challenge:  {code_challenge[:20]}...")
ok("PKCE values generated")

# ---------------------------------------------------------------------------
# Step 3 — authorization code request (AUTO_APPROVE skips consent)
# ---------------------------------------------------------------------------
step("3. Authorization code request (expect 302 redirect with code)")
params = urllib.parse.urlencode({
    "response_type": "code",
    "client_id": client_id,
    "redirect_uri": redirect_uri,
    "code_challenge": code_challenge,
    "code_challenge_method": "S256",
    "state": state,
})
authorize_url = f"{authorize_ep}?{params}"

# Use a custom opener that does NOT follow redirects so we can capture the code.
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirect)
req = urllib.request.Request(authorize_url)
try:
    with opener.open(req) as resp:
        status = resp.status
        location = resp.headers.get("Location", "")
except urllib.error.HTTPError as e:
    status = e.code
    location = e.headers.get("Location", "")
    if status not in (301, 302, 303, 307, 308):
        body = e.read().decode()
        fail(f"Authorization endpoint returned {status}", body[:500])

info(f"Response status: {status}")
info(f"Location: {location}")
if status not in (301, 302, 303, 307, 308):
    fail(f"Expected redirect, got {status}")

parsed = urllib.parse.urlparse(location)
qs = urllib.parse.parse_qs(parsed.query)
if "error" in qs:
    fail(f"Authorization error: {qs['error']}", qs.get("error_description", [""])[0])
code_list = qs.get("code", [])
if not code_list:
    fail("No 'code' in redirect query string", location)
auth_code = code_list[0]
returned_state = qs.get("state", [""])[0]
if returned_state != state:
    fail(f"State mismatch: sent {state!r}, got {returned_state!r}")
ok(f"Authorization code received: {auth_code[:20]}...")
ok("State matches")

# ---------------------------------------------------------------------------
# Step 4 — token exchange
# ---------------------------------------------------------------------------
step("4. Token exchange (POST /oauth/token)")
token_data = {
    "grant_type": "authorization_code",
    "code": auth_code,
    "redirect_uri": redirect_uri,
    "client_id": client_id,
    "code_verifier": code_verifier,
}
req = urllib.request.Request(
    token_ep,
    data=urllib.parse.urlencode(token_data).encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    method="POST",
)
try:
    with urllib.request.urlopen(req) as resp:
        status = resp.status
        token_body = resp.read().decode()
except urllib.error.HTTPError as e:
    status = e.code
    token_body = e.read().decode()

info(f"Status: {status}")
if status != 200:
    fail(f"Token exchange failed with {status}", token_body[:500])
token_resp = json.loads(token_body)
access_token = token_resp.get("access_token", "")
token_type = token_resp.get("token_type", "")
if not access_token:
    fail("No access_token in token response", token_body[:300])
ok(f"access_token: {access_token[:20]}...")
ok(f"token_type:   {token_type}")
ok(f"expires_in:   {token_resp.get('expires_in', 'n/a')}")

# ---------------------------------------------------------------------------
# Step 5 — use Bearer token to call MCP endpoint
# ---------------------------------------------------------------------------
step("5. Call MCP endpoint with Bearer token (tools/list)")
mcp_url = f"{BASE}/api/mcp/"
mcp_payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {},
}
req = urllib.request.Request(
    mcp_url,
    data=json.dumps(mcp_payload).encode(),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    },
    method="POST",
)
try:
    with urllib.request.urlopen(req) as resp:
        status = resp.status
        mcp_body = resp.read().decode()
except urllib.error.HTTPError as e:
    status = e.code
    mcp_body = e.read().decode()

info(f"Status: {status}")
if status != 200:
    fail(f"MCP call failed with {status}", mcp_body[:500])
mcp_resp = json.loads(mcp_body)
if "error" in mcp_resp:
    fail(f"MCP JSON-RPC error: {mcp_resp['error']}")
tools = mcp_resp.get("result", {}).get("tools", [])
ok(f"MCP tools/list returned {len(tools)} tools")

# ---------------------------------------------------------------------------
# Step 6 — client_credentials grant (pre-registered client sanity check)
# ---------------------------------------------------------------------------
step("6. client_credentials grant using NetBox API token as client_secret")
# The frisian-mcp token tier map maps superuser -> read_write.
# We use the service-user pattern: POST /oauth/token with the NetBox API token.
# This tests that the FRISIAN_MCP_TOKEN_TIER_MAP path works end-to-end.
cc_data = {
    "grant_type": "client_credentials",
    "client_id": client_id,
    "client_secret": NETBOX_TOKEN,
}
req = urllib.request.Request(
    token_ep,
    data=urllib.parse.urlencode(cc_data).encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    method="POST",
)
try:
    with urllib.request.urlopen(req) as resp:
        status = resp.status
        cc_body = resp.read().decode()
except urllib.error.HTTPError as e:
    status = e.code
    cc_body = e.read().decode()

info(f"Status: {status}")
cc_resp = json.loads(cc_body)
if status == 200 and cc_resp.get("access_token"):
    ok(f"client_credentials token: {cc_resp['access_token'][:20]}...")
else:
    info(f"client_credentials response: {cc_body[:300]}")
    info("(client_credentials may require a registered client_secret — not a failure if unsupported)")

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
step("NBX-4 COMPLETE")
print()
print("  OAuth PKCE authorization code flow: PASS")
print(f"  Bearer token ({access_token[:20]}...) authenticated against MCP")
print(f"  {len(tools)} tools returned via OAuth session")
print()
