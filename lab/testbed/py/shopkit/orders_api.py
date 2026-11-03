from dateutils import parse_iso

from shopkit.date_utils import format_day


def order_date(order):
    """Parse the order's ISO-8601 timestamp (e.g. '2026-03-01T10:00:00Z') into an aware datetime."""
    return parse_iso(order["created_at"])


def order_day(order):
    return format_day(order_date(order))


if __name__ == "__main__":
    print(order_day({"created_at": "2026-03-01T10:00:00Z"}))
