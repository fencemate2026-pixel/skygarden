// Request signing shared by the ingest function.
// Must match pi-agent/detect_agent/uplink.py:
//   signature = hex(HMAC_SHA256(secret, "<unix seconds>.<raw body bytes>"))

const enc = new TextEncoder();

export async function hmacHex(secret: string, message: Uint8Array<ArrayBuffer>): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, message));
  return Array.from(sig, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Constant-time comparison of two hex strings. */
export function timingSafeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export type VerifyResult = { ok: true } | { ok: false; reason: string };

export async function verifySignedBody(
  secret: string,
  timestampHeader: string | null,
  signatureHeader: string | null,
  body: Uint8Array<ArrayBuffer>,
  nowSeconds: number,
  windowSeconds = 300,
): Promise<VerifyResult> {
  if (!timestampHeader || !/^\d{9,11}$/.test(timestampHeader)) {
    return { ok: false, reason: "bad timestamp header" };
  }
  if (!signatureHeader || !/^[0-9a-f]{64}$/.test(signatureHeader)) {
    return { ok: false, reason: "bad signature header" };
  }
  const ts = Number(timestampHeader);
  if (Math.abs(nowSeconds - ts) > windowSeconds) {
    return { ok: false, reason: "timestamp outside window" };
  }
  const prefix = enc.encode(`${ts}.`);
  const msg = new Uint8Array(prefix.length + body.length);
  msg.set(prefix, 0);
  msg.set(body, prefix.length);
  const expected = await hmacHex(secret, msg);
  return timingSafeEqualHex(expected, signatureHeader)
    ? { ok: true }
    : { ok: false, reason: "signature mismatch" };
}
