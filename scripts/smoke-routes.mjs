/**
 * Route smoke test for the Pulse dashboard.
 *
 * This exists because `next build` passing does not mean the app runs. Both
 * patient routes once crashed at request time with "trajectoryState is not a
 * function", caused by importing a plain function out of a "use client"
 * module into a server component. The build compiled clean, the type checker
 * saw nothing wrong, and CI was green on every job. The failure only appears
 * when a page is actually requested, because those routes are dynamic and are
 * never rendered during the build.
 *
 * So this boots the production server and asks for every route. In production
 * a server component that throws returns a 500, which is the signal we check.
 * Each route also has to contain a marker string, because a page can return
 * 200 while rendering an empty shell.
 *
 * It runs the standalone server, `.next/standalone/apps/web/server.js`, and
 * not `next start`. Two reasons. `next start` does not work at all with the
 * `output: "standalone"` setting this app uses and answers 404 to everything,
 * which would make this test either useless or permanently red. More
 * importantly, the standalone bundle is the exact artifact the Dockerfile
 * copies and runs, so this exercises what actually ships.
 *
 * Static assets are not copied into the standalone tree by the build, so
 * requests for CSS and JS chunks will 404 during this test. That is expected
 * and does not affect what is being checked, which is whether the server
 * renders each route's HTML.
 *
 * Usage:
 *   npm run web:smoke                             requires a prior next build
 *   node scripts/smoke-routes.mjs --base <url>    test an already running server
 */

import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Resolved from this file rather than from the working directory, because npm
// runs workspace scripts with the cwd set to the workspace, not the repo root.
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const STANDALONE_DIR = resolve(REPO_ROOT, 'apps/web/.next/standalone');
const SERVER_ENTRY = 'apps/web/server.js';

const PORT = Number(process.env.SMOKE_PORT ?? 3210);
// The server binds to this address, and requests go to the same one. Using
// "localhost" on the request side is a trap: it can resolve to ::1 while the
// server listens on IPv4 only.
const HOST = '127.0.0.1';
const START_TIMEOUT_MS = 90_000;
const REQUEST_TIMEOUT_MS = 30_000;

/**
 * Every route the dashboard serves, with a string that proves the page
 * actually rendered its own content rather than an error shell.
 *
 * The two patient routes are the reason this file exists. Do not remove them.
 */
const ROUTES = [
  { path: '/', contains: 'Psychiatric relapse follows a detectable behavioral pattern' },
  { path: '/dashboard', contains: 'Patient panel' },
  { path: '/patients', contains: 'Sorted for triage' },
  { path: '/alerts', contains: 'Every alert fired across the panel' },
  { path: '/login', contains: 'Sign in through your EHR' },
  { path: '/patients/TRC-4f21a9c4', contains: '30 day behavioral trajectory' },
  { path: '/patients/TRC-4f21a9c4/trajectory', contains: 'Reading this chart' },
  // A patient with no established baseline. Exercises the null score path,
  // which renders a placeholder rather than a number.
  { path: '/patients/TRC-a83c6e10', contains: 'NO BASELINE' },
];

/**
 * Strings that mean the page rendered an error, whatever the status code.
 *
 * Deliberately short. "This page could not be found" was tried here and is
 * useless: the App Router embeds the not-found boundary in the RSC payload of
 * every page, so it matches on healthy routes too. A genuinely missing route
 * answers 404 and is caught by the status check instead.
 */
const ERROR_MARKERS = [
  'Unhandled Runtime Error',
  'Application error: a client-side exception',
  '__NEXT_ERROR_CODE',
];

const baseFlagIndex = process.argv.indexOf('--base');
const externalBase = baseFlagIndex > -1 ? process.argv[baseFlagIndex + 1] : null;

async function waitForServer(base) {
  const deadline = Date.now() + START_TIMEOUT_MS;

  while (Date.now() < deadline) {
    try {
      const response = await fetch(base, { signal: AbortSignal.timeout(5000) });
      // Any response at all means the server is listening. A non 200 here is
      // a real failure and the route loop below will report it properly.
      if (response.status > 0) return true;
    } catch {
      // Not up yet.
    }
    await sleep(1000);
  }

  return false;
}

async function checkRoute(base, route) {
  const url = `${base}${route.path}`;

  let response;
  try {
    response = await fetch(url, { signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
  } catch (error) {
    return { ok: false, detail: `request failed: ${error.message}` };
  }

  const body = await response.text();

  if (response.status !== 200) {
    return { ok: false, detail: `HTTP ${response.status}` };
  }

  const marker = ERROR_MARKERS.find((needle) => body.includes(needle));
  if (marker) {
    return { ok: false, detail: `error page rendered: "${marker}"` };
  }

  if (route.contains && !body.includes(route.contains)) {
    return { ok: false, detail: `missing expected content: "${route.contains}"` };
  }

  return { ok: true, detail: `${body.length} bytes` };
}

async function run() {
  const base = externalBase ?? `http://${HOST}:${PORT}`;
  let server = null;

  if (!externalBase) {
    if (!existsSync(resolve(STANDALONE_DIR, SERVER_ENTRY))) {
      console.error(
        `no standalone build at ${STANDALONE_DIR}.\n` +
          'Run "npm run build --workspace=@traycer/web" first, and make sure no ' +
          'dev server is running, because both write to apps/web/.next.',
      );
      return 1;
    }

    console.log(`starting the standalone server on port ${PORT}`);

    // Spawned without a shell so kill() reaches the process directly. With
    // shell:true on Windows the signal lands on cmd.exe and leaves the server
    // orphaned holding the port.
    server = spawn(process.execPath, [SERVER_ENTRY], {
      cwd: STANDALONE_DIR,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        NODE_ENV: 'production',
        PORT: String(PORT),
        HOSTNAME: HOST,
      },
    });

    // Both pipes are drained and forwarded. An unread stdout pipe fills its
    // buffer and blocks the child once the server logs enough, and swallowing
    // the output makes a failure here impossible to diagnose from a CI log.
    server.stdout.on('data', (chunk) => process.stderr.write(`[server] ${chunk}`));
    server.stderr.on('data', (chunk) => process.stderr.write(`[server] ${chunk}`));

    server.on('error', (error) => {
      console.error(`could not start the server: ${error.message}`);
    });
  }

  let failures = 0;

  try {
    if (!(await waitForServer(base))) {
      console.error(`server did not start within ${START_TIMEOUT_MS / 1000}s`);
      return 1;
    }

    console.log(`server ready at ${base}\n`);

    for (const route of ROUTES) {
      const result = await checkRoute(base, route);
      const status = result.ok ? 'pass' : 'FAIL';
      console.log(`${status.padEnd(5)} ${route.path.padEnd(40)} ${result.detail}`);
      if (!result.ok) failures += 1;
    }
  } finally {
    if (server) server.kill();
  }

  console.log('');
  if (failures > 0) {
    console.error(`${failures} of ${ROUTES.length} routes failed`);
    return 1;
  }

  console.log(`all ${ROUTES.length} routes rendered`);
  return 0;
}

// Exit explicitly. The child server keeps an open handle on the event loop
// briefly after kill(), and a smoke test that hangs in CI is worse than one
// that fails.
process.exit(await run());
