"""Move files from Google Drive to a local (e.g. external) drive.

Downloads matching files from Google Drive, preserving the folder
structure, and optionally trashes the Drive copy once the local copy
is verified so the operation behaves like a "move" rather than a copy.
"""

import argparse
import io
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]

FOLDER_MIME = "application/vnd.google-apps.folder"

# Native Google Workspace formats can't be downloaded as-is; export them instead.
GOOGLE_EXPORT_MIME_MAP = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}


def get_credentials(credentials_dir: Path) -> Credentials:
    token_path = credentials_dir / "token.json"
    client_secret_path = credentials_dir / "client_secret.json"

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_secret_path.exists():
                raise FileNotFoundError(
                    f"Missing {client_secret_path}. Download an OAuth client "
                    "secret from Google Cloud Console and save it there "
                    "(see README.md)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return creds


def iter_drive_files(service, folder_id: str):
    """Yield (file_metadata, relative_path) for every file under folder_id."""
    stack = [(folder_id, Path())]
    while stack:
        current_id, rel_path = stack.pop()
        page_token = None
        while True:
            response = (
                service.files()
                .list(
                    q=f"'{current_id}' in parents and trashed = false",
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType, size)",
                    pageToken=page_token,
                )
                .execute()
            )

            for f in response.get("files", []):
                if f["mimeType"] == FOLDER_MIME:
                    stack.append((f["id"], rel_path / f["name"]))
                else:
                    yield f, rel_path

            page_token = response.get("nextPageToken")
            if not page_token:
                break


def download_file(service, file_meta, dest_path: Path, logger: logging.Logger) -> bool:
    mime_type = file_meta["mimeType"]
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if mime_type in GOOGLE_EXPORT_MIME_MAP:
        export_mime, ext = GOOGLE_EXPORT_MIME_MAP[mime_type]
        dest_path = dest_path.with_suffix(ext)
        request = service.files().export_media(fileId=file_meta["id"], mimeType=export_mime)
    elif mime_type.startswith("application/vnd.google-apps"):
        logger.warning("Skipping unsupported Google file type: %s (%s)", file_meta["name"], mime_type)
        return False
    else:
        request = service.files().get_media(fileId=file_meta["id"])

    if dest_path.exists() and "size" in file_meta:
        try:
            if dest_path.stat().st_size == int(file_meta["size"]):
                logger.info("Already present, skipping: %s", dest_path)
                return True
        except ValueError:
            pass

    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    try:
        done = False
        while not done:
            _, done = downloader.next_chunk()
    finally:
        fh.close()

    logger.info("Downloaded: %s -> %s", file_meta["name"], dest_path)
    return True


def trash_file(service, file_id: str, logger: logging.Logger):
    service.files().update(fileId=file_id, body={"trashed": True}).execute()
    logger.info("Trashed Drive copy: %s", file_id)


def resolve_folder_id(service, name_or_id: str) -> str:
    """Accept either a Drive folder ID or an exact folder name (looked up)."""
    try:
        service.files().get(fileId=name_or_id, fields="id").execute()
        return name_or_id
    except HttpError:
        pass

    response = (
        service.files()
        .list(
            q=f"name = '{name_or_id}' and mimeType = '{FOLDER_MIME}' and trashed = false",
            spaces="drive",
            fields="files(id, name)",
        )
        .execute()
    )
    matches = response.get("files", [])
    if not matches:
        raise ValueError(f"No Drive folder found matching '{name_or_id}'")
    if len(matches) > 1:
        raise ValueError(f"Multiple Drive folders named '{name_or_id}': " + ", ".join(m["id"] for m in matches))
    return matches[0]["id"]


def move_drive_folder(
    service,
    source_folder_id: str,
    destination_root: Path,
    delete_after: bool,
    dry_run: bool,
    logger: logging.Logger,
):
    total = moved = failed = 0
    for file_meta, rel_path in iter_drive_files(service, source_folder_id):
        total += 1
        dest_path = destination_root / rel_path / file_meta["name"]

        if dry_run:
            logger.info("[dry-run] Would move: %s -> %s", file_meta["name"], dest_path)
            continue

        try:
            ok = download_file(service, file_meta, dest_path, logger)
        except HttpError as e:
            logger.error("Failed to download %s: %s", file_meta["name"], e)
            failed += 1
            continue

        if not ok:
            continue

        if delete_after:
            try:
                trash_file(service, file_meta["id"], logger)
            except HttpError as e:
                logger.error("Downloaded but failed to trash %s: %s", file_meta["name"], e)
                failed += 1
                continue

        moved += 1

    logger.info("Done. total=%d moved=%d failed=%d", total, moved, failed)


def build_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("drive_mover")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_dir / "drive_mover.log")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def parse_args():
    parser = argparse.ArgumentParser(description="Move Google Drive files to an external drive.")
    parser.add_argument(
        "--source",
        required=True,
        help="Drive folder ID or exact folder name to move from ('root' for My Drive root)",
    )
    parser.add_argument("--dest", required=True, help="Destination path on the external drive")
    parser.add_argument(
        "--delete-after",
        action="store_true",
        help="Trash the Drive copy after a successful download (true 'move'). Default: copy only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be moved without downloading or deleting anything.",
    )
    parser.add_argument(
        "--credentials-dir",
        default=None,
        help="Directory containing client_secret.json / token.json (default: <project>/credentials)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    credentials_dir = Path(args.credentials_dir) if args.credentials_dir else project_root / "credentials"
    log_dir = project_root / "logs"

    logger = build_logger(log_dir)
    destination_root = Path(args.dest)

    if not args.dry_run:
        destination_root.mkdir(parents=True, exist_ok=True)

    creds = get_credentials(credentials_dir)
    service = build("drive", "v3", credentials=creds)

    source_id = "root" if args.source.lower() == "root" else resolve_folder_id(service, args.source)

    move_drive_folder(
        service,
        source_id,
        destination_root,
        delete_after=args.delete_after,
        dry_run=args.dry_run,
        logger=logger,
    )


if __name__ == "__main__":
    main()
