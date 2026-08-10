"""Google Drive upload support for final Daily Report PDFs.

Credentials are read only from environment variables.  The Google client
libraries are imported lazily so the Daily Report application can still run
normally when Drive has not been configured yet.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping


DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

INDONESIAN_MONTHS = (
    "",
    "Januari",
    "Februari",
    "Maret",
    "April",
    "Mei",
    "Juni",
    "Juli",
    "Agustus",
    "September",
    "Oktober",
    "November",
    "Desember",
)

PROJECT_NUMBER_CATEGORIES = {
    "002/kn-gpa/epc-2k-p2/xi/2025": "electrical",
    "pc-26-0004-kn-gpa-029-dar": "electrical",
    "p01.0825.j075": "control_valve",
    "001/kn-gpa/epc-2f-p2/iv/2025": "turbine_generator",
}

CATEGORY_FOLDER_NAMES = {
    "electrical": "Daily Reports Electrical",
    "control_valve": "Daily Reports Control Valve",
    "turbine_generator": "Daily Reports Turbine & Generator",
}


class GoogleDriveError(RuntimeError):
    """Base error safe to show as a short upload failure."""


class GoogleDriveNotConfigured(GoogleDriveError):
    pass


class ProjectCategoryError(GoogleDriveError):
    pass


class GoogleDriveUploadError(GoogleDriveError):
    pass


class GoogleDriveReauthorizationRequired(GoogleDriveUploadError):
    pass


class GoogleDrivePermissionError(GoogleDriveUploadError):
    pass


@dataclass(frozen=True)
class GoogleDriveConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    parent_folder_id: str = "root"
    root_folder_name: str = "Daily Reports"

    @classmethod
    def from_env(cls) -> "GoogleDriveConfig":
        return cls(
            client_id=os.environ.get("GDRIVE_CLIENT_ID", "").strip(),
            client_secret=os.environ.get("GDRIVE_CLIENT_SECRET", "").strip(),
            refresh_token=os.environ.get("GDRIVE_REFRESH_TOKEN", "").strip(),
            parent_folder_id=os.environ.get("GDRIVE_PARENT_FOLDER_ID", "").strip() or "root",
            root_folder_name=os.environ.get("GDRIVE_ROOT_FOLDER_NAME", "").strip()
            or "Daily Reports",
        )

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)


def google_drive_is_configured(config: GoogleDriveConfig | None = None) -> bool:
    return (config or GoogleDriveConfig.from_env()).configured


def _normalise(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("&", " and ").replace("/", " ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _normalise_project_number(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().casefold())


def resolve_project_category(project_title: Any, project_no: Any = "") -> str:
    """Resolve a known project to one of the three requested Drive branches."""

    number_match = PROJECT_NUMBER_CATEGORIES.get(_normalise_project_number(project_no))
    title = _normalise(project_title)
    matches: set[str] = set()
    if "control valve" in title or "on off valve" in title:
        matches.add("control_valve")
    if "turbine" in title or "generator" in title:
        matches.add("turbine_generator")
    if "electrical" in title or "manpower supply" in title:
        matches.add("electrical")

    if len(matches) > 1:
        raise ProjectCategoryError(
            "Project title matches more than one Google Drive branch. Review the project title first."
        )
    if len(matches) == 1:
        title_match = next(iter(matches))
        if number_match and number_match != title_match:
            raise ProjectCategoryError(
                "Project title and project number point to different Google Drive branches. "
                "Review the project data first."
            )
        return title_match
    if number_match:
        return number_match
    if not title:
        raise ProjectCategoryError(
            "Project title is empty. Choose a project before uploading to Google Drive."
        )
    raise ProjectCategoryError(
        "Project title does not match Electrical, Control Valve, or Turbine & Generator."
    )


def build_drive_folder_path(
    *,
    project_title: Any,
    project_no: Any,
    report_date: Any,
    root_folder_name: str = "Daily Reports",
    category_override: Any = "",
) -> tuple[str, list[str]]:
    """Return category and folder path using report month, then report year."""

    category_override = str(category_override or "").strip()
    if category_override and category_override not in CATEGORY_FOLDER_NAMES:
        raise ProjectCategoryError("Invalid Google Drive project folder selection.")
    category = category_override or resolve_project_category(project_title, project_no)
    try:
        parsed_date = date.fromisoformat(str(report_date or "").strip())
    except ValueError as exc:
        raise GoogleDriveError("Report date must use YYYY-MM-DD before Drive upload.") from exc

    month_folder = f"{parsed_date.month:02d} - {INDONESIAN_MONTHS[parsed_date.month]}"
    return category, [
        str(root_folder_name or "Daily Reports").strip() or "Daily Reports",
        CATEGORY_FOLDER_NAMES[category],
        month_folder,
        str(parsed_date.year),
    ]


def _escape_query_value(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _truncate_utf8(value: Any, max_bytes: int) -> str:
    encoded = str(value or "").encode("utf-8")[:max_bytes]
    return encoded.decode("utf-8", errors="ignore")


class GoogleDriveUploader:
    def __init__(
        self,
        config: GoogleDriveConfig | None = None,
        *,
        service: Any | None = None,
        media_factory: Any | None = None,
    ) -> None:
        self.config = config or GoogleDriveConfig.from_env()
        if not self.config.configured and service is None:
            raise GoogleDriveNotConfigured(
                "Google Drive is not configured. Add GDRIVE_CLIENT_ID, "
                "GDRIVE_CLIENT_SECRET, and GDRIVE_REFRESH_TOKEN to Railway Variables."
            )
        self._service = service
        self._media_factory = media_factory

    def _drive(self) -> Any:
        if self._service is not None:
            return self._service
        try:
            import httplib2
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from google_auth_httplib2 import AuthorizedHttp
        except ImportError as exc:  # pragma: no cover - depends on deployment packages
            raise GoogleDriveNotConfigured(
                "Google Drive libraries are not installed. Install requirements.txt first."
            ) from exc

        credentials = Credentials(
            token=None,
            refresh_token=self.config.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self.config.client_id,
            client_secret=self.config.client_secret,
            scopes=[DRIVE_FILE_SCOPE],
        )
        transport = AuthorizedHttp(credentials, http=httplib2.Http(timeout=60))
        self._service = build(
            "drive",
            "v3",
            http=transport,
            cache_discovery=False,
        )
        return self._service

    @staticmethod
    def _execute(request: Any) -> Any:
        """Execute a Drive request with bounded retries for transient API errors."""

        return request.execute(num_retries=3)

    def _find_folder(self, name: str, parent_id: str) -> dict[str, Any] | None:
        query = (
            f"name = '{_escape_query_value(name)}' and "
            f"'{_escape_query_value(parent_id)}' in parents and "
            f"mimeType = '{FOLDER_MIME_TYPE}' and trashed = false"
        )
        request = self._drive().files().list(
            q=query,
            spaces="drive",
            fields="files(id,name,webViewLink)",
            orderBy="createdTime",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        result = self._execute(request)
        rows = result.get("files", []) if isinstance(result, Mapping) else []
        return dict(rows[0]) if rows else None

    def _get_or_create_folder(self, name: str, parent_id: str) -> dict[str, Any]:
        existing = self._find_folder(name, parent_id)
        if existing:
            return existing
        request = self._drive().files().create(
            body={"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        )
        created = self._execute(request)
        if not isinstance(created, Mapping) or not created.get("id"):
            raise GoogleDriveUploadError("Google Drive did not return a folder ID.")
        return dict(created)

    def _find_file(self, report_key: str, parent_id: str) -> dict[str, Any] | None:
        query = (
            "appProperties has { key = 'gpaReportKey' and "
            f"value = '{_escape_query_value(report_key)}' }} and "
            f"'{_escape_query_value(parent_id)}' in parents and "
            f"mimeType = 'application/pdf' and trashed = false"
        )
        request = self._drive().files().list(
            q=query,
            spaces="drive",
            fields="files(id,name,md5Checksum,webViewLink)",
            orderBy="modifiedTime desc",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        result = self._execute(request)
        rows = result.get("files", []) if isinstance(result, Mapping) else []
        return dict(rows[0]) if rows else None

    def upload_pdf(
        self,
        pdf_bytes: bytes,
        *,
        filename: str,
        project_title: str,
        project_no: str,
        report_date: str,
        category_override: str = "",
    ) -> dict[str, Any]:
        if not pdf_bytes or not bytes(pdf_bytes).startswith(b"%PDF"):
            raise GoogleDriveError("Only a generated PDF can be uploaded to Google Drive.")
        if not filename or os.path.basename(filename) != filename:
            raise GoogleDriveError("Invalid Daily Report filename.")

        category, folder_path = build_drive_folder_path(
            project_title=project_title,
            project_no=project_no,
            report_date=report_date,
            root_folder_name=self.config.root_folder_name,
            category_override=category_override,
        )
        normalized_number = _normalise_project_number(project_no)
        project_identity = (
            f"number:{normalized_number}"
            if normalized_number
            else f"title:{_normalise(project_title)}"
        )
        report_key = hashlib.sha256(
            f"{category}\0{project_identity}\0{str(report_date).strip()}\0{filename}".encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        app_properties = {
            "gpaReportKey": report_key,
            # Leave room for the appProperties key within Drive's byte limit.
            "projectNo": _truncate_utf8(project_no, 96),
            "reportDate": str(report_date or "")[:20],
        }
        try:
            parent_id = self.config.parent_folder_id
            folder_ids = []
            for folder_name in folder_path:
                folder = self._get_or_create_folder(folder_name, parent_id)
                parent_id = str(folder["id"])
                folder_ids.append(parent_id)

            if self._media_factory is None:
                try:
                    from googleapiclient.http import MediaIoBaseUpload
                except ImportError as exc:  # pragma: no cover - deployment dependency
                    raise GoogleDriveNotConfigured(
                        "Google Drive libraries are not installed. Install requirements.txt first."
                    ) from exc
                self._media_factory = MediaIoBaseUpload

            media = self._media_factory(
                io.BytesIO(bytes(pdf_bytes)),
                mimetype="application/pdf",
                chunksize=5 * 1024 * 1024,
                resumable=True,
            )
            local_md5 = hashlib.md5(bytes(pdf_bytes), usedforsecurity=False).hexdigest()
            existing = self._find_file(report_key, parent_id)
            if existing and existing.get("md5Checksum") == local_md5:
                uploaded = existing
                status = "existing"
            elif existing:
                request = self._drive().files().update(
                    fileId=str(existing["id"]),
                    body={"name": filename, "appProperties": app_properties},
                    media_body=media,
                    fields="id,name,md5Checksum,webViewLink",
                    supportsAllDrives=True,
                )
                uploaded = self._execute(request)
                status = "updated"
            else:
                request = self._drive().files().create(
                    body={
                        "name": filename,
                        "mimeType": "application/pdf",
                        "parents": [parent_id],
                        "appProperties": app_properties,
                    },
                    media_body=media,
                    fields="id,name,md5Checksum,webViewLink",
                    supportsAllDrives=True,
                )
                uploaded = self._execute(request)
                status = "uploaded"
        except GoogleDriveError:
            raise
        except Exception as exc:
            response = getattr(exc, "resp", None)
            status = getattr(response, "status", None)
            error_text = str(exc).casefold()
            if status == 401 or "invalid_grant" in error_text:
                raise GoogleDriveReauthorizationRequired(
                    "Google Drive authorization has expired or was revoked. "
                    "Create a new refresh token."
                ) from exc
            if status == 403:
                raise GoogleDrivePermissionError(
                    "Google Drive denied access. Check the connected account and parent folder."
                ) from exc
            raise GoogleDriveUploadError(
                "Google Drive upload failed. Check the OAuth variables and folder access."
            ) from exc

        if not isinstance(uploaded, Mapping) or not uploaded.get("id"):
            raise GoogleDriveUploadError("Google Drive did not return a file ID.")
        file_id = str(uploaded["id"])
        return {
            "status": status,
            "file_id": file_id,
            "web_view_link": str(
                uploaded.get("webViewLink")
                or f"https://drive.google.com/file/d/{file_id}/view"
            ),
            "filename": filename,
            "category": category,
            "folder_path": folder_path,
            "folder_ids": folder_ids,
            "md5_checksum": str(uploaded.get("md5Checksum") or local_md5),
            "report_key": report_key,
        }


def upload_daily_report_pdf(
    pdf_bytes: bytes,
    *,
    filename: str,
    project_title: str,
    project_no: str,
    report_date: str,
    category_override: str = "",
    config: GoogleDriveConfig | None = None,
) -> dict[str, Any]:
    return GoogleDriveUploader(config).upload_pdf(
        pdf_bytes,
        filename=filename,
        project_title=project_title,
        project_no=project_no,
        report_date=report_date,
        category_override=category_override,
    )
