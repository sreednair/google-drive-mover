import base64
from pathlib import Path
from unittest.mock import MagicMock

import gmail_cleanup


# --- clean_up_attachments -------------------------------------------------------


def test_clean_up_attachments_dry_run_works_without_a_destination(monkeypatch):
    # Regression test: --dry-run doesn't require --dest, so destination_root
    # can be None. clean_up_attachments must not touch it in that path.
    sample = [
        (
            "msg1",
            "someone@example.com",
            "Big file",
            "2026-01-01",
            [{"filename": "report.pdf", "attachmentId": "att1", "size": 6_000_000}],
        )
    ]
    monkeypatch.setattr(gmail_cleanup, "iter_large_attachment_messages", lambda service, query: iter(sample))

    gmail_cleanup.clean_up_attachments(
        MagicMock(), query="has:attachment", destination_root=None, delete_after=False, dry_run=True, logger=MagicMock()
    )


# --- sanitize_filename -------------------------------------------------------


def test_sanitize_filename_strips_illegal_windows_characters():
    assert gmail_cleanup.sanitize_filename("a:b/c.txt") == "a_b_c.txt"


def test_sanitize_filename_leaves_normal_names_untouched():
    assert gmail_cleanup.sanitize_filename("Annual Report 2026.pdf") == "Annual Report 2026.pdf"


def test_sanitize_filename_truncates_to_max_length():
    result = gmail_cleanup.sanitize_filename("a" * 200, max_length=10)
    assert result == "a" * 10


def test_sanitize_filename_empty_becomes_untitled():
    assert gmail_cleanup.sanitize_filename("   ") == "untitled"


# --- get_header ----------------------------------------------------------------


def test_get_header_finds_case_insensitively():
    headers = [{"name": "Subject", "value": "Hello"}, {"name": "From", "value": "a@b.com"}]
    assert gmail_cleanup.get_header(headers, "subject") == "Hello"
    assert gmail_cleanup.get_header(headers, "FROM") == "a@b.com"


def test_get_header_missing_returns_empty_string():
    assert gmail_cleanup.get_header([], "Subject") == ""


# --- iter_attachment_parts ------------------------------------------------------


def test_iter_attachment_parts_finds_nested_attachment():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"size": 10}},
            {
                "filename": "report.pdf",
                "mimeType": "application/pdf",
                "body": {"attachmentId": "att1", "size": 5000},
            },
        ],
    }
    results = list(gmail_cleanup.iter_attachment_parts(payload))
    assert len(results) == 1
    assert results[0]["filename"] == "report.pdf"
    assert results[0]["attachmentId"] == "att1"
    assert results[0]["size"] == 5000


def test_iter_attachment_parts_ignores_inline_body_without_attachment_id():
    payload = {"filename": "", "body": {"size": 10}, "parts": []}
    assert list(gmail_cleanup.iter_attachment_parts(payload)) == []


def test_iter_attachment_parts_ignores_filename_without_attachment_id():
    # e.g. an inline image referenced by contentId rather than a real attachment
    payload = {"filename": "inline.png", "body": {"size": 10}, "parts": []}
    assert list(gmail_cleanup.iter_attachment_parts(payload)) == []


# --- download_attachment ---------------------------------------------------------


def test_download_attachment_skips_when_already_present_with_matching_size(tmp_path):
    dest = tmp_path / "file.pdf"
    dest.write_bytes(b"1234567890")  # 10 bytes
    service = MagicMock()

    ok = gmail_cleanup.download_attachment(
        service, "msg1", {"attachmentId": "att1", "filename": "file.pdf", "size": 10}, dest, MagicMock()
    )

    assert ok is True
    service.users.return_value.messages.return_value.attachments.return_value.get.assert_not_called()


def test_download_attachment_decodes_and_writes_base64_content(tmp_path):
    dest = tmp_path / "sub" / "file.pdf"
    content = b"hello world, this is attachment content"
    encoded = base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")  # simulate stripped padding

    service = MagicMock()
    service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
        "size": len(content),
        "data": encoded,
    }

    ok = gmail_cleanup.download_attachment(
        service,
        "msg1",
        {"attachmentId": "att1", "filename": "file.pdf", "size": len(content)},
        dest,
        MagicMock(),
    )

    assert ok is True
    assert dest.read_bytes() == content


def test_download_attachment_flags_size_mismatch(tmp_path):
    dest = tmp_path / "file.pdf"
    content = b"short"
    encoded = base64.urlsafe_b64encode(content).decode("ascii")

    service = MagicMock()
    service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
        "size": 9999,
        "data": encoded,
    }

    ok = gmail_cleanup.download_attachment(
        service, "msg1", {"attachmentId": "att1", "filename": "file.pdf", "size": 9999}, dest, MagicMock()
    )

    assert ok is False
