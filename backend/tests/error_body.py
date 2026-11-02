"""Helpers for the error body (plans/f0/api-draft.md section 3.6)."""


def without_request_id(response) -> dict:
    """The JSON body of a response without error.request_id, which differs on every request."""
    body = response.json()
    body.get("error", {}).pop("request_id", None)
    return body
