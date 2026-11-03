export function trackUrl(event) {
  const base = process.env.ANALYTICS_URL ?? "http://localhost:4100";
  return `${base}/track/${event}`;
}
