'use client';

import { useMemo } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { TooltipProps } from 'recharts';
import type { TrajectorySeries } from '@/lib/types';

/**
 * The signature element of the dashboard.
 *
 * Thirty days of behavioral score, the patient's own baseline as a dashed
 * line, and a shaded band above the deviation threshold. When the line enters
 * the band the chart border turns amber. When it crosses the alert threshold
 * the border turns red.
 *
 * The border is the whole point. A psychiatrist scrolling a panel of forty
 * patients is not reading axis values. They are looking for the one chart that
 * changed colour, and the colour is a clinical statement rather than a
 * decoration: this patient's own pattern has moved, and it has stayed moved.
 *
 * Everything else is deliberately quiet. No gradient fill, no animated draw
 * in, no dot on every point. The chart should be boring until it is not.
 */

const COLORS = {
  line: '#6366F1',
  baseline: '#64748B',
  warningBand: 'rgba(217, 119, 6, 0.10)',
  alertBand: 'rgba(220, 38, 38, 0.12)',
  warningBorder: '#D97706',
  alertBorder: '#DC2626',
  quietBorder: '#1B2436',
  grid: '#141C2C',
  axis: '#64748B',
  surface: '#0C1220',
} as const;

export type TrajectoryState = 'quiet' | 'warning' | 'alert';

export interface TrajectoryChartProps {
  series: TrajectorySeries;
  /** Chart height in pixels. The panel view uses a shorter chart. */
  height?: number;
  /** Hide the axes for a compact embed. */
  compact?: boolean;
  className?: string;
}

interface ChartRow {
  date: string;
  label: string;
  score: number;
  sparse: boolean;
}

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

const BORDER_BY_STATE: Record<TrajectoryState, string> = {
  quiet: COLORS.quietBorder,
  warning: COLORS.warningBorder,
  alert: COLORS.alertBorder,
};

export function TrajectoryChart({
  series,
  height = 280,
  compact = false,
  className = '',
}: TrajectoryChartProps) {
  const state = trajectoryState(series);

  const rows = useMemo<ChartRow[]>(
    () =>
      series.points.map((point) => ({
        date: point.date,
        label: formatDayLabel(point.date),
        score: point.score,
        sparse: point.sparse ?? false,
      })),
    [series.points],
  );

  const latest = series.points.at(-1);

  return (
    <div
      className={`panel ${className}`}
      style={{ borderColor: BORDER_BY_STATE[state] }}
      data-state={state}
    >
      <div className="panel-header" style={{ borderColor: BORDER_BY_STATE[state] }}>
        <div className="flex items-baseline gap-3">
          <span className="panel-title">30 day behavioral trajectory</span>
          {series.baselineQuality !== 'established' ? (
            <span className="label text-alert-medium">{series.baselineQuality} baseline</span>
          ) : null}
        </div>
        <div className="flex items-baseline gap-4">
          <StateReadout state={state} />
          {latest ? (
            <span className="readout font-mono text-sm text-text-primary">
              {latest.score.toFixed(1)}
            </span>
          ) : null}
        </div>
      </div>

      <div className="px-2 pb-2 pt-4" style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} margin={{ top: 4, right: 12, bottom: 0, left: compact ? 0 : 4 }}>
            <CartesianGrid stroke={COLORS.grid} strokeDasharray="0" vertical={false} />

            {/* The warning band sits between the deviation threshold and the
                alert threshold. The alert band runs from the alert threshold
                to the top of the scale. Both are flat fills at low opacity so
                the trajectory line stays the brightest thing in the frame. */}
            <ReferenceArea
              y1={series.warningThreshold}
              y2={series.alertThreshold}
              fill={COLORS.warningBand}
              stroke="none"
              ifOverflow="extendDomain"
            />
            <ReferenceArea
              y1={series.alertThreshold}
              y2={100}
              fill={COLORS.alertBand}
              stroke="none"
              ifOverflow="extendDomain"
            />

            {/* The patient's own baseline. Dashed, muted, unlabelled on the
                compact variant. This is the reference every reading on the
                chart is relative to. */}
            <ReferenceLine
              y={series.baseline}
              stroke={COLORS.baseline}
              strokeDasharray="4 4"
              strokeWidth={1}
              label={
                compact
                  ? undefined
                  : {
                      value: 'personal baseline',
                      position: 'insideBottomLeft',
                      fill: COLORS.baseline,
                      fontSize: 10,
                      fontFamily: 'var(--font-fira-code)',
                      dy: -6,
                    }
              }
            />

            <ReferenceLine
              y={series.alertThreshold}
              stroke={COLORS.alertBorder}
              strokeDasharray="2 4"
              strokeWidth={1}
              strokeOpacity={0.5}
            />

            <XAxis
              dataKey="label"
              hide={compact}
              stroke={COLORS.axis}
              tick={{ fill: COLORS.axis, fontSize: 10, fontFamily: 'var(--font-fira-code)' }}
              tickLine={false}
              axisLine={{ stroke: COLORS.grid }}
              interval="preserveStartEnd"
              minTickGap={28}
            />
            <YAxis
              hide={compact}
              domain={[0, 100]}
              ticks={[0, 25, 50, 75, 100]}
              stroke={COLORS.axis}
              tick={{ fill: COLORS.axis, fontSize: 10, fontFamily: 'var(--font-fira-code)' }}
              tickLine={false}
              axisLine={false}
              width={28}
            />

            <Tooltip
              content={<TrajectoryTooltip baseline={series.baseline} />}
              cursor={{ stroke: COLORS.baseline, strokeWidth: 1, strokeDasharray: '3 3' }}
            />

            <Line
              type="monotone"
              dataKey="score"
              stroke={COLORS.line}
              strokeWidth={1.75}
              // A dot per day turns thirty points into visual noise. The
              // active dot on hover is enough to read an individual value.
              dot={false}
              activeDot={{ r: 3, fill: COLORS.line, stroke: COLORS.surface, strokeWidth: 2 }}
              isAnimationActive={false}
              connectNulls
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {compact ? null : (
        <div className="flex items-center gap-5 border-t border-line px-4 py-2">
          <Legend swatch={COLORS.line} label="behavioral score" />
          <Legend swatch={COLORS.baseline} label="personal baseline" dashed />
          <Legend swatch={COLORS.warningBorder} label="deviation band" />
          <Legend swatch={COLORS.alertBorder} label="alert threshold" />
        </div>
      )}
    </div>
  );
}

function StateReadout({ state }: { state: TrajectoryState }) {
  const copy: Record<TrajectoryState, string> = {
    quiet: 'WITHIN RANGE',
    warning: 'IN WARNING BAND',
    alert: 'ABOVE ALERT THRESHOLD',
  };

  const color: Record<TrajectoryState, string> = {
    quiet: 'text-text-muted',
    warning: 'text-alert-medium',
    alert: 'text-alert-high',
  };

  return (
    <span className={`font-mono text-[10px] uppercase tracking-[0.16em] ${color[state]}`}>
      {copy[state]}
    </span>
  );
}

function Legend({
  swatch,
  label,
  dashed = false,
}: {
  swatch: string;
  label: string;
  dashed?: boolean;
}) {
  return (
    <span className="flex items-center gap-2">
      <span
        aria-hidden
        className="inline-block h-[2px] w-5"
        style={
          dashed
            ? {
                backgroundImage: `repeating-linear-gradient(to right, ${swatch} 0 4px, transparent 4px 8px)`,
              }
            : { backgroundColor: swatch }
        }
      />
      <span className="text-[10px] uppercase tracking-[0.1em] text-text-muted">{label}</span>
    </span>
  );
}

function TrajectoryTooltip({
  active,
  payload,
  baseline,
}: TooltipProps<number, string> & { baseline: number }) {
  if (!active || !payload || payload.length === 0) return null;

  const row = payload[0]?.payload as ChartRow | undefined;
  if (!row) return null;

  const delta = row.score - baseline;
  const deltaColor =
    delta > 0 ? 'text-alert-medium' : delta < 0 ? 'text-stable' : 'text-text-muted';

  return (
    <div className="border border-line bg-void px-3 py-2">
      <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-text-muted">
        {formatFullDate(row.date)}
      </div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="readout font-mono text-lg text-text-primary">{row.score.toFixed(1)}</span>
        <span className={`readout font-mono text-[11px] ${deltaColor}`}>
          {delta >= 0 ? '+' : ''}
          {delta.toFixed(1)} vs baseline
        </span>
      </div>
      {row.sparse ? (
        <div className="mt-1 font-mono text-[10px] text-alert-medium">
          thin sensor coverage this day
        </div>
      ) : null}
    </div>
  );
}

function formatDayLabel(iso: string): string {
  const date = new Date(iso);
  return `${String(date.getUTCDate()).padStart(2, '0')}.${String(date.getUTCMonth() + 1).padStart(2, '0')}`;
}

function formatFullDate(iso: string): string {
  const date = new Date(iso);
  return date.toLocaleDateString('en-GB', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  });
}
