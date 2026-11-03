export const outbox = [];

async function sendEmail(userId, subject) {
  outbox.push({ userId, subject });
}

function logError(err) {
  console.error("[notify] email failed:", err.message);
}

export function notifyShipped(order) {
  // Fire-and-forget on purpose: a slow or failed email must never block shipping.
  // Failures are logged by logError.
  sendEmail(order.userId, "Your order shipped").catch(logError);
}
