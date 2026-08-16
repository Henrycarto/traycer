import type { TrajectorySeries } from './types';

/**
 * Pure trajectory helpers, deliberately outside the chart component.
 *
 * TrajectoryChart is a client component. A plain function exported from a
 * "use client" module cannot be called from a server component: the import
 * resolves to a client reference rather than the function, and the page
 * crashes at request time with "is not a function". The production build does
 * not catch it, because the pages that call this are dynamic and are never
 * rendered during the build.
 *
 * So the logic lives here, in a module with no client boundary, and both the
 * chart and the server rendered pages import it from the same place.
 */

export type TrajectoryState = 'quiet' | 'warning' | 'alert';

/**
 * Which band the trajectory currently sits in.
 *
 * Reads the most recent point rather than the maximum across the window. A
 * patient who crossed the threshold three weeks ago and has been stable since
 * is not in an alert state now, and showing a red border for history would
 * train clinicians to ignore the colour.
 */
export function trajectoryState(series: TrajectorySeries): TrajectoryState {
  const latest = series.points.at(-1);
  if (!latest) return 'quiet';
  if (latest.score >= series.alertThreshold) return 'alert';
  if (latest.score >= series.warningThreshold) return 'warning';
  return 'quiet';
}
