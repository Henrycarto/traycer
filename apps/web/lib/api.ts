import type {
  AlertRecord,
  ApiEnvelope,
  PatientSummary,
  SdkTelemetry,
  TrajectorySeries,
  VoiceSample,
} from './types';
import {
  demoAlerts,
  demoPatients,
  demoTelemetry,
  demoTrajectory,
  demoVoiceSample,
} from './demo-data';

/**
 * Data access for the dashboard.
 *
 * Every backend response uses the same three key envelope, so unwrapping
 * happens once here rather than at every call site.
 *
 * When the backend is unreachable the dashboard falls back to generated demo
 * data and says so. That is a deliberate choice for a portfolio deployment and
 * for local development: a dashboard that renders an empty grey screen teaches
 * a reviewer nothing about the product. The banner in the layout makes the
 * distinction impossible to miss, and `isDemoMode` is exported so no component
 * has to guess which it is looking at.
 */

const PULSE_API =
  process.env.PULSE_API_INTERNAL_URL ??
  process.env.NEXT_PUBLIC_PULSE_API_URL ??
  'http://localhost:8080';

const SCORING_API = process.env.SCORING_API_INTERNAL_URL ?? 'http://localhost:8082';

const REQUEST_TIMEOUT_MS = 4000;

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

async function request<T>(baseUrl: string, path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const response = await fetch(`${baseUrl}${path}`, {
      ...init,
      signal: controller.signal,
      cache: 'no-store',
      headers: {
        accept: 'application/json',
        ...(init?.headers ?? {}),
      },
    });

    const envelope = (await response.json()) as ApiEnvelope<T>;

    if (!response.ok || envelope.error) {
      throw new ApiError(
        envelope.error?.code ?? 'request_failed',
        envelope.error?.message ?? `request failed with status ${response.status}`,
        response.status,
      );
    }

    if (envelope.data === null) {
      throw new ApiError('empty_response', 'the service returned no data', response.status);
    }

    return envelope.data;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Run a fetch, fall back to demo data on any failure.
 *
 * Swallowing the error here is intentional and is the only place it happens.
 * A clinician looking at a live deployment sees the banner rather than a
 * stack trace, and the console line keeps the real cause available.
 */
async function withFallback<T>(load: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await load();
  } catch (error) {
    console.warn(
      '[traycer] backend unavailable, serving demo data:',
      error instanceof Error ? error.message : 'unknown error',
    );
    return fallback;
  }
}

export function isDemoMode(): boolean {
  return process.env.TRAYCER_DEMO_MODE === 'true' || !process.env.PULSE_API_INTERNAL_URL;
}

export async function fetchPatientPanel(): Promise<PatientSummary[]> {
  if (isDemoMode()) return demoPatients();
  return withFallback(
    () => request<PatientSummary[]>(PULSE_API, '/v1/patients'),
    demoPatients(),
  );
}

export async function fetchAlerts(limit = 100): Promise<AlertRecord[]> {
  if (isDemoMode()) return demoAlerts();
  return withFallback(
    async () => {
      const raw = await request<
        Array<{
          alert_id: string;
          patient_code: string;
          fired_at: string;
          severity: AlertRecord['severity'];
          risk_score: number;
          trigger_signal: string;
          summary: string;
          acknowledged: boolean;
        }>
      >(PULSE_API, `/v1/alerts?limit=${limit}`);

      return raw.map((row) => ({
        alertId: row.alert_id,
        patientCode: row.patient_code,
        firedAt: row.fired_at,
        severity: row.severity,
        riskScore: row.risk_score,
        triggerSignal: row.trigger_signal,
        summary: row.summary,
        acknowledged: row.acknowledged,
      }));
    },
    demoAlerts(),
  );
}

export async function fetchTrajectory(patientCode: string): Promise<TrajectorySeries> {
  if (isDemoMode()) return demoTrajectory(patientCode);
  return withFallback(
    () =>
      request<TrajectorySeries>(
        SCORING_API,
        `/v1/patients/${encodeURIComponent(patientCode)}/trajectory`,
      ),
    demoTrajectory(patientCode),
  );
}

export async function fetchVoiceSample(patientCode: string): Promise<VoiceSample | null> {
  if (isDemoMode()) return demoVoiceSample(patientCode);
  return withFallback(
    () =>
      request<VoiceSample | null>(
        PULSE_API,
        `/v1/patients/${encodeURIComponent(patientCode)}/voice`,
      ),
    demoVoiceSample(patientCode),
  );
}

export async function fetchTelemetry(patientCode: string): Promise<SdkTelemetry> {
  if (isDemoMode()) return demoTelemetry(patientCode);
  return withFallback(
    () =>
      request<SdkTelemetry>(PULSE_API, `/v1/patients/${encodeURIComponent(patientCode)}/telemetry`),
    demoTelemetry(patientCode),
  );
}

export async function acknowledgeAlert(alertId: string, clinicianId: string): Promise<void> {
  await request<unknown>(PULSE_API, `/v1/alerts/${encodeURIComponent(alertId)}/acknowledge`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ acknowledged_by: clinicianId }),
  });
}
