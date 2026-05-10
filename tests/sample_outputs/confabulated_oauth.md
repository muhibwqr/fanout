# Security Audit: OAuth flow

## Critical

**Missing PKCE on authorization code flow** — `src/auth/oauth.py:42` `authorize_callback()`
- The callback exchanges code for token without PKCE verifier. Public clients are vulnerable to code interception.
- Exploit: malicious app on same device intercepts redirect URI, exchanges code for access token.
- Fix: require code_verifier, validate against stored code_challenge.

**State parameter not validated** — `src/auth/oauth.py:78` `validate_state()`
- Compares against session string with `==`, vulnerable to timing attack and not bound to PKCE.
- Fix: `secrets.compare_digest()` and bind state to nonce.

## High

**Refresh token stored in localStorage** — `src/web/login.js:113` `storeRefresh()`
- XSS exfiltrates refresh tokens.
- Fix: HttpOnly secure cookie with SameSite=strict.

**JWT signing key in env var without rotation** — `config/secrets.yaml:7` `JWT_SIGNING_KEY`
- Single static key used for all tokens. Compromise = forge any token.
- Fix: rotate via KMS, multiple active keys, kid header.
