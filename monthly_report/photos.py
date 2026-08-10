"""Bounded extraction and storage helpers for periodic-report photographs.

Uploaded Daily Report PDFs are untrusted input.  This module therefore keeps
image decoding resource-bounded, normalises accepted images to JPEG, removes
exact duplicates and recurring document artwork, and returns bytes separately
from JSON-safe metadata.  Callers persist the bytes in a draft-local asset
directory; base64 image payloads are deliberately not stored in draft JSON.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import warnings as python_warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping


_PHOTO_HEADING_RE = re.compile(
    r"\b(?:PHOTO\s+DOCUMENTATION|PHOTOGRAPHS?\s+ACTIVIT(?:Y|IES))\b",
    re.IGNORECASE,
)
_ASSET_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "JPEG2000"}


@dataclass(frozen=True)
class PhotoLimits:
    """Resource bounds for one PDF and one resulting report draft."""

    max_pdf_bytes: int = 50 * 1024 * 1024
    max_images_per_pdf: int = 15
    max_images_per_draft: int = 60
    max_embedded_image_bytes: int = 12 * 1024 * 1024
    max_asset_bytes: int = 2 * 1024 * 1024
    max_total_asset_bytes_per_pdf: int = 16 * 1024 * 1024
    max_total_asset_bytes_per_draft: int = 32 * 1024 * 1024
    max_pixels: int = 40_000_000
    min_dimension: int = 120
    min_pixels: int = 45_000
    max_dimension: int = 1280
    jpeg_quality: int = 78

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            if int(getattr(self, field)) <= 0:
                raise ValueError(f"{field} must be greater than zero")


DEFAULT_PHOTO_LIMITS = PhotoLimits()


def is_asset_id(value: Any) -> bool:
    return bool(_ASSET_ID_RE.fullmatch(str(value or "")))


def asset_filename(asset_id: Any) -> str:
    value = str(asset_id or "")
    if not is_asset_id(value):
        raise ValueError("Invalid photo asset ID")
    return f"{value}.jpg"


def _read_source(
    source: bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO,
    *,
    maximum: int,
) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    elif isinstance(source, (str, os.PathLike)):
        path = Path(source)
        if path.stat().st_size > maximum:
            raise ValueError("PDF exceeds the photo extraction size limit")
        with path.open("rb") as handle:
            data = handle.read(maximum + 1)
    else:
        stream = getattr(source, "stream", source)
        if not hasattr(stream, "read"):
            raise ValueError("PDF photo source must be a byte stream")
        position = None
        try:
            if hasattr(stream, "tell"):
                position = stream.tell()
            if hasattr(stream, "seek"):
                stream.seek(0)
            data = stream.read(maximum + 1)
        finally:
            if position is not None and hasattr(stream, "seek"):
                try:
                    stream.seek(position)
                except (OSError, ValueError):
                    pass
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("PDF photo stream must return bytes")
    data = bytes(data)
    if len(data) > maximum:
        raise ValueError("PDF exceeds the photo extraction size limit")
    if not data.startswith(b"%PDF-"):
        raise ValueError("File does not have a valid PDF header")
    return data


def _page_text(page: Any) -> str:
    try:
        return str(page.extract_text() or "")
    except Exception:
        return ""


def _normalise_image(raw: bytes, limits: PhotoLimits) -> tuple[bytes, int, int] | None:
    """Decode one embedded image and return a bounded orientation-safe JPEG."""

    if not raw or len(raw) > limits.max_embedded_image_bytes:
        return None
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None

    try:
        with python_warnings.catch_warnings():
            python_warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as opened:
                image_format = str(opened.format or "").upper()
                if image_format not in _ALLOWED_IMAGE_FORMATS:
                    return None
                width, height = opened.size
                if (
                    width < limits.min_dimension
                    or height < limits.min_dimension
                    or width * height < limits.min_pixels
                    or width * height > limits.max_pixels
                ):
                    return None
                # Header logos and signatures tend to be very wide and short.
                ratio = max(width / max(height, 1), height / max(width, 1))
                if ratio > 3.75:
                    return None

                image = ImageOps.exif_transpose(opened)
                image.load()
                if max(image.size) > limits.max_dimension:
                    image.thumbnail(
                        (limits.max_dimension, limits.max_dimension),
                        Image.Resampling.LANCZOS,
                    )
                if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                elif image.mode != "RGB":
                    image = image.convert("RGB")

                encoded = io.BytesIO()
                image.save(
                    encoded,
                    format="JPEG",
                    quality=limits.jpeg_quality,
                    optimize=True,
                    progressive=True,
                )
                payload = encoded.getvalue()
                if len(payload) > limits.max_asset_bytes:
                    # A second bounded pass avoids retaining unusually noisy,
                    # oversized photographs in a report draft.
                    image.thumbnail((960, 960), Image.Resampling.LANCZOS)
                    encoded = io.BytesIO()
                    image.save(encoded, format="JPEG", quality=68, optimize=True)
                    payload = encoded.getvalue()
                if len(payload) > limits.max_asset_bytes:
                    return None
                return payload, int(image.width), int(image.height)
    except Exception:
        return None


def _iter_page_images(page: Any) -> Iterable[bytes]:
    try:
        images = page.images
    except Exception:
        return ()
    result: list[bytes] = []
    try:
        for image in images:
            raw = getattr(image, "data", None)
            if isinstance(raw, (bytes, bytearray, memoryview)):
                result.append(bytes(raw))
    except Exception:
        return result
    return result


def extract_pdf_photo_candidates(
    source: bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO,
    *,
    filename: str = "report.pdf",
    limits: PhotoLimits = DEFAULT_PHOTO_LIMITS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract reviewable photographs from a Daily Report PDF.

    Each returned item contains JSON-safe metadata plus a temporary ``content``
    byte value.  ``store_photo_candidates`` removes that byte value before the
    metadata is written to JSON.
    """

    try:
        from pypdf import PdfReader
    except ImportError:
        return [], [f"{filename}: photo extraction requires pypdf."]

    try:
        data = _read_source(source, maximum=limits.max_pdf_bytes)
        reader = PdfReader(io.BytesIO(data), strict=False)
        if getattr(reader, "is_encrypted", False):
            return [], [f"{filename}: encrypted PDF photos were not processed."]
    except Exception:
        return [], [f"{filename}: embedded photos could not be inspected safely."]

    pages = list(reader.pages)
    heading_pages = {
        page_number
        for page_number, page in enumerate(pages, start=1)
        if _PHOTO_HEADING_RE.search(_page_text(page))
    }
    # Photo grids can flow onto following pages without repeating the heading.
    # In all supported Daily Report templates Photo Documentation is the final
    # section, so its first heading safely anchors the remaining pages.
    photo_pages = (
        set(range(min(heading_pages), len(pages) + 1))
        if heading_pages
        else set()
    )
    candidates: list[dict[str, Any]] = []
    digest_pages: dict[str, set[int]] = {}
    normalized_cache: dict[str, tuple[bytes, int, int] | None] = {}
    skipped_unsafe = 0

    for page_number, page in enumerate(pages, start=1):
        for raw in _iter_page_images(page):
            raw_digest = hashlib.sha256(raw).hexdigest()
            if raw_digest not in normalized_cache:
                normalized_cache[raw_digest] = _normalise_image(raw, limits)
            normalized = normalized_cache[raw_digest]
            if normalized is None:
                skipped_unsafe += 1
                continue
            content, width, height = normalized
            digest = hashlib.sha256(content).hexdigest()
            digest_pages.setdefault(digest, set()).add(page_number)
            candidates.append({
                "asset_id": digest,
                "content": content,
                "source": str(filename or "report.pdf")[:255],
                "page": page_number,
                "width": width,
                "height": height,
            })

    warnings: list[str] = []
    if not candidates:
        if skipped_unsafe:
            warnings.append(
                f"{filename}: no reviewable photos were found; {skipped_unsafe} small or unsupported image(s) were ignored."
            )
        return [], warnings

    # If the template labels photo pages, those pages are authoritative.  A
    # digest also seen outside them is recurring artwork (normally a logo), so
    # it must not appear in Appendix 6.6.
    useful: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_count = 0
    artwork_count = 0
    total_bytes = 0
    for candidate in candidates:
        digest = candidate["asset_id"]
        pages_for_digest = digest_pages.get(digest, set())
        if photo_pages:
            if candidate["page"] not in photo_pages:
                continue
            if any(page not in photo_pages for page in pages_for_digest):
                artwork_count += 1
                continue
        elif len(pages_for_digest) > 1:
            artwork_count += 1
            continue
        if digest in seen:
            duplicate_count += 1
            continue
        content_size = len(candidate["content"])
        if total_bytes + content_size > limits.max_total_asset_bytes_per_pdf:
            warnings.append(
                f"{filename}: photo extraction stopped at the per-file asset byte limit."
            )
            break
        if len(useful) >= limits.max_images_per_pdf:
            warnings.append(
                f"{filename}: only the first {limits.max_images_per_pdf} reviewable photos were retained."
            )
            break
        seen.add(digest)
        total_bytes += content_size
        useful.append(candidate)

    if duplicate_count:
        warnings.append(f"{filename}: {duplicate_count} duplicate photo occurrence(s) were removed.")
    if artwork_count:
        warnings.append(
            f"{filename}: {artwork_count} recurring header/logo image occurrence(s) were excluded."
        )
    if skipped_unsafe:
        warnings.append(
            f"{filename}: {skipped_unsafe} small, oversized, or unsupported image occurrence(s) were ignored."
        )
    if candidates and not useful:
        warnings.append(f"{filename}: no useful Photo Documentation images remained after filtering.")
    return useful, warnings


def store_photo_candidates(
    candidates: Iterable[Mapping[str, Any]],
    directory: str | os.PathLike[str],
    *,
    source_report_id: str,
    maximum: int = DEFAULT_PHOTO_LIMITS.max_images_per_pdf,
    max_total_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Persist normalized bytes by digest and return JSON-safe references."""

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    stored_bytes = 0
    if max_total_bytes is not None:
        for existing in root.glob("*.jpg"):
            try:
                if is_asset_id(existing.stem):
                    stored_bytes += existing.stat().st_size
            except OSError:
                continue
    result: list[dict[str, Any]] = []
    for order, item in enumerate(candidates):
        if len(result) >= maximum:
            break
        asset_id = str(item.get("asset_id") or "")
        content = item.get("content")
        if not is_asset_id(asset_id) or not isinstance(content, bytes):
            continue
        if hashlib.sha256(content).hexdigest() != asset_id:
            continue
        target = root / asset_filename(asset_id)
        if not target.exists():
            if max_total_bytes is not None and stored_bytes + len(content) > max_total_bytes:
                continue
            temporary = root / f".{asset_id}.tmp"
            try:
                with temporary.open("wb") as handle:
                    handle.write(content)
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
            stored_bytes += len(content)
        result.append({
            "schema_version": "periodic-photo/1",
            "asset_id": asset_id,
            "source_report_id": str(source_report_id or "")[:160],
            "source": str(item.get("source") or "")[:255],
            "page": max(1, int(item.get("page") or 1)),
            "width": max(1, int(item.get("width") or 1)),
            "height": max(1, int(item.get("height") or 1)),
            "size_bytes": len(content),
            "order": order,
            "caption": "",
        })
    return result


def copy_photo_assets(
    references: Iterable[Mapping[str, Any]],
    source_directory: str | os.PathLike[str],
    target_directory: str | os.PathLike[str],
) -> None:
    source_root = Path(source_directory)
    target_root = Path(target_directory)
    target_root.mkdir(parents=True, exist_ok=True)
    for reference in references:
        asset_id = str(reference.get("asset_id") or "")
        if not is_asset_id(asset_id):
            continue
        filename = asset_filename(asset_id)
        source = source_root / filename
        target = target_root / filename
        if not source.is_file() or target.exists():
            continue
        temporary = target_root / f".{asset_id}.tmp"
        try:
            with source.open("rb") as input_handle, temporary.open("wb") as output_handle:
                payload = input_handle.read(DEFAULT_PHOTO_LIMITS.max_asset_bytes + 1)
                if len(payload) > DEFAULT_PHOTO_LIMITS.max_asset_bytes:
                    continue
                if hashlib.sha256(payload).hexdigest() != asset_id:
                    continue
                output_handle.write(payload)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass


__all__ = [
    "DEFAULT_PHOTO_LIMITS",
    "PhotoLimits",
    "asset_filename",
    "copy_photo_assets",
    "extract_pdf_photo_candidates",
    "is_asset_id",
    "store_photo_candidates",
]
