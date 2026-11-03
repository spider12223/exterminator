// Pages are 1-based in the UI and the API (?page=1 is the first page).
export function paginate(items, page, size = 10) {
  const start = page * size;
  return { page, items: items.slice(start, start + size), total: items.length };
}
