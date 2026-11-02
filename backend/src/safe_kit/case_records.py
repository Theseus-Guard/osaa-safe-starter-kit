"""Case record: awareness events, impact assessments, legal duties, classification versions, and the preliminary report.

Every change runs in one transaction with its audit row. The case row is locked first, so changes to one case run one
after another; then cases.is_visible_on decides whether the caller may see or change the case. A case the caller may
not see is reported as not found, as everywhere else.

Free text (basis, scope, rationale, and the other notes) is never written to the audit log: only its length is.
Legal duties live apart from deadline_tasks and never change them: a legal due time is independent of the SAFE
deadlines (plans/01, R02).

Classification versions are never edited. An edit inserts the next version, so the history stays complete.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from safe_kit import audit, cases
from safe_kit.cases import Case, CaseScope
from safe_kit.db import connect
from safe_kit.idempotency import KeyScope, check, remember

# Awareness: the three facts that can start the clock (plans/01, timeline). Each is stored with its own time and basis.
AWARENESS_KINDS = frozenset({"operator", "affected_party", "external_report"})
# An incident is told apart from a near miss and from a blocked attempt (T01). Used for awareness events and for the
# case classification.
EVENT_KINDS = frozenset({"incident", "near_miss", "blocked_attempt"})

# The five compact trigger values of R01, in the order of the plan, and the explicit "none applies" value.
# They are the input of the classification. An empty trigger list means the triggers were not assessed.
NONE_APPLIES = "none_applies"
TRIGGERS = (
    "unauthorized_access_or_change",  # unauthorized access or change
    "boundary_escape",  # boundary escape
    "confidential_data_exposure",  # confidential data
    "out_of_scope_continuation",  # continuation outside the approved scope
    "harm_inaction_near_miss",  # harm, inaction, or near miss
    NONE_APPLIES,  # no trigger applies; it cannot be combined with another trigger
)

# Impact areas that every case must address (plans/01, impact section, and T03). An area with no active row is
# "not assessed"; severity "unknown" is the explicit value for an assessed area whose answer is not known yet.
IMPACT_AREAS = (
    "affected_people_groups",
    "severity",
    "scale",
    "duration",
    "reversibility",
    "disproportionate_effect",
)
IMPACT_SEVERITIES = frozenset({"none", "low", "medium", "high", "critical", "unknown"})

DUTY_KINDS = frozenset({"statutory", "contractual"})
DUTY_STATUSES = frozenset({"open", "done"})
DUTY_EDITABLE_FIELDS = ("description", "source_reference", "jurisdiction", "due_at", "status")

CLASSIFICATION_TEXT_FIELDS = (
    "classification_rationale",
    "responsible_operator",
    "affected_versions",
    "approved_scope",
    "scope_permission_source",
    "containment",
    "evidence_gap_plan",
)
# internal_incident_option is no longer a field: the external notice tasks are marked not applicable per task.
CLASSIFICATION_EDITABLE_FIELDS = (
    "triggers",
    "event_kind",
    *CLASSIFICATION_TEXT_FIELDS,
    "next_update_due_at",
)
# The fields the preliminary report needs. An empty one is marked as missing, never refused.
REPORT_CLASSIFICATION_FIELDS = (
    "triggers",
    "event_kind",
    "classification_rationale",
    "responsible_operator",
    "affected_versions",
    "approved_scope",
    "scope_permission_source",
    "containment",
    "evidence_gap_plan",
    "next_update_due_at",
)


class RecordOutcome(Enum):
    OK = "ok"
    # The case does not exist, is deleted, or is not visible to the caller. Also an unknown awareness or duty row.
    NOT_FOUND = "not_found"
    CLOSED = "closed"
    DUTY_NOT_FOUND = "duty_not_found"
    # The caller read an older classification version than the current one.
    VERSION_CONFLICT = "version_conflict"


@dataclass(frozen=True)
class AwarenessEvent:
    id: str
    case_id: str
    awareness_kind: str
    event_kind: str
    occurred_at: datetime
    basis: str
    recorded_by: str
    created_at: datetime


@dataclass(frozen=True)
class ImpactAssessment:
    """One impact area of a case. assessed is False when no active row exists; then the other fields are None."""

    area: str
    assessed: bool
    severity: str | None = None
    scope: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class LegalDuty:
    id: str
    case_id: str
    kind: str
    description: str
    source_reference: str
    jurisdiction: str | None
    due_at: datetime
    status: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class Classification:
    """One version of the classification. Version 0 means nothing has been recorded yet."""

    version: int
    triggers: tuple[str, ...] = ()
    event_kind: str | None = None
    classification_rationale: str | None = None
    responsible_operator: str | None = None
    affected_versions: str | None = None
    approved_scope: str | None = None
    scope_permission_source: str | None = None
    containment: str | None = None
    evidence_gap_plan: str | None = None
    next_update_due_at: datetime | None = None
    created_by: str | None = None
    created_at: datetime | None = None

    def editable_values(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in CLASSIFICATION_EDITABLE_FIELDS}


def normalize_triggers(triggers: Iterable[str]) -> tuple[str, ...]:
    """Distinct trigger values in the plan's order. A value outside TRIGGERS is refused, and so is none_applies
    combined with another value."""
    chosen = set(triggers)
    unknown = chosen - set(TRIGGERS)
    if unknown:
        # The names are not repeated: an error body never quotes the submitted values (api-draft 3.6).
        raise ValueError("unknown trigger: choose only values from the trigger list")
    if NONE_APPLIES in chosen and len(chosen) > 1:
        raise ValueError("none_applies cannot be combined with other triggers")
    return tuple(trigger for trigger in TRIGGERS if trigger in chosen)


def _marker(text: str | None) -> dict[str, int] | None:
    # Free text is never written to the audit log. Only its length is recorded.
    return None if text is None else {"length": len(text)}


def _audit_value(name: str, value: Any) -> Any:
    if name in CLASSIFICATION_TEXT_FIELDS or name in ("description", "source_reference", "jurisdiction"):
        return _marker(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return list(value)
    return value


def classification_changes(old: Classification, new_values: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The editable fields whose value differs, as {field: {"old": ..., "new": ...}} with text reduced to lengths."""
    changes: dict[str, dict[str, Any]] = {}
    for name in CLASSIFICATION_EDITABLE_FIELDS:
        before = getattr(old, name)
        after = new_values[name]
        if name == "triggers":
            before, after = tuple(before), tuple(after)
        if before != after:
            changes[name] = {"old": _audit_value(name, before), "new": _audit_value(name, after)}
    return changes


def duty_changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: {"old": _audit_value(name, old[name]), "new": _audit_value(name, new[name])}
        for name in DUTY_EDITABLE_FIELDS
        if old[name] != new[name]
    }


def impacts_in_order(assessed: Mapping[str, ImpactAssessment]) -> list[ImpactAssessment]:
    """Every impact area, in the plan's order. An area missing from the mapping is returned as not assessed."""
    return [assessed.get(area, ImpactAssessment(area=area, assessed=False)) for area in IMPACT_AREAS]


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == ()


def missing_fields(
    classification: Classification,
    awareness_events: list[AwarenessEvent],
    impacts: list[ImpactAssessment],
    contact_recorded: bool,
) -> list[dict[str, str]]:
    """What the preliminary report still lacks. Each entry has a field name and a state: missing or unknown."""
    missing: list[dict[str, str]] = []
    if not contact_recorded:
        missing.append({"field": "contact", "state": "missing"})
    for name in REPORT_CLASSIFICATION_FIELDS:
        if _is_empty(getattr(classification, name)):
            missing.append({"field": f"classification.{name}", "state": "missing"})
    if not awareness_events:
        missing.append({"field": "awareness_events", "state": "missing"})
    for impact in impacts:
        if not impact.assessed:
            missing.append({"field": f"impacts.{impact.area}", "state": "missing"})
        elif impact.severity == "unknown":
            missing.append({"field": f"impacts.{impact.area}", "state": "unknown"})
    return missing


def _lock_case_for_change(conn, case_id: str, scope: CaseScope) -> str | None:
    """Lock the case row, then check the caller's visibility. Returns the case status, or None when not visible."""
    row = conn.execute("SELECT status FROM cases WHERE id = %s AND NOT is_deleted FOR UPDATE", (case_id,)).fetchone()
    if row is None or not cases.is_visible_on(conn, case_id, scope):
        return None
    return row[0]


_AWARENESS_COLUMNS = "id::text, case_id::text, awareness_kind, event_kind, occurred_at, basis, recorded_by::text, created_at"
_DUTY_COLUMNS = (
    "id::text, case_id::text, kind, description, source_reference, jurisdiction, due_at, status, created_by::text, created_at"
)
_CLASSIFICATION_COLUMNS = (
    "version, triggers, event_kind, classification_rationale, responsible_operator, "
    "affected_versions, approved_scope, scope_permission_source, containment, evidence_gap_plan, "
    "next_update_due_at, created_by::text, created_at"
)


def _load_awareness(conn, case_id: str) -> list[AwarenessEvent]:
    rows = conn.execute(
        f"SELECT {_AWARENESS_COLUMNS} FROM awareness_events WHERE case_id = %s AND NOT is_deleted ORDER BY occurred_at, id",
        (case_id,),
    ).fetchall()
    return [AwarenessEvent(*row) for row in rows]


def _load_impacts(conn, case_id: str) -> list[ImpactAssessment]:
    rows = conn.execute(
        "SELECT area, severity, scope, updated_at FROM impacts WHERE case_id = %s AND NOT is_deleted",
        (case_id,),
    ).fetchall()
    assessed = {row[0]: ImpactAssessment(area=row[0], assessed=True, severity=row[1], scope=row[2], updated_at=row[3]) for row in rows}
    return impacts_in_order(assessed)


def _load_duties(conn, case_id: str) -> list[LegalDuty]:
    rows = conn.execute(
        f"SELECT {_DUTY_COLUMNS} FROM legal_duties WHERE case_id = %s AND NOT is_deleted ORDER BY due_at, id",
        (case_id,),
    ).fetchall()
    return [LegalDuty(*row) for row in rows]


def _current_classification(conn, case_id: str) -> Classification:
    row = conn.execute(
        f"SELECT {_CLASSIFICATION_COLUMNS} FROM case_classifications "
        "WHERE case_id = %s AND NOT is_deleted ORDER BY version DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if row is None:
        return Classification(version=0)
    return Classification(
        version=row[0],
        triggers=tuple(row[1]),
        event_kind=row[2],
        classification_rationale=row[3],
        responsible_operator=row[4],
        affected_versions=row[5],
        approved_scope=row[6],
        scope_permission_source=row[7],
        containment=row[8],
        evidence_gap_plan=row[9],
        next_update_due_at=row[10],
        created_by=row[11],
        created_at=row[12],
    )


def _contact_recorded(conn, case_id: str) -> bool:
    row = conn.execute(
        "SELECT contact IS NOT NULL FROM reporter_identities WHERE case_id = %s AND NOT is_deleted",
        (case_id,),
    ).fetchone()
    return bool(row and row[0])


# --- awareness events ----------------------------------------------------------------------------------------------


def record_awareness_event(
    case_id: str,
    awareness_kind: str,
    event_kind: str,
    occurred_at: datetime,
    basis: str,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[AwarenessEvent], Any] | None = None,
) -> tuple[RecordOutcome, AwarenessEvent | None]:
    """Append an awareness event. Events are never edited or removed, so the record of what was known stays intact.

    With an idempotency scope the key is checked first, and the stored response is render(event).
    """
    with connect() as conn:
        check(conn, idempotency)
        status = _lock_case_for_change(conn, case_id, scope)
        if status is None:
            return RecordOutcome.NOT_FOUND, None
        if status == "closed":
            return RecordOutcome.CLOSED, None
        row = conn.execute(
            """
            INSERT INTO awareness_events (case_id, awareness_kind, event_kind, occurred_at, basis, recorded_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id::text, created_at
            """,
            (case_id, awareness_kind, event_kind, occurred_at, basis, scope.user_id),
        ).fetchone()
        details: dict[str, Any] = {
            "awareness_event_id": row[0],
            "awareness_kind": awareness_kind,
            "event_kind": event_kind,
            "occurred_at": occurred_at.isoformat(),
            "basis": _marker(basis),
        }
        # Only an operator event can move t0. When it moves the SAFE due dates, the same audit row records the move.
        if awareness_kind == "operator":
            start, moved = cases.resync_deadlines(conn, case_id)
            if moved:
                details["deadline_start"] = start.isoformat()
                details["deadlines"] = moved
        audit.record(
            conn,
            audit.CASE_AWARENESS_RECORDED,
            "case",
            case_id,
            actor_id=scope.user_id,
            details=details,
        )
        event = AwarenessEvent(
            id=row[0],
            case_id=case_id,
            awareness_kind=awareness_kind,
            event_kind=event_kind,
            occurred_at=occurred_at,
            basis=basis,
            recorded_by=scope.user_id,
            created_at=row[1],
        )
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 201, render(event))
    return RecordOutcome.OK, event


def list_awareness_events(case_id: str, scope: CaseScope) -> list[AwarenessEvent] | None:
    """Awareness events of the case in time order, or None when the scope may not see the case."""
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return None
        return _load_awareness(conn, case_id)


# --- impacts -------------------------------------------------------------------------------------------------------


def set_impact(
    case_id: str,
    area: str,
    severity: str,
    impact_scope: str,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[ImpactAssessment], Any] | None = None,
) -> tuple[RecordOutcome, ImpactAssessment | None]:
    """Create or replace the assessment of one impact area. Writes only what changed, with one audit row.

    With an idempotency scope the key is checked first, and the stored response is render(assessment).
    """
    with connect() as conn:
        check(conn, idempotency)
        status = _lock_case_for_change(conn, case_id, scope)
        if status is None:
            return RecordOutcome.NOT_FOUND, None
        if status == "closed":
            return RecordOutcome.CLOSED, None
        row = conn.execute(
            "SELECT id::text, severity, scope FROM impacts WHERE case_id = %s AND area = %s AND NOT is_deleted FOR UPDATE",
            (case_id, area),
        ).fetchone()
        if row is None:
            updated_at = conn.execute(
                """
                INSERT INTO impacts (case_id, area, severity, scope, updated_by)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING updated_at
                """,
                (case_id, area, severity, impact_scope, scope.user_id),
            ).fetchone()[0]
            changes: dict[str, Any] = {
                "severity": {"old": None, "new": severity},
                "scope": {"old": None, "new": _marker(impact_scope)},
            }
        else:
            changes = {}
            if row[1] != severity:
                changes["severity"] = {"old": row[1], "new": severity}
            if row[2] != impact_scope:
                changes["scope"] = {"old": _marker(row[2]), "new": _marker(impact_scope)}
            if changes:
                updated_at = conn.execute(
                    "UPDATE impacts SET severity = %s, scope = %s, updated_by = %s WHERE id = %s RETURNING updated_at",
                    (severity, impact_scope, scope.user_id, row[0]),
                ).fetchone()[0]
            else:
                updated_at = conn.execute("SELECT updated_at FROM impacts WHERE id = %s", (row[0],)).fetchone()[0]
        if changes:
            audit.record(conn, audit.CASE_IMPACT_CHANGE, "case", case_id, actor_id=scope.user_id, details={"area": area, **changes})
            # The weekly update depends on the significant risks, so it follows every change of an impact area.
            cases.sync_weekly_update(conn, case_id, scope.user_id)
        assessment = ImpactAssessment(
            area=area, assessed=True, severity=severity, scope=impact_scope, updated_at=updated_at
        )
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 200, render(assessment))
    return RecordOutcome.OK, assessment


def list_impacts(case_id: str, scope: CaseScope) -> list[ImpactAssessment] | None:
    """Every impact area of the case, in the plan's order. None when the scope may not see the case."""
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return None
        return _load_impacts(conn, case_id)


# --- legal duties --------------------------------------------------------------------------------------------------


def add_legal_duty(
    case_id: str,
    kind: str,
    description: str,
    source_reference: str,
    jurisdiction: str | None,
    due_at: datetime,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[LegalDuty], Any] | None = None,
) -> tuple[RecordOutcome, LegalDuty | None]:
    with connect() as conn:
        check(conn, idempotency)
        status = _lock_case_for_change(conn, case_id, scope)
        if status is None:
            return RecordOutcome.NOT_FOUND, None
        if status == "closed":
            return RecordOutcome.CLOSED, None
        row = conn.execute(
            """
            INSERT INTO legal_duties (case_id, kind, description, source_reference, jurisdiction, due_at, status, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'open', %s)
            RETURNING id::text, created_at
            """,
            (case_id, kind, description, source_reference, jurisdiction, due_at, scope.user_id),
        ).fetchone()
        audit.record(
            conn,
            audit.CASE_LEGAL_DUTY_ADD,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={
                "legal_duty_id": row[0],
                "kind": kind,
                "due_at": due_at.isoformat(),
                "description": _marker(description),
                "source_reference": _marker(source_reference),
                "jurisdiction": _marker(jurisdiction),
            },
        )
        duty = LegalDuty(
            id=row[0],
            case_id=case_id,
            kind=kind,
            description=description,
            source_reference=source_reference,
            jurisdiction=jurisdiction,
            due_at=due_at,
            status="open",
            created_by=scope.user_id,
            created_at=row[1],
        )
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 201, render(duty))
    return RecordOutcome.OK, duty


def update_legal_duty(case_id: str, duty_id: str, changes: Mapping[str, Any], scope: CaseScope) -> tuple[RecordOutcome, LegalDuty | None]:
    """Change the given fields of a duty. Fields not in changes keep their value. Each real change is audited."""
    unknown = set(changes) - set(DUTY_EDITABLE_FIELDS)
    if unknown:
        raise ValueError("fields that cannot be changed: " + ", ".join(sorted(unknown)))
    with connect() as conn:
        status = _lock_case_for_change(conn, case_id, scope)
        if status is None:
            return RecordOutcome.NOT_FOUND, None
        if status == "closed":
            return RecordOutcome.CLOSED, None
        row = conn.execute(
            f"SELECT {_DUTY_COLUMNS} FROM legal_duties WHERE id = %s AND case_id = %s AND NOT is_deleted FOR UPDATE",
            (duty_id, case_id),
        ).fetchone()
        if row is None:
            return RecordOutcome.DUTY_NOT_FOUND, None
        duty = LegalDuty(*row)
        current = {name: getattr(duty, name) for name in DUTY_EDITABLE_FIELDS}
        new = {**current, **changes}
        diff = duty_changes(current, new)
        if diff:
            conn.execute(
                """
                UPDATE legal_duties SET description = %s, source_reference = %s, jurisdiction = %s, due_at = %s, status = %s
                WHERE id = %s
                """,
                (new["description"], new["source_reference"], new["jurisdiction"], new["due_at"], new["status"], duty_id),
            )
            audit.record(
                conn,
                audit.CASE_LEGAL_DUTY_CHANGE,
                "case",
                case_id,
                actor_id=scope.user_id,
                details={"legal_duty_id": duty_id, **diff},
            )
            duty = replace(duty, **new)
    return RecordOutcome.OK, duty


def list_legal_duties(case_id: str, scope: CaseScope) -> list[LegalDuty] | None:
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return None
        return _load_duties(conn, case_id)


# --- classification ------------------------------------------------------------------------------------------------


def read_classification(case_id: str, scope: CaseScope) -> Classification | None:
    """The current classification (version 0 when none is recorded), or None when the scope may not see the case."""
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return None
        return _current_classification(conn, case_id)


def update_classification(
    case_id: str,
    expected_version: int,
    values: Mapping[str, Any],
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Classification], Any] | None = None,
) -> tuple[RecordOutcome, Classification | None]:
    """Store the given classification as the next version.

    expected_version is the version the editor read. A different current version is a conflict, and nothing is stored.
    A request that changes nothing stores nothing and writes no audit row. With an idempotency scope the key is checked
    first, and a successful request stores render(classification), changed or not.
    """
    with connect() as conn:
        check(conn, idempotency)
        status = _lock_case_for_change(conn, case_id, scope)
        if status is None:
            return RecordOutcome.NOT_FOUND, None
        if status == "closed":
            return RecordOutcome.CLOSED, None
        current = _current_classification(conn, case_id)
        if current.version != expected_version:
            return RecordOutcome.VERSION_CONFLICT, current
        changes = classification_changes(current, values)
        if not changes:
            if idempotency is not None:
                assert render is not None
                remember(conn, idempotency, 200, render(current))
            return RecordOutcome.OK, current
        new_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM case_classifications WHERE case_id = %s",
            (case_id,),
        ).fetchone()[0]
        # The column keeps its default (false) in new versions: the option is no longer written. Older versions keep
        # what they recorded, and migration 0037 turned that into per-task exceptions.
        created_at = conn.execute(
            """
            INSERT INTO case_classifications (
                case_id, version, triggers, event_kind, classification_rationale,
                responsible_operator, affected_versions, approved_scope, scope_permission_source, containment,
                evidence_gap_plan, next_update_due_at, created_by
            )
            VALUES (%s, %s, %s::text[], %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING created_at
            """,
            (
                case_id,
                new_version,
                list(values["triggers"]),
                values["event_kind"],
                values["classification_rationale"],
                values["responsible_operator"],
                values["affected_versions"],
                values["approved_scope"],
                values["scope_permission_source"],
                values["containment"],
                values["evidence_gap_plan"],
                values["next_update_due_at"],
                scope.user_id,
            ),
        ).fetchone()[0]
        audit.record(
            conn,
            audit.CASE_CLASSIFICATION_CHANGE,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={"version": {"old": current.version, "new": new_version}, **changes},
        )
        stored = Classification(
            version=new_version,
            **{name: values[name] for name in CLASSIFICATION_EDITABLE_FIELDS if name != "triggers"},
            triggers=tuple(values["triggers"]),
            created_by=scope.user_id,
            created_at=created_at,
        )
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 200, render(stored))
    return RecordOutcome.OK, stored


# --- preliminary report --------------------------------------------------------------------------------------------


def build_preliminary_report(
    case: Case,
    classification: Classification,
    awareness_events: list[AwarenessEvent],
    impacts: list[ImpactAssessment],
    duties: list[LegalDuty],
    contact_recorded: bool,
    generated_at: datetime,
) -> dict[str, Any]:
    """The preliminary report as plain data. It is generated even when fields are missing: they are listed, not refused."""
    missing = missing_fields(classification, awareness_events, impacts, contact_recorded)
    return {
        "case_id": case.id,
        "reference": case.reference,
        "case_status": case.status,
        "generated_at": generated_at.isoformat(),
        "awareness_time": case.awareness_time.isoformat(),
        "complete": not missing,
        "missing_fields": missing,
        "contact": {"status": "recorded" if contact_recorded else "missing"},
        "classification": {
            "version": classification.version,
            "triggers": list(classification.triggers),
            "event_kind": classification.event_kind,
            **{name: getattr(classification, name) for name in CLASSIFICATION_TEXT_FIELDS},
            "next_update_due_at": _iso(classification.next_update_due_at),
        },
        "awareness_events": [
            {
                "id": event.id,
                "awareness_kind": event.awareness_kind,
                "event_kind": event.event_kind,
                "occurred_at": event.occurred_at.isoformat(),
                "basis": event.basis,
            }
            for event in awareness_events
        ],
        "impacts": [
            {
                "area": impact.area,
                "assessed": impact.assessed,
                "severity": impact.severity,
                "scope": impact.scope,
            }
            for impact in impacts
        ],
        # Legal duties are listed apart from the SAFE deadlines (the case's deadlines), which keep their own clocks.
        "legal_duties": [
            {
                "id": duty.id,
                "kind": duty.kind,
                "description": duty.description,
                "source_reference": duty.source_reference,
                "jurisdiction": duty.jurisdiction,
                "due_at": duty.due_at.isoformat(),
                "status": duty.status,
            }
            for duty in duties
        ],
        "safe_deadlines": [
            {"kind": task.kind, "due_at": task.due_at.isoformat(), "status": task.status} for task in case.deadlines
        ],
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def preliminary_report(case_id: str, scope: CaseScope) -> dict[str, Any] | None:
    """The preliminary report of a visible case, or None when the scope may not see it."""
    case = cases.find_visible_case(case_id, scope)
    if case is None:
        return None
    with connect() as conn:
        classification = _current_classification(conn, case_id)
        awareness_events = _load_awareness(conn, case_id)
        impacts = _load_impacts(conn, case_id)
        duties = _load_duties(conn, case_id)
        contact_recorded = _contact_recorded(conn, case_id)
    return build_preliminary_report(
        case,
        classification,
        awareness_events,
        impacts,
        duties,
        contact_recorded,
        datetime.now(UTC),
    )
