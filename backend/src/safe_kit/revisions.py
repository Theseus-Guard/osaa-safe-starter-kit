"""Revision checks for change requests (plans/f0/api-draft.md section 3.5, decision D-16).

A record that accepts changes carries a revision that starts at 1 and rises by one with each successful change. A GET
of the record answers the revision as a quoted ETag. A change names the revision it was made against in If-Match. The
server answers 428 when If-Match is missing, 400 when it is not a quoted whole number, and 412 when the revision is
stale. The case title (api.py) and the review layers (reviews_api.py) use these helpers.
"""

import re

from fastapi import HTTPException

_QUOTED_REVISION = re.compile(r'"([0-9]+)"')


def etag_for(revision: int) -> str:
    """The ETag value of a record at this revision, for example "2"."""
    return f'"{revision}"'


def if_match_revision(value: str | None) -> int:
    """The revision a change was made against.

    Missing is refused with 428 precondition_required. A value that is not a quoted whole number is refused with 400
    invalid_if_match.
    """
    if value is None:
        raise HTTPException(status_code=428, detail={"status": "precondition_required"})
    match = _QUOTED_REVISION.fullmatch(value.strip())
    if match is None:
        raise HTTPException(status_code=400, detail={"status": "invalid_if_match"})
    return int(match.group(1))
