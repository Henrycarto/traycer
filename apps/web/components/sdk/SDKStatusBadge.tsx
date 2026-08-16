import type { SdkState } from '@/lib/types';

/**
 * Whether the platform is actually receiving data from this patient.
 *
 * This badge exists because the most dangerous state in a passive monitoring
 * system is silence that reads as calm. A patient whose SDK stopped uploading
 * three days ago shows no deviation, no alerts, and a perfectly quiet
 * trajectory. The panel has to make that visibly different from a patient who
 * is genuinely stable.
 */

export interface SDKStatusBadgeProps {
  state: SdkState;
  lastSeenAt?: string | null;
  compact?: boolean;
  className?: string;
}

const LABEL: Record<SdkState, string> = {
  active: 'ACTIVE',
  stale: 'STALE',
  inactive: 'INACTIVE',
  enrolling: 'ENROLLING',
};

const COLOR: Record<SdkState, string> = {
  active: 'text-stable border-stable/40',
  stale: 'text-alert-medium border-alert-medium/40',
  inactive: 'text-inactive border-inactive',
  enrolling: 'text-indigo border-indigo/40',
};

const DOT: Record<SdkState, string> = {
  active: 'bg-stable',
  stale: 'bg-alert-medium',
  inactive: 'bg-inactive',
  enrolling: 'bg-indigo',
};

export function SDKStatusBadge({
  state,
  lastSeenAt,
  compact = false,
  className = '',
}: SDKStatusBadgeProps) {
  if (compact) {
    return (
      <span
        className={`inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.12em] ${
          COLOR[state].split(' ')[0]
        } ${className}`}
        title={describe(state, lastSeenAt)}
      >
        <span className={`h-1.5 w-1.5 ${DOT[state]}`} aria-hidden />
        {LABEL[state]}
      </span>
    );
  }

  return (
    <div className={`inline-flex flex-col gap-1 border px-3 py-2 ${COLOR[state]} ${className}`}>
      <span className="inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.14em]">
        <span className={`h-1.5 w-1.5 ${DOT[state]}`} aria-hidden />
        SDK {LABEL[state]}
      </span>
      <span className="readout font-mono text-[10px] text-text-muted">
        {describe(state, lastSeenAt)}
      </span>
    </div>
  );
}

/**
 * Says how long the gap is rather than when the last upload was.
 *
 * A clinician reading this needs to know whether to chase the patient. "4h
 * ago" answers that. A raw timestamp makes them work it out.
 */
function describe(state: SdkState, lastSeenAt?: string | null): string {
  if (state === 'enrolling') return 'baseline window in progress';
  if (!lastSeenAt) return 'no upload recorded';

  const elapsedMs = Date.now() - new Date(lastSeenAt).getTime();
  if (elapsedMs < 0) return 'last upload just now';

  const hours = Math.floor(elapsedMs / 3_600_000);
  if (hours < 1) return `last upload ${Math.floor(elapsedMs / 60_000)}m ago`;
  if (hours < 48) return `last upload ${hours}h ago`;
  return `last upload ${Math.floor(hours / 24)}d ago`;
}

/** Classify an SDK state from the last upload timestamp. */
export function sdkStateFromLastSeen(
  lastSeenAt: string | null,
  baselineReady: boolean,
): SdkState {
  if (!baselineReady) return 'enrolling';
  if (!lastSeenAt) return 'inactive';

  const hours = (Date.now() - new Date(lastSeenAt).getTime()) / 3_600_000;
  // Six hours is generous for a one hour collection window. Background task
  // scheduling on iOS is unpredictable enough that a tighter bound would flag
  // healthy devices every day.
  if (hours <= 6) return 'active';
  if (hours <= 48) return 'stale';
  return 'inactive';
}
