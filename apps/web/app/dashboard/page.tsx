import { AlertFeed } from '@/components/pulse/AlertFeed';
import { PatientRiskPanel } from '@/components/pulse/PatientRiskPanel';
import { fetchAlerts, fetchPatientPanel, isDemoMode } from '@/lib/api';

export const metadata = { title: 'Panel · Traycer' };
export const dynamic = 'force-dynamic';

export default async function DashboardPage() {
  const [patients, alerts] = await Promise.all([fetchPatientPanel(), fetchAlerts()]);

  const open = alerts.filter((alert) => !alert.acknowledged);
  const needsAttention = patients.filter(
    (patient) => patient.riskScore !== null && patient.severity !== 'stable',
  ).length;
  const notReporting = patients.filter(
    (patient) => patient.sdkState === 'inactive' || patient.sdkState === 'stale',
  ).length;

  return (
    <div className="mx-auto w-full max-w-[1400px] px-6 py-6">
      {isDemoMode() ? <DemoBanner /> : null}

      <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[15px] font-medium text-text-primary">Patient panel</h1>
          <p className="mt-1 text-[13px] text-text-secondary">
            {patients.length} patients monitored. {needsAttention} outside their own range.{' '}
            {notReporting} not reporting.
          </p>
        </div>

        <div className="flex gap-6">
          <Headline value={open.length} label="open alerts" />
          <Headline value={needsAttention} label="need review" />
          <Headline value={notReporting} label="sensor gaps" />
        </div>
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]">
        <PatientRiskPanel patients={patients} />
        <AlertFeed alerts={alerts} maxRows={14} />
      </div>
    </div>
  );
}

function Headline({ value, label }: { value: number; label: string }) {
  return (
    <div className="flex flex-col items-end">
      <span className="readout font-mono text-2xl text-text-primary">{value}</span>
      <span className="label">{label}</span>
    </div>
  );
}

/**
 * Says plainly that the numbers are generated.
 *
 * A demo dashboard that looks identical to a live one is the kind of thing
 * that ends up in a screenshot in a pitch deck and then in a conversation with
 * a regulator. The banner makes that impossible.
 */
function DemoBanner() {
  return (
    <div className="mb-5 border border-alert-medium/40 bg-alert-medium/[0.06] px-4 py-2.5">
      <p className="text-[12px] text-alert-medium">
        Demo data. The backend services are not connected, so every score, alert and patient on
        this page is generated. No real patient data is shown anywhere in this deployment.
      </p>
    </div>
  );
}
