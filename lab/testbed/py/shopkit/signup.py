from shopkit.users import get_user_or_raise


def welcome_message(user_id):
    return f"Welcome, {get_user_or_raise(user_id)['name'].title()}!"
