const PER_PAGE = 25;

// Admin user list, ?p=1 is the first page.
export function listUsers(users, p = 1) {
  const sorted = [...users].sort((a, b) => a.name.localeCompare(b.name));
  return sorted.slice(p * PER_PAGE, (p + 1) * PER_PAGE);
}
