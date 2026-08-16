/**
 * Batch identifiers.
 *
 * A batch id is a random UUID v4. It is the deduplication key on the ingest
 * side, so it must survive a retry after the app is killed mid upload, which
 * is why it is generated once at enqueue time and persisted with the batch
 * rather than generated at send time.
 */

const HEX = '0123456789abcdef';

export async function createBatchId(
  randomBytes: (length: number) => Promise<Uint8Array>,
): Promise<string> {
  const bytes = await randomBytes(16);
  const buf = new Uint8Array(bytes);

  buf[6] = ((buf[6] as number) & 0x0f) | 0x40;
  buf[8] = ((buf[8] as number) & 0x3f) | 0x80;

  let out = '';
  for (let i = 0; i < 16; i += 1) {
    const byte = buf[i] as number;
    out += HEX[byte >> 4];
    out += HEX[byte & 0x0f];
    if (i === 3 || i === 5 || i === 7 || i === 9) out += '-';
  }
  return out;
}
