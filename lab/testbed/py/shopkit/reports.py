from shopkit.users import get_user


def plan_of(user_id):
    return get_user(user_id).get("plan", "free")


def plan_counts(user_ids):
    counts = {}
    for uid in user_ids:
        plan = plan_of(uid)
        counts[plan] = counts.get(plan, 0) + 1
    return counts
