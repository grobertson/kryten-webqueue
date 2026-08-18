from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import random


templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Expose the package version to every template (used in the footer).
from .. import __version__ as _wq_version

templates.env.globals["app_version"] = _wq_version

# Duration filter options: (value, label, min_sec, max_sec)
# Default ("10+") hides <10min content (min=600, max=None)
DURATION_FILTERS = [
    ("all", "All durations", 0, None),
    ("10+", "10+ min (default)", 600, None),
    ("60+", "1+ hour", 3600, None),
    ("120+", "2+ hours", 7200, None),
    ("10-60", "10-60 min", 600, 3600),
    ("10-45", "10-45 min", 600, 2700),
    ("10-30", "10-30 min", 600, 1800),
    ("short", "< 10 min", 0, 600),
]
DURATION_FILTER_MAP = {k: (mi, ma) for k, _, mi, ma in DURATION_FILTERS}
DURATION_FILTER_LABELS = [(k, lab) for k, lab, _, _ in DURATION_FILTERS]

router = APIRouter(tags=["pages"])

# Sort keys exposed in the browse/search UI. Order defines the dropdown order.
SORT_OPTIONS = [
    ("default", "Default"),
    ("title_asc", "Title A–Z"),
    ("title_desc", "Title Z–A"),
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
]
_VALID_SORTS = {key for key, _ in SORT_OPTIONS}


def _decorate_placeholder_art(request: Request, items: list[dict]) -> None:
    """Assign a random branded placeholder URL to tiles lacking real poster art.

    A genuine poster match (cover_art_source in tmdb/omdb) is left untouched;
    everything else gets a stable-per-render random placeholder, with the real
    MediaCMS thumbnail revealed on hover. Mutates each item dict in place.
    """
    cover_art = getattr(request.app.state, "cover_art", None)
    urls = cover_art.list_placeholder_urls() if cover_art else []
    if not urls:
        return
    for item in items:
        if item.get("cover_art_source") not in ("tmdb", "omdb"):
            item["placeholder_art"] = random.choice(urls)


def _get_user_or_none(request: Request) -> dict | None:
    """Non-throwing user extraction for page rendering."""
    token = request.cookies.get("session")
    if not token:
        return None
    import jwt

    try:
        payload = jwt.decode(
            token, request.app.state.config.secret_key, algorithms=["HS256"]
        )
        return {"username": payload["sub"], "rank": payload["rank"]}
    except Exception:
        return None


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = _get_user_or_none(request)
    if user:
        return RedirectResponse("/catalog/browse")
    return RedirectResponse("/auth/login")


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = _get_user_or_none(request)
    if user:
        return RedirectResponse("/catalog/browse")
    return templates.TemplateResponse(request, "auth/login.html", {"user": None})


@router.get("/race", response_class=HTMLResponse)
async def race_view(request: Request):
    """Public race spectator view — no auth. Streams frames over /ws/race."""
    user = _get_user_or_none(request)
    return templates.TemplateResponse(request, "race.html", {"user": user})


@router.get("/catalog/browse", response_class=HTMLResponse)
async def catalog_browse_page(
    request: Request,
    category: str | None = None,
    tag: str | None = None,
    person: str | None = None,
    studio: str | None = None,
    page: int = 1,
    show_hidden: int = 0,
    sort: str = "default",
    duration: str = "10+",
):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    db = request.app.state.db
    is_admin = (user.get("rank") or 0) >= 3
    show_hidden = bool(show_hidden) and is_admin
    sort = sort if sort in _VALID_SORTS else "default"
    recently_played_days = (
        0 if is_admin else request.app.state.config.catalog_recently_played_hide_days
    )
    min_duration_sec, max_duration_sec = DURATION_FILTER_MAP.get(duration, (600, None))
    items = await db.browse(
        category=category,
        tag=tag,
        person=person,
        studio=studio,
        page=page,
        show_hidden=show_hidden,
        sort=sort,
        recently_played_days=recently_played_days,
        min_duration_sec=min_duration_sec,
        max_duration_sec=max_duration_sec,
    )
    total = await db.browse_count(
        category=category,
        tag=tag,
        person=person,
        studio=studio,
        show_hidden=show_hidden,
        recently_played_days=recently_played_days,
        min_duration_sec=min_duration_sec,
    )
    total_pages = max(1, (total + 23) // 24)
    categories = await db.get_categories(show_hidden=show_hidden)
    tags = await db.get_tags(
        show_hidden=show_hidden,
        min_duration_sec=min_duration_sec,
        max_duration_sec=max_duration_sec,
    )
    _decorate_placeholder_art(request, items)
    return templates.TemplateResponse(
        request,
        "catalog/browse.html",
        {
            "user": user,
            "items": items,
            "categories": categories,
            "tags": tags,
            "page": page,
            "total_pages": total_pages,
            "active_category": category,
            "active_tag": tag,
            "active_person": person,
            "active_studio": studio,
            "query": None,
            "is_admin": is_admin,
            "show_hidden": show_hidden,
            "duration": duration,
            "sort": sort,
            "sort_options": SORT_OPTIONS,
            "duration_options": DURATION_FILTER_LABELS,
            "mediacms_url": request.app.state.config.mediacms_url,
        },
    )


@router.get("/catalog/search", response_class=HTMLResponse)
async def catalog_search_page(
    request: Request,
    q: str = "",
    page: int = 1,
    category: str | None = None,
    tag: str | None = None,
    person: str | None = None,
    studio: str | None = None,
    show_hidden: int = 0,
    sort: str = "default",
    duration: str = "10+",
):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    if not q.strip():
        return RedirectResponse("/catalog/browse")
    db = request.app.state.db
    is_admin = (user.get("rank") or 0) >= 3
    show_hidden = bool(show_hidden) and is_admin
    sort = sort if sort in _VALID_SORTS else "default"
    recently_played_days = (
        0 if is_admin else request.app.state.config.catalog_recently_played_hide_days
    )
    min_duration_sec, max_duration_sec = DURATION_FILTER_MAP.get(duration, (600, None))
    items = await db.search(
        q,
        category=category,
        tag=tag,
        person=person,
        studio=studio,
        page=page,
        show_hidden=show_hidden,
        sort=sort,
        recently_played_days=recently_played_days,
        min_duration_sec=min_duration_sec,
        max_duration_sec=max_duration_sec,
    )
    total = await db.search_count(
        q,
        category=category,
        tag=tag,
        person=person,
        studio=studio,
        show_hidden=show_hidden,
        recently_played_days=recently_played_days,
        min_duration_sec=min_duration_sec,
        max_duration_sec=max_duration_sec,
    )
    total_pages = max(1, (total + 23) // 24)
    categories = await db.get_categories(show_hidden=show_hidden)
    tags = await db.get_tags(
        show_hidden=show_hidden,
        min_duration_sec=min_duration_sec,
        max_duration_sec=max_duration_sec,
    )
    _decorate_placeholder_art(request, items)
    return templates.TemplateResponse(
        request,
        "catalog/browse.html",
        {
            "user": user,
            "items": items,
            "categories": categories,
            "tags": tags,
            "page": page,
            "total_pages": total_pages,
            "active_category": category,
            "active_tag": tag,
            "active_person": person,
            "active_studio": studio,
            "query": q,
            "is_admin": is_admin,
            "show_hidden": show_hidden,
            "duration": duration,
            "sort": sort,
            "sort_options": SORT_OPTIONS,
            "duration_options": DURATION_FILTER_LABELS,
            "mediacms_url": request.app.state.config.mediacms_url,
        },
    )


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse(request, "queue/index.html", {"user": user})


@router.get("/catalog/item/{friendly_token}", response_class=HTMLResponse)
async def catalog_item_page(request: Request, friendly_token: str):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    db = request.app.state.db
    item = await db.get_item_admin(friendly_token)
    if not item:
        return templates.TemplateResponse(
            request, "catalog/item_not_found.html", {"user": user}, status_code=404
        )
    facets = await db.get_item_facets(friendly_token)
    return templates.TemplateResponse(
        request,
        "catalog/item_detail.html",
        {
            "user": user,
            "item": item,
            "categories": facets.get("categories") or [],
            "tags": facets.get("tags") or [],
            "people": facets.get("people") or {},
            "studios": facets.get("studios") or [],
            "mediacms_url": request.app.state.config.mediacms_url,
        },
    )


@router.get("/user/dashboard", response_class=HTMLResponse)
async def user_dashboard_page(request: Request):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse(request, "user/dashboard.html", {"user": user})


@router.get("/user/my-list", response_class=HTMLResponse)
async def my_list_page(request: Request, page: int = 1):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    db = request.app.state.db
    items = await db.watchlist_get(user["username"], page=page)
    total = await db.watchlist_count(user["username"])
    total_pages = max(1, (total + 23) // 24)
    _decorate_placeholder_art(request, items)
    return templates.TemplateResponse(
        request,
        "user/my_list.html",
        {
            "user": user,
            "items": items,
            "page": page,
            "total_pages": total_pages,
            "mediacms_url": request.app.state.config.mediacms_url,
        },
    )


@router.get("/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request):
    """Public feedback + 'Suggest a Title' page (login required)."""
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse(request, "feedback/index.html", {"user": user})


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = _get_user_or_none(request)
    if not user or user.get("rank", 0) < 3:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse(request, "admin/index.html", {"user": user})


@router.get("/admin/playlists", response_class=HTMLResponse)
async def admin_playlists_page(request: Request):
    user = _get_user_or_none(request)
    if not user or user.get("rank", 0) < 3:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse(request, "admin/playlists.html", {"user": user})


@router.get("/admin/schedules", response_class=HTMLResponse)
async def admin_schedules_page(request: Request):
    user = _get_user_or_none(request)
    if not user or user.get("rank", 0) < 3:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse(request, "admin/schedules.html", {"user": user})


@router.get("/admin/queue-mgmt", response_class=HTMLResponse)
async def admin_queue_mgmt_page(request: Request):
    user = _get_user_or_none(request)
    if not user or user.get("rank", 0) < 3:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse(request, "admin/queue_mgmt.html", {"user": user})


@router.get("/admin/promos", response_class=HTMLResponse)
async def admin_promos_page(request: Request):
    user = _get_user_or_none(request)
    if not user or user.get("rank", 0) < 3:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse(request, "admin/promos.html", {"user": user})
