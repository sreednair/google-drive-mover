import base64
from pathlib import Path
from unittest.mock import MagicMock

from googleapiclient.errors import HttpError

import gmail_cleanup


# --- list_all_message_ids / batch_trash ------------------------------------------


class FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "error"


def test_list_all_message_ids_paginates():
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.side_effect = [
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"},
        {"messages": [{"id": "c"}]},
    ]

    ids = gmail_cleanup.list_all_message_ids(service, "some query")

    assert ids == ["a", "b", "c"]


def test_batch_trash_chunks_by_batch_size():
    service = MagicMock()
    ids = [str(i) for i in range(2500)]

    total = gmail_cleanup.batch_trash(service, ids, MagicMock(), batch_size=1000)

    assert total == 2500
    calls = service.users.return_value.messages.return_value.batchModify.call_args_list
    assert len(calls) == 3
    assert len(calls[0].kwargs["body"]["ids"]) == 1000
    assert len(calls[1].kwargs["body"]["ids"]) == 1000
    assert len(calls[2].kwargs["body"]["ids"]) == 500
    assert all(c.kwargs["body"]["addLabelIds"] == ["TRASH"] for c in calls)


def test_batch_trash_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(gmail_cleanup.time, "sleep", lambda s: None)
    service = MagicMock()
    error = HttpError(FakeResp(429), b"{}")
    service.users.return_value.messages.return_value.batchModify.return_value.execute.side_effect = [
        error,
        error,
        None,
    ]

    total = gmail_cleanup.batch_trash(service, ["a", "b"], MagicMock(), batch_size=1000)

    assert total == 2
    assert service.users.return_value.messages.return_value.batchModify.return_value.execute.call_count == 3


def test_batch_trash_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(gmail_cleanup.time, "sleep", lambda s: None)
    service = MagicMock()
    error = HttpError(FakeResp(429), b"{}")
    service.users.return_value.messages.return_value.batchModify.return_value.execute.side_effect = error

    try:
        gmail_cleanup.batch_trash(service, ["a"], MagicMock(), batch_size=1000)
        assert False, "expected HttpError to propagate"
    except HttpError:
        pass


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


# --- download_raw_message -----------------------------------------------------------


def test_download_raw_message_decodes_and_writes_eml(tmp_path):
    dest = tmp_path / "msg1" / "message.eml"
    content = b"From: a@b.com\r\nSubject: Hi\r\n\r\nBody text"
    encoded = base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")

    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {"raw": encoded}

    ok = gmail_cleanup.download_raw_message(service, "msg1", dest, MagicMock())

    assert ok is True
    assert dest.read_bytes() == content


def test_download_raw_message_skips_when_already_present(tmp_path):
    dest = tmp_path / "message.eml"
    dest.write_bytes(b"already here")
    service = MagicMock()

    ok = gmail_cleanup.download_raw_message(service, "msg1", dest, MagicMock())

    assert ok is True
    service.users.return_value.messages.return_value.get.assert_not_called()


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
