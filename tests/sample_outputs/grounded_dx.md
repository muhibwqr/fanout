# DX Audit: auth layer

## High

**Silent-failure return signal** — `src/auth/middleware.py:7-8` `authenticate(request)`
- Returns `None` on missing/malformed Authorization header. No exception, no log.
- Impact: callers cannot distinguish "no token" from "bad token" from "valid token but unknown user". All three collapse to `None`.
- Fix: raise typed exceptions (`MissingAuthError`, `InvalidTokenError`) or return a result enum.

**Empty test stubs** — `tests/auth/test_middleware.py:4` `test_authenticate_no_header`
- Body is `pass`. Test names suggest coverage but enforce nothing.
- Impact: false confidence; CI passes whether or not auth works.
- Fix: implement the assertions the names imply.

## Medium

**TokenStore lacks lookup/remove** — `src/auth/tokens.py:14` `class TokenStore`
- Only `add()` exists. No `get`, `verify`, `revoke`. Can write tokens, can't read or invalidate.
- Fix: complete the API.
