"""Admin triage endpoints for viewer feedback + movie-title suggestions.

These back the "Feedback" and "Suggestions" tabs on the admin dashboard. Both
queues support the same lightweight triage: list (optionally filtered by
status), mark read/unread, and delete stale entries. Suggestion rows also carry
the resolved match + a catalog token when we already own the title, which the UI
turns into a direct link to the catalog item.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel

from ..auth.session import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])

_VALID_STATUSES = {"new", "read"}


class StatusUpdate(BaseModel):
    status: str


# --- Feedback ---------------------------------------------------------------


@router.get("/feedback")
async def list_feedback(
    request: Request, status: str | None = None, user: dict = Depends(require_admin)
):
    """List feedback submissions, newest first, optionally filtered by status."""
    db = request.app.state.db
    if status not in _VALID_STATUSES:
        status = None
    items = await db.list_feedback(status=status)
    return {
        "items": items,
        "counts": {
            "new": await db.count_feedback(status="new"),
            "total": await db.count_feedback(),
        },
    }


@router.post("/feedback/{feedback_id}/status")
async def set_feedback_status(
    feedback_id: int,
    payload: StatusUpdate,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Mark a feedback entry read/unread."""
    if payload.status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    db = request.app.state.db
    if not await db.set_feedback_status(feedback_id, payload.status):
        raise HTTPException(status_code=404, detail="Feedback not found")
    return {"success": True}


@router.delete("/feedback/{feedback_id}")
async def delete_feedback(
    feedback_id: int, request: Request, user: dict = Depends(require_admin)
):
    """Delete a stale feedback entry."""
    db = request.app.state.db
    if not await db.delete_feedback(feedback_id):
        raise HTTPException(status_code=404, detail="Feedback not found")
    return {"success": True}


# --- Title suggestions ------------------------------------------------------


@router.get("/suggestions")
async def list_suggestions(
    request: Request, status: str | None = None, user: dict = Depends(require_admin)
):
    """List movie suggestions, newest first, optionally filtered by status."""
    db = request.app.state.db
    if status not in _VALID_STATUSES:
        status = None
    items = await db.list_title_suggestions(status=status)
    return {
        "items": items,
        "counts": {
            "new": await db.count_title_suggestions(status="new"),
            "total": await db.count_title_suggestions(),
        },
    }


@router.post("/suggestions/{suggestion_id}/status")
async def set_suggestion_status(
    suggestion_id: int,
    payload: StatusUpdate,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Mark a suggestion read/unread."""
    if payload.status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    db = request.app.state.db
    if not await db.set_title_suggestion_status(suggestion_id, payload.status):
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return {"success": True}


@router.delete("/suggestions/{suggestion_id}")
async def delete_suggestion(
    suggestion_id: int, request: Request, user: dict = Depends(require_admin)
):
    """Delete a stale suggestion."""
    db = request.app.state.db
    if not await db.delete_title_suggestion(suggestion_id):
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return {"success": True}
