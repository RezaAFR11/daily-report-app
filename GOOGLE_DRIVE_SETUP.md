# Google Drive setup

The application uploads only final Daily Report PDFs. Preview PDFs are never
uploaded. Browser download and the Railway/My Reports archive remain available
if Google Drive is offline.

## Folder structure

The application creates these folders in the connected account's My Drive:

```text
Daily Reports/
  Daily Reports Electrical/2026/Agustus/<current Daily Report filename>.pdf
  Daily Reports Control Valve/2026/Agustus/<current Daily Report filename>.pdf
  Daily Reports Turbine & Generator/2026/Agustus/<current Daily Report filename>.pdf
  Daily Reports Other Projects/2026/Agustus/<current Daily Report filename>.pdf
```

Year and month come from the report date, not the upload date. Month folders
use Indonesian names without a numeric prefix. A non-empty project that does
not match the three main branches is stored in `Daily Reports Other Projects`.
Ambiguous or conflicting project identities are not guessed; My Reports shows
a Retry/mapping warning.

## One-time Google Cloud setup

1. Create or select a Google Cloud project.
2. Enable **Google Drive API**.
3. Configure the OAuth consent screen.
4. Create an OAuth client with application type **Desktop app**.
5. If the consent screen is External, publish it to **In production** after
   testing; refresh tokens for External apps left in Testing can expire.
6. Never download or commit tokens into this repository.

The application requests only the `drive.file` scope and creates its own
`Daily Reports` folder. The generic `https://drive.google.com/drive/my-drive`
link does not contain credentials and needs no folder ID.

## Create the refresh token locally

Install dependencies and run the helper on a trusted computer:

```powershell
Set-Location "D:\RezaWork\Project\Daily Report App"
python -m pip install -r requirements.txt
$env:GDRIVE_CLIENT_ID="your OAuth client ID"
$env:GDRIVE_CLIENT_SECRET="your OAuth client secret"
python .\scripts\google_drive_authorize.py
```

Sign in with the Google account whose My Drive should receive the reports.
Copy the printed refresh token directly to Railway Variables.

## Railway Variables

```text
GDRIVE_CLIENT_ID=<OAuth client ID>
GDRIVE_CLIENT_SECRET=<OAuth client secret>
GDRIVE_REFRESH_TOKEN=<refresh token from the helper>
GDRIVE_ROOT_FOLDER_NAME=Daily Reports
```

Optional: set `GDRIVE_PARENT_FOLDER_ID` to a specific accessible **parent of the
Daily Reports folder**. Do not paste the ID of `Daily Reports` itself, because
the application creates that folder below this parent. If omitted, the
application uses the connected account's My Drive root.

Redeploy after adding the variables. The Preview modal then shows
**Download PDF + Drive**. My Reports also shows **Open Drive** or **Retry Drive**.

Official references:

- https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- https://developers.google.com/workspace/drive/api/guides/folder
- https://developers.google.com/workspace/drive/api/guides/manage-uploads
