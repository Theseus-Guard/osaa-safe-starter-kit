"""If-Match headers for the changes to a deadline task, a legal duty and a user (F2, D-16; plans/f0/api-draft.md 3.5).

Each of these records has its own revision. The API sends it in the body of the GET that lists the record (the case
response for a deadline task, the legal duty list, the user list), because a list has one ETag for many records. The
helpers read it with the same client that sends the change, so the revision is the one that caller may see.

A refusal that comes before the revision check (not visible, closed, forbidden) does not need the right revision. For
those the helpers send "1", so the test reaches the refusal it is about.
"""

PLACEHOLDER = {"If-Match": '"1"'}


def _if_match(revision: int) -> dict:
    return {"If-Match": f'"{revision}"'}


def task_if_match(client, url: str) -> dict:
    """If-Match for PATCH cases/{case_id}/deadlines/{task_id}. The revision is read from the case response."""
    case_url, task_id = url.split("/deadlines/")
    response = client.get(case_url)
    if response.status_code != 200:
        return PLACEHOLDER
    for task in response.json()["deadlines"]:
        if task["id"] == task_id:
            return _if_match(task["revision"])
    return PLACEHOLDER


def duty_if_match(client, url: str) -> dict:
    """If-Match for PATCH cases/{case_id}/legal-duties/{duty_id}. The revision is read from the legal duty list."""
    case_url, duty_id = url.split("/legal-duties/")
    response = client.get(f"{case_url}/legal-duties")
    if response.status_code != 200:
        return PLACEHOLDER
    for duty in response.json():
        if duty["id"] == duty_id:
            return _if_match(duty["revision"])
    return PLACEHOLDER


def user_if_match(client, user_id: str) -> dict:
    """If-Match for PATCH users/{user_id}. The revision is read from the user list, which the admin may read."""
    cursor = None
    while True:
        query = "limit=200" if cursor is None else f"limit=200&cursor={cursor}"
        response = client.get(f"/api/v1/users?{query}")
        if response.status_code != 200:
            return PLACEHOLDER
        page = response.json()
        for user in page["items"]:
            if user["id"] == user_id:
                return _if_match(user["revision"])
        cursor = page["next_cursor"]
        if cursor is None:
            return PLACEHOLDER
