import { clamp, round } from '../internal/statistics';
import type { AppSession, FeatureSample, Logger, ScreenEvent, UsageAdapter } from '../types';

/**
 * Phone usage and social interaction proxies.
 *
 * Two clinically distinct patterns hide inside screen time, and the reason
 * this module exists is to separate them. A patient who uses the phone for
 * four hours across eight sessions is behaving normally. A patient who uses
 * it for four hours across ninety unlocks is showing the checking loop that
 * precedes both manic escalation and anxious rumination. The usage loop index
 * captures that difference, and it moves earlier than total screen time.
 *
 * Night usage is tracked separately because sleep disruption is the single
 * most reliable prodrome in bipolar disorder.
 *
 * Nothing here reads content. The adapter supplies session categories, never
 * package names, so the payload cannot be turned back into an app inventory.
 */

/** Hours counted as night for the night usage feature, local time. */
const NIGHT_START_HOUR = 0;
const NIGHT_END_HOUR = 6;

/** A gap shorter than this joins two screen on periods into one session. */
const SESSION_MERGE_GAP_MS = 90 * 1000;

/** Sessions shorter than this are treated as a glance, not real use. */
const GLANCE_THRESHOLD_MS = 15 * 1000;

export interface UsageResult {
  readonly samples: readonly FeatureSample[];
  readonly skipped: boolean;
  readonly skipReason?: string;
}

const EMPTY_USAGE_FEATURES: readonly FeatureSample[] = [
  { feature: 'screen_on_minutes', value: 0, confidence: 0 },
  { feature: 'unlock_count', value: 0, confidence: 0 },
  { feature: 'mean_session_minutes', value: 0, confidence: 0 },
  { feature: 'usage_loop_index', value: 0, confidence: 0 },
  { feature: 'night_usage_minutes', value: 0, confidence: 0 },
  { feature: 'communication_session_count', value: 0, confidence: 0 },
  { feature: 'communication_minutes', value: 0, confidence: 0 },
];

export class PhoneUsageSensor {
  private readonly adapter: UsageAdapter;
  private readonly logger: Logger;

  constructor(adapter: UsageAdapter, logger: Logger) {
    this.adapter = adapter;
    this.logger = logger;
  }

  async collect(windowStart: number, windowEnd: number, utcOffsetMinutes: number): Promise<UsageResult> {
    let permitted = false;
    try {
      permitted = await this.adapter.hasPermission();
    } catch (error) {
      this.logger.debug('usage permission check failed', {
        reason: error instanceof Error ? error.name : 'unknown',
      });
    }

    if (!permitted) {
      // Usage access is a separate grant on Android and is unavailable on iOS
      // without Screen Time entitlements. A patient without it still produces
      // motion and mobility features, so the window is emitted with the usage
      // domain at zero confidence rather than dropped.
      return { samples: EMPTY_USAGE_FEATURES, skipped: true, skipReason: 'no_permission' };
    }

    try {
      const [screenEvents, appSessions] = await Promise.all([
        this.adapter.getScreenEvents(windowStart),
        this.adapter.getAppSessions(windowStart),
      ]);

      return {
        samples: reduceUsage(screenEvents, appSessions, windowStart, windowEnd, utcOffsetMinutes),
        skipped: false,
      };
    } catch (error) {
      this.logger.warn('usage collection failed', {
        reason: error instanceof Error ? error.name : 'unknown',
      });
      return { samples: EMPTY_USAGE_FEATURES, skipped: true, skipReason: 'adapter_error' };
    }
  }
}

interface ScreenSession {
  start: number;
  end: number;
}

export function reduceUsage(
  screenEvents: readonly ScreenEvent[],
  appSessions: readonly AppSession[],
  windowStart: number,
  windowEnd: number,
  utcOffsetMinutes: number,
): readonly FeatureSample[] {
  const sessions = buildScreenSessions(screenEvents, windowStart, windowEnd);
  const realSessions = sessions.filter((s) => s.end - s.start >= GLANCE_THRESHOLD_MS);

  let screenOnMs = 0;
  for (const session of sessions) screenOnMs += session.end - session.start;

  const unlockCount = screenEvents.filter((event) => event.type === 'unlock').length;

  let realSessionMs = 0;
  for (const session of realSessions) realSessionMs += session.end - session.start;
  const meanSessionMs = realSessions.length > 0 ? realSessionMs / realSessions.length : 0;

  const nightMs = nightUsageMs(sessions, utcOffsetMinutes);

  let communicationMs = 0;
  let communicationCount = 0;
  for (const session of appSessions) {
    if (session.category !== 'communication') continue;
    const start = Math.max(session.startedAt, windowStart);
    const end = Math.min(session.endedAt, windowEnd);
    if (end <= start) continue;
    communicationMs += end - start;
    communicationCount += 1;
  }

  // A window with no screen events at all is ambiguous: the phone may have
  // been untouched, or the collector may have missed the events. Confidence
  // is full whenever the adapter answered, because the adapter reports
  // absence explicitly by returning an empty array rather than failing.
  const confidence = 1;

  return [
    { feature: 'screen_on_minutes', value: round(screenOnMs / 60000, 3), confidence },
    { feature: 'unlock_count', value: unlockCount, confidence },
    { feature: 'mean_session_minutes', value: round(meanSessionMs / 60000, 3), confidence },
    {
      feature: 'usage_loop_index',
      value: round(usageLoopIndex(unlockCount, screenOnMs, realSessions), 4),
      confidence,
    },
    { feature: 'night_usage_minutes', value: round(nightMs / 60000, 3), confidence },
    { feature: 'communication_session_count', value: communicationCount, confidence },
    { feature: 'communication_minutes', value: round(communicationMs / 60000, 3), confidence },
  ];
}

/**
 * Unlocks per minute of screen time, scaled by how short the sessions are.
 *
 * The index rises when a patient picks the phone up repeatedly and puts it
 * down quickly. It stays flat for a long single session regardless of length,
 * which is why it is not just a rename of unlock count.
 */
export function usageLoopIndex(
  unlockCount: number,
  screenOnMs: number,
  sessions: readonly ScreenSession[],
): number {
  if (unlockCount === 0 || screenOnMs <= 0) return 0;

  const screenOnMinutes = screenOnMs / 60000;
  const unlocksPerMinute = unlockCount / screenOnMinutes;

  const meanSessionMinutes = sessions.length > 0 ? screenOnMinutes / sessions.length : screenOnMinutes;

  // Sessions at or above five minutes carry no brevity penalty. Below that
  // the weight climbs toward 1, so thirty second checks dominate the index.
  const brevityWeight = clamp(1 - meanSessionMinutes / 5, 0, 1);

  return unlocksPerMinute * (0.5 + brevityWeight);
}

function buildScreenSessions(
  events: readonly ScreenEvent[],
  windowStart: number,
  windowEnd: number,
): ScreenSession[] {
  const sorted = [...events].sort((a, b) => a.timestamp - b.timestamp);
  const sessions: ScreenSession[] = [];
  let openedAt: number | null = null;

  for (const event of sorted) {
    if (event.type === 'screen_on' || event.type === 'unlock') {
      if (openedAt === null) openedAt = clampTs(event.timestamp, windowStart, windowEnd);
    } else if (event.type === 'screen_off' && openedAt !== null) {
      const end = clampTs(event.timestamp, windowStart, windowEnd);
      if (end > openedAt) sessions.push({ start: openedAt, end });
      openedAt = null;
    }
  }

  // The screen can still be on when the window closes. Truncating at the
  // boundary keeps the minutes attributable to the right window instead of
  // spilling a long evening session into the next one.
  if (openedAt !== null && windowEnd > openedAt) {
    sessions.push({ start: openedAt, end: windowEnd });
  }

  return mergeAdjacent(sessions);
}

function mergeAdjacent(sessions: readonly ScreenSession[]): ScreenSession[] {
  if (sessions.length === 0) return [];
  const merged: ScreenSession[] = [{ ...(sessions[0] as ScreenSession) }];

  for (let i = 1; i < sessions.length; i += 1) {
    const current = sessions[i] as ScreenSession;
    const last = merged[merged.length - 1] as ScreenSession;
    if (current.start - last.end <= SESSION_MERGE_GAP_MS) {
      last.end = Math.max(last.end, current.end);
    } else {
      merged.push({ ...current });
    }
  }

  return merged;
}

function nightUsageMs(sessions: readonly ScreenSession[], utcOffsetMinutes: number): number {
  let total = 0;
  const offsetMs = utcOffsetMinutes * 60 * 1000;

  for (const session of sessions) {
    // Walk the session in one minute steps rather than solving the interval
    // intersection analytically. Sessions are short, windows are one hour,
    // and the loop is easier to verify than the boundary arithmetic.
    const startMinute = Math.floor((session.start + offsetMs) / 60000);
    const endMinute = Math.ceil((session.end + offsetMs) / 60000);

    for (let minute = startMinute; minute < endMinute; minute += 1) {
      const hourOfDay = Math.floor(((minute % 1440) + 1440) % 1440 / 60);
      if (hourOfDay >= NIGHT_START_HOUR && hourOfDay < NIGHT_END_HOUR) {
        total += 60000;
      }
    }
  }

  return total;
}

function clampTs(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
