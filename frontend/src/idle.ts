// Inactivity limit for a signed-in browser session. The server ends a session 60 minutes after its last token renewal,
// so the limit matches it. The warning gives five minutes' notice before the browser signs out.
export const IDLE_LIMIT_MS = 60 * 60 * 1000;
export const IDLE_WARNING_MS = 5 * 60 * 1000;
const CHECK_INTERVAL_MS = 15 * 1000;

let lastActivity = Date.now();
let onActivity: (() => void) | null = null;

// Only API requests count as activity. Mouse and keyboard use do not, so a user who only reads the page is signed out.
export function recordActivity(): void {
  lastActivity = Date.now();
  onActivity?.();
}

interface IdleHandlers {
  onWarning: (showing: boolean) => void;
  onExpired: () => void;
}

// Reports onWarning(true) once the session has been idle for IDLE_LIMIT_MS - IDLE_WARNING_MS, and onWarning(false)
// as soon as an API request is made. Calls onExpired() once the session has been idle for IDLE_LIMIT_MS, then stops.
// Returns a function that stops the watch.
export function watchIdle(handlers: IdleHandlers): () => void {
  let warning = false;
  recordActivity();

  onActivity = () => {
    if (warning) {
      warning = false;
      handlers.onWarning(false);
    }
  };

  const check = () => {
    const idle = Date.now() - lastActivity;
    if (idle >= IDLE_LIMIT_MS) {
      stop();
      handlers.onExpired();
      return;
    }
    const shouldWarn = idle >= IDLE_LIMIT_MS - IDLE_WARNING_MS;
    if (shouldWarn !== warning) {
      warning = shouldWarn;
      handlers.onWarning(shouldWarn);
    }
  };

  const timer = window.setInterval(check, CHECK_INTERVAL_MS);

  function stop(): void {
    window.clearInterval(timer);
    onActivity = null;
  }
  return stop;
}
