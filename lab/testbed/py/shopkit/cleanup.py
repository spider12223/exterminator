import os


def remove_temp(path):
    """Best-effort removal of a temp export file; it may already be gone."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
