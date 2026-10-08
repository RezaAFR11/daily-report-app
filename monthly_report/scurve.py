"""Project-specific historical snapshots; never add cumulative percentages."""
from datetime import date
import math

METHOD = "monthly_history_snapshots"
WEEKLY_METHOD = "weekly_history_snapshots"


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
    """Build only the history points explicitly selected by the reviewer."""
    kind = report.get('report_type') or 'monthly'
    if kind not in ('monthly', 'weekly'):
        return
    existing = report.get('s_curve')
    if isinstance(existing, dict) and existing.get('source_method') not in (METHOD, WEEKLY_METHOD) and 's_curve_selection' not in report:
        return
    report.pop('s_curve', None)
    report['include_s_curve'] = False
    report.pop('s_curve_selection_error', None)
    current = snapshot(report)
    report['s_curve_current_point'] = current
    report['s_curve_candidates'] = []
    project = str(report.get('project_no', '')).strip().casefold()
    start_current = _date((report.get('period') or {}).get('start'))
    if not start_current or not project:
        return
    latest = {}
    for old in history:
        if ((old.get('report_type') or 'monthly') != kind
                or str(old.get('project_no', '')).strip().casefold() != project
                or str(old.get('status', '')).upper() != 'FINAL'
                or old.get('lifecycle_status', 'active') != 'active'):
            continue
        period = old.get('period', {})
        if not isinstance(period, dict):
            continue
        start, end = _date(period.get('start')), _date(period.get('end'))
        if not start or not end or end >= start_current:
            continue
        key = (start, end)
        try:
            rank = (int(old.get('revision', 0)), str(old.get('generated_at', '')), str(old.get('report_id', '')))
        except (ValueError, TypeError):
            continue
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, old)
    candidates = []
    for _, old in sorted(latest.values(), key=lambda x: x[0]):
        point = snapshot(old)
        candidates.append({'id': str(old.get('_history_id') or old.get('report_id') or ''),
                           'period_start': old['period']['start'], 'period_end': old['period']['end'],
                           'revision': old.get('revision', 0), 'point': point,
                           'available': point is not None,
                           'reason': '' if point else 'Progress data unavailable'})
    report['s_curve_candidates'] = sorted(candidates, key=lambda c: (c['period_start'], c['period_end']))
    selection = report.get('s_curve_selection') or {}
    if selection.get('enabled') is not True:
        return
    selected = selection.get('report_ids', [])
    by_id = {c['id']: c for c in candidates if c['id']}
    if any(key not in by_id or not by_id[key]['available'] for key in selected):
        report['s_curve_selection_error'] = 'A selected S-Curve report is unavailable or has been revised. Select the available reports again.'
        return
    points = {}
    for key in selected:
        point = by_id[key]['point']
        if point['date'] in points and points[point['date']] != point:
            report['s_curve_selection_error'] = 'Selected reports have the same progress source date. Select only one report for that date.'
            return
        points[point['date']] = point
    if current:
        points[current['date']] = current
    ordered = [points[k] for k in sorted(points)]
    if len(ordered) < 2:
        report['s_curve_selection_error'] = 'Select progress data for at least two different source dates, including the current period when available.'
        return
    report['s_curve'] = {
        'source_method': WEEKLY_METHOD if kind == 'weekly' else METHOD,
        'labels': [p['date'] for p in ordered],
        'plan': [p['plan'] for p in ordered], 'actual': [p['actual'] for p in ordered],
        'points': ordered, 'illustrative': False, 'approved': True,
        'approval_basis': 'Explicit report selection and current report review; not baseline schedule approval',
        'note': 'Cumulative snapshots by source date. Plan is as reported, not an approved schedule baseline. Missing dates are not estimated. Historical values are not added to current progress.',
    }
    report['include_s_curve'] = True
