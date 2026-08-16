import { NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { exchangeCodeForToken, safeEquals } from '@/lib/fhir';

export const dynamic = 'force-dynamic';

const FLOW_COOKIE = 'traycer_smart_flow';
const SESSION_COOKIE = 'traycer_session';

interface FlowState {
  state: string;
  codeVerifier: string;
  issuer: string;
}

/**
 * Authorization callback.
 *
 * Exchanges the code for an access token and stores the result in an httpOnly
 * session cookie. The access token never reaches client side JavaScript: every
 * FHIR read happens in a server component or a route handler, so an XSS bug in
 * the dashboard cannot exfiltrate a token that carries patient read scope.
 */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const code = url.searchParams.get('code');
  const state = url.searchParams.get('state');
  const error = url.searchParams.get('error');

  if (error) {
    const description = url.searchParams.get('error_description') ?? error;
    return failure(description);
  }

  if (!code || !state) {
    return failure('The authorization server returned no code.');
  }

  const jar = cookies();
  const raw = jar.get(FLOW_COOKIE)?.value;
  if (!raw) {
    return failure('The sign in attempt expired. Start again.');
  }

  let flow: FlowState;
  try {
    flow = JSON.parse(raw) as FlowState;
  } catch {
    return failure('The sign in state was unreadable. Start again.');
  }

  // Constant time compare, and the cookie is cleared whatever the outcome so a
  // replayed callback cannot reuse the same verifier.
  jar.delete(FLOW_COOKIE);

  if (!safeEquals(flow.state, state)) {
    return failure('The authorization state did not match. Start again.');
  }

  try {
    const token = await exchangeCodeForToken({
      issuer: flow.issuer,
      code,
      codeVerifier: flow.codeVerifier,
    });

    jar.set({
      name: SESSION_COOKIE,
      value: JSON.stringify({
        issuer: flow.issuer,
        accessToken: token.access_token,
        refreshToken: token.refresh_token ?? null,
        fhirUser: token.fhirUser ?? null,
        scope: token.scope,
        expiresAt: Date.now() + token.expires_in * 1000,
      }),
      httpOnly: true,
      secure: process.env.NODE_ENV === 'production',
      sameSite: 'lax',
      path: '/',
      // The cookie outlives the access token so the refresh token is still
      // reachable when the access token expires mid shift.
      maxAge: token.refresh_token ? 60 * 60 * 12 : token.expires_in,
    });

    return NextResponse.redirect(new URL('/dashboard', baseUrl()));
  } catch (caught) {
    const message =
      caught instanceof Error ? caught.message : 'the token exchange failed';
    return failure(message);
  }
}

function failure(message: string): NextResponse {
  const target = new URL('/login', baseUrl());
  target.searchParams.set('error', message.slice(0, 300));
  return NextResponse.redirect(target);
}

function baseUrl(): string {
  return process.env.APP_BASE_URL ?? 'http://localhost:3000';
}
