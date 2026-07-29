from fastapi import APIRouter, Request, Depends, HTTPException

from ..auth.session import get_current_user

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/browse")
async def browse(
    request: Request,
    category: str | None = None,
    tag: str | None = None,
    page: int = 1,
    show_hidden: int = 0,
    sort: str = "default",
    user: dict = Depends(get_current_user),
):
    """Browse catalog with optional category/tag filter."""
    db = request.app.state.db
    is_admin = (user.get("rank") or 0) >= 3
    show_hidden = bool(show_hidden) and is_admin
    # Admins always see every title; regular users have recently-played items
    # hidden for the configured window.
    recently_played_days = (
        0 if is_admin else request.app.state.config.catalog_recently_played_hide_days
    )
    items = await db.browse(
        category=category,
        tag=tag,
        page=page,
        show_hidden=show_hidden,
        sort=sort,
        recently_played_days=recently_played_days,
    )
    categories = await db.get_categories(show_hidden=show_hidden)
    tags = await db.get_tags(show_hidden=show_hidden)
    return {
        "items": items,
        "categories": categories,
        "tags": tags,
        "page": page,
        "sort": sort,
    }


@router.get("/search")
async def search(
    request: Request,
    q: str = "",
    category: str | None = None,
    tag: str | None = None,
    page: int = 1,
    show_hidden: int = 0,
    sort: str = "default",
    user: dict = Depends(get_current_user),
):
    """Full-text search of catalog, optionally narrowed by category/tag."""
    if not q.strip():
        raise HTTPException(400, "Query required")
    db = request.app.state.db
    is_admin = (user.get("rank") or 0) >= 3
    show_hidden = bool(show_hidden) and is_admin
    recently_played_days = (
        0 if is_admin else request.app.state.config.catalog_recently_played_hide_days
    )
    items = await db.search(
        q,
        category=category,
        tag=tag,
        page=page,
        show_hidden=show_hidden,
        sort=sort,
        recently_played_days=recently_played_days,
    )
    categories = await db.get_categories(show_hidden=show_hidden)
    tags = await db.get_tags(show_hidden=show_hidden)
    return {
        "items": items,
        "categories": categories,
        "tags": tags,
        "query": q,
        "active_category": category,
        "active_tag": tag,
        "page": page,
        "sort": sort,
    }


@router.get("/item/{friendly_token}")
async def get_item(
    request: Request, friendly_token: str, user: dict = Depends(get_current_user)
):
    """Get single catalog item detail."""
    db = request.app.state.db
    item = await db.get_item(friendly_token)
    if not item:
        raise HTTPException(404, "Item not found")
    return item


@router.get("/categories")
async def list_categories(request: Request, user: dict = Depends(get_current_user)):
    """List all categories."""
    db = request.app.state.db
    return await db.get_categories()
