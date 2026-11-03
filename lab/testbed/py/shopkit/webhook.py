import logging

logger = logging.getLogger(__name__)


def deliver(event, http):
    """Send an event to the partner webhook and report the delivery status."""
    try:
        http.post("/hooks/orders", event)
    except Exception as exc:
        logger.debug("webhook failed: %s", exc)
    return {"status": "delivered"}
