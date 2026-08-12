"""Find large-attachment emails, download the attachments to a local (e.g.
external) drive, and optionally trash the email once the attachment is
verified on disk.

Gmail has no "delete just the attachment" operation via the API — the unit
of deletion is the whole message. --delete-after trashes the entire email
(recoverable from Gmail Trash for 30 days), not just the attachment.
"""

import argparse
import base64
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

DEFAULT_QUERY = "has:attachment larger:5M"


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
                    "secret (Desktop app, gmail.modify scope) from Google "
                    "Cloud Console and save it there (see README.md)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return creds


def sanitize_filename(name: str, max_length: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = "untitled"
    return name[:max_length]


def get_header(headers, name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def iter_attachment_parts(payload):
    """Recursively yield attachment-bearing parts of a Gmail message payload."""
    if payload.get("filename"):
        body = payload.get("body", {})
        if body.get("attachmentId"):
            yield {
                "filename": payload["filename"],
                "mimeType": payload.get("mimeType", "application/octet-stream"),
                "attachmentId": body["attachmentId"],
                "size": body.get("size", 0),
            }
    for part in payload.get("parts", []) or []:
        yield from iter_attachment_parts(part)


def iter_large_attachment_messages(service, query: str):
    """Yield (message_id, sender, subject, date_str, [attachment_info, ...])
    for every message matching query that has at least one real attachment."""
    page_token = None
    while True:
        response = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token)
            .execute()
        )
        for msg_ref in response.get("messages", []):
            message = (
                service.users()
                .messages()
                .get(userId="me", id=msg_ref["id"], format="full")
                .execute()
            )
            headers = message["payload"].get("headers", [])
            attachments = list(iter_attachment_parts(message["payload"]))
            if not attachments:
                continue

            sender = get_header(headers, "From")
            subject = get_header(headers, "Subject") or "(no subject)"
            internal_ms = int(message.get("internalDate", "0"))
            date_str = (
                datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                if internal_ms
                else "unknown-date"
            )

            yield message["id"], sender, subject, date_str, attachments

        page_token = response.get("nextPageToken")
        if not page_token:
            break


def download_attachment(service, message_id: str, attachment: dict, dest_path: Path, logger: logging.Logger) -> bool:
    expected_size = attachment.get("size", 0)

    if dest_path.exists() and expected_size and dest_path.stat().st_size == expected_size:
        logger.info("Already present, skipping: %s", dest_path)
        return True

    result = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment["attachmentId"])
        .execute()
    )
    encoded = result["data"]
    encoded += "=" * (-len(encoded) % 4)
    data = base64.urlsafe_b64decode(encoded)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(data)

    if expected_size and len(data) != expected_size:
        logger.error(
            "Size mismatch after download: %s expected=%d actual=%d", dest_path, expected_size, len(data)
        )
        return False

    logger.info("Downloaded: %s -> %s", attachment["filename"], dest_path)
    return True


def trash_message(service, message_id: str, logger: logging.Logger):
    service.users().messages().trash(userId="me", id=message_id).execute()
    logger.info("Trashed message: %s", message_id)


def build_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("gmail_cleanup")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_dir / "gmail_cleanup.log")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def clean_up_attachments(
    service,
    query: str,
    destination_root: Path,
    delete_after: bool,
    dry_run: bool,
    logger: logging.Logger,
):
    total_messages = moved = failed = 0

    for message_id, sender, subject, date_str, attachments in iter_large_attachment_messages(service, query):
        total_messages += 1
        folder_name = (
            f"{date_str} - {sanitize_filename(sender, 30)} - {sanitize_filename(subject, 40)} [{message_id[:8]}]"
        )
        message_dir = destination_root / folder_name

        if dry_run:
            total_size = sum(a.get("size", 0) for a in attachments)
            logger.info(
                "[dry-run] %s | %s | %s | %d attachment(s), %.1f MB total",
                date_str,
                sender,
                subject,
                len(attachments),
                total_size / (1024**2),
            )
            continue

        all_ok = True
        for attachment in attachments:
            dest_path = message_dir / sanitize_filename(attachment["filename"], 100)
            try:
                ok = download_attachment(service, message_id, attachment, dest_path, logger)
            except HttpError as e:
                logger.error("Failed to download attachment for message %s: %s", message_id, e)
                ok = False
            if not ok:
                all_ok = False

        if not all_ok:
            failed += 1
            continue

        if delete_after:
            try:
                trash_message(service, message_id, logger)
            except HttpError as e:
                logger.error("Downloaded but failed to trash message %s: %s", message_id, e)
                failed += 1
                continue

        moved += 1

    logger.info("Done. total=%d moved=%d failed=%d", total_messages, moved, failed)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Find large-attachment emails and move the attachments to a local drive."
    )
    parser.add_argument("--dest", help="Destination path for downloaded attachments")
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help=f"Gmail search query for candidate messages (default: '{DEFAULT_QUERY}'). "
        "Use normal Gmail search syntax, e.g. add \"-from:someone@example.com\" to exclude a sender.",
    )
    parser.add_argument(
        "--delete-after",
        action="store_true",
        help="Trash the whole email after its attachment(s) are successfully downloaded and verified. "
        "Trashes the entire message, not just the attachment (Gmail has no API for that). "
        "Recoverable from Gmail Trash for 30 days.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching emails (sender, subject, date, attachment size) without downloading or deleting.",
    )
    parser.add_argument(
        "--credentials-dir",
        default=None,
        help="Directory containing client_secret.json / token.json (default: <project>/credentials-gmail)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    credentials_dir = Path(args.credentials_dir) if args.credentials_dir else project_root / "credentials-gmail"
    log_dir = project_root / "logs"

    logger = build_logger(log_dir)

    if not args.dest and not args.dry_run:
        raise SystemExit("--dest is required unless --dry-run is given.")

    destination_root = Path(args.dest) if args.dest else None
    if destination_root and not args.dry_run:
        destination_root.mkdir(parents=True, exist_ok=True)

    creds = get_credentials(credentials_dir)
    service = build("gmail", "v1", credentials=creds)

    clean_up_attachments(
        service,
        query=args.query,
        destination_root=destination_root,
        delete_after=args.delete_after,
        dry_run=args.dry_run,
        logger=logger,
    )


if __name__ == "__main__":
    main()
