"""Auth middleware (fixture)."""


def authenticate(request):
    token = request.headers.get("Authorization", "")
    if not token.startswith("Bearer "):
        return None
    return verify_token(token[len("Bearer "):])


def verify_token(token):
    return {"user_id": 1} if token == "valid" else None


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, scope, receive, send):
        return self.app(scope, receive, send)
