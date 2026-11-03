export function chargeUrl(orderId) {
  const base = process.env.API_URL;
  return new URL(`/charge/${orderId}`, base).toString();
}
