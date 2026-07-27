# Manual patch: Metabrain OAuth2 (client-credentials) for a v4.3 install

Apply to a copy already inside the network when re-transferring the v4.4
package is not convenient. Two files change: `pdf2db.py` and `webapp.py`.
Each edit is FIND (exact text in v4.3) → REPLACE. Apply in order; the internal
Claude Code can apply this file directly ("apply the edits in
METABRAIN_OAUTH_PATCH.md").

Why: Metabrain issues JWTs via Keycloak client-credentials that expire after
~3600 s with no refresh token. A static `Authorization: Bearer` key therefore
fails mid-batch. This patch teaches the client to fetch tokens itself, cache
them, renew 120 s before expiry, and force-refresh once on an unexpected 401.

---

## File 1 of 2 — `pdf2db.py` (4 edits)

### Edit 1.1 — imports

FIND:
```python
import sqlite3
import sys
import time
import urllib.error
import urllib.request
```
REPLACE:
```python
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
```

### Edit 1.2 — CONFIG keys

FIND:
```python
    "llm_api_key": "",              # discouraged; prefer the env var below
    "llm_api_key_env": "PDF2DB_API_KEY",
```
REPLACE:
```python
    "llm_api_key": "",              # discouraged; prefer the env var below
    "llm_api_key_env": "PDF2DB_API_KEY",
    # OAuth2 client-credentials (e.g. Metabrain via Keycloak SSO). When
    # llm_auth_url is set, a bearer token is fetched from it and renewed
    # automatically before expiry — llm_api_key is then ignored.
    "llm_auth_url": "",             # token endpoint URL; "" = static key auth
    "llm_client_id": "",
    "llm_client_secret": "",        # discouraged; prefer the env var below
    "llm_client_secret_env": "PDF2DB_CLIENT_SECRET",
```

### Edit 1.3 — replace the whole HTTP client

FIND the line:
```python
def call_openai_compat(cfg, messages):
```
Delete from that line down to (and including) the line:
```python
    raise LLMTransportError(last_err)
```
REPLACE the deleted block with all of the following:
```python
def _llm_opener(cfg):
    handlers = [] if cfg["llm_use_env_proxy"] else \
        [urllib.request.ProxyHandler({})]  # ambient proxies must never re-route
    if cfg.get("llm_ca_file"):  # HTTPS gateway signed by an internal/private CA
        ctx = ssl.create_default_context(cafile=cfg["llm_ca_file"])
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


# OAuth2 client-credentials token cache. Tokens from SSO gateways (Metabrain/
# Keycloak) expire — typically after 3600 s with no refresh token — so long
# batch runs must re-fetch. One cache per (endpoint, client), shared across
# the llm worker threads and the web console's request threads.
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {}  # (auth_url, client_id) -> (token, renew_after_epoch)


def _oauth_token(cfg, opener, force=False):
    """Bearer token via the client-credentials grant, renewed 120 s before
    expiry so a token can never lapse mid-request. force=True drops the cached
    token first (used once after an unexpected 401)."""
    cache_key = (cfg["llm_auth_url"], cfg.get("llm_client_id", ""))
    with _TOKEN_LOCK:
        cached = None if force else _TOKEN_CACHE.get(cache_key)
        if cached and time.time() < cached[1]:
            return cached[0]
        secret = os.environ.get(cfg.get("llm_client_secret_env") or "", "") \
            or cfg.get("llm_client_secret", "")
        form = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": cfg.get("llm_client_id", ""),
            "client_secret": secret,
        }).encode("ascii")
        req = urllib.request.Request(
            cfg["llm_auth_url"], data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "pdf2db/1.0"})
        try:
            with opener.open(req, timeout=cfg["llm_timeout_s"]) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:300]
            except OSError:
                detail = b""
            raise LLMTransportError(
                f"token endpoint HTTP {e.code}: {detail!r} — check "
                f"llm_client_id / client secret") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMTransportError(f"token endpoint unreachable: {e}") from e
        token = body.get("access_token")
        if not token:
            raise LLMTransportError("token endpoint returned no access_token "
                                    f"(keys: {sorted(body)[:8]})")
        try:
            ttl = float(body.get("expires_in", 300))
        except (TypeError, ValueError):
            ttl = 300.0
        _TOKEN_CACHE[cache_key] = (token, time.time() + max(30.0, ttl - 120.0))
        return token


def call_openai_compat(cfg, messages):
    url = cfg["llm_base_url"].rstrip("/") + "/chat/completions"
    payload = {"model": cfg["llm_model"], "messages": messages,
               "temperature": cfg["llm_temperature"]}
    if cfg["llm_force_json"]:
        payload["response_format"] = {"type": "json_object"}
    # explicit User-Agent: python-urllib's default gets blocked by bot filters
    # (e.g. Cloudflare error 1010) in front of some OpenAI-compatible providers
    headers = {"Content-Type": "application/json", "User-Agent": "pdf2db/1.0"}
    opener = _llm_opener(cfg)
    use_oauth = bool(cfg.get("llm_auth_url"))
    if use_oauth:
        headers["Authorization"] = "Bearer " + _oauth_token(cfg, opener)
    else:
        key = os.environ.get(cfg["llm_api_key_env"] or "", "") or cfg["llm_api_key"]
        if key:
            headers["Authorization"] = f"Bearer {key}"
    last_err = "no attempt made"
    refreshed_after_401 = False
    for i in range(cfg["llm_transport_retries"]):
        delay = cfg["llm_retry_backoff_s"] * (2 ** i)
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers=headers, method="POST")
            with opener.open(req, timeout=cfg["llm_timeout_s"]) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:500]
            except OSError:
                detail = b""
            last_err = f"HTTP {e.code}: {detail!r}"
            if e.code == 401 and use_oauth and not refreshed_after_401:
                # token may have been revoked or expired early — one fresh
                # token, then continue the normal retry budget
                refreshed_after_401 = True
                headers["Authorization"] = "Bearer " + \
                    _oauth_token(cfg, opener, force=True)
                continue
            if e.code in (400, 401, 403, 404, 422):
                break  # config problem; retrying will not help
            if e.code == 429:  # rate limited — honor the server's Retry-After
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = max(delay, min(float(ra), 60.0))
                except (TypeError, ValueError):
                    pass
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
        except (KeyError, IndexError, TypeError, ValueError) as e:
            last_err = f"unexpected response shape: {type(e).__name__}: {e}"
        time.sleep(delay)
    raise LLMTransportError(last_err)
```

### Edit 1.4 — keep the secret out of run_summary.json (REQUIRED)

FIND:
```python
        "config": {k: v for k, v in cfg.items()
                   if k != "llm_api_key" and not k.startswith("_")},
```
REPLACE:
```python
        "config": {k: v for k, v in cfg.items()
                   if k not in ("llm_api_key", "llm_client_secret")
                   and not k.startswith("_")},
```

### Edit 1.5 (optional, recommended) — fail loudly on half-configured auth

FIND (inside `validate_cfg`, just above its final `return errs`):
```python
    return errs
```
REPLACE:
```python
    if cfg.get("llm_auth_url") and cfg["llm_base_url"] != "mock":
        if not cfg.get("llm_client_id"):
            errs.append("llm_auth_url is set but llm_client_id is empty")
        if not (cfg.get("llm_client_secret")
                or os.environ.get(cfg.get("llm_client_secret_env") or "", "")):
            errs.append("llm_auth_url is set but no client secret is available "
                        "(set llm_client_secret or the PDF2DB_CLIENT_SECRET "
                        "env var)")
    return errs
```
(There are several `return errs` in the file — this is the one at the END of
`def validate_cfg(...)`, after the OCR/tessdata check.)

---

## File 2 of 2 — `webapp.py` (4 edits)

### Edit 2.1 — settings defaults (REQUIRED — unknown keys in settings.json
are otherwise silently dropped by `_load_settings`)

FIND:
```python
DEFAULT_SETTINGS = {"llm_base_url": "mock", "llm_model": "internal-model",
                    "llm_api_key": "",
```
REPLACE:
```python
DEFAULT_SETTINGS = {"llm_base_url": "mock", "llm_model": "internal-model",
                    "llm_api_key": "",
                    "llm_auth_url": "", "llm_client_id": "",
                    "llm_client_secret": "",
```

### Edit 2.2 — the browser must not be able to set the new keys

FIND:
```python
    if any(k in body for k in ("llm_base_url", "llm_model", "llm_api_key")):
```
REPLACE:
```python
    if any(k in body for k in ("llm_base_url", "llm_model", "llm_api_key",
                               "llm_auth_url", "llm_client_id",
                               "llm_client_secret")):
```

### Edit 2.3 — helper that carries the auth settings into a run config

FIND:
```python
def _llm_cfg(settings):
```
INSERT ABOVE that line:
```python
def _oauth_cfg(settings):
    return {"llm_auth_url": (settings.get("llm_auth_url") or "").strip(),
            "llm_client_id": (settings.get("llm_client_id") or "").strip(),
            "llm_client_secret": settings.get("llm_client_secret") or ""}


```

### Edit 2.4 — three one-line insertions (all three matter: new corpus,
retry, and schema drafting each build their own config)

a) In `api_create_job` — FIND:
```python
    cfg.update({
        "root": str(root), "schema": str(schema_path), "out": str(out_dir),
        "llm_base_url": base_url, "llm_model": model, "llm_api_key": key,
```
INSERT ABOVE the `cfg.update({`:
```python
    cfg.update(_oauth_cfg(settings))
```

b) In `api_retry_job` — FIND:
```python
    cfg.update({"root": str(root), "schema": str(schema_path),
                "out": str(out_dir), "llm_base_url": base_url,
```
INSERT ABOVE the `cfg.update({"root"...`:
```python
    cfg.update(_oauth_cfg(_load_settings()))
```

c) In `_draft_schema` — FIND:
```python
    cfg.update({"llm_base_url": settings["llm_base_url"],
                "llm_model": settings["llm_model"],
                "llm_api_key": settings["llm_api_key"],
                "llm_timeout_s": 120, "llm_transport_retries": 2})
```
REPLACE:
```python
    cfg.update({"llm_base_url": settings["llm_base_url"],
                "llm_model": settings["llm_model"],
                "llm_api_key": settings["llm_api_key"],
                "llm_timeout_s": 120, "llm_transport_retries": 2})
    cfg.update(_oauth_cfg(settings))
```

---

## Configure

Edit `webapp_data/settings.json` (owner-only file; re-read on every request,
no restart needed):
```json
{
  "llm_base_url": "https://GATEWAY-HOST/v1",
  "llm_model": "MODELNAME",
  "llm_auth_url": "https://HOST/realms/metabrain-sso/protocol/openid-connect/token",
  "llm_client_id": "YOUR_CLIENT_ID",
  "llm_client_secret": "YOUR_CLIENT_SECRET"
}
```
CLI runs: `--set llm_auth_url=... --set llm_client_id=...` with the secret in
the `PDF2DB_CLIENT_SECRET` environment variable.

## Verify (in this order, each step cheap)

```bash
# 1. no syntax slips:
python -m py_compile pdf2db.py webapp.py

# 2. nothing else broke (offline, no network):
python pdf2db.py --selftest

# 3. the token flow against the real Metabrain endpoint (prints 40 chars of a JWT):
python -c "import pdf2db; cfg=dict(pdf2db.CONFIG); cfg.update({'llm_auth_url':'https://HOST/realms/metabrain-sso/protocol/openid-connect/token','llm_client_id':'YOUR_CLIENT_ID','llm_client_secret':'YOUR_SECRET','llm_timeout_s':30}); print(pdf2db._oauth_token(cfg, pdf2db._llm_opener(cfg))[:40])"

# 4. one real extraction before committing the archive:
python pdf2db.py --root /path/to/archive --schema schemas/failure_reports.json \
    --out out_smoke/ --stages llm,load --limit 1 \
    --llm-base-url https://GATEWAY-HOST/v1 --llm-model MODELNAME \
    --set llm_auth_url=... --set llm_client_id=...
```

Optionally set `APP_VERSION = "4.3+oauth"` near the top of `webapp.py` so the
console badge records that this copy is hand-patched.
