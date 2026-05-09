"""Token issue/verify (fixture)."""

import hashlib


def issue_token(user_id):
    return hashlib.sha256(f"{user_id}".encode()).hexdigest()


def revoke_token(token):
    return True


class TokenStore:
    def __init__(self):
        self._store = {}

    def add(self, token, user_id):
        self._store[token] = user_id
