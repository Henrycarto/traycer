export const metadata = { title: 'Sign in · Traycer' };

/**
 * Clinician sign in.
 *
 * There is no Traycer password field on this page and there never will be.
 * Authentication goes through the hospital's own SMART on FHIR authorization
 * server, which means access follows the clinician's EHR account: revoked
 * there, revoked here, the same afternoon.
 */
export default function LoginPage({
  searchParams,
}: {
  searchParams: { error?: string; iss?: string };
}) {
  const defaultIssuer = process.env.NEXT_PUBLIC_FHIR_ISSUER ?? '';

  return (
    <div className="mx-auto flex w-full max-w-[560px] flex-col px-6 py-20">
      <h1 className="text-[15px] font-medium text-text-primary">Sign in through your EHR</h1>
      <p className="mt-3 text-[13px] leading-relaxed text-text-secondary">
        Traycer does not hold clinician credentials. Sign in against your hospital&apos;s FHIR
        server and Traycer receives a scoped, expiring token. Your access to the dashboard ends
        when your EHR account does.
      </p>

      {searchParams.error ? (
        <div className="mt-5 border border-alert-high/40 bg-alert-high/[0.06] px-4 py-2.5">
          <p className="font-mono text-[12px] text-alert-high">{decodeSafely(searchParams.error)}</p>
        </div>
      ) : null}

      <form action="/api/fhir/launch" method="GET" className="mt-8 flex flex-col gap-4">
        <label className="flex flex-col gap-1.5">
          <span className="label">FHIR base URL</span>
          <input
            type="url"
            name="iss"
            required
            defaultValue={searchParams.iss ?? defaultIssuer}
            placeholder="https://fhir.your-hospital.example/r4"
            className="field"
          />
        </label>

        <p className="text-[12px] leading-relaxed text-text-muted">
          If you opened Traycer from inside your EHR, this field is filled in for you and the
          launch context is carried through automatically.
        </p>

        <button type="submit" className="btn-primary self-start">
          Continue to your EHR
        </button>
      </form>

      <div className="mt-10 border-t border-line pt-6">
        <h2 className="label">What Traycer requests</h2>
        <ul className="mt-3 flex flex-col gap-2 font-mono text-[12px] text-text-secondary">
          <li>openid fhirUser profile</li>
          <li>patient/Patient.read</li>
          <li>patient/Observation.read</li>
          <li>offline_access</li>
        </ul>
        <p className="mt-4 max-w-[60ch] text-[12px] leading-relaxed text-text-muted">
          Read only. Traycer writes nothing back to the record and stores no demographics of its
          own. Names and identifiers are read from the EHR at display time and held only for the
          length of the request.
        </p>
      </div>
    </div>
  );
}

function decodeSafely(value: string): string {
  try {
    return decodeURIComponent(value).slice(0, 300);
  } catch {
    return 'sign in failed';
  }
}
