import Link from 'next/link';
import { RiskScoreBadge } from '@/components/pulse/RiskScoreBadge';
import { TrajectoryChart } from '@/components/pulse/TrajectoryChart';
import { trajectoryState } from '@/lib/trajectory';
import { AlertFeed } from '@/components/pulse/AlertFeed';
import { SDKStatusBadge } from '@/components/sdk/SDKStatusBadge';
import { SensorFeedPanel } from '@/components/sdk/SensorFeedPanel';
import { VoiceBiomarkerPanel } from '@/components/voice/VoiceBiomarkerPanel';
import {
  fetchAlerts,
  fetchPatientPanel,
  fetchTelemetry,
  fetchTrajectory,
  fetchVoiceSample,
} from '@/lib/api';
import type { Severity } from '@/lib/types';

export const dynamic = 'force-dynamic';

interface PageProps {
  params: { id: string };
}

export async function generateMetadata({ params }: PageProps) {
  return { title: `${decodeURIComponent(params.id)} · Traycer` };
}

export default async function PatientPage({ params }: PageProps) {
  const patientCode = decodeURIComponent(params.id);

  const [panel, series, alerts, voice, telemetry] = await Promise.all([
    fetchPatientPanel(),
    fetchTrajectory(patientCode),
    fetchAlerts(),
    fetchVoiceSample(patientCode),
    fetchTelemetry(patientCode),
  ]);

  const patient = panel.find((entry) => entry.patientCode === patientCode);
  const patientAlerts = alerts.filter((alert) => alert.patientCode === patientCode);
  const state = trajectoryState(series);
  const latest = series.points.at(-1);

  const severity: Severity =
    patient?.severity ?? (state === 'alert' ? 'critical' : state === 'warning' ? 'elevated' : 'stable');

  return (
    <div className="mx-auto w-full max-w-[1400px] px-6 py-6">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-6 border-b border-line pb-5">
        <div className="flex items-start gap-10">
          <div>
            <div className="label">Patient</div>
            <h1 className="readout mt-1 font-mono text-xl text-text-primary">{patientCode}</h1>
            <div className="mt-2 flex items-center gap-3">
              <SDKStatusBadge
                state={telemetry.state}
                lastSeenAt={telemetry.lastWindowAt}
                compact
              />
              <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-text-muted">
                {series.baselineQuality} baseline
              </span>
            </div>
          </div>

          <RiskScoreBadge score={latest?.score ?? null} severity={severity} size="large" />
        </div>

        <div className="flex flex-col items-end gap-3">
          <div className="flex gap-6">
            <Figure label="baseline" value={series.baseline.toFixed(1)} />
            <Figure
              label="deviation"
              value={latest ? formatDelta(latest.score - series.baseline) : 'n/a'}
            />
            <Figure label="open alerts" value={String(patientAlerts.filter((a) => !a.acknowledged).length)} />
          </div>
          <Link
            href={`/patients/${encodeURIComponent(patientCode)}/trajectory`}
            className="btn-quiet"
          >
            Full trajectory view
          </Link>
        </div>
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]">
        <div className="flex flex-col gap-6">
          <TrajectoryChart series={series} height={300} />
          <SensorFeedPanel telemetry={telemetry} />
        </div>

        <div className="flex flex-col gap-6">
          <AlertFeed alerts={patientAlerts} linkPatients={false} />
          <VoiceBiomarkerPanel sample={voice} />
        </div>
      </div>
    </div>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col items-end">
      <span className="readout font-mono text-sm text-text-primary">{value}</span>
      <span className="label">{label}</span>
    </div>
  );
}

function formatDelta(value: number): string {
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)}`;
}
