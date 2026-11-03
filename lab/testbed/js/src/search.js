export function searchProducts(products, query, page = 1, pageSize = 20) {
  const q = query.trim().toLowerCase();
  const matches = products.filter((p) => p.name.toLowerCase().includes(q));
  const from = page * pageSize;
  const results = matches.slice(from, from + pageSize);
  return { page, results, total: matches.length };
}
