#!/usr/bin/env python3
"""Patch MediaCMS MediaBulkUserActions so editors/superusers can tag any media,
and add_tags creates missing tags (normalized like Tag.save)."""

import sys

PATH = "/home/mediacms.io/mediacms/files/views/media.py"

QS_OLD = "        media = Media.objects.filter(user=request.user, friendly_token__in=media_ids)\n"
QS_NEW = (
    "        if is_mediacms_editor(request.user):\n"
    "            media = Media.objects.filter(friendly_token__in=media_ids)\n"
    "        else:\n"
    "            media = Media.objects.filter(user=request.user, friendly_token__in=media_ids)\n"
)

TAGS_OLD = (
    '        elif action == "add_tags":\n'
    "            tag_titles = request.data.get('tag_titles', [])\n"
    "            if not tag_titles:\n"
    '                return Response({"detail": "tag_titles is required for add_tags action"}, status=status.HTTP_400_BAD_REQUEST)\n'
    "\n"
    "            tags = Tag.objects.filter(title__in=tag_titles)\n"
    "            if not tags:\n"
    '                return Response({"detail": "No matching tags found"}, status=status.HTTP_400_BAD_REQUEST)\n'
    "\n"
    "            added_count = 0\n"
    "            for tag in tags:\n"
    "                for m in media:\n"
    "                    if not m.tags.filter(title=tag.title).exists():\n"
    "                        m.tags.add(tag)\n"
    "                        added_count += 1\n"
    "\n"
    '            return Response({"detail": f"Added {added_count} media items to {tags.count()} tags"})\n'
)
TAGS_NEW = (
    '        elif action == "add_tags":\n'
    "            tag_titles = request.data.get('tag_titles', [])\n"
    "            if not tag_titles:\n"
    '                return Response({"detail": "tag_titles is required for add_tags action"}, status=status.HTTP_400_BAD_REQUEST)\n'
    "\n"
    "            tags = []\n"
    "            for raw in tag_titles:\n"
    "                title = helpers.get_alphanumeric_only(raw)[:100]\n"
    "                if not title:\n"
    "                    continue\n"
    '                tag, _ = Tag.objects.get_or_create(title=title, defaults={"user": request.user})\n'
    "                tags.append(tag)\n"
    "            if not tags:\n"
    '                return Response({"detail": "No matching tags found"}, status=status.HTTP_400_BAD_REQUEST)\n'
    "\n"
    "            added_count = 0\n"
    "            for tag in tags:\n"
    "                for m in media:\n"
    "                    if not m.tags.filter(title=tag.title).exists():\n"
    "                        m.tags.add(tag)\n"
    "                        added_count += 1\n"
    "\n"
    '            return Response({"detail": f"Added {added_count} media items to {len(tags)} tags"})\n'
)


def main() -> int:
    with open(PATH, encoding="utf-8") as f:
        src = f.read()

    if QS_NEW in src and TAGS_NEW in src:
        print("Already patched; nothing to do.")
        return 0

    if src.count(QS_OLD) != 1:
        print(f"ERROR: queryset anchor found {src.count(QS_OLD)} times (expected 1)")
        return 2
    if src.count(TAGS_OLD) != 1:
        print(f"ERROR: add_tags anchor found {src.count(TAGS_OLD)} times (expected 1)")
        return 3

    src = src.replace(QS_OLD, QS_NEW, 1)
    src = src.replace(TAGS_OLD, TAGS_NEW, 1)

    with open(PATH, "w", encoding="utf-8") as f:
        f.write(src)
    print("Patched OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
