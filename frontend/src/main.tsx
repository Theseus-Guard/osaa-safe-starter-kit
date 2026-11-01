import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { initTheme, useResolvedTheme } from "./theme";
import "./index.css";

// Applied before the first render so the page does not flash in the wrong theme.
initTheme();

function Root() {
  const resolved = useResolvedTheme();
  return (
    <TooltipProvider>
      <App />
      <Toaster theme={resolved} />
    </TooltipProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
