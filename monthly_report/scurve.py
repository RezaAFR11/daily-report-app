"""Project-specific historical snapshots; never add cumulative percentages."""
from datetime import date
import math

METHOD = "monthly_history_snapshots"


def _date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (ValueError, TypeError):
        return None


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().replace('%', '').replace(',', '.'))
        return number if math.isfinite(number) and 0 <= number <= 100 else None
    except ValueError:
        return None


def snapshot(report):
    progress = report.get('progress') or {}
    rows = progress.get('rows', []) if isinstance(progress, dict) else progress
    if not isinstance(rows, list):
        return None
    totals = [r for r in rows if isinstance(r, dict) and
              (r.get('is_total') or str(r.get('description', '')).strip().upper() in ('OVERALL PROGRESS', 'TOTAL OVERALL'))]
    if len(totals) != 1:
        return None
    row = totals[0]
    when = _date(row.get('source_date') or (progress.get('source_snapshot_date') if isinstance(progress, dict) else None))
    period = report.get('period', {})
    if not isinstance(period, dict):
        return None
    start, end = _date(period.get('start')), _date(period.get('end'))
    plan, actual = _number(row.get('plan')), _number(row.get('to_date'))
    if not (when and start and end and start <= when <= end) or plan is None or actual is None:
        return None
    return {'date': when, 'plan': plan, 'actual': actual,
            'report_id': report.get('report_id', report.get('draft_id', '')),
            'period_start': start, 'period_end': end, 'revision': report.get('revision', 0)}


def apply_history_curve(report, history):
    """Mutate only curve fields. An explicitly supplied curve takes precedence."""
    if report.get('report_type', 'monthly') != 'monthly':
        return
    existing = report.get('s_curve')
    if isinstance(existing, dict) and existing.get('source_method') != METHOD:
        return
    report.pop('s_curve', None)
    report['include_s_curve'] = False
    current = snapshot(report)
    project = str(report.get('project_no', '')).strip().casefold()
    if current is None or not project:
        return
    latest = {}
    for old in history:
        if (old.get('report_type', 'monthly') != 'monthly'
                or str(old.get('project_no', '')).strip().casefold() != project
                or str(old.get('status', '')).upper() != 'FINAL'
                or old.get('lifecycle_status', 'active') != 'active'):
            continue
        period = old.get('period', {})
        if not isinstance(period, dict):
            continue
        start, end = _date(period.get('start')), _date(period.get('end'))
        if not start or not end or end >= current['period_start']:
            continue
        key = (start, end)
        try:
            rank = (int(old.get('revision', 0)), str(old.get('generated_at', '')), str(old.get('report_id', '')))
        except (ValueError, TypeError):
            continue
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, old)
    points = {}
    for _, old in sorted(latest.values(), key=lambda x: x[0]):
        point = snapshot(old)
        if point:
            points[point['date']] = point
    points[current['date']] = current
    ordered = [points[k] for k in sorted(points)]
    if len(ordered) < 2:
        return
    report['s_curve'] = {
        'source_method': METHOD, 'labels': [p['date'] for p in ordered],
        'plan': [p['plan'] for p in ordered], 'actual': [p['actual'] for p in ordered],
        'points': ordered, 'illustrative': False, 'approved': True,
        'approval_basis': 'Saved Final history and current report review; not baseline schedule approval',
        'note': 'Cumulative snapshots by source date. Plan is as reported, not an approved schedule baseline. Missing dates are not estimated. Historical values are not added to current progress.',
    }
    report['include_s_curve'] = True
