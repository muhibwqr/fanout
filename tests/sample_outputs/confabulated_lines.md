# Performance Audit: auth layer

## Critical

**Synchronous DB call in hot path** — `src/auth/middleware.py:847` `authenticate(request)`
- Blocks event loop on every request. Throughput collapses past 50 RPS.
- Fix: async DB driver.

**N+1 query on session lookup** — `src/auth/tokens.py:312` `verify_token()`
- Iterates `users` table per request. O(N) per call.
- Fix: indexed lookup, batched fetch.

**Connection pool exhaustion** — `src/auth/tokens.py:589` `TokenStore.__init__`
- No pool reuse. Each `add()` opens a fresh connection.
- Fix: shared `aiopg.Pool` injected at construction.

## High

**Redundant hash computation** — `src/auth/tokens.py:204`
- `hashlib.sha256(...)` recomputed on every `verify_token`. Cache the digest.
