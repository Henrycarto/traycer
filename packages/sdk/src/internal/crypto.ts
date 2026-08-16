import { bytesToBase64, base64ToBytes, toArrayBuffer, utf8ToBytes } from './encoding';
import type { CryptoAdapter, EncryptedEnvelope, ServerPublicKey } from '../types';

/**
 * Hybrid encryption for the upload envelope.
 *
 * Every batch gets a fresh AES-256-GCM content key. That key is wrapped with
 * the clinic's RSA public key and travels inside the envelope. The device
 * therefore holds no long lived decryption secret, and a stolen phone yields
 * nothing readable from the queue on disk.
 *
 * RSA-OAEP rather than ECDH because the ingest side already manages an RSA
 * key pair in KMS for the FHIR client assertion, and one key custody process
 * is easier to audit than two.
 */

const AES_KEY_BITS = 256;
const GCM_IV_BYTES = 12;
const MIN_RSA_MODULUS_BYTES = 256; // 2048 bit

export class CryptoUnavailableError extends Error {
  constructor() {
    super(
      'WebCrypto subtle is not available. Install react-native-quick-crypto and ' +
        'install its global polyfill before calling Traycer.start, or pass your own ' +
        'adapters.crypto implementation.',
    );
    this.name = 'CryptoUnavailableError';
  }
}

export class WeakServerKeyError extends Error {
  constructor(bytes: number) {
    super(
      `Server public key modulus is ${bytes * 8} bits. Traycer requires 2048 bits or more.`,
    );
    this.name = 'WeakServerKeyError';
  }
}

function resolveCrypto(): Crypto {
  const candidate = (globalThis as { crypto?: Crypto }).crypto;
  if (!candidate || !candidate.subtle || typeof candidate.getRandomValues !== 'function') {
    throw new CryptoUnavailableError();
  }
  return candidate;
}

function assertKeyStrength(jwk: JsonWebKey): void {
  const modulus = jwk.n;
  if (typeof modulus !== 'string') {
    throw new Error('Server public key JWK is missing the modulus parameter "n".');
  }
  // Base64url string length maps to byte length closely enough to reject a
  // 1024 bit key without pulling in a full ASN.1 parser.
  const bytes = Math.floor((modulus.length * 3) / 4);
  if (bytes < MIN_RSA_MODULUS_BYTES) {
    throw new WeakServerKeyError(bytes);
  }
}

export function createWebCryptoAdapter(serverKey: ServerPublicKey): CryptoAdapter {
  assertKeyStrength(serverKey.jwk);

  let importedKey: CryptoKey | null = null;

  const getWrappingKey = async (subtle: SubtleCrypto): Promise<CryptoKey> => {
    if (importedKey) return importedKey;
    importedKey = await subtle.importKey(
      'jwk',
      serverKey.jwk,
      { name: 'RSA-OAEP', hash: 'SHA-256' },
      false,
      ['encrypt'],
    );
    return importedKey;
  };

  return {
    async randomBytes(length: number): Promise<Uint8Array> {
      const out = new Uint8Array(length);
      resolveCrypto().getRandomValues(out);
      return out;
    },

    async encryptBatch(plaintext: string, batchId: string): Promise<EncryptedEnvelope> {
      const webCrypto = resolveCrypto();
      const subtle = webCrypto.subtle;

      const contentKey = await subtle.generateKey(
        { name: 'AES-GCM', length: AES_KEY_BITS },
        true,
        ['encrypt'],
      );

      const iv = new Uint8Array(GCM_IV_BYTES);
      webCrypto.getRandomValues(iv);

      // The batch id is authenticated but not encrypted. The server needs it
      // to deduplicate a retried upload before it can decrypt anything.
      const additionalData = toArrayBuffer(utf8ToBytes(batchId));

      const ciphertext = await subtle.encrypt(
        { name: 'AES-GCM', iv, additionalData, tagLength: 128 },
        contentKey,
        toArrayBuffer(utf8ToBytes(plaintext)),
      );

      const rawContentKey = await subtle.exportKey('raw', contentKey);
      const wrappingKey = await getWrappingKey(subtle);
      const wrappedKey = await subtle.encrypt({ name: 'RSA-OAEP' }, wrappingKey, rawContentKey);

      return {
        v: 1,
        batchId,
        alg: 'A256GCM',
        keyAlg: 'RSA-OAEP-256',
        wrappedKey: bytesToBase64(new Uint8Array(wrappedKey)),
        iv: bytesToBase64(iv),
        ciphertext: bytesToBase64(new Uint8Array(ciphertext)),
        keyId: serverKey.keyId,
      };
    },
  };
}

/** Exposed for host apps that persist a server key as a base64 JWK blob. */
export function decodeJwkFromBase64(encoded: string): JsonWebKey {
  const bytes = base64ToBytes(encoded);
  let text = '';
  for (let i = 0; i < bytes.length; i += 1) text += String.fromCharCode(bytes[i] as number);
  return JSON.parse(text) as JsonWebKey;
}
