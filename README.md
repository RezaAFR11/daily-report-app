# Daily Report App

Flask application for creating PT. Garuda Prima Aksara daily reports.

## Run locally

On Windows, double-click `START DAILY REPORT APP.bat`, or run:

```powershell
python daily_report_app.py
```

The app opens at <http://localhost:5050>.

## Deploy to Railway

1. Create a Railway service from this private GitHub repository.
2. Attach a persistent Railway Volume with mount path `/data`.
3. Add these service variables:
   - `DATA_DIR=/data`
   - `SECRET_KEY=<long-random-value>`
   - `ADMIN_PIN=<secure-initial-pin>`
   - `ANTHROPIC_API_KEY=<optional>`
4. Use this start command if Railway does not detect the `Procfile`:

   ```text
   gunicorn --workers 1 --bind 0.0.0.0:$PORT daily_report_app:app
   ```

5. Generate a public domain from the service Networking settings.

The service intentionally uses one Gunicorn worker because the current application stores records in JSON files. Runtime data is excluded from Git and must be stored on the mounted Volume.

## Migrating existing data

Upload these items to the root of the mounted `/data` Volume:

```text
users.json
app_config.json
activity_log.json
logos/
users/
```

Do not commit these files to Git because they can contain account data, reports, photos, and API credentials.
