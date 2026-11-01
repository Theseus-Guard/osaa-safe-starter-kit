import { createContext, ReactNode, useContext, useEffect, useMemo, useState } from "react";
import { getSystemTimezone, SystemTimezone } from "./api";
import { describeError } from "./messages";

interface SystemTimezoneContextValue {
  // The active timezone. Null until it has loaded; display code then falls back to UTC.
  timezone: string | null;
  effectiveFrom: string | null;
  error: string | null;
  // Replaces the loaded value, for example with the response of a successful save.
  setCurrent: (value: SystemTimezone) => void;
  // Fetches the value again. Used after a 409 conflict.
  reload: () => void;
}

const SystemTimezoneContext = createContext<SystemTimezoneContextValue | null>(null);

export function SystemTimezoneProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<SystemTimezone | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let stale = false;
    getSystemTimezone()
      .then((value) => {
        if (stale) return;
        setCurrent(value);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [reloadToken]);

  const value = useMemo<SystemTimezoneContextValue>(
    () => ({
      timezone: current?.timezone ?? null,
      effectiveFrom: current?.effective_from ?? null,
      error,
      setCurrent,
      reload: () => setReloadToken((token) => token + 1),
    }),
    [current, error],
  );

  return <SystemTimezoneContext.Provider value={value}>{children}</SystemTimezoneContext.Provider>;
}

export function useSystemTimezone(): SystemTimezoneContextValue {
  const value = useContext(SystemTimezoneContext);
  if (value === null) throw new Error("useSystemTimezone must be used inside SystemTimezoneProvider");
  return value;
}
