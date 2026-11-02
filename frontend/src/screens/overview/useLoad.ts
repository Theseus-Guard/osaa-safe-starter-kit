import { useEffect, useState } from "react";
import { describeError } from "../../messages";

export type LoadState<T> = { status: "loading" } | { status: "error"; message: string } | { status: "ready"; data: T };

// Runs load() whenever deps change. A response that arrives after the deps changed is dropped, so an old range
// never overwrites a newer one.
export function useLoad<T>(load: () => Promise<T>, deps: readonly unknown[]): LoadState<T> {
  const [state, setState] = useState<LoadState<T>>({ status: "loading" });

  useEffect(() => {
    let stale = false;
    setState({ status: "loading" });
    load().then(
      (data) => {
        if (!stale) setState({ status: "ready", data });
      },
      (caught: unknown) => {
        if (!stale) setState({ status: "error", message: describeError(caught) });
      },
    );
    return () => {
      stale = true;
    };
    // The caller lists every value load() reads in deps.
  }, deps);

  return state;
}
