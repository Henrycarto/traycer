import Link from 'next/link';
import { TrajectoryChart, trajectoryState } from '@/components/pulse/TrajectoryChart';
import { AlertFeed } from '@/components/pulse/AlertFeed';
import { fetchAlerts, fetchTrajectory } from '@/lib/api';

export const dynamic = 'force-dynamic';

interface PageProps {
  params: { id: string };
}

export async function generateMetadata({ params }: PageProps) {
  return { title: `Trajectory ${decodeURIComponent(params.id)} · Traycer` };
}

export default async function TrajectoryPage({ params }: PageProps) {
  const patientCode = decodeURIComponent(params.id);
  const [series, alerts] = await Promise.all([fetchTrajectory(patientCode), fetchAlerts()]);

  const patientAlerts = alerts.filter((alert) => alert.patientCode === patientCode);
  const state = trajectoryState(series);
  const scores = series.points.map((point) => point.score);

  const daysInBand = series.points.filter(
    (point) => point.score >= series.warningThreshold,
  ).length;
  const peak = scores.length > 0 ? Math.max(...scores) : 0;
  const sparseDays = series.points.filter((point) => point.sparse).length;

  return (
    <div className="mx-auto w-full max-w-[1200px] px-6 py-6">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <Link
            href={`/patients/${encodeURIComponent(patientCode)}`}
            className="font-mono text-[11px] uppercase tracking-[0.14em] text-text-muted hover:text-text-primary"
          >
            Back to patient
          </Link>
          <h1 className="readout mt-2 font-mono text-lg text-text-primary">{patientCode}</h1>
        </div>

        <div className="flex gap-6">
          <Figure label="peak" value={peak.toFixed(1)} />
          <Figure label="days in band" value={String(daysInBand)} />
          <Figure label="thin days" value={String(sparseDays)} />
        </div>
      </div>

      <TrajectoryChart series={series} height={420} />

      <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="panel">
          <div className="panel-header">
            <span className="panel-title">Reading this chart</span>
          </div>
          <div className="flex flex-col gap-3 px-4 py-4 text-[13px] leading-relaxed text-text-secondary">
            <p>
              The dashed line is this patient&apos;s own behavioral baseline, computed from their
              first fourteen days of passive data. Every point on the trajectory is a deviation
              from that line, not a comparison against other patients.
            </p>
            <p>
              The shaded band begins where deviation stops being ordinary week to week variation.
              Entering it is not an emergency. Staying in it across consecutive windows is what
              fires an alert, and that confirmation requirement is why one bad night does not
              reach your phone.
            </p>
            <p>
              A day marked as thin had poor sensor coverage. Those days are scored with reduced
              weight rather than dropped, so a phone left on a charger does not read as a
              behavioral change.
            </p>
            <p className="text-text-muted">
              Current state: <span className="font-mono">{state}</span>. Alert threshold sits at{' '}
              <span className="readout font-mono">{series.alertThreshold}</span>, deviation band
              begins at <span className="readout font-mono">{series.warningThreshold}</span>.
            </p>
          </div>
        </div>

        <AlertFeed alerts={patientAlerts} linkPatients={false} />
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
