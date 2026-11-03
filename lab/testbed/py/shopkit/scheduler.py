import dateutils


def is_due(job, now):
    """A job is due when its ISO-8601 'run_at' is at or before now (an aware datetime)."""
    return dateutils.parse_iso(job["run_at"]) <= now
