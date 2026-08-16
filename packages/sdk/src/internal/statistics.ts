/**
 * Numeric helpers used by the sensor reducers.
 *
 * These run on device inside a background task with a short execution budget,
 * so every function here is single pass and allocation light.
 */

export function mean(values: readonly number[]): number {
  if (values.length === 0) return 0;
  let total = 0;
  for (let i = 0; i < values.length; i += 1) total += values[i] as number;
  return total / values.length;
}

export function standardDeviation(values: readonly number[]): number {
  if (values.length < 2) return 0;
  const m = mean(values);
  let sum = 0;
  for (let i = 0; i < values.length; i += 1) {
    const diff = (values[i] as number) - m;
    sum += diff * diff;
  }
  return Math.sqrt(sum / (values.length - 1));
}

export function percentile(values: readonly number[], p: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const rank = (sorted.length - 1) * clamp(p, 0, 1);
  const low = Math.floor(rank);
  const high = Math.ceil(rank);
  if (low === high) return sorted[low] as number;
  const weight = rank - low;
  return (sorted[low] as number) * (1 - weight) + (sorted[high] as number) * weight;
}

export function clamp(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) return min;
  return Math.min(max, Math.max(min, value));
}

/**
 * Shannon entropy over a discrete distribution, in bits.
 *
 * Used for location entropy. A patient who splits time evenly across four
 * places scores high, a patient who spends the whole window in one place
 * scores zero. Withdrawal shows up here before it shows up in radius, because
 * radius stays large as long as one long trip happens.
 */
export function shannonEntropy(counts: readonly number[]): number {
  let total = 0;
  for (let i = 0; i < counts.length; i += 1) total += counts[i] as number;
  if (total <= 0) return 0;

  let entropy = 0;
  for (let i = 0; i < counts.length; i += 1) {
    const p = (counts[i] as number) / total;
    if (p > 0) entropy -= p * Math.log2(p);
  }
  return entropy;
}

/** Round to a fixed number of decimals to keep the wire payload compact. */
export function round(value: number, decimals: number): number {
  if (!Number.isFinite(value)) return 0;
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

/**
 * Great circle distance in metres.
 *
 * Used only inside the mobility reducer to turn a set of fixes into a radius.
 * The fixes are discarded in the same function call.
 */
export function haversineMetres(
  lat1: number,
  lon1: number,
  lat2: number,
  lon2: number,
): number {
  const earthRadiusM = 6371008.8;
  const toRad = Math.PI / 180;
  const dLat = (lat2 - lat1) * toRad;
  const dLon = (lon2 - lon1) * toRad;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLon / 2) ** 2;
  return 2 * earthRadiusM * Math.asin(Math.min(1, Math.sqrt(a)));
}
