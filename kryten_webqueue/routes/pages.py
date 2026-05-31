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
    return templates.TemplateResponse("auth/login.html", {"request": request, "user": None})


@router.get("/catalog/browse", response_class=HTMLResponse)
async def catalog_browse_page(request: Request, category: str | None = None, page: int = 1):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    db = request.app.state.db
    items = await db.browse(category=category, page=page)
    categories = await db.get_categories()
    return templates.TemplateResponse("catalog/browse.html", {
        "request": request,
        "user": user,
        "items": items,
        "categories": categories,
        "page": page,
        "active_category": category,
        "query": None,
    })


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse("queue/index.html", {"request": request, "user": user})


@router.get("/user/dashboard", response_class=HTMLResponse)
async def user_dashboard_page(request: Request):
    user = _get_user_or_none(request)
    if not user:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse("user/dashboard.html", {"request": request, "user": user})


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = _get_user_or_none(request)
    if not user or user.get("rank", 0) < 3:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse("admin/index.html", {"request": request, "user": user})
