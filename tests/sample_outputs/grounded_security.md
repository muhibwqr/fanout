# Security Audit: src/auth/tokens.py + src/auth/middleware.py

## Critical

**Deterministic token generation** — `src/auth/tokens.py:6` `issue_token(user_id)`
- Uses `hashlib.sha256(f"{user_id}".encode()).hexdigest()`. Same user_id always returns same token. Zero entropy.
- Exploit: enumerate user_ids, precompute sha256 offline, full account takeover.
- Fix: `secrets.token_urlsafe(32)`.

**Revoke is a no-op** — `src/auth/tokens.py:10` `revoke_token(token)`
- Returns `True` unconditionally. Token is never invalidated.
- Exploit: stolen tokens are valid forever; logout/breach response ineffective.
- Fix: persist revocation in TokenStore with TTL-aware check at verify time.

## High

**Header parse case-sensitive** — `src/auth/middleware.py:5` `authenticate(request)`
- Uses literal `"Authorization"`. HTTP headers are case-insensitive but this matches only one case.
- Exploit: `authorization: Bearer ...` (lowercase) bypasses the auth path entirely.
- Fix: case-insensitive lookup or normalize headers upstream.

**Middleware does not call authenticate** — `src/auth/middleware.py:18` `AuthMiddleware.__call__`
- Forwards directly to `self.app(scope, receive, send)`. Class is named auth but does no auth.
- Fix: invoke `authenticate()` on the request, attach user to scope, reject 401 on None.
