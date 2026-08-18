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
import unicodedata
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
_CANONICAL_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CANONICAL_DIGEST_RE = re.compile(r"^[a-fA-F0-9]{64}$")
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



def _looks_like_signature_or_line_art(content: bytes) -> bool:
    """Conservatively identify sparse monochrome signatures/line art.

    New Daily Report layouts can place SIGN-OFF and PHOTO DOCUMENTATION on the
    same page. pypdf exposes page image resources without reliable section
    coordinates, so page-level filtering alone may include signatures as photos.
    """

    try:
        from PIL import Image
    except ImportError:
        return False

    try:
        with Image.open(io.BytesIO(content)) as opened:
            image = opened.convert("RGB")
            image.thumbnail((256, 256))
            pixels = list(image.getdata())
    except Exception:
        return False

    if not pixels:
        return False

    total = len(pixels)
    white = 0
    dark = 0
    chromatic = 0
    for red, green, blue in pixels:
        low = min(red, green, blue)
        high = max(red, green, blue)
        if low >= 235:
            white += 1
        if high <= 120:
            dark += 1
        if high - low >= 30:
            chromatic += 1

    return (
        (white / total) >= 0.85
        and (dark / total) <= 0.18
        and (chromatic / total) <= 0.06
    )



def _normalise_photo_text(value: Any) -> str:
    """Normalise photo captions/activity text for deterministic matching."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _photo_status_suffix(value: Any) -> str:
    """Return the client-facing status token used by Daily Report photo cards."""

    status = _normalise_photo_text(value)
    labels = {
        "finished": "FINISH",
        "finish": "FINISH",
        "completed": "COMPLETE",
        "complete": "COMPLETE",
        "in progress": "IN PROGRESS",
        "ongoing": "ONGOING",
        "on hold": "ON HOLD",
        "pending": "PENDING",
    }
    label = labels.get(status)
    return f" ({label})" if label else ""


def _photo_activity_identifiers(value: Any) -> list[str]:
    """Return distinctive equipment/tag fragments usable as safe fallbacks."""

    text = str(value or "")
    identifiers: list[str] = []
    for match in re.finditer(
        r"(?<![A-Za-z0-9])(?:\d{1,4}\s*[- ]\s*)?[A-Za-z]{2,}"
        r"(?:\s*[- ]\s*[A-Za-z0-9]+){1,4}(?![A-Za-z0-9])",
        text,
    ):
        key = _normalise_photo_text(match.group(0))
        if key and any(char.isdigit() for char in key) and key not in identifiers:
            identifiers.append(key)
    return identifiers


def _photo_context_entries(areas: Iterable[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    """Build source-backed area/caption candidates from parsed Daily Report areas."""

    result: list[dict[str, str]] = []
    for area in areas or ():
        if not isinstance(area, Mapping):
            continue
        area_name = str(area.get("id") or area.get("area") or area.get("name") or "").strip()
        status_by_description: dict[str, str] = {}
        statuses = area.get("activity_statuses")
        for item in statuses if isinstance(statuses, list) else []:
            if not isinstance(item, Mapping):
                continue
            description_key = _normalise_photo_text(item.get("description"))
            status = str(item.get("status") or "").strip()
            if description_key and status:
                status_by_description[description_key] = status

        activities = area.get("activities_today")
        for description in activities if isinstance(activities, list) else []:
            description_text = " ".join(str(description or "").split()).strip()
            description_key = _normalise_photo_text(description_text)
            if not description_text or not description_key:
                continue
            status = status_by_description.get(description_key, "")
            result.append({
                "area": area_name[:255],
                "caption": (description_text + _photo_status_suffix(status))[:500],
                "key": description_key,
                "identifiers": "|".join(_photo_activity_identifiers(description_text)),
            })
    return result


def _match_photo_page_contexts(
    page_text: str,
    entries: list[dict[str, str]],
    *,
    trim_before_heading: bool,
) -> list[dict[str, str]]:
    """Match parsed activities to captions visible in one Photo Documentation page."""

    text = str(page_text or "")
    if trim_before_heading:
        heading = _PHOTO_HEADING_RE.search(text)
        if heading:
            text = text[heading.start():]
    normalised_page = _normalise_photo_text(text)
    if not normalised_page:
        return []

    matches: list[tuple[int, int, dict[str, str]]] = []
    seen: set[tuple[str, str]] = set()
    for order, entry in enumerate(entries):
        key = entry.get("key", "")
        position = normalised_page.find(key) if key else -1
        if position < 0:
            positions = [
                normalised_page.find(identifier)
                for identifier in entry.get("identifiers", "").split("|")
                if identifier
            ]
            positions = [value for value in positions if value >= 0]
            position = min(positions) if positions else -1
        if position < 0:
            continue
        identity = (entry.get("area", ""), entry.get("caption", ""))
        if identity in seen:
            continue
        seen.add(identity)
        matches.append((position, order, entry))

    matches.sort(key=lambda item: (item[0], item[1]))
    return [dict(item[2]) for item in matches]


def _attach_photo_contexts(
    candidates: list[dict[str, Any]],
    pages: list[Any],
    heading_pages: set[int],
    areas: Iterable[Mapping[str, Any]] | None,
) -> None:
    """Attach source_area/caption metadata to retained PDF photographs in-place."""

    entries = _photo_context_entries(areas)
    if not entries or not candidates:
        return

    by_page: dict[int, list[dict[str, Any]]] = {}
    for candidate in candidates:
        try:
            page_number = int(candidate.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if page_number > 0:
            by_page.setdefault(page_number, []).append(candidate)

    for page_number, page_candidates in by_page.items():
        if not (1 <= page_number <= len(pages)):
            continue
        contexts = _match_photo_page_contexts(
            _page_text(pages[page_number - 1]),
            entries,
            trim_before_heading=page_number in heading_pages,
        )
        if not contexts:
            continue

        # One activity can legitimately have several photographs.  When only
        # one caption is visible on the page, apply it to every retained image.
        if len(contexts) == 1:
            pairs = [(candidate, contexts[0]) for candidate in page_candidates]
        else:
            pairs = list(zip(page_candidates, contexts))

        for candidate, context in pairs:
            area = str(context.get("area") or "").strip()
            caption = str(context.get("caption") or "").strip()
            if area:
                candidate["source_area"] = area[:255]
            if caption:
                candidate["caption"] = caption[:500]

def extract_pdf_photo_candidates(
    source: bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO,
    *,
    filename: str = "report.pdf",
    limits: PhotoLimits = DEFAULT_PHOTO_LIMITS,
    areas: Iterable[Mapping[str, Any]] | None = None,
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
    skipped_signature_like = 0

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
            # In split-layout reports SIGN-OFF and PHOTO DOCUMENTATION can share
            # one page. Keep signature resources out of the activity appendix.
            if page_number in photo_pages and _looks_like_signature_or_line_art(content):
                skipped_signature_like += 1
                continue
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
    if skipped_signature_like:
        warnings.append(
            f"{filename}: {skipped_signature_like} signature/line-art image occurrence(s) were excluded from Photo Documentation."
        )
    if candidates and not useful:
        warnings.append(f"{filename}: no useful Photo Documentation images remained after filtering.")

    _attach_photo_contexts(useful, pages, heading_pages, areas)
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
        reference = {
            "schema_version": "periodic-photo/1",
            "asset_id": asset_id,
            "source_report_id": str(source_report_id or "")[:160],
            "source": str(item.get("source") or "")[:255],
            "width": max(1, int(item.get("width") or 1)),
            "height": max(1, int(item.get("height") or 1)),
            "size_bytes": len(content),
            "order": order,
            "caption": str(item.get("caption") or "")[:500],
        }
        # PDF candidates have meaningful page provenance. Canonical Stored
        # JSON assets do not, so do not invent "page 1" for them.
        try:
            page = int(item.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        if page > 0:
            reference["page"] = page
        for key, maximum_length in (("source_date", 10), ("source_area", 255)):
            value = str(item.get(key) or "").strip()
            if value:
                reference[key] = value[:maximum_length]
        result.append(reference)
    return result


def _canonical_photo_warning(
    record: Mapping[str, Any],
    area: Any,
    photo_index: int,
    message: str,
) -> str:
    report_id = str(record.get("report_id") or "unknown report")[:160]
    area_name = str(area or "unnamed area")[:160]
    return f"Stored JSON photo: {report_id} / {area_name} / photo {photo_index}: {message}"


def _canonical_asset_source(
    data_dir: str | os.PathLike[str],
    username: Any,
    asset_path: Any,
) -> Path:
    """Resolve an owner-scoped canonical asset without following an escape."""

    owner = str(username or "").strip()
    if (
        not _CANONICAL_OWNER_RE.fullmatch(owner)
        or owner in {".", ".."}
    ):
        raise ValueError("the Stored JSON owner is invalid")

    relative_text = str(asset_path or "").strip()
    if not relative_text or "\x00" in relative_text:
        raise ValueError("the canonical asset path is missing or unsafe")
    relative = Path(relative_text)
    if relative.is_absolute() or relative.drive:
        raise ValueError("the canonical asset path is absolute")

    canonical_root = (
        Path(data_dir).expanduser()
        / "users"
        / owner
        / "reports"
        / "canonical"
    ).resolve(strict=False)
    candidate = (canonical_root / relative).resolve(strict=False)
    try:
        candidate.relative_to(canonical_root)
    except ValueError as exc:
        raise ValueError("the canonical asset path escapes its owner directory") from exc

    if not candidate.is_file():
        raise FileNotFoundError("the canonical photo asset file is missing")

    # Re-resolve existing paths to catch a symlink/junction that points outside
    # the owner-specific canonical root.
    canonical_existing = canonical_root.resolve(strict=True)
    candidate_existing = candidate.resolve(strict=True)
    try:
        candidate_existing.relative_to(canonical_existing)
    except ValueError as exc:
        raise ValueError("the canonical asset resolves outside its owner directory") from exc
    return candidate_existing


def attach_canonical_photo_candidates(
    records: Iterable[dict[str, Any]],
    data_dir: str | os.PathLike[str],
    target_directory: str | os.PathLike[str],
    *,
    limits: PhotoLimits = DEFAULT_PHOTO_LIMITS,
) -> list[str]:
    """Attach safe Stored JSON photo references to canonical records.

    Only ``payload.areas[].photos[].asset`` entries are accepted. Source files
    are constrained to the canonical directory belonging to the trusted
    ``_canonical_owner`` injected by :func:`list_canonical_records`, verified
    against their archived size and SHA-256 digest,
    decoded within :class:`PhotoLimits`, and normalized to content-addressed
    JPEG assets in ``target_directory``. Each mutable record receives a
    JSON-safe ``_photo_candidates`` list. The return value contains reviewable
    warnings for rejected or legacy references.
    """

    warnings: list[str] = []
    suppressed_warnings = 0
    maximum_warnings = max(40, limits.max_images_per_draft * 2)

    def add_warning(message: str) -> None:
        nonlocal suppressed_warnings
        if len(warnings) < maximum_warnings:
            warnings.append(message)
        else:
            suppressed_warnings += 1

    records = list(records)
    mutable_records: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            add_warning(
                "Stored JSON photo: a non-object record was skipped while loading photos."
            )
            continue
        # Always clear stale candidates. Source validation may call this
        # function repeatedly with a different selected revision/project.
        record["_photo_candidates"] = []
        mutable_records.append(record)

    seen_assets: set[str] = set()
    total_asset_bytes = 0
    retained_assets = 0
    target = Path(target_directory)
    draft_limit_warning_added = False

    for record in mutable_records:
        if (
            retained_assets >= limits.max_images_per_draft
            or total_asset_bytes >= limits.max_total_asset_bytes_per_draft
        ):
            if not draft_limit_warning_added:
                add_warning(
                    "Stored JSON photo: the draft photo count or byte limit was reached; "
                    "remaining selected records were not scanned."
                )
                draft_limit_warning_added = True
            break
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        areas = payload.get("areas")
        if not isinstance(areas, list):
            continue

        candidates: list[dict[str, Any]] = []
        record_seen_assets: set[str] = set()
        record_asset_bytes = 0
        attempted_entries = 0
        maximum_attempts = max(
            limits.max_images_per_pdf,
            limits.max_images_per_pdf * 4,
        )
        record_limit_reached = False
        stop_record = False
        for area_index, area in enumerate(areas, start=1):
            if stop_record:
                break
            if not isinstance(area, Mapping):
                continue
            area_name = str(area.get("id") or area.get("name") or f"Area {area_index}").strip()
            photos = area.get("photos")
            if not isinstance(photos, list):
                continue
            for photo_index, photo in enumerate(photos, start=1):
                attempted_entries += 1
                if attempted_entries > maximum_attempts:
                    add_warning(
                        _canonical_photo_warning(
                            record,
                            area_name,
                            photo_index,
                            f"only the first {maximum_attempts} photo entries were inspected",
                        )
                    )
                    stop_record = True
                    break
                if len(candidates) >= limits.max_images_per_pdf:
                    if not record_limit_reached:
                        add_warning(
                            _canonical_photo_warning(
                                record,
                                area_name,
                                photo_index,
                                f"only the first {limits.max_images_per_pdf} photos were retained",
                            )
                        )
                        record_limit_reached = True
                    break
                if not isinstance(photo, Mapping):
                    continue
                asset = photo.get("asset")
                if not isinstance(asset, Mapping):
                    if photo.get("photo_filename"):
                        add_warning(
                            _canonical_photo_warning(
                                record,
                                area_name,
                                photo_index,
                                "legacy filename-only photo has no archived asset and cannot be included",
                            )
                        )
                    continue

                digest_text = str(asset.get("sha256") or "").strip()
                if not _CANONICAL_DIGEST_RE.fullmatch(digest_text):
                    add_warning(
                        _canonical_photo_warning(
                            record, area_name, photo_index, "canonical asset digest is invalid"
                        )
                    )
                    continue
                expected_digest = digest_text.lower()
                try:
                    declared_size = int(asset.get("size_bytes"))
                except (TypeError, ValueError):
                    declared_size = -1
                if (
                    declared_size <= 0
                    or declared_size > limits.max_embedded_image_bytes
                ):
                    add_warning(
                        _canonical_photo_warning(
                            record, area_name, photo_index, "canonical asset size is invalid or exceeds the limit"
                        )
                    )
                    continue

                try:
                    source = _canonical_asset_source(
                        data_dir,
                        record.get("_canonical_owner"),
                        asset.get("asset_path"),
                    )
                except (OSError, ValueError) as exc:
                    add_warning(
                        _canonical_photo_warning(record, area_name, photo_index, str(exc))
                    )
                    continue

                try:
                    actual_size = source.stat().st_size
                    if actual_size != declared_size:
                        add_warning(
                            _canonical_photo_warning(
                                record,
                                area_name,
                                photo_index,
                                "canonical asset size does not match its metadata",
                            )
                        )
                        continue
                    with source.open("rb") as handle:
                        raw = handle.read(limits.max_embedded_image_bytes + 1)
                except OSError:
                    add_warning(
                        _canonical_photo_warning(
                            record, area_name, photo_index, "canonical photo asset could not be read"
                        )
                    )
                    continue
                if len(raw) != declared_size or len(raw) > limits.max_embedded_image_bytes:
                    add_warning(
                        _canonical_photo_warning(
                            record, area_name, photo_index, "canonical photo asset exceeds the read limit"
                        )
                    )
                    continue
                if hashlib.sha256(raw).hexdigest() != expected_digest:
                    add_warning(
                        _canonical_photo_warning(
                            record, area_name, photo_index, "canonical asset hash does not match its metadata"
                        )
                    )
                    continue

                normalized = _normalise_image(raw, limits)
                if normalized is None:
                    add_warning(
                        _canonical_photo_warning(
                            record,
                            area_name,
                            photo_index,
                            "photo is unsupported, unsafe, or outside the image limits",
                        )
                    )
                    continue
                content, width, height = normalized
                asset_id = hashlib.sha256(content).hexdigest()
                is_new_for_record = asset_id not in record_seen_assets
                if is_new_for_record and (
                    record_asset_bytes + len(content)
                    > limits.max_total_asset_bytes_per_pdf
                ):
                    add_warning(
                        _canonical_photo_warning(
                            record,
                            area_name,
                            photo_index,
                            "photo exceeded the per-Daily-Report photo byte limit",
                        )
                    )
                    continue
                is_new_asset = asset_id not in seen_assets
                if is_new_asset and (
                    retained_assets >= limits.max_images_per_draft
                    or total_asset_bytes + len(content) > limits.max_total_asset_bytes_per_draft
                ):
                    if not draft_limit_warning_added:
                        add_warning(
                            _canonical_photo_warning(
                                record,
                                area_name,
                                photo_index,
                                "photo exceeded the overall Stored JSON photo count or byte limit",
                            )
                        )
                        draft_limit_warning_added = True
                    # A full count budget cannot accept any later unique
                    # image. Stop decoding the remainder of the selection.
                    if retained_assets >= limits.max_images_per_draft:
                        stop_record = True
                        break
                    # When only the byte budget is tight, a smaller later
                    # image may still fit, so continue within the scan cap.
                    continue
                if is_new_for_record:
                    record_seen_assets.add(asset_id)
                    record_asset_bytes += len(content)
                if is_new_asset:
                    seen_assets.add(asset_id)
                    retained_assets += 1
                    total_asset_bytes += len(content)
                report_date = str(record.get("date") or payload.get("date") or "").strip()[:10]
                source_label = " - ".join(
                    part for part in ("Stored JSON", report_date, area_name) if part
                )
                candidates.append({
                    "asset_id": asset_id,
                    "content": content,
                    "source": source_label[:255],
                    "source_date": report_date,
                    "source_area": area_name[:255],
                    "width": width,
                    "height": height,
                    "caption": str(photo.get("desc") or "")[:500],
                })

        record["_photo_candidates"] = store_photo_candidates(
            candidates,
            target,
            source_report_id=str(record.get("report_id") or "")[:160],
            maximum=limits.max_images_per_pdf,
            # Whole-draft limits are already applied above to the currently
            # selected records. Do not let stale assets from a prior Source
            # Validation choice block the new selection from being copied.
            max_total_bytes=None,
        )

    if suppressed_warnings:
        warnings.append(
            "Stored JSON photo: "
            f"{suppressed_warnings} additional photo warning(s) were suppressed."
        )
    return warnings


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
    "attach_canonical_photo_candidates",
    "asset_filename",
    "copy_photo_assets",
    "extract_pdf_photo_candidates",
    "is_asset_id",
    "store_photo_candidates",
]
