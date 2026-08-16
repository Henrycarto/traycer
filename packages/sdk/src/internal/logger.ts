import type { LogLevel, Logger } from '../types';

const LEVEL_ORDER: Record<LogLevel, number> = {
  debug: 10,
  info: 20,
  warn: 30,
  error: 40,
  silent: 100,
};

/**
 * Console logger with a level gate.
 *
 * Context objects are passed through untouched. Sensor modules are
 * responsible for never putting a coordinate, a package name or a raw sample
 * into a log context, and the data minimizer asserts that separately for
 * anything that reaches the queue.
 */
export function createLogger(level: LogLevel = 'warn'): Logger {
  const threshold = LEVEL_ORDER[level];

  const emit = (
    method: 'debug' | 'info' | 'warn' | 'error',
    message: string,
    context?: Record<string, unknown>,
  ): void => {
    if (LEVEL_ORDER[method] < threshold) return;
    const line = `[traycer] ${message}`;
    if (context) {
      // eslint-disable-next-line no-console
      console[method](line, context);
    } else {
      // eslint-disable-next-line no-console
      console[method](line);
    }
  };

  return {
    debug: (message, context) => emit('debug', message, context),
    info: (message, context) => emit('info', message, context),
    warn: (message, context) => emit('warn', message, context),
    error: (message, context) => emit('error', message, context),
  };
}

export const silentLogger: Logger = {
  debug: () => undefined,
  info: () => undefined,
  warn: () => undefined,
  error: () => undefined,
};
