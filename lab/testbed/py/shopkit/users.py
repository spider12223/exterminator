from shopkit.errors import UserNotFound

_USERS = {
    "u1": {"id": "u1", "name": "ada lovelace", "email": "ada@example.com", "plan": "pro"},
    "u2": {"id": "u2", "name": "alan turing", "email": "alan@example.com", "plan": "free"},
}


def get_user(user_id):
    """Return the user dict, or None if there is no such user (e.g. deleted)."""
    return _USERS.get(user_id)


def get_user_or_raise(user_id):
    user = _USERS.get(user_id)
    if user is None:
        raise UserNotFound(user_id)
    return user
