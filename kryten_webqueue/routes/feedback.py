"""User-facing feedback + movie-title suggestion endpoints.

Backs the public "Feedback" page (templates/feedback/index.html). Both features
require a logged-in user so submissions carry a canonical username for
attribution and light spam control.

Movie suggestions are resolved against TMDB/OMDB (reusing the cover-art
resolver). The flow is two-step: ``/suggest/resolve`` returns candidate matches
the user confirms, then ``/suggest/submit`` records the chosen match. A title we
can't match is still accepted and stored as ``unresolved``; a title we already
have is stored as ``already_have`` and the user is told it's available.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel

from ..auth.session import get_current_user

router = APIRouter(tags=["feedback"])

FEEDBACK_MAX_LEN = 2000
SUGGEST_QUERY_MAX_LEN = 200
_THANKS = "Thanks for helping make Channel-Z better!"
_VALID_SOURCES = {"tmdb", "omdb"}


class FeedbackSubmit(BaseModel):
    body: str


class SuggestResolve(BaseModel):
    query: str


class SuggestChoice(BaseModel):
    source: str | None = None
    id: str | None = None
    title: str | None = None
    year: str | None = None
    media_type: str | None = None
    poster_url: str | None = None


class SuggestSubmit(BaseModel):
    query: str
    choice: SuggestChoice | None = None
    unresolved: bool = False


def _rate_limit(request: Request, key: str, message: str) -> None:
    """Raise 429 when the per-user sliding window for ``key`` is exhausted."""
    limiter = request.app.state.feedback_rate_limiter
    if not limiter.is_allowed(key):
        raise HTTPException(status_code=429, detail=message)


def _submission_quota(request: Request, key: str, noun: str) -> None:
    """Enforce the hard per-user daily/weekly submission quota (2/day, 6/week).

    Raises 429 with a tier-appropriate message when the day or week cap for
    ``key`` is reached. A permitted submission is recorded against both tiers.
    """
    limiter = request.app.state.feedback_quota_limiter
    exceeded = limiter.check(key)
    if exceeded == "day":
        raise HTTPException(
            status_code=429,
            detail=f"You've reached today's limit of 2 {noun}. Please try again tomorrow.",
        )
    if exceeded == "week":
        raise HTTPException(
            status_code=429,
            detail=f"You've reached this week's limit of 6 {noun}. Please try again next week.",
        )


@router.post("/feedback/submit")
async def submit_feedback(
    payload: FeedbackSubmit, request: Request, user: dict = Depends(get_current_user)
):
    """Record a free-text feedback submission and thank the user."""
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(
            status_code=400, detail="Please enter some feedback before submitting."
        )
    if len(body) > FEEDBACK_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Feedback is too long (max {FEEDBACK_MAX_LEN} characters).",
        )
    _rate_limit(
        request,
        f"feedback:{user['username']}",
        "You're sending feedback very quickly. Please wait a moment and try again.",
    )
    _submission_quota(request, f"feedback:{user['username']}", "feedback submissions")
    db = request.app.state.db
    await db.add_feedback(username=user["username"], body=body)
    return {
        "success": True,
        "message": (
            f"Thanks, {user['username']}! {_THANKS} Your feedback has been "
            "sent to the Channel-Z team."
        ),
    }


@router.post("/feedback/suggest/resolve")
async def resolve_suggestion(
    payload: SuggestResolve, request: Request, user: dict = Depends(get_current_user)
):
    """Search the movie databases for candidate matches to a title query.

    Each candidate is annotated with ``catalog_token``/``catalog_title`` when we
    already have that title, so the UI can flag it before the user submits.
    """
    query = (payload.query or "").strip()
    if not query:
        raise HTTPException(
            status_code=400, detail="Enter a movie or show title to search."
        )
    if len(query) > SUGGEST_QUERY_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Title is too long (max {SUGGEST_QUERY_MAX_LEN} characters).",
        )
    _rate_limit(
        request,
        f"suggest-resolve:{user['username']}",
        "Too many searches in a short time. Please wait a moment and try again.",
    )
    cover_art = request.app.state.cover_art
    db = request.app.state.db
    candidates = await cover_art.search_titles(query)
    for c in candidates:
        match = await db.find_catalog_by_title(c.get("title") or "")
        if match:
            c["catalog_token"] = match["friendly_token"]
            c["catalog_title"] = match["title"]
    return {"query": query, "candidates": candidates}


@router.post("/feedback/suggest/submit")
async def submit_suggestion(
    payload: SuggestSubmit, request: Request, user: dict = Depends(get_current_user)
):
    """Record a movie suggestion, resolving 'already have' server-side.

    A missing/empty choice (or ``unresolved=true``) is stored as an unresolved
    suggestion — still surfaced to admins. The chosen candidate fields are
    user-supplied and so are sanitized and length-capped; the catalog match is
    always re-derived from the DB rather than trusted from the client.
    """
    query = (payload.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Enter a movie or show title.")
    if len(query) > SUGGEST_QUERY_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Title is too long (max {SUGGEST_QUERY_MAX_LEN} characters).",
        )
    _rate_limit(
        request,
        f"suggest:{user['username']}",
        "You're sending suggestions very quickly. Please wait a moment and try again.",
    )
    _submission_quota(request, f"suggest:{user['username']}", "suggestions")
    db = request.app.state.db
    username = user["username"]
    choice = payload.choice

    # Unresolved path: no match chosen (or explicitly flagged). Still recorded.
    if payload.unresolved or not choice or not (choice.title or "").strip():
        await db.add_title_suggestion(
            username=username, query=query, resolution="unresolved"
        )
        return {
            "success": True,
            "resolution": "unresolved",
            "message": (
                f"Thanks, {username}! {_THANKS} We couldn't match that to a "
                "database entry, but we've passed your suggestion to the "
                "Channel-Z team."
            ),
        }

    # Sanitize the client-provided candidate before storing.
    title = (choice.title or "").strip()[:300]
    year = ((choice.year or "").strip()[:8]) or None
    source = ((choice.source or "").strip().lower()[:16]) or None
    if source not in _VALID_SOURCES:
        source = None
    resolved_id = ((choice.id or "").strip()[:64]) or None
    poster = ((choice.poster_url or "").strip()[:500]) or None

    # Authoritatively re-check whether we already have this title.
    match = await db.find_catalog_by_title(title)
    if match:
        await db.add_title_suggestion(
            username=username,
            query=query,
            resolved_title=title,
            resolved_year=year,
            resolved_source=source,
            resolved_id=resolved_id,
            poster_url=poster,
            resolution="already_have",
            catalog_token=match["friendly_token"],
        )
        return {
            "success": True,
            "resolution": "already_have",
            "catalog_token": match["friendly_token"],
            "catalog_title": match["title"],
            "message": (
                f"Good news, {username} — we already have \u201c{match['title']}\u201d "
                "in the catalog! Thanks for the suggestion."
            ),
        }

    await db.add_title_suggestion(
        username=username,
        query=query,
        resolved_title=title,
        resolved_year=year,
        resolved_source=source,
        resolved_id=resolved_id,
        poster_url=poster,
        resolution="resolved",
    )
    return {
        "success": True,
        "resolution": "resolved",
        "message": (
            f"Thanks, {username}! {_THANKS} We've added \u201c{title}\u201d to the "
            "suggestions list for the Channel-Z team to review."
        ),
    }
