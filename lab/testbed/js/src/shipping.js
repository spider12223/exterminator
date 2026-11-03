export function ratesUrl(zip) {
  const base = process.env.API_URL || "http://localhost:4000";
  return `${base}/shipping/rates?zip=${encodeURIComponent(zip)}`;
}
