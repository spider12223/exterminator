from shopkit.users import get_user

BANNED = set()


def ban(user_id):
    user = get_user(user_id)
    if user is None:
        return False
    BANNED.add(user["id"])
    return True
