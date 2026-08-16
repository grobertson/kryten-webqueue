from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import ValidationError

from ..auth.session import require_admin
from ..config import PromoConfig
from ..promos import PROMO_TYPES, GENERAL_PROMO_TYPES, LEAD_IN_PROMO_TYPES

router = APIRouter(prefix="/admin/promos", tags=["admin"])


@router.get("/config")
async def get_promo_config(request: Request, user: dict = Depends(require_admin)):
    """Return the current promo configuration plus type metadata.

    The admin panel renders this for display and inline editing (see the
    matching ``PUT``).
    """
    cfg = request.app.state.config.promos
    return {
        "config": cfg.model_dump(),
        "promo_types": list(PROMO_TYPES),
        "general_types": list(GENERAL_PROMO_TYPES),
        "lead_in_types": list(LEAD_IN_PROMO_TYPES),
    }


@router.put("/config")
async def update_promo_config(request: Request, user: dict = Depends(require_admin)):
    """Replace the promo configuration, persist it, and apply it live.

    Validates the body against :class:`PromoConfig`, writes it back to the
    service config file, and hot-applies it to the running ``PromoDirector`` so
    the change takes effect without a restart.
    """
    body = await request.json()
    try:
        new_cfg = PromoConfig(**body)
    except ValidationError as e:
        raise HTTPException(400, f"Invalid promo config: {e.errors()}") from e

    config = request.app.state.config
    previous = config.promos
    config.promos = new_cfg
    try:
        config.save()
    except (RuntimeError, OSError) as e:
        # Persistence failed (no source path, or the config dir is read-only under
        # the systemd sandbox). Roll the in-memory config back so the GET view and
        # the live PromoDirector stay consistent — otherwise the panel would show
        # the new value while promos kept running with the old one.
        config.promos = previous
        raise HTTPException(500, f"Could not persist promo config: {e}") from e

    director = getattr(request.app.state, "promo_director", None)
    if director is not None:
        director.update_config(new_cfg)

    return {"config": new_cfg.model_dump()}


@router.get("/pools")
async def get_promo_pools(request: Request, user: dict = Depends(require_admin)):
    """List saved playlists that are designated promo pools, with item counts."""
    db = request.app.state.db
    pools = await db.get_promo_pools()
    out = []
    for p in pools:
        items = await db.get_saved_playlist_items(p["id"])
        out.append({**p, "item_count": len(items)})
    return out
