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

## Periodic report architecture

Weekly and Monthly reports share one deterministic pipeline:

```text
Stored JSON or uploaded Daily Report PDF
    → normalize and validate source identity
    → select project/revision and aggregate period facts
    → review workforce, photos, and optional AI wording
    → run Preview/Final preflight
    → render PDF and archive the reviewed result
```

The implementation stays inside `monthly_report/`, with these responsibilities:

| Module | Responsibility |
| --- | --- |
| `web.py` | Flask routes, draft lifecycle, review workflow, and orchestration only. |
| `importer.py` | Tolerant parsing of legacy and current Daily Report PDF layouts into reviewable normalized data. |
| `validation.py` | Project identity and duplicate-date decisions without rewriting archived Daily Reports. |
| `aggregate.py` | Deterministic period coverage, activities, constraints, progress, manpower, and provenance. |
| `photos.py` | Bounded PDF/canonical photo verification, normalization, deduplication, mapping, and storage. |
| `timesheet.py` / `overtime.py` | Workbook parsing and review previews; no automatic application to the report. |
| `workforce.py` | Explicit keep/apply decisions and deterministic workforce calculations. |
| `report_quality.py` | Preview warnings and Final blockers, including readiness/risk metadata. |
| `ai_summary.py` | Grounded, review-only narrative suggestions with source validation and bounded provider calls. |
| `renderer.py` | Pure PDF composition; it has no Flask or report-storage dependency. |
| `storage.py` | Canonical Daily Report discovery and safe persistence helpers. |
| `identity.py` / `area_normalization.py` | Shared deterministic identity and reporting-area rules. |

Compatibility rules for future changes:

- Accept older and newer Daily Report layouts conservatively; unfamiliar or incomplete content becomes a review warning instead of an automatic rejection whenever it is safe to continue.
- Keep Preview available for reviewable data. Only factual ambiguity or explicitly critical conditions should block Final issue.
- Preserve source values and provenance. Project merges, duplicate choices, workforce overrides, AI wording, and photo review must remain explicit draft-local decisions.
- Never turn missing manpower, man-hours, overtime, safety, progress, or photo evidence into zero.
- Keep calculations and report facts deterministic. AI may improve wording only and must not replace verified tables or totals.
- Keep resource limits for PDF, image, workbook, and AI processing bounded; changing a limit requires focused regression tests.
- Preserve public route payloads and the reviewed JSON/PDF schema unless a versioned migration is introduced.

Run the full regression suite after changing the periodic-report pipeline:

```powershell
python -m unittest discover -s tests
```

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
