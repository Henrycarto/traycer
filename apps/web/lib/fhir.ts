import 'server-only';

/**
 * SMART on FHIR OAuth 2.0 for the Pulse dashboard.
 *
 * Clinicians do not get a Traycer password. They authenticate against the
 * hospital's own authorization server and Traycer receives a scoped access
 * token. That is the only arrangement a hospital information security review
 * will accept, and it means an offboarded clinician loses dashboard access the
 * moment their EHR account is disabled rather than whenever someone remembers
 * to tell us.
 *
 * The flow implemented here is authorization code with PKCE, which SMART App
 * Launch 2.0 requires for all clients including confidential ones. Endpoints
 * are discovered from the issuer rather than configured, so a hospital can
 * rotate them without a redeploy.
 *
 * Two launch modes are supported:
 *
 *   EHR launch      the EHR opens Traycer with `iss` and `launch` parameters
 *   Standalone      the clinician opens Traycer directly and picks a context
 */

export interface SmartConfiguration {
  issuer?: string;
  authorization_endpoint: string;
  token_endpoint: string;
  jwks_uri?: string;
  scopes_supported?: string[];
  code_challenge_methods_supported?: string[];
  capabilities?: string[];
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  scope: string;
  refresh_token?: string;
  id_token?: string;
  patient?: string;
  encounter?: string;
  fhirUser?: string;
  need_patient_banner?: boolean;
}

export interface AuthorizationRequest {
  url: string;
  state: string;
  codeVerifier: string;
}

export class SmartConfigurationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SmartConfigurationError';
  }
}

export class TokenExchangeError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'TokenExchangeError';
    this.status = status;
  }
}

const DISCOVERY_TTL_MS = 60 * 60 * 1000;

const discoveryCache = new Map<string, { document: SmartConfiguration; fetchedAt: number }>();

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new SmartConfigurationError(`${name} is not set`);
  }
  return value;
}

export function defaultScopes(): string {
  return (
    process.env.FHIR_SCOPES ??
    [
      'launch',
      'openid',
      'fhirUser',
      'profile',
      'patient/Patient.read',
      'patient/Observation.read',
      'offline_access',
    ].join(' ')
  );
}

/**
 * Read the issuer's SMART configuration.
 *
 * Falls back to the OpenID document because some FHIR servers publish only
 * that. Cached for an hour, which is long enough to keep the dashboard off
 * the EHR's discovery endpoint and short enough that an endpoint change takes
 * effect the same shift.
 */
export async function discoverSmartConfiguration(issuer: string): Promise<SmartConfiguration> {
  const normalised = issuer.replace(/\/+$/, '');
  const cached = discoveryCache.get(normalised);
  if (cached && Date.now() - cached.fetchedAt < DISCOVERY_TTL_MS) {
    return cached.document;
  }

  const candidates = [
    `${normalised}/.well-known/smart-configuration`,
    `${normalised}/.well-known/openid-configuration`,
  ];

  for (const url of candidates) {
    const response = await fetch(url, {
      headers: { accept: 'application/json' },
      cache: 'no-store',
    }).catch(() => null);

    if (!response || !response.ok) continue;

    const document = (await response.json()) as SmartConfiguration;
    if (!document.authorization_endpoint || !document.token_endpoint) continue;

    discoveryCache.set(normalised, { document, fetchedAt: Date.now() });
    return document;
  }

  throw new SmartConfigurationError(
    `could not discover SMART endpoints from ${normalised}. Check that the FHIR base URL is correct and reachable.`,
  );
}

/**
 * Build the authorization redirect.
 *
 * Returns the state and the PKCE verifier alongside the URL. The caller stores
 * both in an httpOnly cookie and hands them back at the callback. Keeping them
 * out of this function means no shared server side session store is needed for
 * a flow that lasts thirty seconds.
 */
export async function buildAuthorizationRequest(options: {
  issuer: string;
  launch?: string;
  scopes?: string;
}): Promise<AuthorizationRequest> {
  const config = await discoverSmartConfiguration(options.issuer);

  const clientId = requiredEnv('FHIR_CLIENT_ID');
  const redirectUri = requiredEnv('FHIR_REDIRECT_URI');

  const state = randomToken(32);
  const codeVerifier = randomToken(64);
  const codeChallenge = await pkceChallenge(codeVerifier);

  const params = new URLSearchParams({
    response_type: 'code',
    client_id: clientId,
    redirect_uri: redirectUri,
    scope: options.scopes ?? defaultScopes(),
    state,
    // `aud` is required by SMART App Launch and is the FHIR base URL. An
    // authorization server that ignores it is not SMART conformant, and one
    // that checks it prevents a token minted for another app being replayed
    // against this one.
    aud: options.issuer.replace(/\/+$/, ''),
    code_challenge: codeChallenge,
    code_challenge_method: 'S256',
  });

  if (options.launch) {
    params.set('launch', options.launch);
  }

  return {
    url: `${config.authorization_endpoint}?${params.toString()}`,
    state,
    codeVerifier,
  };
}

/** Exchange the authorization code for an access token. */
export async function exchangeCodeForToken(options: {
  issuer: string;
  code: string;
  codeVerifier: string;
}): Promise<TokenResponse> {
  const config = await discoverSmartConfiguration(options.issuer);

  const clientId = requiredEnv('FHIR_CLIENT_ID');
  const redirectUri = requiredEnv('FHIR_REDIRECT_URI');
  const clientSecret = process.env.FHIR_CLIENT_SECRET;

  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code: options.code,
    redirect_uri: redirectUri,
    client_id: clientId,
    code_verifier: options.codeVerifier,
  });

  const headers: Record<string, string> = {
    'content-type': 'application/x-www-form-urlencoded',
    accept: 'application/json',
  };

  // Confidential clients authenticate with HTTP Basic. Public clients send
  // the client id in the body only, which the block above already does.
  if (clientSecret) {
    const credentials = Buffer.from(`${clientId}:${clientSecret}`).toString('base64');
    headers.authorization = `Basic ${credentials}`;
  }

  const response = await fetch(config.token_endpoint, {
    method: 'POST',
    headers,
    body: body.toString(),
    cache: 'no-store',
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new TokenExchangeError(
      `token exchange failed: ${detail.slice(0, 300)}`,
      response.status,
    );
  }

  return (await response.json()) as TokenResponse;
}

/** Refresh an access token using the refresh token from offline_access. */
export async function refreshAccessToken(options: {
  issuer: string;
  refreshToken: string;
}): Promise<TokenResponse> {
  const config = await discoverSmartConfiguration(options.issuer);
  const clientId = requiredEnv('FHIR_CLIENT_ID');
  const clientSecret = process.env.FHIR_CLIENT_SECRET;

  const headers: Record<string, string> = {
    'content-type': 'application/x-www-form-urlencoded',
    accept: 'application/json',
  };
  if (clientSecret) {
    headers.authorization = `Basic ${Buffer.from(`${clientId}:${clientSecret}`).toString('base64')}`;
  }

  const response = await fetch(config.token_endpoint, {
    method: 'POST',
    headers,
    body: new URLSearchParams({
      grant_type: 'refresh_token',
      refresh_token: options.refreshToken,
      client_id: clientId,
    }).toString(),
    cache: 'no-store',
  });

  if (!response.ok) {
    throw new TokenExchangeError(
      `refresh failed with status ${response.status}`,
      response.status,
    );
  }

  return (await response.json()) as TokenResponse;
}

/**
 * Read a FHIR resource with the clinician's access token.
 *
 * Traycer stores no patient demographics of its own. Names, dates of birth
 * and record numbers are read from the EHR at display time and never written
 * to a Traycer database, which is why the platform only ever holds a
 * pseudonymous patient code.
 */
export async function readFhirResource<T>(options: {
  issuer: string;
  accessToken: string;
  resourcePath: string;
}): Promise<T> {
  const url = `${options.issuer.replace(/\/+$/, '')}/${options.resourcePath.replace(/^\/+/, '')}`;

  const response = await fetch(url, {
    headers: {
      authorization: `Bearer ${options.accessToken}`,
      accept: 'application/fhir+json',
    },
    cache: 'no-store',
  });

  if (!response.ok) {
    throw new TokenExchangeError(
      `FHIR read failed with status ${response.status}`,
      response.status,
    );
  }

  return (await response.json()) as T;
}

/** URL safe random token from the platform CSPRNG. */
export function randomToken(bytes: number): string {
  const buffer = new Uint8Array(bytes);
  crypto.getRandomValues(buffer);
  return base64UrlEncode(buffer);
}

/** S256 PKCE challenge for a verifier. */
export async function pkceChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return base64UrlEncode(new Uint8Array(digest));
}

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i] as number);
  }
  return Buffer.from(binary, 'binary')
    .toString('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}

/**
 * Constant time string comparison for the state parameter.
 *
 * A timing safe compare here is close to paranoia given how short the window
 * is, but the alternative costs one function and this one is written.
 */
export function safeEquals(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i += 1) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}
