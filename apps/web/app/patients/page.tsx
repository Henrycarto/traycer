import { PatientRiskPanel } from '@/components/pulse/PatientRiskPanel';
import { fetchPatientPanel } from '@/lib/api';

export const metadata = { title: 'Patients · Traycer' };
export const dynamic = 'force-dynamic';

export default async function PatientsPage() {
  const patients = await fetchPatientPanel();

  return (
    <div className="mx-auto w-full max-w-[1100px] px-6 py-6">
      <div className="mb-5">
        <h1 className="text-[15px] font-medium text-text-primary">Patients</h1>
        <p className="mt-1 text-[13px] text-text-secondary">
          Sorted for triage: critical first, then elevated by score, then everyone within range.
          Patients with no usable data sit at the bottom.
        </p>
      </div>

      <PatientRiskPanel patients={patients} />
    </div>
  );
}
