"""
gdrive_sync.py
Minimal helper for downloading/uploading one file to Google Drive using a
service account. Used to pull the Trade Planner workbook down, edit it
locally with openpyxl, then push the same file back up (same file ID).

Requires env vars:
  GDRIVE_SA_KEY   - full JSON contents of the service account key
  GDRIVE_FILE_ID  - Drive file ID of the target .xlsx

Requires packages (add to requirements.txt):
  google-api-python-client
  google-auth
"""

import io
import json
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_drive_service():
    key_json = os.environ["GDRIVE_SA_KEY"]
    info = json.loads(key_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def download_workbook(local_path: str, file_id: str = None) -> str:
    """Download the Drive file to local_path. Returns local_path."""
    file_id = file_id or os.environ["GDRIVE_FILE_ID"]
    service = _get_drive_service()
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(local_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.close()
    return local_path


def upload_workbook(local_path: str, file_id: str = None) -> None:
    """Push local_path back to the SAME Drive file id (overwrite in place)."""
    file_id = file_id or os.environ["GDRIVE_FILE_ID"]
    service = _get_drive_service()
    media = MediaFileUpload(
        local_path,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        resumable=True,
    )
    service.files().update(fileId=file_id, media_body=media).execute()
