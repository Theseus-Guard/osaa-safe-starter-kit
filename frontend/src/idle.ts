// Inactivity limit for a signed-in browser session. The server ends a session 60 minutes after its last token renewal,
// so the limit matches it. The warning gives five minutes' notice before the browser signs out.
export const IDLE_LIMIT_MS = 60 * 60 * 1000;
export const IDLE_WARNING_MS = 5 * 60 * 1000;
const CHECK_INTERVAL_MS = 15 * 1000;
const ACTIVITY_EVENTS = ["pointerdown", "keydown", "scroll", "mousemove"] as const;

let lastActivity = Date.now();

// Called on user input and on every API request, so a page that is only polling the API is not idle.
export function recordActivity(): void {
  lastActivity = Date.now();
}

interface IdleHandlers {
  onWarning: (showing: boolean) => void;
  onExpired: () => void;
}

// Reports onWarning(true) once the session has been idle for IDLE_LIMIT_MS - IDLE_WARNING_MS, and onWarning(false)
// as soon as activity resumes. Calls onExpired() once the session has been idle for IDLE_LIMIT_MS, then stops.
// Returns a function that stops the watch.
export function watchIdle(handlers: IdleHandlers): () => void {
  let warning = false;
  recordActivity();

  const onActivity = () => {
    recordActivity();
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
  for (const event of ACTIVITY_EVENTS) window.addEventListener(event, onActivity, { passive: true });

  function stop(): void {
    window.clearInterval(timer);
    for (const event of ACTIVITY_EVENTS) window.removeEventListener(event, onActivity);
  }
  return stop;
}
