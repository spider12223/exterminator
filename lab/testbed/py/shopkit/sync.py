from shopkit.errors import SyncError


def push_orders(orders, client):
    """Upload orders to the warehouse system. Returns how many were sent."""
    sent = 0
    for order in orders:
        try:
            client.upload(order)
            sent += 1
        except Exception:
            pass
    return sent
