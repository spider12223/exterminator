from dateutils import parse_iso, to_utc


def export_row(order):
    created = to_utc(parse_iso(order["created_at"]))
    return f'{order["id"]},{created.isoformat()}'
