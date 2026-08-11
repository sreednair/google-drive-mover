# google-drive-mover

Moves files from Google Drive to a local path (e.g. an external drive),
preserving folder structure. Can optionally trash the Drive copy after a
verified download, turning it into a true "move".

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
