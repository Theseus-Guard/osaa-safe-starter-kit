import { ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

interface Props {
  // Pass a reason only while the control is locked. The reason shows on hover and keyboard focus.
  reason?: string;
  children: ReactNode;
}

// A disabled control does not receive pointer events, so the explanation is attached to a focusable wrapper.
export function DisabledHint({ reason, children }: Props) {
  if (!reason) return <>{children}</>;
  return (
    <Tooltip>
      <TooltipTrigger
        render={<span tabIndex={0} className="inline-flex rounded-md outline-none focus-visible:ring-3 focus-visible:ring-ring/50" />}
      >
        {children}
      </TooltipTrigger>
      <TooltipContent>{reason}</TooltipContent>
    </Tooltip>
  );
}
