"""DB queries (fixture)."""


def get_user(user_id):
    return {"id": user_id, "name": "fixture"}


def list_users():
    return [{"id": i} for i in range(3)]
