from shopkit.users import get_user


def display_name(user_id):
    user = get_user(user_id)
    return user["name"].title()
