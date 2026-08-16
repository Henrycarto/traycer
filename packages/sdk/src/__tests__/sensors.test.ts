import { describe, expect, it } from 'vitest';
import { reduceMotion } from '../sensors/accelerometer';
import { hashCell, radiusOfGyration, updateRegistry } from '../sensors/mobility';
import { reduceUsage, usageLoopIndex } from '../sensors/phone_usage';
import type { AccelerometerReading, AppSession, ScreenEvent } from '../types';

function makeReadings(pattern: readonly number[], hz: number): AccelerometerReading[] {
  return pattern.map((magnitudeG, index) => ({
    x: magnitudeG + 1,
    y: 0,
    z: 0,
    timestamp: index * (1000 / hz),
  }));
}

function featureValue(samples: readonly { feature: string; value: number }[], name: string): number {
  const found = samples.find((s) => s.feature === name);
  if (!found) throw new Error(`missing feature ${name}`);
  return found.value;
}

describe('reduceMotion', () => {
  it('reports a still device as inactive with a long sedentary bout', () => {
    // 90 seconds of stillness, which is past the 60 second bout threshold.
    const readings = makeReadings(new Array(25 * 90).fill(0.01), 25);
    const samples = reduceMotion(readings, 25, 1);

    expect(featureValue(samples, 'activity_ratio')).toBe(0);
    expect(featureValue(samples, 'longest_sedentary_minutes')).toBeCloseTo(1.5, 2);
  });

  it('reports no sedentary bout for a still period under the threshold', () => {
    const readings = makeReadings(new Array(25 * 20).fill(0.01), 25);
    const samples = reduceMotion(readings, 25, 1);

    expect(featureValue(samples, 'longest_sedentary_minutes')).toBe(0);
  });

  it('separates one continuous walk from the same amount of restless movement', () => {
    const hz = 25;
    const continuous = new Array(500).fill(0).map((_, i) => (i < 250 ? 0.4 : 0.01));

    const restless = new Array(500).fill(0).map((_, i) => (Math.floor(i / 5) % 2 === 0 ? 0.4 : 0.01));

    const continuousSamples = reduceMotion(makeReadings(continuous, hz), hz, 1);
    const restlessSamples = reduceMotion(makeReadings(restless, hz), hz, 1);

    expect(featureValue(continuousSamples, 'activity_ratio')).toBeCloseTo(
      featureValue(restlessSamples, 'activity_ratio'),
      2,
    );
    expect(featureValue(restlessSamples, 'activity_fragmentation')).toBeGreaterThan(
      featureValue(continuousSamples, 'activity_fragmentation') * 10,
    );
  });

  it('normalises readings reported in metres per second squared', () => {
    const inMetres: AccelerometerReading[] = new Array(100).fill(0).map((_, i) => ({
      x: 9.80665,
      y: 0,
      z: 0,
      timestamp: i * 40,
    }));

    const samples = reduceMotion(inMetres, 25, 1);
    expect(featureValue(samples, 'activity_ratio')).toBe(0);
  });
});

describe('radiusOfGyration', () => {
  it('is zero for a device that never moved', () => {
    const fixes = new Array(4).fill(0).map((_, i) => ({
      latitude: 48.2082,
      longitude: 16.3738,
      accuracyM: 30,
      timestamp: i,
    }));

    expect(radiusOfGyration(fixes)).toBe(0);
  });

  it('grows with the spread of the fixes', () => {
    const tight = [
      { latitude: 48.2082, longitude: 16.3738, accuracyM: 30, timestamp: 0 },
      { latitude: 48.2085, longitude: 16.374, accuracyM: 30, timestamp: 1 },
    ];
    const wide = [
      { latitude: 48.2082, longitude: 16.3738, accuracyM: 30, timestamp: 0 },
      { latitude: 48.28, longitude: 16.45, accuracyM: 30, timestamp: 1 },
    ];

    expect(radiusOfGyration(wide)).toBeGreaterThan(radiusOfGyration(tight) * 20);
  });
});

describe('hashCell', () => {
  it('maps nearby coordinates in the same grid cell to one identity', () => {
    const salt = 'abc123';
    expect(hashCell(48.20821, 16.37381, salt)).toBe(hashCell(48.20829, 16.37389, salt));
  });

  it('produces a different identity under a different salt', () => {
    expect(hashCell(48.2082, 16.3738, 'salt-a')).not.toBe(hashCell(48.2082, 16.3738, 'salt-b'));
  });

  it('never returns anything that contains the coordinate', () => {
    const hash = hashCell(48.2082, 16.3738, 'abc123');
    expect(hash).toMatch(/^[0-9a-f]{16}$/);
    expect(hash).not.toContain('48');
  });
});

describe('updateRegistry', () => {
  const day = 24 * 60 * 60 * 1000;

  it('decays a place that has not been visited', () => {
    const start = 1_700_000_000_000;
    const seeded = updateRegistry([], ['home', 'home', 'home', 'home'], start);
    const later = updateRegistry(seeded, ['work'], start + 21 * day);

    const home = later.find((r) => r.hash === 'home');
    expect(home?.weight).toBeCloseTo(2, 1);
  });

  it('keeps only the most weighted places', () => {
    const start = 1_700_000_000_000;
    const many = Array.from({ length: 50 }, (_, i) => `cell-${i}`);
    const registry = updateRegistry([], many, start);
    expect(registry.length).toBeLessThanOrEqual(32);
  });
});

describe('reduceUsage', () => {
  const windowStart = Date.UTC(2026, 0, 15, 12, 0, 0);
  const windowEnd = windowStart + 3_600_000;

  const minute = (m: number): number => windowStart + m * 60_000;

  it('measures one long session as low loop index', () => {
    const events: ScreenEvent[] = [
      { type: 'unlock', timestamp: minute(5) },
      { type: 'screen_off', timestamp: minute(35) },
    ];

    const samples = reduceUsage(events, [], windowStart, windowEnd, 0);
    expect(featureValue(samples, 'screen_on_minutes')).toBeCloseTo(30, 1);
    expect(featureValue(samples, 'unlock_count')).toBe(1);
    expect(featureValue(samples, 'usage_loop_index')).toBeLessThan(0.1);
  });

  it('measures many short checks as a high loop index', () => {
    const events: ScreenEvent[] = [];
    for (let i = 0; i < 30; i += 1) {
      events.push({ type: 'unlock', timestamp: minute(i * 2) });
      events.push({ type: 'screen_off', timestamp: minute(i * 2) + 20_000 });
    }

    const samples = reduceUsage(events, [], windowStart, windowEnd, 0);
    expect(featureValue(samples, 'unlock_count')).toBe(30);
    expect(featureValue(samples, 'usage_loop_index')).toBeGreaterThan(1);
  });

  it('truncates a session that is still open when the window closes', () => {
    const events: ScreenEvent[] = [{ type: 'unlock', timestamp: minute(50) }];
    const samples = reduceUsage(events, [], windowStart, windowEnd, 0);
    expect(featureValue(samples, 'screen_on_minutes')).toBeCloseTo(10, 1);
  });

  it('counts night usage against local time, not UTC', () => {
    // 12:00 UTC is 02:00 local at an offset of minus ten hours.
    const events: ScreenEvent[] = [
      { type: 'unlock', timestamp: minute(0) },
      { type: 'screen_off', timestamp: minute(20) },
    ];

    const atNight = reduceUsage(events, [], windowStart, windowEnd, -600);
    const atNoon = reduceUsage(events, [], windowStart, windowEnd, 0);

    expect(featureValue(atNight, 'night_usage_minutes')).toBeGreaterThan(19);
    expect(featureValue(atNoon, 'night_usage_minutes')).toBe(0);
  });

  it('counts communication sessions without any app identity', () => {
    const sessions: AppSession[] = [
      { category: 'communication', startedAt: minute(1), endedAt: minute(6) },
      { category: 'other', startedAt: minute(10), endedAt: minute(40) },
    ];

    const samples = reduceUsage([], sessions, windowStart, windowEnd, 0);
    expect(featureValue(samples, 'communication_session_count')).toBe(1);
    expect(featureValue(samples, 'communication_minutes')).toBeCloseTo(5, 1);
  });
});

describe('usageLoopIndex', () => {
  it('is zero when the phone was never unlocked', () => {
    expect(usageLoopIndex(0, 600_000, [])).toBe(0);
  });
});
