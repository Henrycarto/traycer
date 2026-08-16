import { AlertFeed } from '@/components/pulse/AlertFeed';
import { fetchAlerts } from '@/lib/api';

export const metadata = { title: 'Alerts · Traycer' };
export const dynamic = 'force-dynamic';

export default async function AlertsPage() {
  const alerts = await fetchAlerts(200);

  const open = alerts.filter((alert) => !alert.acknowledged);
  const critical = open.filter((alert) => alert.severity === 'critical').length;
  const elevated = open.filter((alert) => alert.severity === 'elevated').length;

  return (
    <div className="mx-auto w-full max-w-[1100px] px-6 py-6">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[15px] font-medium text-text-primary">Alerts</h1>
          <p className="mt-1 max-w-[68ch] text-[13px] text-text-secondary">
            Every alert fired across the panel. An alert requires a deviation to hold across
            consecutive scoring windows, so a single unusual day does not appear here.
          </p>
        </div>

        <div className="flex gap-6">
          <Figure label="critical" value={String(critical)} color="text-alert-high" />
          <Figure label="elevated" value={String(elevated)} color="text-alert-medium" />
          <Figure label="acknowledged" value={String(alerts.length - open.length)} color="text-text-muted" />
        </div>
      </div>

      <AlertFeed alerts={alerts} />
    </div>
  );
}

function Figure({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div className="flex flex-col items-end">
      <span className={`readout font-mono text-xl ${color}`}>{value}</span>
      <span className="label">{label}</span>
    </div>
  );
}
