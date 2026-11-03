// Central config. API_URL was renamed to API_BASE_URL in v2.
export function apiBaseUrl() {
  return process.env.API_BASE_URL ?? "http://localhost:4000";
}
