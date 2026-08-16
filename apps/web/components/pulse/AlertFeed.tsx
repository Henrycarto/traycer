import Link from 'next/link';
import { ScrollPane } from '../ui/ScrollPane';
import type { AlertRecord, Severity } from '@/lib/types';

/**
 * Chronological list of fired alerts across the whole panel.
 *
 * Dense on purpose. Each row is one line: patient code, a severity dot, the
 * behavioral signal that fired it, and a timestamp. A clinician should be able
 * to read the overnight state of a forty patient panel without scrolling and
 * without clicking anything.
 *
 * Row height is fixed so the eye can scan the timestamp column straight down.
 */

const DOT: Record<Severity, string> = {
  stable: 'bg-stable',
  elevated: 'bg-alert-medium',
  critical: 'bg-alert-high',
};

const SIGNAL_LABEL: Record<string, string> = {
  mobility_radius_m: 'mobility radius',
  location_entropy: 'location entropy',
  place_count: 'places visited',
  primary_place_ratio: 'time at home',
  activity_ratio: 'activity',
  movement_intensity: 'movement intensity',
  longest_sedentary_minutes: 'sedentary bouts',
  activity_fragmentation: 'activity fragmentation',
  screen_on_minutes: 'screen time',
  unlock_count: 'unlock count',
  mean_session_minutes: 'session length',
  usage_loop_index: 'checking loop',
  night_usage_minutes: 'night phone use',
  communication_session_count: 'social contact',
  communication_minutes: 'social contact time',
};

/** Row height in pixels, matching the h-row spacing token. */
const ROW_HEIGHT = 44;

export interface AlertFeedProps {
  alerts: AlertRecord[];
  /** Show the patient code as a link through to their trajectory. */
  linkPatients?: boolean;
  maxRows?: number;
  className?: string;
}

export function AlertFeed({
  alerts,
  linkPatients = true,
  maxRows,
  className = '',
}: AlertFeedProps) {
  // maxRows caps the visible height and scrolls the rest rather than hiding
  // it. An alert that exists but cannot be reached is worse than one that
  // needs a scroll.
  const scrollHeight = maxRows ? maxRows * ROW_HEIGHT : null;

  return (
    <div className={`panel ${className}`}>
      <div className="panel-header">
        <span className="panel-title">Alert feed</span>
        <span className="readout font-mono text-[11px] text-text-muted">
          {alerts.filter((alert) => !alert.acknowledged).length} open
        </span>
      </div>

      {alerts.length === 0 ? (
        <p className="px-4 py-6 text-sm text-text-muted">
          No alerts. Every patient on the panel is within their own behavioral range.
        </p>
      ) : (
        <Rows alerts={alerts} linkPatients={linkPatients} scrollHeight={scrollHeight} />
      )}
    </div>
  );
}

function Rows({
  alerts,
  linkPatients,
  scrollHeight,
}: {
  alerts: AlertRecord[];
  linkPatients: boolean;
  scrollHeight: number | null;
}) {
  const list = (
    <ul className="divide-y divide-line">
      {alerts.map((alert) => (
        <li key={alert.alertId}>
          <AlertRow alert={alert} linkPatient={linkPatients} />
        </li>
      ))}
    </ul>
  );

  if (scrollHeight === null || alerts.length * ROW_HEIGHT <= scrollHeight) {
    return list;
  }

  return <ScrollPane maxHeight={scrollHeight}>{list}</ScrollPane>;
}

function AlertRow({ alert, linkPatient }: { alert: AlertRecord; linkPatient: boolean }) {
  const label = SIGNAL_LABEL[alert.triggerSignal] ?? alert.triggerSignal.replace(/_/g, ' ');

  const body = (
    <div
      className={`grid h-row grid-cols-[10px_minmax(0,7rem)_1fr_auto] items-center gap-3 px-4 ${
        alert.acknowledged ? 'opacity-45' : ''
      }`}
    >
      <span
        aria-label={alert.severity}
        className={`h-2 w-2 ${DOT[alert.severity]}`}
        title={alert.severity}
      />
      <span className="readout truncate font-mono text-xs text-text-primary">
        {alert.patientCode}
      </span>
      <span className="truncate text-xs text-text-secondary">
        {label}
        <span className="mx-2 text-text-muted">·</span>
        <span className="readout font-mono text-text-muted">{alert.riskScore.toFixed(0)}</span>
      </span>
      <time
        dateTime={alert.firedAt}
        className="readout font-mono text-[11px] tabular-nums text-text-muted"
      >
        {formatTimestamp(alert.firedAt)}
      </time>
    </div>
  );

  if (!linkPatient) return body;

  return (
    <Link
      href={`/patients/${encodeURIComponent(alert.patientCode)}/trajectory`}
      className="block hover:bg-raised"
    >
      {body}
    </Link>
  );
}

/**
 * Fixed width timestamp so the column aligns.
 *
 * Day and time rather than a relative string. "Two hours ago" forces a
 * clinician to do arithmetic against their own shift, and a handover at 08:00
 * needs to know whether something fired at 03:00 or at 07:40.
 */
function formatTimestamp(iso: string): string {
  const date = new Date(iso);
  const day = String(date.getDate()).padStart(2, '0');
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');
  return `${day}.${month} ${hours}:${minutes}`;
}
