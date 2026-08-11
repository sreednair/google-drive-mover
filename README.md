# google-drive-mover

Moves files from Google Drive to a local path (e.g. an external drive),
preserving folder structure. Can optionally trash the Drive copy after a
verified download, turning it into a true "move".

## Full walkthrough (checklist)

Follow in order. Each step links to the section with the full details.

1. [Install dependencies](#1-install-dependencies)
2. [Get Google API credentials](#2-set-up-google-api-credentials) for the account you're moving files from, and save them as `credentials/client_secret.json`
3. **Dry run everything first** — lists what would move, writes/deletes nothing, and triggers the browser sign-in:
   ```bash
   python src/drive_mover.py --source root --dest "<dest>" --dry-run
   ```
4. **Pick one small folder** from that output and test-copy just it (no deletion yet):
   ```bash
   python src/drive_mover.py --source "<TestFolder>" --dest "<dest>\<TestFolder>"
   ```
5. **Verify** the copied file(s) on disk match Drive before trusting it further (sizes, and ideally open one file to confirm it's not corrupted)
6. **Copy everything**, still without deleting anything from Drive:
   ```bash
   python src/drive_mover.py --source root --dest "<dest>"
   ```
7. **Verify again** — every file's size on disk should match what Drive reports before you touch anything remotely
8. **Trash the Drive originals** now that copies are verified (recoverable for 30 days):
   ```bash
   python src/drive_mover.py --source root --dest "<dest>" --delete-after
   ```
9. **Clean up the now-empty folders** left behind in Drive:
   ```bash
   python src/drive_mover.py --delete-empty-folders
   ```
10. **Permanently reclaim the space** — the one irreversible step, do it deliberately:
    ```bash
    python src/drive_mover.py --empty-trash
    ```

Some files can't be trashed if you don't own them (e.g. shared-with-you
content) — that's expected, not a bug; they're simply left in place on
Drive. See [Usage](#3-usage) below for full flag details.

## Folder structure

```
google-drive-mover/
  src/
    drive_mover.py      # main script
  credentials/           # OAuth client secret + saved token (gitignored)
  logs/                   # run logs (gitignored)
  requirements.txt
```

## 1. Install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

For running the test suite, install dev dependencies instead:

```bash
pip install -r requirements-dev.txt
pytest
```

Tests cover the pure logic (duplicate-filename handling, folder resolution,
empty-folder detection, export/skip behavior) against a mocked Drive API —
they don't touch your real Google Drive or filesystem beyond pytest's tmp
directories.

## 2. Set up Google API credentials

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or pick an existing one).
3. Go to **APIs & Services > Library**, search for **Google Drive API**, and enable it.
4. Go to **APIs & Services > OAuth consent screen**. Choose **External** (unless you have a
   Workspace org), fill in the required fields, and add your own Google account as a test user.
5. Go to **APIs & Services > Credentials > Create Credentials > OAuth client ID**.
   - Application type: **Desktop app**.
   - Download the resulting JSON file.
6. Save the downloaded file as `credentials/client_secret.json` in this project.

The first time you run the script, it opens a browser window for you to sign in and
grant access. A `credentials/token.json` file is then saved so future runs don't
need to re-authenticate (it auto-refreshes until you revoke access).

## 3. Usage

Dry run (see what would move, no changes):

```bash
python src/drive_mover.py --source "My Photos" --dest "E:\DriveBackup\My Photos" --dry-run
```

Copy files to the external drive, leaving Drive untouched:

```bash
python src/drive_mover.py --source "My Photos" --dest "E:\DriveBackup\My Photos"
```

Move files (download, then trash the Drive copy once confirmed on disk):

```bash
python src/drive_mover.py --source "My Photos" --dest "E:\DriveBackup\My Photos" --delete-after
```

`--source` accepts either a Drive folder name (exact match) or a folder ID from
its URL. Use `--source root` to target your entire My Drive.

Google Docs/Sheets/Slides/Drawings are exported to `.docx`/`.xlsx`/`.pptx`/`.png`
since they have no native downloadable format. Trashed files can be restored
from Drive's Trash for 30 days before permanent deletion.

Note: files sent to Trash still count against your Drive storage quota until
the trash is actually emptied — `--delete-after` alone won't free up space.

After a `--delete-after` run, the now-empty source folders themselves are
still left behind in Drive. Clean those up separately:

```bash
python src/drive_mover.py --delete-empty-folders --source "My Photos"
```

This recursively trashes folders that end up with no remaining files or
subfolders. Folders that still contain anything (e.g. files that couldn't be
trashed because you don't own them — shared-with-you content) are left
alone. Omit `--source` to sweep your entire My Drive. Supports `--dry-run`.

Logs are written to `logs/drive_mover.log` and echoed to the console.

## 4. Reclaiming space (optional)

Trashed files don't free up Drive storage until Trash is emptied. Once you're
confident your external-drive copies are good, you can permanently empty Drive
Trash:

```bash
python src/drive_mover.py --empty-trash
```

This asks for interactive confirmation (type `EMPTY`) before doing anything,
since it's irreversible.

Note: Google's storage-quota numbers lag behind the actual deletion by a few
minutes. The "GB reclaimed" the script reports right after emptying trash may
show `0.00` even though the delete succeeded — re-run with `--empty-trash`
(it'll just report an already-empty trash) or check usage again a few minutes
later to see the real number.
