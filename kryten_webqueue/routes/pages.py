from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..auth.session import get_current_user

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

router = APIRouter(tags=["pages"])


def _get_user_or_none(request: Request) -> dict | None:
    """Non-throwing user extraction for page rendering."""
    token = request.cookies.get("session")
    if not token:
        return None
    import jwt
    try:
        payload = jwt.decode(token, request.app.state.config.secret_key, algorithms=["HS256"])
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


@router.get("/catalog/browse", response_class=HTMLResponse)
async def catalog_browse_page(request: Request, category: str | None = None,
                              tag: str | None = None, page: int = 1,
                              show_hidden: int = 0):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    db = request.app.state.db
    is_admin = (user.get("rank") or 0) >= 3
    show_hidden = bool(show_hidden) and is_admin
    items = await db.browse(category=category, tag=tag, page=page, show_hidden=show_hidden)
    total = await db.browse_count(category=category, tag=tag, show_hidden=show_hidden)
    total_pages = max(1, (total + 23) // 24)
    categories = await db.get_categories(show_hidden=show_hidden)
    tags = await db.get_tags(show_hidden=show_hidden)
    return templates.TemplateResponse(request, "catalog/browse.html", {
        "user": user,
        "items": items,
        "categories": categories,
        "tags": tags,
        "page": page,
        "total_pages": total_pages,
        "active_category": category,
        "active_tag": tag,
        "query": None,
        "is_admin": is_admin,
        "show_hidden": show_hidden,
    })


@router.get("/catalog/search", response_class=HTMLResponse)
async def catalog_search_page(request: Request, q: str = "", page: int = 1,
                             show_hidden: int = 0):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    if not q.strip():
        return RedirectResponse("/catalog/browse")
    db = request.app.state.db
    is_admin = (user.get("rank") or 0) >= 3
    show_hidden = bool(show_hidden) and is_admin
    items = await db.search(q, page=page, show_hidden=show_hidden)
    total = await db.search_count(q, show_hidden=show_hidden)
    total_pages = max(1, (total + 23) // 24)
    categories = await db.get_categories(show_hidden=show_hidden)
    tags = await db.get_tags(show_hidden=show_hidden)
    return templates.TemplateResponse(request, "catalog/browse.html", {
        "user": user,
        "items": items,
        "categories": categories,
        "tags": tags,
        "page": page,
        "total_pages": total_pages,
        "active_category": None,
        "active_tag": None,
        "query": q,
        "is_admin": is_admin,
        "show_hidden": show_hidden,
    })


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
    return templates.TemplateResponse(request, "catalog/item_detail.html", {
        "user": user,
        "item": item,
        "categories": facets.get("categories") or [],
        "tags": facets.get("tags") or [],
    })


@router.get("/user/dashboard", response_class=HTMLResponse)
async def user_dashboard_page(request: Request):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse(request, "user/dashboard.html", {"user": user})


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
