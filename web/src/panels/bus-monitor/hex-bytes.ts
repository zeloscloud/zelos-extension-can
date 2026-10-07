/**
 * `0x…` → bytes; that is how a binary cell reaches the panel (JSON has no binary type, so the query layer
 * hex-encodes it). Anything malformed yields NO bytes rather than a partial decode, so a garbled cell reads
 * as "no frame" instead of plausible-looking wrong bytes.
 */
export function parseFrameHex(hex: string): Uint8Array {
  const body = hex.startsWith("0x") || hex.startsWith("0X") ? hex.slice(2) : hex;
  if (body.length === 0 || body.length % 2 !== 0 || !/^[0-9a-fA-F]+$/.test(body)) return new Uint8Array(0);

  const bytes = new Uint8Array(body.length / 2);
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = Number.parseInt(body.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}
