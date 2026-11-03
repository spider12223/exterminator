_TEAMS = {"t1": {"id": "t1", "owner": "ada lovelace", "members": ["u1", "u2"]}}


def get_team(team_id):
    """Return the team dict, or None if the team was disbanded."""
    return _TEAMS.get(team_id)


def team_owner_name(team_id):
    return get_team(team_id)["owner"].title()
