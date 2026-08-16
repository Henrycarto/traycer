import { NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { buildAuthorizationRequest } from '@/lib/fhir';

export const dynamic = 'force-dynamic';

const FLOW_COOKIE = 'traycer_smart_flow';
const FLOW_TTL_SECONDS = 600;

/**
 * Start the SMART on FHIR authorization code flow.
 *
 * Handles both launch modes. An EHR launch arrives with `iss` and `launch`
 * query parameters set by the EHR itself. A standalone launch arrives from the
 * sign in form with only `iss`.
 *
 * The state and PKCE verifier go into one short lived httpOnly cookie rather
 * than a server side session. The flow lasts under a minute, so a cookie is
 * the whole state store the dashboard needs, and it keeps the app horizontally
 * scalable without a shared session backend.
 */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const issuer = url.searchParams.get('iss') ?? process.env.FHIR_ISSUER ?? '';
  const launch = url.searchParams.get('launch') ?? undefined;

  if (!issuer) {
    return redirectToLogin('No FHIR base URL was provided.');
  }

  if (!/^https:\/\//i.test(issuer) && !issuer.startsWith('http://localhost')) {
    return redirectToLogin('The FHIR base URL must use https.');
  }

  try {
    const authorization = await buildAuthorizationRequest({ issuer, launch });

    cookies().set({
      name: FLOW_COOKIE,
      value: JSON.stringify({
        state: authorization.state,
        codeVerifier: authorization.codeVerifier,
        issuer,
      }),
      httpOnly: true,
      secure: process.env.NODE_ENV === 'production',
      sameSite: 'lax',
      path: '/',
      maxAge: FLOW_TTL_SECONDS,
    });

    return NextResponse.redirect(authorization.url);
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'could not start the authorization flow';
    return redirectToLogin(message);
  }
}

function redirectToLogin(message: string): NextResponse {
  const target = new URL('/login', process.env.APP_BASE_URL ?? 'http://localhost:3000');
  target.searchParams.set('error', message);
  return NextResponse.redirect(target);
}
