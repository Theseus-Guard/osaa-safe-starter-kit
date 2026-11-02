import { useEffect, useState } from "react";
import { demoStatus } from "../api";

// A thin strip shown only when the server reports demo mode with the synthetic dataset. While the answer is pending,
// or when the request fails, nothing is shown: the banner never claims demo mode on its own.
export function DemoBanner() {
  const [demo, setDemo] = useState(false);

  useEffect(() => {
    let active = true;
    demoStatus()
      .then((status) => {
        if (active) setDemo(status.demo);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);

  if (!demo) return null;
  return (
    <div role="status" className="bg-amber-400 px-2 py-0.5 text-center text-xs font-semibold tracking-wide text-black">
      DEMO / SYNTHETIC DATA
    </div>
  );
}
