#!/usr/bin/env python3
"""Add a read-only bulk facets endpoint to MediaCMS so integrations can pull
every item's friendly_token + tags + categories in a few paginated calls
instead of one GET per media. Editor/superuser only. Idempotent."""

import sys

BASE = "/home/mediacms.io/mediacms/files"
MEDIA = f"{BASE}/views/media.py"
INIT = f"{BASE}/views/__init__.py"
URLS = f"{BASE}/urls.py"

VIEW_MARKER = "class MediaFacetsList(APIView):"
VIEW_CODE = '''

class MediaFacetsList(APIView):
    """Bulk read-only listing of media friendly_token + tags + categories.

    Lets trusted integrations (MediaCMS editors) pull every item's facets in a
    few paginated calls instead of one GET per media. Slice pagination by id.
    """

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, format=None):
        if not is_mediacms_editor(request.user):
            return Response({"detail": "not allowed"}, status=status.HTTP_403_FORBIDDEN)
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(request.query_params.get("page_size", 500))
        except (TypeError, ValueError):
            page_size = 500
        page_size = max(1, min(2000, page_size))
        start = (page - 1) * page_size
        rows = Media.objects.order_by("id").prefetch_related("tags", "category")[
            start : start + page_size
        ]
        results = [
            {
                "friendly_token": m.friendly_token,
                "tags": [t.title for t in m.tags.all()],
                "categories": [c.title for c in m.category.all()],
            }
            for m in rows
        ]
        return Response(
            {
                "page": page,
                "page_size": page_size,
                "has_next": len(results) == page_size,
                "results": results,
            }
        )
'''

INIT_ANCHOR = "from .media import MediaBulkUserActions  # noqa: F401\n"
INIT_ADD = "from .media import MediaFacetsList  # noqa: F401\n"

URL_ANCHOR = '    re_path(r"^api/v1/media/user/bulk_actions/$", views.MediaBulkUserActions.as_view()),\n'
URL_ADD = '    re_path(r"^api/v1/media_facets$", views.MediaFacetsList.as_view()),\n'


def patch(path, check, apply_fn):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if check in src:
        print(f"{path}: already patched")
        return True
    new = apply_fn(src)
    if new is None:
        print(f"ERROR: anchor not found in {path}")
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"{path}: patched")
    return True


def main() -> int:
    ok = True
    ok &= patch(MEDIA, VIEW_MARKER, lambda s: s.rstrip("\n") + "\n" + VIEW_CODE)
    ok &= patch(
        INIT,
        INIT_ADD.strip(),
        lambda s: (
            s.replace(INIT_ANCHOR, INIT_ANCHOR + INIT_ADD, 1)
            if INIT_ANCHOR in s
            else None
        ),
    )
    ok &= patch(
        URLS,
        "media_facets",
        lambda s: (
            s.replace(URL_ANCHOR, URL_ANCHOR + URL_ADD, 1) if URL_ANCHOR in s else None
        ),
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
