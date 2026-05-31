from fastapi import APIRouter, Request, Depends, HTTPException

from ..auth.session import get_current_user

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/browse")
async def browse(request: Request, category: str | None = None, page: int = 1,
                 user: dict = Depends(get_current_user)):
    """Browse catalog with optional category filter."""
    db = request.app.state.db
    items = await db.browse(category=category, page=page)
    categories = await db.get_categories()
    return {"items": items, "categories": categories, "page": page}


@router.get("/search")
async def search(request: Request, q: str = "", page: int = 1,
                 user: dict = Depends(get_current_user)):
    """Full-text search of catalog."""
    if not q.strip():
        raise HTTPException(400, "Query required")
    db = request.app.state.db
    items = await db.search(q, page=page)
    return {"items": items, "query": q, "page": page}


@router.get("/item/{friendly_token}")
async def get_item(request: Request, friendly_token: str,
                   user: dict = Depends(get_current_user)):
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
