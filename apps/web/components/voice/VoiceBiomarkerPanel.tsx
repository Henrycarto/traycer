import { AcousticFeatureBar } from './AcousticFeatureBar';
import type { VoiceSample } from '@/lib/types';

/**
 * Acoustic readout from the patient's most recent voice sample.
 *
 * The panel states twice, in the header and in the footer, that no speech
 * content is processed. That repetition is intentional. Every clinician who
 * sees a voice panel in a psychiatric tool asks the same question within
 * about four seconds, and answering it before they ask is the difference
 * between a tool they trust and one they quietly stop using.
 */

const QUALITY_COPY: Record<VoiceSample['quality'], string> = {
  usable: 'usable',
  noisy: 'noisy recording',
  too_short: 'sample too short',
  no_speech: 'no speech detected',
};

const QUALITY_COLOR: Record<VoiceSample['quality'], string> = {
  usable: 'text-stable',
  noisy: 'text-alert-medium',
  too_short: 'text-inactive',
  no_speech: 'text-inactive',
};

export interface VoiceBiomarkerPanelProps {
  sample: VoiceSample | null;
  className?: string;
}

export function VoiceBiomarkerPanel({ sample, className = '' }: VoiceBiomarkerPanelProps) {
  return (
    <div className={`panel ${className}`}>
      <div className="panel-header">
        <span className="panel-title">Voice biomarkers</span>
        <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-text-muted">
          acoustic only, never transcribed
        </span>
      </div>

      {sample === null ? (
        <p className="px-4 py-6 text-sm text-text-muted">
          No voice sample recorded. Voice is optional and patient initiated, so an absent
          sample is not a gap in monitoring.
        </p>
      ) : (
        <>
          <div className="flex items-baseline justify-between border-b border-line px-4 py-2.5">
            <div className="flex items-baseline gap-4">
              <span className="readout font-mono text-[11px] text-text-secondary">
                {formatDateTime(sample.recordedAt)}
              </span>
              <span className="readout font-mono text-[11px] text-text-muted">
                {sample.sampleSeconds.toFixed(1)}s
              </span>
            </div>
            <span
              className={`font-mono text-[10px] uppercase tracking-[0.12em] ${
                QUALITY_COLOR[sample.quality]
              }`}
            >
              {QUALITY_COPY[sample.quality]}
            </span>
          </div>

          <div className="flex flex-col gap-2.5 px-4 py-4">
            {sample.features.map((feature) => (
              <AcousticFeatureBar key={feature.feature} feature={feature} />
            ))}
          </div>

          <div className="border-t border-line px-4 py-2.5">
            <p className="text-[11px] leading-relaxed text-text-muted">
              Bars show deviation from this patient&apos;s own acoustic baseline. The extractor
              produces numeric descriptors of the signal. No audio is retained past extraction
              and no transcript is produced at any point.
            </p>
            <p className="readout mt-1.5 font-mono text-[10px] text-text-muted">
              extractor {sample.extractor}
            </p>
          </div>
        </>
      )}
    </div>
  );
}

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  const day = String(date.getDate()).padStart(2, '0');
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');
  return `${day}.${month} ${hours}:${minutes}`;
}
