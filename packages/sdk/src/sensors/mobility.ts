import { clamp, haversineMetres, round, shannonEntropy } from '../internal/statistics';
import type {
  FeatureSample,
  LocationAdapter,
  Logger,
  PositionFix,
  StorageAdapter,
} from '../types';

/**
 * Mobility sensing without storing coordinates.
 *
 * Life space contraction is one of the earliest and most consistent
 * behavioral markers of depressive relapse. Measuring it does not require
 * knowing where the patient is, only how far and how variably they move. This
 * module therefore takes a handful of coarse fixes per window, reduces them
 * to a radius and a distribution, and drops the coordinates before returning.
 *
 * Places that need to persist across windows are stored as salted grid cell
 * hashes. Nothing in device storage or in the upload payload can be turned
 * back into a street address by reading it. To be exact about the guarantee:
 * an attacker who has both the device salt and the hash table could enumerate
 * a grid and recover approximate cells, so this is data minimisation rather
 * than a cryptographic boundary. The boundary is the OS application sandbox.
 * The point is that the platform operator never receives a coordinate at all.
 */

/** Fixes worse than this are noise for a life space measure. */
const MAX_ACCEPTABLE_ACCURACY_M = 250;

/** Grid resolution used for place identity, roughly 110 metres of latitude. */
const CELL_DEGREES = 0.001;

/** Places tracked at once. A patient with a stable routine uses far fewer. */
const MAX_TRACKED_PLACES = 32;

/** Visit weight halves over this many days, so old routines fade out. */
const PLACE_HALF_LIFE_DAYS = 21;

const PLACE_REGISTRY_KEY = 'traycer.mobility.places.v1';
const PLACE_SALT_KEY = 'traycer.mobility.salt.v1';

export interface MobilityOptions {
  readonly fixesPerWindow: number;
  /** Milliseconds between fixes inside one window. */
  readonly fixSpacingMs: number;
}

export interface MobilityResult {
  readonly samples: readonly FeatureSample[];
  readonly skipped: boolean;
  readonly skipReason?: string;
}

interface PlaceRecord {
  hash: string;
  weight: number;
  lastSeen: number;
}

const EMPTY_MOBILITY_FEATURES: readonly FeatureSample[] = [
  { feature: 'mobility_radius_m', value: 0, confidence: 0 },
  { feature: 'location_entropy', value: 0, confidence: 0 },
  { feature: 'place_count', value: 0, confidence: 0 },
  { feature: 'primary_place_ratio', value: 0, confidence: 0 },
];

export class MobilitySensor {
  private readonly adapter: LocationAdapter;
  private readonly storage: StorageAdapter;
  private readonly logger: Logger;
  private salt: string | null = null;

  constructor(adapter: LocationAdapter, storage: StorageAdapter, logger: Logger) {
    this.adapter = adapter;
    this.storage = storage;
    this.logger = logger;
  }

  async collect(options: MobilityOptions, now: number): Promise<MobilityResult> {
    const fixes = await this.gatherFixes(options);
    const confidence = clamp(fixes.length / Math.max(1, options.fixesPerWindow), 0, 1);

    if (fixes.length === 0) {
      return { samples: EMPTY_MOBILITY_FEATURES, skipped: true, skipReason: 'no_fixes' };
    }

    const salt = await this.getSalt();
    const cellHashes = fixes.map((fix) => hashCell(fix.latitude, fix.longitude, salt));

    const registry = await this.loadRegistry();
    const updated = updateRegistry(registry, cellHashes, now);
    await this.saveRegistry(updated);

    const samples = reduceMobility(fixes, cellHashes, updated, confidence);

    // Coordinates go out of scope here. Nothing downstream of this line has
    // access to the fix array.
    return { samples, skipped: false };
  }

  private async gatherFixes(options: MobilityOptions): Promise<PositionFix[]> {
    const fixes: PositionFix[] = [];

    for (let i = 0; i < options.fixesPerWindow; i += 1) {
      try {
        const fix = await this.adapter.getCurrentPosition();
        if (fix && fix.accuracyM <= MAX_ACCEPTABLE_ACCURACY_M) {
          fixes.push(fix);
        }
      } catch (error) {
        this.logger.debug('location fix failed', {
          reason: error instanceof Error ? error.name : 'unknown',
        });
      }

      if (i < options.fixesPerWindow - 1 && options.fixSpacingMs > 0) {
        await delay(options.fixSpacingMs);
      }
    }

    return fixes;
  }

  private async getSalt(): Promise<string> {
    if (this.salt) return this.salt;

    const stored = await this.storage.getItem(PLACE_SALT_KEY);
    if (stored) {
      this.salt = stored;
      return stored;
    }

    // The salt is device local and never uploaded. Regenerating it resets
    // place identity, which shows up as a one window entropy discontinuity.
    const fresh = createSalt();
    await this.storage.setItem(PLACE_SALT_KEY, fresh);
    this.salt = fresh;
    return fresh;
  }

  private async loadRegistry(): Promise<PlaceRecord[]> {
    const raw = await this.storage.getItem(PLACE_REGISTRY_KEY);
    if (!raw) return [];
    try {
      const parsed = JSON.parse(raw) as PlaceRecord[];
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      this.logger.warn('place registry unreadable, starting a new one');
      return [];
    }
  }

  private async saveRegistry(records: PlaceRecord[]): Promise<void> {
    await this.storage.setItem(PLACE_REGISTRY_KEY, JSON.stringify(records));
  }
}

/** Radius of gyration and place distribution for one window. */
export function reduceMobility(
  fixes: readonly PositionFix[],
  cellHashes: readonly string[],
  registry: readonly PlaceRecord[],
  confidence: number,
): readonly FeatureSample[] {
  const radius = radiusOfGyration(fixes);

  const counts = new Map<string, number>();
  for (const hash of cellHashes) {
    counts.set(hash, (counts.get(hash) ?? 0) + 1);
  }

  const entropy = shannonEntropy([...counts.values()]);
  const placeCount = counts.size;

  // Primary place is the most weighted place across the patient's history,
  // not the most visited place in this window. A patient who stops leaving
  // home reads as a rising ratio, which is the signal we want. Using the
  // within window mode would report 1.0 for any window spent anywhere.
  const primaryHash = mostWeightedPlace(registry);
  const atPrimary = primaryHash ? (counts.get(primaryHash) ?? 0) : 0;
  const primaryRatio = cellHashes.length > 0 ? atPrimary / cellHashes.length : 0;

  return [
    { feature: 'mobility_radius_m', value: round(radius, 1), confidence },
    { feature: 'location_entropy', value: round(entropy, 4), confidence },
    { feature: 'place_count', value: placeCount, confidence },
    { feature: 'primary_place_ratio', value: round(primaryRatio, 4), confidence },
  ];
}

/**
 * Radius of gyration: root mean square distance of every fix from their
 * centroid. Preferred over maximum displacement because one commute does not
 * dominate it, and preferred over total distance because it does not require
 * a continuous track.
 */
export function radiusOfGyration(fixes: readonly PositionFix[]): number {
  if (fixes.length < 2) return 0;

  let latSum = 0;
  let lonSum = 0;
  for (const fix of fixes) {
    latSum += fix.latitude;
    lonSum += fix.longitude;
  }
  const centroidLat = latSum / fixes.length;
  const centroidLon = lonSum / fixes.length;

  let squaredSum = 0;
  for (const fix of fixes) {
    const d = haversineMetres(centroidLat, centroidLon, fix.latitude, fix.longitude);
    squaredSum += d * d;
  }

  return Math.sqrt(squaredSum / fixes.length);
}

export function updateRegistry(
  existing: readonly PlaceRecord[],
  cellHashes: readonly string[],
  now: number,
): PlaceRecord[] {
  const halfLifeMs = PLACE_HALF_LIFE_DAYS * 24 * 60 * 60 * 1000;
  const byHash = new Map<string, PlaceRecord>();

  for (const record of existing) {
    const ageMs = Math.max(0, now - record.lastSeen);
    const decayed = record.weight * 0.5 ** (ageMs / halfLifeMs);
    if (decayed >= 0.01) {
      byHash.set(record.hash, { hash: record.hash, weight: decayed, lastSeen: record.lastSeen });
    }
  }

  for (const hash of cellHashes) {
    const current = byHash.get(hash);
    if (current) {
      current.weight += 1;
      current.lastSeen = now;
    } else {
      byHash.set(hash, { hash, weight: 1, lastSeen: now });
    }
  }

  return [...byHash.values()]
    .sort((a, b) => b.weight - a.weight)
    .slice(0, MAX_TRACKED_PLACES);
}

function mostWeightedPlace(registry: readonly PlaceRecord[]): string | null {
  let best: PlaceRecord | null = null;
  for (const record of registry) {
    if (!best || record.weight > best.weight) best = record;
  }
  return best ? best.hash : null;
}

/**
 * Salted grid cell identity.
 *
 * FNV-1a over the salted cell string. Not a cryptographic commitment, and the
 * comment at the top of this file says so plainly. What it does buy is that a
 * dump of application storage contains no readable coordinate.
 */
export function hashCell(latitude: number, longitude: number, salt: string): string {
  const latCell = Math.floor(latitude / CELL_DEGREES);
  const lonCell = Math.floor(longitude / CELL_DEGREES);
  const input = `${salt}:${latCell}:${lonCell}`;

  let h1 = 0x811c9dc5;
  let h2 = 0x01000193;

  for (let i = 0; i < input.length; i += 1) {
    const code = input.charCodeAt(i);
    h1 ^= code;
    h1 = Math.imul(h1, 0x01000193) >>> 0;
    h2 ^= code + i;
    h2 = Math.imul(h2, 0x85ebca6b) >>> 0;
  }

  return `${h1.toString(16).padStart(8, '0')}${h2.toString(16).padStart(8, '0')}`;
}

function createSalt(): string {
  const webCrypto = (globalThis as { crypto?: Crypto }).crypto;
  if (webCrypto && typeof webCrypto.getRandomValues === 'function') {
    const bytes = new Uint8Array(16);
    webCrypto.getRandomValues(bytes);
    let out = '';
    for (let i = 0; i < bytes.length; i += 1) {
      out += (bytes[i] as number).toString(16).padStart(2, '0');
    }
    return out;
  }

  // Fallback used only when the host app has no crypto polyfill. The salt is
  // never uploaded and never authenticates anything, so a weaker source here
  // degrades storage hygiene rather than transport security.
  let fallback = '';
  for (let i = 0; i < 8; i += 1) {
    fallback += Math.floor(Math.random() * 0xffff)
      .toString(16)
      .padStart(4, '0');
  }
  return fallback;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
