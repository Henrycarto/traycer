import { SDKStatusBadge } from './SDKStatusBadge';
import { InfoTip } from '../ui/InfoTip';
import type { SdkTelemetry } from '@/lib/types';

/**
 * Raw sensor state for one patient's device.
 *
 * This is the diagnostic view, not the clinical one. When a clinician thinks
 * the trajectory looks wrong, the first question is whether the phone is
 * actually reporting, and this panel answers it: which domains are live, how
 * far behind the queue is, and what the most recent window measured.
 *
 * Values are shown with their confidence because a reading at 0.2 confidence
 * and a reading at 1.0 are not the same observation.
 */

const DOMAIN_LABEL: Record<string, string> = {
  motion: 'motion',
  mobility: 'mobility',
  usage: 'phone usage',
};

export interface SensorFeedPanelProps {
  telemetry: SdkTelemetry;
  className?: string;
}

export function SensorFeedPanel({ telemetry, className = '' }: SensorFeedPanelProps) {
  return (
    <div className={`panel ${className}`}>
      <div className="panel-header">
        <span className="panel-title">Sensor feed</span>
        <SDKStatusBadge state={telemetry.state} lastSeenAt={telemetry.lastWindowAt} compact />
      </div>

      <div className="grid grid-cols-2 gap-x-6 gap-y-2 border-b border-line px-4 py-3 sm:grid-cols-4">
        <Stat label="sdk" value={telemetry.sdkVersion ?? 'unknown'} />
        <Stat label="queued windows" value={String(telemetry.queuedWindows)} />
        <Stat label="last window" value={relative(telemetry.lastWindowAt)} />
        <Stat label="last upload" value={relative(telemetry.lastUploadAt)} />
      </div>

      <div className="flex flex-wrap gap-2 border-b border-line px-4 py-2.5">
        {(['motion', 'mobility', 'usage'] as const).map((domain) => {
          const live = telemetry.activeDomains.includes(domain);
          return (
            <span
              key={domain}
              className={`inline-flex items-center gap-1.5 border px-2 py-1 font-mono text-[10px] uppercase tracking-[0.1em] ${
                live ? 'border-stable/40 text-stable' : 'border-inactive text-inactive'
              }`}
            >
              <span className={`h-1.5 w-1.5 ${live ? 'bg-stable' : 'bg-inactive'}`} aria-hidden />
              {DOMAIN_LABEL[domain]}
            </span>
          );
        })}
      </div>

      {telemetry.recentReadings.length === 0 ? (
        <p className="px-4 py-6 text-sm text-text-muted">
          No readings in the most recent window.
        </p>
      ) : (
        <ul className="divide-y divide-line">
          {telemetry.recentReadings.map((reading) => (
            <li
              key={reading.feature}
              className="grid grid-cols-[1fr_auto_4rem] items-center gap-3 px-4 py-2"
            >
              <span className="truncate text-[11px] text-text-secondary">
                {reading.feature.replace(/_/g, ' ')}
              </span>
              <span className="readout font-mono text-xs text-text-primary">
                {formatReading(reading.value)}
                <span className="ml-1 text-text-muted">{reading.unit}</span>
              </span>
              <ConfidenceMark confidence={reading.confidence} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="label">{label}</span>
      <span className="readout font-mono text-xs text-text-primary">{value}</span>
    </div>
  );
}

function ConfidenceMark({ confidence }: { confidence: number }) {
  const color =
    confidence >= 0.8
      ? 'text-stable'
      : confidence >= 0.4
        ? 'text-alert-medium'
        : 'text-inactive';

  return (
    <InfoTip label="Fraction of the collection window during which the sensor was readable. Low confidence readings are scored with reduced weight rather than dropped.">
      <span className={`readout block text-right font-mono text-[10px] ${color}`}>
        {confidence.toFixed(2)}
      </span>
    </InfoTip>
  );
}

function formatReading(value: number): string {
  if (Math.abs(value) >= 1000) return value.toFixed(0);
  if (Math.abs(value) >= 1) return value.toFixed(2);
  return value.toFixed(3);
}

function relative(iso: string | null): string {
  if (!iso) return 'never';

  const elapsedMs = Date.now() - new Date(iso).getTime();
  if (elapsedMs < 0) return 'just now';

  const minutes = Math.floor(elapsedMs / 60_000);
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
