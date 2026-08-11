# Daily Report App

Flask application for creating PT. Garuda Prima Aksara daily reports.

## Run locally

On Windows, double-click `START DAILY REPORT APP.bat`, or run:

```powershell
python daily_report_app.py
```

The app opens at <http://localhost:5050>.

## Weekly / Monthly Reports

Open **My Reports → Weekly / Monthly Reports**. Choose one source:

- **Stored JSON** compiles final Daily Reports generated after this feature was enabled.
- **Upload Daily Report PDF** parses older machine-generated PDFs, then requires manual review.

Select one project and reporting period, then click **Compile & Review**. Weekly reports use a rolling seven-day period from the selected start date. Before Preview or Generate:

1. Apply **Source Data Validation** and resolve project or duplicate-report ambiguities.
2. Optionally upload the KN attendance `.xlsx`, review the 10-hour-per-present-day calculation, then apply it or keep the Daily Report baseline.
3. Optionally upload the overtime `.xlsx`, review elapsed clock hours and coverage, then apply confirmed records or keep the report without OT.
4. Optionally generate a Claude narrative suggestion, edit it, and explicitly accept or reject it.
5. Review Appendix 6.6 photos, then Preview or Generate the report.

Missing attendance, OT, safety, progress, or source data remains **Not supplied**; it is not silently converted to zero. A Final report requires an explicit review confirmation. Generated weekly/monthly PDFs and their reviewed JSON are stored under `DATA_DIR/monthly_reports/`.

Claude only suggests narrative wording. Dates, manpower, man-hours, safety values, source records, and other verified facts stay deterministic. Configure the key only through `ANTHROPIC_API_KEY`; never store it in Settings or `app_config.json`. The API account must also have usage credit. `ANTHROPIC_AI_ADMIN_ONLY=true` is recommended so only administrators can create paid requests.

## Deploy to Railway

1. Create a Railway service from this private GitHub repository.
2. Attach a persistent Railway Volume with mount path `/data`.
3. Add these service variables:
   - `DATA_DIR=/data`
   - `SECRET_KEY=<long-random-value>`
   - `ADMIN_PIN=<secure-initial-pin>`
   - `ANTHROPIC_API_KEY=<optional>`
   - `ANTHROPIC_MODEL=claude-sonnet-4-6` (optional)
   - `ANTHROPIC_AI_ADMIN_ONLY=true`
4. Use this start command if Railway does not detect the `Procfile`:

   ```text
   gunicorn --workers 1 --timeout 180 --bind 0.0.0.0:$PORT daily_report_app:app
   ```

5. Generate a public domain from the service Networking settings.

The service intentionally uses one Gunicorn worker because the current application stores records in JSON files. Runtime data is excluded from Git and must be stored on the mounted Volume.

### Optional Google Drive upload

Final Daily Report PDFs can be downloaded and uploaded automatically to a
structured Google Drive folder. This requires one-time Google OAuth setup and
Railway environment variables; a normal My Drive browser link alone does not
grant API access. Follow [GOOGLE_DRIVE_SETUP.md](GOOGLE_DRIVE_SETUP.md). Preview
PDFs are never uploaded, and a Drive failure does not remove the Railway copy.

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
