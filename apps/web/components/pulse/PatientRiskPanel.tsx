import Link from 'next/link';
import { RiskScoreBadge } from './RiskScoreBadge';
import { SDKStatusBadge } from '../sdk/SDKStatusBadge';
import type { PatientSummary, Severity } from '@/lib/types';

/**
 * The panel overview. One row per patient, forty rows on one screen.
 *
 * The row is the unit of triage: name, score, a seven day sparkline, and SDK
 * state. Nothing expands, nothing hides behind a hover. A clinician opening
 * this on a Monday morning should be able to decide who to call before they
 * finish scrolling.
 *
 * Sorting puts critical first, then elevated, then everyone else by score
 * descending. Patients with no usable data sink to the bottom, because a
 * patient the platform cannot see is a data problem rather than a clinical
 * one, and mixing the two wastes attention.
 */

const SEVERITY_ORDER: Record<Severity, number> = { critical: 0, elevated: 1, stable: 2 };

export interface PatientRiskPanelProps {
  patients: PatientSummary[];
  className?: string;
}

export function PatientRiskPanel({ patients, className = '' }: PatientRiskPanelProps) {
  const sorted = [...patients].sort(compareForTriage);
  const counts = countBySeverity(patients);

  return (
    <div className={`panel ${className}`}>
      <div className="panel-header">
        <span className="panel-title">Patient panel</span>
        <div className="flex items-center gap-4">
          <Count label="critical" value={counts.critical} color="text-alert-high" />
          <Count label="elevated" value={counts.elevated} color="text-alert-medium" />
          <Count label="stable" value={counts.stable} color="text-stable" />
          <Count label="no data" value={counts.noData} color="text-inactive" />
        </div>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_5.5rem_7rem_6rem] items-center gap-3 border-b border-line px-4 py-2">
        <span className="label">Patient</span>
        <span className="label text-right">Risk</span>
        <span className="label text-right">7 day</span>
        <span className="label text-right">SDK</span>
      </div>

      <ul className="divide-y divide-line">
        {sorted.map((patient) => (
          <li key={patient.patientCode}>
            <Link
              href={`/patients/${encodeURIComponent(patient.patientCode)}`}
              className="grid grid-cols-[minmax(0,1fr)_5.5rem_7rem_6rem] items-center gap-3 px-4 py-2.5 hover:bg-raised"
            >
              <div className="min-w-0">
                <div className="readout truncate font-mono text-xs text-text-primary">
                  {patient.patientCode}
                </div>
                <div className="truncate text-[11px] text-text-muted">
                  {patient.dominantSignal
                    ? `driven by ${patient.dominantSignal.replace(/_/g, ' ')}`
                    : patient.baselineQuality === 'established'
                      ? 'within range'
                      : `${patient.baselineQuality} baseline`}
                </div>
              </div>

              <RiskScoreBadge score={patient.riskScore} severity={patient.severity} />

              <Sparkline values={patient.sparkline} severity={patient.severity} />

              <div className="flex justify-end">
                <SDKStatusBadge state={patient.sdkState} lastSeenAt={patient.lastSeenAt} compact />
              </div>
            </Link>
          </li>
        ))}
      </ul>

      {sorted.length === 0 ? (
        <p className="px-4 py-6 text-sm text-text-muted">
          No patients on this panel yet.
        </p>
      ) : null}
    </div>
  );
}

function Count({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <span className="flex items-baseline gap-1.5">
      <span className={`readout font-mono text-xs ${color}`}>{value}</span>
      <span className="text-[10px] uppercase tracking-[0.1em] text-text-muted">{label}</span>
    </span>
  );
}

const SPARK_STROKE: Record<Severity, string> = {
  stable: '#4ADE80',
  elevated: '#D97706',
  critical: '#DC2626',
};

/**
 * Seven day trend as an inline polyline.
 *
 * Hand rolled rather than a chart library. Forty Recharts instances on one
 * page is a measurable amount of work for the browser to do, and this needs
 * to be a shape rather than a chart. There are no axes, no tooltip, and no
 * interaction: the clinician clicks the row for the real chart.
 */
function Sparkline({ values, severity }: { values: number[]; severity: Severity }) {
  if (values.length < 2) {
    return <span className="text-right font-mono text-[10px] text-inactive">no trend</span>;
  }

  const width = 92;
  const height = 22;
  const min = Math.min(...values);
  const max = Math.max(...values);
  // A flat week would divide by zero and, worse, would render as a line at
  // the top of the box. The floor keeps a stable patient looking stable.
  const range = Math.max(max - min, 8);

  const points = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width;
      const y = height - ((value - min) / range) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className="justify-self-end"
      aria-hidden
    >
      <polyline
        points={points}
        fill="none"
        stroke={SPARK_STROKE[severity]}
        strokeWidth={1.25}
        strokeLinejoin="round"
      />
    </svg>
  );
}

function compareForTriage(a: PatientSummary, b: PatientSummary): number {
  const aScored = a.riskScore !== null;
  const bScored = b.riskScore !== null;
  if (aScored !== bScored) return aScored ? -1 : 1;

  const severity = SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity];
  if (severity !== 0) return severity;

  return (b.riskScore ?? 0) - (a.riskScore ?? 0);
}

function countBySeverity(patients: PatientSummary[]) {
  return patients.reduce(
    (acc, patient) => {
      if (patient.riskScore === null) {
        acc.noData += 1;
        return acc;
      }
      acc[patient.severity] += 1;
      return acc;
    },
    { critical: 0, elevated: 0, stable: 0, noData: 0 },
  );
}
