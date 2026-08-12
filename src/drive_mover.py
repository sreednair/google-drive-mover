"""Move files from Google Drive to a local (e.g. external) drive.

Downloads matching files from Google Drive, preserving the folder
structure, and optionally trashes the Drive copy once the local copy
is verified so the operation behaves like a "move" rather than a copy.
"""

import argparse
import csv
import io
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

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


def iter_drive_files(
    service,
    folder_id: str,
    logger: logging.Logger | None = None,
    skip_shortcuts: bool = False,
):
    """Yield (file_metadata, relative_path) for every file under folder_id.

    Follows Drive shortcuts transparently by default: a shortcut to a folder
    is recursed into (using the shortcut's own name, matching what's shown
    in Drive), and a shortcut to a file yields that file's own metadata.
    Shortcuts whose target can't be resolved (e.g. permission issues on a
    cross-account share) are skipped with a warning rather than failing.
    Pass skip_shortcuts=True to ignore all shortcuts instead."""
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
                    fields="nextPageToken, files(id, name, mimeType, size, shortcutDetails)",
                    pageToken=page_token,
                )
                .execute()
            )

            for f in response.get("files", []):
                if f["mimeType"] == FOLDER_MIME:
                    stack.append((f["id"], rel_path / f["name"]))
                elif f["mimeType"] == SHORTCUT_MIME:
                    if skip_shortcuts:
                        if logger:
                            logger.info("Skipping shortcut (--skip-shortcuts): %s", f["name"])
                        continue
                    details = f.get("shortcutDetails") or {}
                    target_id = details.get("targetId")
                    target_mime = details.get("targetMimeType")
                    if not target_id:
                        continue
                    if target_mime == FOLDER_MIME:
                        stack.append((target_id, rel_path / f["name"]))
                    else:
                        try:
                            target = (
                                service.files()
                                .get(fileId=target_id, fields="id, name, mimeType, size")
                                .execute()
                            )
                        except HttpError as e:
                            if logger:
                                logger.warning("Skipping shortcut '%s' (couldn't resolve target): %s", f["name"], e)
                            continue
                        yield target, rel_path
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


def list_subfolders(service, folder_id: str):
    result = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false and mimeType = '{FOLDER_MIME}'",
                spaces="drive",
                fields="nextPageToken, files(id, name)",
                pageToken=page_token,
            )
            .execute()
        )
        result.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return result


def has_direct_files(service, folder_id: str) -> bool:
    """Whether folder_id directly contains any non-folder item. Subfolders
    are deliberately excluded here; those are judged separately/recursively
    so the check works correctly even in dry-run mode, where nothing is
    actually removed from Drive between recursive calls."""
    response = (
        service.files()
        .list(
            q=f"'{folder_id}' in parents and trashed = false and mimeType != '{FOLDER_MIME}'",
            spaces="drive",
            fields="files(id)",
            pageSize=1,
        )
        .execute()
    )
    return len(response.get("files", [])) > 0


def trash_empty_folders(service, root_folder_id: str, dry_run: bool, logger: logging.Logger):
    """Recursively trash folders under root_folder_id that end up with no
    remaining children (files or subfolders), bottom-up, so a folder that
    only becomes empty after its subfolder is removed is still caught.
    Folders that still contain anything (e.g. files that couldn't be
    trashed due to permissions) are left alone."""

    def walk(folder_id: str, rel_path: Path) -> bool:
        """Returns True if folder_id is empty (and was trashed, unless dry_run)."""
        all_subfolders_empty = True
        for sub in list_subfolders(service, folder_id):
            if not walk(sub["id"], rel_path / sub["name"]):
                all_subfolders_empty = False

        if not all_subfolders_empty or has_direct_files(service, folder_id):
            logger.info("Leaving non-empty folder: %s", rel_path)
            return False

        if dry_run:
            logger.info("[dry-run] Would trash empty folder: %s", rel_path)
        else:
            service.files().update(fileId=folder_id, body={"trashed": True}).execute()
            logger.info("Trashed empty folder: %s", rel_path)
        return True

    for top in list_subfolders(service, root_folder_id):
        walk(top["id"], Path(top["name"]))


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


def disambiguate_filenames(entries):
    """Google Drive allows multiple files with the identical name in the same
    folder. Detect any (rel_path, name) collisions up front and give every
    file in a colliding group a unique, stable filename (Drive file ID
    suffix) so none of them silently overwrite each other on disk."""
    groups: dict[tuple[Path, str], list] = {}
    for file_meta, rel_path in entries:
        groups.setdefault((rel_path, file_meta["name"]), []).append(file_meta)

    result = []
    for file_meta, rel_path in entries:
        key = (rel_path, file_meta["name"])
        if len(groups[key]) > 1:
            name = Path(file_meta["name"])
            filename = f"{name.stem} [{file_meta['id'][:8]}]{name.suffix}"
        else:
            filename = file_meta["name"]
        result.append((file_meta, rel_path, filename))
    return result


def move_drive_folder(
    service,
    source_folder_id: str,
    destination_root: Path,
    delete_after: bool,
    dry_run: bool,
    logger: logging.Logger,
    skip_shortcuts: bool = False,
):
    entries = disambiguate_filenames(
        list(iter_drive_files(service, source_folder_id, logger, skip_shortcuts=skip_shortcuts))
    )

    total = moved = failed = 0
    for file_meta, rel_path, filename in entries:
        total += 1
        dest_path = destination_root / rel_path / filename

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


RECLAIM_LOG_HEADER = ["timestamp_utc", "trash_gb_before", "confirmed", "trash_gb_after", "gb_reclaimed"]


def record_reclaim(log_dir: Path, trash_gb_before: float, confirmed: bool, trash_gb_after: float | None = None):
    reclaim_log_path = log_dir / "reclaim_history.csv"
    is_new = not reclaim_log_path.exists()
    with reclaim_log_path.open("a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(RECLAIM_LOG_HEADER)
        gb_reclaimed = trash_gb_before - trash_gb_after if trash_gb_after is not None else None
        writer.writerow(
            [
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                f"{trash_gb_before:.2f}",
                confirmed,
                f"{trash_gb_after:.2f}" if trash_gb_after is not None else "",
                f"{gb_reclaimed:.2f}" if gb_reclaimed is not None else "",
            ]
        )


def empty_trash(service, log_dir: Path, logger: logging.Logger):
    quota_before = service.about().get(fields="storageQuota").execute()["storageQuota"]
    trash_gb = int(quota_before.get("usageInDriveTrash", 0)) / (1024**3)

    print(f"This will PERMANENTLY delete all {trash_gb:.2f} GB currently in Drive Trash.")
    print("This cannot be undone. Type EMPTY to confirm:")
    confirmation = input("> ")
    if confirmation != "EMPTY":
        logger.info("Empty-trash cancelled by user.")
        record_reclaim(log_dir, trash_gb, confirmed=False)
        return

    service.files().emptyTrash().execute()

    quota_after = service.about().get(fields="storageQuota").execute()["storageQuota"]
    trash_gb_after = int(quota_after.get("usageInDriveTrash", 0)) / (1024**3)

    logger.info("Drive Trash emptied (%.2f GB reclaimed).", trash_gb - trash_gb_after)
    record_reclaim(log_dir, trash_gb, confirmed=True, trash_gb_after=trash_gb_after)


def build_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("drive_mover")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_dir / "drive_mover.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # File/folder names can contain characters the Windows console's default
    # codepage can't encode. Replace rather than crash the logging call.
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def parse_args():
    parser = argparse.ArgumentParser(description="Move Google Drive files to an external drive.")
    parser.add_argument(
        "--source",
        help="Drive folder ID or exact folder name to move from ('root' for My Drive root)",
    )
    parser.add_argument("--dest", help="Destination path on the external drive")
    parser.add_argument(
        "--empty-trash",
        action="store_true",
        help="Permanently empty Google Drive Trash (asks for interactive confirmation) and exit. "
        "Ignores --source/--dest.",
    )
    parser.add_argument(
        "--delete-after",
        action="store_true",
        help="Trash the Drive copy after a successful download (true 'move'). Default: copy only.",
    )
    parser.add_argument(
        "--delete-empty-folders",
        action="store_true",
        help="Trash folders under --source (default: root) that end up with no remaining files or "
        "subfolders — e.g. after a --delete-after run. Folders that still contain anything "
        "(such as shared files you don't have permission to trash) are left alone. Ignores --dest.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be moved without downloading or deleting anything.",
    )
    parser.add_argument(
        "--skip-shortcuts",
        action="store_true",
        help="Ignore Drive shortcuts instead of following them. By default, a shortcut to a folder "
        "is recursed into and a shortcut to a file is downloaded like a normal file.",
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

    creds = get_credentials(credentials_dir)
    service = build("drive", "v3", credentials=creds)

    if args.empty_trash:
        empty_trash(service, log_dir, logger)
        return

    if args.delete_empty_folders:
        source_id = "root" if not args.source or args.source.lower() == "root" else resolve_folder_id(service, args.source)
        trash_empty_folders(service, source_id, dry_run=args.dry_run, logger=logger)
        return

    if not args.source or not args.dest:
        raise SystemExit("--source and --dest are required unless --empty-trash is given.")

    destination_root = Path(args.dest)

    if not args.dry_run:
        destination_root.mkdir(parents=True, exist_ok=True)

    source_id = "root" if args.source.lower() == "root" else resolve_folder_id(service, args.source)

    move_drive_folder(
        service,
        source_id,
        destination_root,
        delete_after=args.delete_after,
        dry_run=args.dry_run,
        logger=logger,
        skip_shortcuts=args.skip_shortcuts,
    )


if __name__ == "__main__":
    main()
