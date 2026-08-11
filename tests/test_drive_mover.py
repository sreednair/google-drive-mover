from pathlib import Path
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import drive_mover


class FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "error"


def make_http_error(status=404):
    return HttpError(FakeResp(status), b"{}")


# --- disambiguate_filenames -------------------------------------------------


def test_disambiguate_filenames_no_collision():
    entries = [
        ({"id": "id1", "name": "a.txt"}, Path("Folder")),
        ({"id": "id2", "name": "b.txt"}, Path("Folder")),
    ]
    result = drive_mover.disambiguate_filenames(entries)
    filenames = [filename for _, _, filename in result]
    assert filenames == ["a.txt", "b.txt"]


def test_disambiguate_filenames_with_collision():
    entries = [
        ({"id": "aaaaaaaa1111", "name": "dup.csv"}, Path("Folder")),
        ({"id": "bbbbbbbb2222", "name": "dup.csv"}, Path("Folder")),
    ]
    result = drive_mover.disambiguate_filenames(entries)
    filenames = sorted(filename for _, _, filename in result)
    assert filenames == ["dup [aaaaaaaa].csv", "dup [bbbbbbbb].csv"]


def test_disambiguate_filenames_same_name_different_folders_is_not_a_collision():
    entries = [
        ({"id": "id1", "name": "same.txt"}, Path("Folder1")),
        ({"id": "id2", "name": "same.txt"}, Path("Folder2")),
    ]
    result = drive_mover.disambiguate_filenames(entries)
    filenames = [filename for _, _, filename in result]
    assert filenames == ["same.txt", "same.txt"]


# --- iter_drive_files (shortcut resolution) -----------------------------------


def _list_response(files):
    resp = MagicMock()
    resp.execute.return_value = {"files": files}
    return resp


def test_iter_drive_files_follows_folder_shortcut():
    service = MagicMock()

    def list_side_effect(q, **kwargs):
        if "'root'" in q:
            return _list_response(
                [
                    {
                        "id": "shortcut1",
                        "name": "Linked Folder",
                        "mimeType": drive_mover.SHORTCUT_MIME,
                        "shortcutDetails": {"targetId": "realfolder1", "targetMimeType": drive_mover.FOLDER_MIME},
                    }
                ]
            )
        if "'realfolder1'" in q:
            return _list_response([{"id": "f1", "name": "photo.jpg", "mimeType": "image/jpeg", "size": "100"}])
        return _list_response([])

    service.files.return_value.list.side_effect = list_side_effect

    results = list(drive_mover.iter_drive_files(service, "root"))

    assert len(results) == 1
    file_meta, rel_path = results[0]
    assert file_meta["name"] == "photo.jpg"
    assert rel_path == Path("Linked Folder")


def test_iter_drive_files_resolves_file_shortcut():
    service = MagicMock()

    def list_side_effect(q, **kwargs):
        if "'root'" in q:
            return _list_response(
                [
                    {
                        "id": "shortcut2",
                        "name": "Linked Doc",
                        "mimeType": drive_mover.SHORTCUT_MIME,
                        "shortcutDetails": {"targetId": "realfile1", "targetMimeType": "application/pdf"},
                    }
                ]
            )
        return _list_response([])

    service.files.return_value.list.side_effect = list_side_effect
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "realfile1",
        "name": "actual.pdf",
        "mimeType": "application/pdf",
        "size": "500",
    }

    results = list(drive_mover.iter_drive_files(service, "root"))

    assert len(results) == 1
    file_meta, rel_path = results[0]
    assert file_meta["name"] == "actual.pdf"
    assert rel_path == Path()


def test_iter_drive_files_skips_unresolvable_shortcut():
    service = MagicMock()

    def list_side_effect(q, **kwargs):
        if "'root'" in q:
            return _list_response(
                [
                    {
                        "id": "shortcut3",
                        "name": "Broken Link",
                        "mimeType": drive_mover.SHORTCUT_MIME,
                        "shortcutDetails": {"targetId": "gone", "targetMimeType": "application/pdf"},
                    }
                ]
            )
        return _list_response([])

    service.files.return_value.list.side_effect = list_side_effect
    service.files.return_value.get.return_value.execute.side_effect = make_http_error(403)

    results = list(drive_mover.iter_drive_files(service, "root", logger=MagicMock()))

    assert results == []


# --- resolve_folder_id -------------------------------------------------------


def test_resolve_folder_id_returns_id_when_already_valid():
    service = MagicMock()
    service.files.return_value.get.return_value.execute.return_value = {"id": "abc123"}
    assert drive_mover.resolve_folder_id(service, "abc123") == "abc123"


def test_resolve_folder_id_looks_up_by_name():
    service = MagicMock()
    service.files.return_value.get.return_value.execute.side_effect = make_http_error()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "folder-id-1", "name": "My Folder"}]
    }
    assert drive_mover.resolve_folder_id(service, "My Folder") == "folder-id-1"


def test_resolve_folder_id_raises_when_not_found():
    service = MagicMock()
    service.files.return_value.get.return_value.execute.side_effect = make_http_error()
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    with pytest.raises(ValueError):
        drive_mover.resolve_folder_id(service, "Nonexistent")


def test_resolve_folder_id_raises_when_ambiguous():
    service = MagicMock()
    service.files.return_value.get.return_value.execute.side_effect = make_http_error()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "id1", "name": "Dup"}, {"id": "id2", "name": "Dup"}]
    }
    with pytest.raises(ValueError):
        drive_mover.resolve_folder_id(service, "Dup")


# --- has_direct_files ---------------------------------------------------------


def test_has_direct_files_true():
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": [{"id": "f1"}]}
    assert drive_mover.has_direct_files(service, "folder1") is True


def test_has_direct_files_false():
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    assert drive_mover.has_direct_files(service, "folder1") is False


# --- trash_empty_folders -------------------------------------------------------


def _fake_tree():
    # root
    #  |- A (empty leaf)
    #  |- B -> C (empty leaf)      => B becomes empty once C is removed
    #  |- E -> D (has a real file) => E must NOT be trashed, D must NOT be trashed
    tree = {
        "root": [{"id": "A", "name": "A"}, {"id": "B", "name": "B"}, {"id": "E", "name": "E"}],
        "A": [],
        "B": [{"id": "C", "name": "C"}],
        "C": [],
        "E": [{"id": "D", "name": "D"}],
        "D": [],
    }
    direct_files = {"A": False, "B": False, "C": False, "E": False, "D": True}
    return tree, direct_files


def test_trash_empty_folders_bottom_up_and_skips_nonempty(monkeypatch):
    tree, direct_files = _fake_tree()
    monkeypatch.setattr(drive_mover, "list_subfolders", lambda service, fid: tree.get(fid, []))
    monkeypatch.setattr(drive_mover, "has_direct_files", lambda service, fid: direct_files.get(fid, False))

    service = MagicMock()
    drive_mover.trash_empty_folders(service, "root", dry_run=False, logger=MagicMock())

    trashed_ids = {call.kwargs["fileId"] for call in service.files.return_value.update.call_args_list}
    assert trashed_ids == {"A", "B", "C"}


def test_trash_empty_folders_dry_run_makes_no_changes(monkeypatch):
    tree, direct_files = _fake_tree()
    monkeypatch.setattr(drive_mover, "list_subfolders", lambda service, fid: tree.get(fid, []))
    monkeypatch.setattr(drive_mover, "has_direct_files", lambda service, fid: direct_files.get(fid, False))

    service = MagicMock()
    drive_mover.trash_empty_folders(service, "root", dry_run=True, logger=MagicMock())

    service.files.return_value.update.assert_not_called()


# --- download_file -------------------------------------------------------------


def test_download_file_skips_when_already_present_with_matching_size(tmp_path, monkeypatch):
    dest = tmp_path / "file.txt"
    dest.write_bytes(b"1234567890")  # 10 bytes
    file_meta = {"id": "id1", "name": "file.txt", "mimeType": "text/plain", "size": "10"}
    service = MagicMock()

    fake_downloader_cls = MagicMock()
    monkeypatch.setattr(drive_mover, "MediaIoBaseDownload", fake_downloader_cls)

    result = drive_mover.download_file(service, file_meta, dest, MagicMock())

    assert result is True
    assert dest.read_bytes() == b"1234567890"  # untouched, not re-downloaded
    fake_downloader_cls.assert_not_called()


def test_download_file_google_export_changes_extension(tmp_path, monkeypatch):
    dest = tmp_path / "MyDoc"
    file_meta = {"id": "id1", "name": "MyDoc", "mimeType": "application/vnd.google-apps.document"}
    service = MagicMock()

    fake_downloader = MagicMock()
    fake_downloader.next_chunk.return_value = (None, True)
    monkeypatch.setattr(drive_mover, "MediaIoBaseDownload", lambda fh, request: fake_downloader)

    result = drive_mover.download_file(service, file_meta, dest, MagicMock())

    assert result is True
    assert dest.with_suffix(".docx").exists()
    service.files.return_value.export_media.assert_called_once()


def test_download_file_skips_unsupported_google_type(tmp_path):
    dest = tmp_path / "form"
    file_meta = {"id": "id1", "name": "form", "mimeType": "application/vnd.google-apps.form"}
    service = MagicMock()

    result = drive_mover.download_file(service, file_meta, dest, MagicMock())

    assert result is False
    assert not dest.exists()
