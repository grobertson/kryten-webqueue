"""Admin media library: validate, sanitize, and store uploaded images.

Every upload is identified by its magic bytes (the client's MIME type and
filename are ignored), decoded under pixel/frame limits, and re-encoded from
fresh frame copies so EXIF/XMP/comments never reach the public URL. Animated
GIF/WebP keep their frame count, per-frame durations, and loop count.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import shutil
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

FORMATS = {
    "gif": ("image/gif", ".gif"),
    "png": ("image/png", ".png"),
    "jpeg": ("image/jpeg", ".jpg"),
    "webp": ("image/webp", ".webp"),
}
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class MediaError(ValueError):
    """An upload was rejected; the message is safe to show the admin."""


@dataclass
class ProcessedImage:
    data: bytes
    format: str
    mime: str
    ext: str
    width: int
    height: int
    frame_count: int
    animated: bool
    sha256: str


def sniff(head: bytes) -> str | None:
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def slugify(text: str | None) -> str:
    stem = Path(text or "").stem if text and "." in text else (text or "")
    slug = _SLUG_STRIP.sub("-", stem.lower()).strip("-")[:80].strip("-")
    return slug or "image"


def _open(data: bytes, max_pixels: int) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            img = Image.open(io.BytesIO(data))
            if img.width * img.height > max_pixels:
                raise MediaError(
                    f"Image is {img.width}x{img.height}; the limit is {max_pixels} pixels"
                )
            return img
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise MediaError("Image dimensions are too large") from exc
        except MediaError:
            raise
        except Exception as exc:  # noqa: BLE001 - any decoder failure is a bad upload
            raise MediaError("File is not a readable image") from exc


def _frames(img: Image.Image) -> tuple[list[Image.Image], list[int]]:
    frames, durations = [], []
    for frame in ImageSequence.Iterator(img):
        frame.load()
        durations.append(
            int(frame.info.get("duration") or img.info.get("duration") or 100)
        )
        copy = frame.convert("RGBA")
        copy.info = {}
        frames.append(copy)
    return frames, durations


def _total_duration(img: Image.Image) -> int:
    total = 0
    for frame in ImageSequence.Iterator(img):
        frame.load()
        total += int(frame.info.get("duration") or 0)
    return total


def _check_animation(
    encoded: bytes, frame_count: int, source: Image.Image | None
) -> None:
    """Reject re-encodes that lose frames; merged identical frames keep total time."""
    check = Image.open(io.BytesIO(encoded))
    out_frames = int(getattr(check, "n_frames", 1))
    if out_frames == frame_count:
        return
    if source is not None and out_frames > 1:
        if _total_duration(check) == _total_duration(source):
            return
    raise MediaError("Re-encoding would drop animation frames; upload rejected")


def process_image(data: bytes, *, max_pixels: int, max_frames: int) -> ProcessedImage:
    fmt = sniff(data[:16])
    if fmt is None:
        raise MediaError("Unsupported file type; use GIF, PNG, JPEG, or WebP")

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            _open(data, max_pixels).verify()
        except MediaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MediaError("Image is corrupt or truncated") from exc

        img = _open(data, max_pixels)
        frame_count = int(getattr(img, "n_frames", 1))
        if frame_count > max_frames:
            raise MediaError(
                f"Animation has {frame_count} frames; the limit is {max_frames}"
            )
        if fmt == "png" and frame_count > 1:
            raise MediaError("Animated PNG (APNG) is not supported; use GIF or WebP")
        if fmt == "jpeg" and frame_count > 1:
            raise MediaError("Multi-picture JPEG is not supported")

        out = io.BytesIO()
        try:
            if frame_count > 1:
                frames, durations = _frames(img)
                options: dict = {
                    "save_all": True,
                    "append_images": frames[1:],
                    "duration": durations,
                }
                if "loop" in img.info:
                    options["loop"] = int(img.info["loop"])
                if fmt == "gif":
                    options["disposal"] = 2
                    options["optimize"] = False
                else:
                    options["quality"] = 90
                    options["method"] = 4
                frames[0].save(out, format=fmt.upper(), **options)
            else:
                clean = ImageOps.exif_transpose(img) or img
                icc = img.info.get("icc_profile")
                transparency = img.info.get("transparency")
                clean = clean.copy()
                clean.info = {}
                options = {"icc_profile": icc} if icc and fmt != "gif" else {}
                if fmt == "jpeg":
                    if clean.mode not in ("RGB", "L"):
                        clean = clean.convert("RGB")
                    options.update(quality=92, optimize=True)
                elif fmt == "webp":
                    options.update(quality=90, method=4)
                elif fmt in ("png", "gif") and transparency is not None:
                    options["transparency"] = transparency
                clean.save(out, format=fmt.upper(), **options)
        except MediaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"Could not re-encode the image: {exc}") from exc

    encoded = out.getvalue()
    _check_animation(encoded, frame_count, img if frame_count > 1 else None)
    mime, ext = FORMATS[fmt]
    return ProcessedImage(
        data=encoded,
        format=fmt,
        mime=mime,
        ext=ext,
        width=img.width,
        height=img.height,
        frame_count=frame_count,
        animated=frame_count > 1,
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def ensure_dir(path: str, setting: str) -> Path:
    directory = Path(path).expanduser()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MediaError(
            f"{setting} {directory} is not writable by this process ({exc.strerror or exc})"
        ) from exc
    return directory


def write_atomic(directory: Path, filename: str, data: bytes) -> Path:
    target = directory / filename
    tmp = directory / f".{filename}.{secrets.token_hex(4)}.tmp"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return target


def move_to_trash(public_dir: Path, trash_dir: Path, filename: str) -> None:
    source = public_dir / filename
    if not source.exists():
        return
    destination = trash_dir / f"{filename}.{secrets.token_hex(4)}"
    try:
        os.replace(source, destination)
    except OSError:
        shutil.move(str(source), str(destination))


def references(body: str, slug: str, filename: str) -> bool:
    if filename in body:
        return True
    return (
        re.search(r"media_url\(\s*['\"]" + re.escape(slug) + r"['\"]\s*\)", body)
        is not None
    )
