"""
Public-link Google Drive client — Phase 4 of docs/timeshare_buildout.md.

Design posture: NO OAuth, NO Drive Picker, NO `drive.readonly` scope. The
user flips their Drive folder (or individual file) to "anyone with the
link — viewer" and pastes the URL. We read it with an API key already in
Andy's Cloud project (CRAB_YOUTUBE_API_KEY — verified Drive-API-enabled).

Bytes fetched here are never persisted. For Google Docs we pull text via
the `/export?mimeType=text/plain` endpoint; for Sheets we pull CSV; for
uploaded PDFs we pull the binary, run pdfplumber in-request, then drop
it. The `document_refs` row keeps the Drive URL + file_id so the user's
own live copy remains the source of truth.

If a file/folder returns 404, the user has NOT flipped it public —
surface that as a clean "make the folder anyone-with-link viewable" flash.
"""

import logging
import re
from typing import Optional

import requests

logger = logging.getLogger('crab_travel.timeshare_drive')

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DOCS_EXPORT_BASE = "https://docs.google.com"

# MIME types we know how to read
MIME_GOOGLE_DOC = 'application/vnd.google-apps.document'
MIME_GOOGLE_SHEET = 'application/vnd.google-apps.spreadsheet'
MIME_GOOGLE_FOLDER = 'application/vnd.google-apps.folder'
MIME_PDF = 'application/pdf'

SUPPORTED_MIMES = {MIME_GOOGLE_DOC, MIME_GOOGLE_SHEET, MIME_PDF}


class DriveError(Exception):
    """User-friendly error — caller should flash the message."""
    pass


def _api_key():
    from utilities.google_auth_utils import get_secret
    return get_secret('CRAB_YOUTUBE_API_KEY')


# ── URL parsing ─────────────────────────────────────────────────────

_PATTERNS = [
    # https://drive.google.com/drive/folders/FOLDER_ID(?resourcekey=X)
    (re.compile(r'drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_\-]{10,})'), 'folder'),
    # https://drive.google.com/drive/u/0/folders/FOLDER_ID (alternate path)
    # Already covered above.
    # https://docs.google.com/document/d/FILE_ID/...
    (re.compile(r'docs\.google\.com/document/d/([A-Za-z0-9_\-]{10,})'), 'document'),
    # https://docs.google.com/spreadsheets/d/FILE_ID/...
    (re.compile(r'docs\.google\.com/spreadsheets/d/([A-Za-z0-9_\-]{10,})'), 'spreadsheet'),
    # https://drive.google.com/file/d/FILE_ID/...
    (re.compile(r'drive\.google\.com/file/d/([A-Za-z0-9_\-]{10,})'), 'file'),
    # https://drive.google.com/open?id=FILE_ID
    (re.compile(r'drive\.google\.com/open\?id=([A-Za-z0-9_\-]{10,})'), 'file'),
]


def parse_drive_url(url: str):
    """Returns (kind, id) or (None, None). kind ∈ {folder, document, spreadsheet, file}."""
    if not url:
        return (None, None)
    url = url.strip()
    for pat, kind in _PATTERNS:
        m = pat.search(url)
        if m:
            return (kind, m.group(1))
    return (None, None)


# ── Drive API — metadata + listing ──────────────────────────────────

def get_file_metadata(file_id: str) -> dict:
    """Returns {id, name, mimeType, modifiedTime} for a public file. Raises
    DriveError on 404 (not shared) or other failure."""
    r = requests.get(
        f"{DRIVE_API_BASE}/files/{file_id}",
        params={
            'key': _api_key(),
            'fields': 'id,name,mimeType,modifiedTime',
        },
        timeout=30,
    )
    if r.status_code == 404:
        raise DriveError("File not found, or not set to 'anyone with the link — viewer'.")
    if r.status_code == 403:
        raise DriveError("Drive refused the request — try making the file 'anyone with the link — viewer'.")
    r.raise_for_status()
    return r.json()


def list_folder_children(folder_id: str, max_items: int = 200) -> list:
    """Returns [{id, name, mimeType, modifiedTime}, ...] for a public folder.
    Skips subfolders for MVP (Phase 4.5 can recurse)."""
    r = requests.get(
        f"{DRIVE_API_BASE}/files",
        params={
            'key': _api_key(),
            'q': f"'{folder_id}' in parents and trashed = false",
            'fields': 'files(id,name,mimeType,modifiedTime,size)',
            'pageSize': max_items,
            'supportsAllDrives': 'false',
        },
        timeout=30,
    )
    if r.status_code == 404:
        raise DriveError("Folder not found, or not set to 'anyone with the link — viewer'.")
    if r.status_code == 403:
        raise DriveError("Drive refused the request — try making the folder 'anyone with the link — viewer'.")
    r.raise_for_status()
    return r.json().get('files', [])


# ── Content fetch (unauthenticated export endpoints) ────────────────

def fetch_doc_text(file_id: str) -> str:
    """Export a Google Doc as plain text. Requires only public-link access."""
    r = requests.get(
        f"{DRIVE_API_BASE}/files/{file_id}/export",
        params={'key': _api_key(), 'mimeType': 'text/plain'},
        timeout=60,
    )
    if r.status_code == 404:
        raise DriveError("Doc not found, or not set to 'anyone with the link — viewer'.")
    if r.status_code == 403:
        raise DriveError("Drive refused the request.")
    r.raise_for_status()
    # Export endpoint returns UTF-8 bytes with BOM sometimes; decode safely.
    return r.content.decode('utf-8-sig', errors='replace')


def fetch_sheet_csv(file_id: str) -> str:
    """Export a Google Sheet as CSV (first sheet)."""
    r = requests.get(
        f"{DRIVE_API_BASE}/files/{file_id}/export",
        params={'key': _api_key(), 'mimeType': 'text/csv'},
        timeout=60,
    )
    if r.status_code == 404:
        raise DriveError("Sheet not found, or not set to 'anyone with the link — viewer'.")
    if r.status_code == 403:
        raise DriveError("Drive refused the request.")
    r.raise_for_status()
    return r.content.decode('utf-8-sig', errors='replace')


def fetch_pdf_bytes(file_id: str) -> bytes:
    """Download a PDF's bytes for in-request pdfplumber extraction. Caller
    must discard the bytes after extracting text."""
    r = requests.get(
        f"{DRIVE_API_BASE}/files/{file_id}",
        params={'key': _api_key(), 'alt': 'media'},
        timeout=120,
        stream=False,
    )
    if r.status_code == 404:
        raise DriveError("PDF not found, or not set to 'anyone with the link — viewer'.")
    if r.status_code == 403:
        raise DriveError("Drive refused the request.")
    r.raise_for_status()
    return r.content


# ── Orchestrator ────────────────────────────────────────────────────

def build_drive_web_url(kind: str, file_id: str) -> str:
    """Return the user-facing Drive URL we stash in document_refs.external_url."""
    if kind == 'document':
        return f"https://docs.google.com/document/d/{file_id}/edit"
    if kind == 'spreadsheet':
        return f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
    if kind == 'folder':
        return f"https://drive.google.com/drive/folders/{file_id}"
    return f"https://drive.google.com/file/d/{file_id}/view"


def resolve_drive_input(url: str) -> dict:
    """Given a pasted Drive URL, decide what to ingest.
    Returns {'kind': 'single'|'folder', 'items': [{id, name, mime_type, source_type}]}
    Raises DriveError with a user-friendly message on access issues."""
    kind, ident = parse_drive_url(url)
    if not kind:
        raise DriveError(
            "That doesn't look like a Google Drive URL. Paste a link from "
            "docs.google.com or drive.google.com."
        )
    if kind == 'folder':
        children = list_folder_children(ident)
        items = []
        for c in children:
            mime = c.get('mimeType')
            if mime not in SUPPORTED_MIMES:
                continue  # skip subfolders, images, unknown types
            items.append({
                'id': c['id'],
                'name': c['name'],
                'mime_type': mime,
                'modified_time': c.get('modifiedTime'),
                'source_type': _source_type_for_mime(mime),
            })
        return {'kind': 'folder', 'folder_id': ident, 'items': items}

    # Single file/doc/sheet path — look up metadata to learn mimeType
    meta = get_file_metadata(ident)
    mime = meta.get('mimeType')
    if mime not in SUPPORTED_MIMES:
        raise DriveError(
            f"Unsupported file type ({mime}). We can ingest Google Docs, "
            f"Google Sheets, and PDFs."
        )
    return {
        'kind': 'single',
        'items': [{
            'id': meta['id'],
            'name': meta['name'],
            'mime_type': mime,
            'modified_time': meta.get('modifiedTime'),
            'source_type': _source_type_for_mime(mime),
        }],
    }


def _source_type_for_mime(mime: str) -> str:
    if mime == MIME_GOOGLE_DOC:
        return 'google_doc'
    if mime == MIME_GOOGLE_SHEET:
        return 'google_sheet'
    if mime == MIME_PDF:
        return 'pdf_upload'
    return 'drive_other'


def fetch_item_text(item: dict) -> str:
    """Pull text for a single Drive item. Returns extracted text (may be empty
    for image-only PDFs — caller should check)."""
    mime = item['mime_type']
    if mime == MIME_GOOGLE_DOC:
        return fetch_doc_text(item['id'])
    if mime == MIME_GOOGLE_SHEET:
        return fetch_sheet_csv(item['id'])
    if mime == MIME_PDF:
        pdf_bytes = fetch_pdf_bytes(item['id'])
        try:
            from utilities.timeshare_ingest import extract_pdf_text
            text = extract_pdf_text(pdf_bytes)
        finally:
            del pdf_bytes
        return text
    raise DriveError(f"Don't know how to fetch mime type: {mime}")


def create_document_ref(group_id, item: dict, ingest_job_id: Optional[int] = None):
    """Create a crab.timeshare_document_refs row pointing at the Drive item."""
    from utilities.postgres_utils import get_db_connection
    external_url = build_drive_web_url(
        'document' if item['mime_type'] == MIME_GOOGLE_DOC else
        'spreadsheet' if item['mime_type'] == MIME_GOOGLE_SHEET else
        'file',
        item['id'],
    )
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.timeshare_document_refs
                (group_id, title, external_url, external_provider, external_id,
                 source_ingest_job_id)
            VALUES (%s::uuid, %s, %s, 'google_drive', %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING pk_id
        """, (group_id, item['name'][:500], external_url, item['id'], ingest_job_id))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        conn.close()
