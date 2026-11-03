const CHUNK = 500;

// Split rows into upload chunks. Chunk numbers are internal and 0-based.
export function chunkRows(rows) {
  const out = [];
  for (let chunk = 0; chunk * CHUNK < rows.length; chunk++) {
    out.push(rows.slice(chunk * CHUNK, (chunk + 1) * CHUNK));
  }
  return out;
}
