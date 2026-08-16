import type { AcousticFeature } from '@/lib/types';

/**
 * One acoustic measurement against the patient's own acoustic baseline.
 *
 * The bar is centred on the baseline rather than filled from zero. An
 * absolute loudness value means nothing to a psychiatrist; the distance from
 * this patient's own usual loudness means quite a lot. Left of centre is
 * below baseline, right of centre is above.
 */

const DISPLAY_NAME: Record<string, string> = {
  vocal_energy: 'vocal energy',
  energy_variability: 'energy variability',
  pitch_mean_semitones: 'pitch mean',
  pitch_variance: 'pitch variance',
  speech_rate: 'speech rate',
  articulation_rate: 'articulation rate',
  pause_frequency: 'pause frequency',
  mean_pause_seconds: 'mean pause',
  voiced_ratio: 'voiced ratio',
  jitter: 'jitter',
  shimmer: 'shimmer',
  harmonicity: 'harmonicity',
  spectral_slope: 'spectral slope',
};

/** Relative change past this fraction is drawn as a deviation, not noise. */
const NOTABLE_DEVIATION = 0.2;

export interface AcousticFeatureBarProps {
  feature: AcousticFeature;
  className?: string;
}

export function AcousticFeatureBar({ feature, className = '' }: AcousticFeatureBarProps) {
  const label = DISPLAY_NAME[feature.feature] ?? feature.feature.replace(/_/g, ' ');
  const relative = relativeChange(feature);

  return (
    <div className={`grid grid-cols-[9rem_1fr_5rem] items-center gap-3 ${className}`}>
      <span className="truncate text-[11px] text-text-secondary">{label}</span>

      <div className="relative h-1.5 bg-void">
        {/* Centre mark is the patient's own baseline for this feature. */}
        <span className="absolute left-1/2 top-0 h-full w-px bg-line" aria-hidden />
        {relative === null ? null : <DeviationBar relative={relative} />}
      </div>

      <span className="readout text-right font-mono text-[11px] text-text-primary">
        {formatValue(feature.value)}
      </span>
    </div>
  );
}

function DeviationBar({ relative }: { relative: number }) {
  // Clamped so a broken sample cannot draw past the track.
  const magnitude = Math.min(Math.abs(relative), 1);
  const widthPercent = magnitude * 50;
  const notable = magnitude >= NOTABLE_DEVIATION;

  const color = !notable ? 'bg-indigo' : relative < 0 ? 'bg-alert-medium' : 'bg-alert-medium';

  return (
    <span
      className={`absolute top-0 h-full ${color}`}
      style={
        relative < 0
          ? { right: '50%', width: `${widthPercent}%` }
          : { left: '50%', width: `${widthPercent}%` }
      }
      aria-hidden
    />
  );
}

/** Signed change against baseline, as a fraction of the baseline value. */
function relativeChange(feature: AcousticFeature): number | null {
  if (feature.baseline === null) return null;
  if (Math.abs(feature.baseline) < 1e-6) return null;
  return (feature.value - feature.baseline) / Math.abs(feature.baseline);
}

function formatValue(value: number): string {
  if (Math.abs(value) >= 100) return value.toFixed(0);
  if (Math.abs(value) >= 1) return value.toFixed(2);
  return value.toFixed(3);
}
