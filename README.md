# google-drive-mover

A "Google space cleaner" toolkit: moves content out of a Google account to a
local path (e.g. an external drive), verifies it landed safely, then
optionally clears the Google-side copy to reclaim storage.

- **`src/drive_mover.py`** — Google Drive files, preserving folder structure
- **`src/gmail_cleanup.py`** — large email attachments
- Google Photos isn't API-scriptable anymore (Google restricted bulk access
  in 2025) — use [Google Takeout](https://takeout.google.com) to export it
  manually, then organize the result onto your drive by hand or with a
  one-off script

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
    drive_mover.py        # Drive -> local drive
    gmail_cleanup.py       # large Gmail attachments -> local drive
  credentials/              # Drive OAuth client secret + token (gitignored)
  credentials-gmail/         # Gmail OAuth client secret + token (gitignored)
  logs/                       # run logs (gitignored)
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

Drive shortcuts (including shared-with-you folders linked into your own Drive
— a common pattern) are followed by default: a shortcut to a folder is
recursed into using the shortcut's own name, and a shortcut to a file is
downloaded like a normal file. Pass `--skip-shortcuts` to ignore them
instead. Either way, files you don't own (shared content) can be copied but
can't be trashed by `--delete-after` — that fails with a permissions error
and is left in place on Drive, which is expected, not a bug.

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

## 5. Gmail cleanup (large attachments)

`gmail_cleanup.py` finds emails with large attachments, downloads the
attachments to a local path, and can trash the whole email once verified.
Gmail has no API to delete just an attachment — the unit of deletion is the
entire message (subject, body, thread), not a surgical attachment-only
removal, so treat `--delete-after` accordingly.

### Credentials

Same idea as Drive, but a separate project/scope:

1. In [Google Cloud Console](https://console.cloud.google.com/), reuse the
   same project as your Drive setup (or create a new one) and enable the
   **Gmail API** (`APIs & Services > Library`).
2. On the **OAuth consent screen**, make sure the account you'll clean up is
   added as a test user (same screen used for Drive works fine).
3. Create a new **OAuth client ID** (`Desktop app`) — Gmail needs its own
   client since the scope differs from Drive's.
4. Download the JSON and save it as `credentials-gmail/client_secret.json`
   (a separate folder from Drive's credentials, to avoid mixing them up).

First run opens a browser to sign in and creates `credentials-gmail/token.json`.

### Usage

Always dry-run first — review the exact list of candidate emails (sender,
subject, date, size) before downloading or deleting anything:

```bash
python src/gmail_cleanup.py --dry-run
```

Narrow or exclude with normal Gmail search syntax via `--query` (default is
`has:attachment larger:5M`):

```bash
python src/gmail_cleanup.py --dry-run --query "has:attachment larger:10M -from:family@example.com -label:personal"
```

Download attachments, leaving Gmail untouched:

```bash
python src/gmail_cleanup.py --dest "E:\GmailBackup"
```

Download and trash the whole email once its attachment(s) are verified on disk:

```bash
python src/gmail_cleanup.py --dest "E:\GmailBackup" --delete-after
```

Each email's attachment(s) are saved into their own subfolder (named by
date, sender, and subject) under `--dest`, alongside a `message.eml` with
the full original email (headers + body) — so trashing the message with
`--delete-after` doesn't lose the body text, just the Gmail Trash copy.
`.eml` files open in any mail client or most browsers. Trashed emails are
recoverable from Gmail Trash for 30 days.

Logs are written to `logs/gmail_cleanup.log`.
