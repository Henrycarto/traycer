import type { Severity } from '@/lib/types';

/**
 * A number and a word. Nothing else.
 *
 * The badge appears inline in a forty row panel and again at full size on a
 * patient page. Adding a trend arrow, a delta, or a sparkline here was
 * considered and rejected: the row already carries a sparkline, and a badge
 * that says three things says none of them quickly.
 */

export type RiskScoreBadgeSize = 'inline' | 'large';

export interface RiskScoreBadgeProps {
  /** 0 to 100. Null when the patient has no established baseline yet. */
  score: number | null;
  severity: Severity;
  size?: RiskScoreBadgeSize;
  className?: string;
}

const WORD: Record<Severity, string> = {
  stable: 'STABLE',
  elevated: 'ELEVATED',
  critical: 'CRITICAL',
};

const COLOR: Record<Severity, string> = {
  stable: 'text-stable',
  elevated: 'text-alert-medium',
  critical: 'text-alert-high',
};

export function RiskScoreBadge({
  score,
  severity,
  size = 'inline',
  className = '',
}: RiskScoreBadgeProps) {
  const large = size === 'large';

  // A patient without an established baseline has no score, and showing a
  // zero would read as the safest patient on the panel. The badge shows a
  // placeholder in the inactive colour instead.
  if (score === null) {
    return (
      <div className={`flex flex-col ${large ? 'items-start gap-1' : 'items-end'} ${className}`}>
        <span
          className={`readout font-mono text-inactive ${large ? 'text-readout' : 'text-base'}`}
        >
          n/a
        </span>
        <span
          className={`font-mono uppercase tracking-[0.18em] text-inactive ${
            large ? 'text-xs' : 'text-[9px]'
          }`}
        >
          NO BASELINE
        </span>
      </div>
    );
  }

  return (
    <div className={`flex flex-col ${large ? 'items-start gap-1' : 'items-end'} ${className}`}>
      <span
        className={`readout font-mono ${COLOR[severity]} ${
          large ? 'text-readout' : 'text-base leading-none'
        }`}
      >
        {Math.round(score)}
      </span>
      <span
        className={`font-mono uppercase tracking-[0.18em] ${COLOR[severity]} ${
          large ? 'text-xs' : 'text-[9px]'
        }`}
      >
        {WORD[severity]}
      </span>
    </div>
  );
}
