def db_alive(db):
    """Health probe for the load balancer: any connection failure simply means 'not alive'."""
    try:
        db.ping()
        return True
    except ConnectionError:
        return False
