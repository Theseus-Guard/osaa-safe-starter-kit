import { useEffect, useState } from "react";
import { CaseLink, CaseLinkType, listCaseLinks } from "../../api";
import { Badge } from "@/components/ui/badge";

interface Props {
  caseId: string;
}

const TYPE_LABELS: Record<CaseLinkType, string> = {
  related: "Related",
  duplicate: "Duplicate",
  root_sub: "Root and sub-case",
};

// Shown under the case title when the case has links. The list is the same one the Links tab shows, without the forms.
export function CaseLinkedCases({ caseId }: Props) {
  const [items, setItems] = useState<CaseLink[] | null>(null);

  useEffect(() => {
    let stale = false;
    listCaseLinks(caseId)
      .then((result) => {
        if (!stale) setItems(result);
      })
      .catch(() => {
        if (!stale) setItems([]);
      });
    return () => {
      stale = true;
    };
  }, [caseId]);

  if (items === null || items.length === 0) return null;
  const hidden = items.filter((item) => item.linked_case_id === null).length;

  return (
    <div className="flex flex-col gap-1 text-sm">
      <p className="font-medium">Linked cases</p>
      <ul className="flex flex-col gap-1">
        {items
          .filter((item) => item.linked_case_id !== null)
          .map((item) => (
            <li key={item.linked_case_id} className="flex flex-wrap items-center gap-2">
              <a className="underline-offset-4 hover:underline" href={`#/cases/${encodeURIComponent(item.linked_case_id ?? "")}`}>
                {item.linked_case_title}
              </a>
              <Badge variant="outline">{TYPE_LABELS[item.link_type]}</Badge>
            </li>
          ))}
      </ul>
      {hidden > 0 && (
        <p className="text-muted-foreground">
          {hidden} linked {hidden === 1 ? "case is" : "cases are"} not available to your account.
        </p>
      )}
    </div>
  );
}
