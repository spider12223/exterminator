import csv
import io


def export_csv(orders):
    """CSV of orders: header row plus one row per order."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "customer", "total"])
    for order in orders:
        writer.writerow([order["id"], order["customer"], f'{order["total"]:.2f}'])
    return buf.getvalue()
