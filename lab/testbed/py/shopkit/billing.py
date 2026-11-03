from shopkit.users import get_user


def invoice_email(user_id):
    return get_user(user_id)["email"]
