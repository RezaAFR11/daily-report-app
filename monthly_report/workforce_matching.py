"""Conservative cross-source identities and per-person attendance precedence."""
from __future__ import annotations

import copy
import re
import unicodedata
from collections import Counter, defaultdict


def name_tokens(value):
    words = re.findall(r"[^\W_]+", unicodedata.normalize('NFKC', str(value)).casefold())
    return ['muhammad' if word in {'muh', 'muhamad', 'mohammad', 'mohamad'} else word for word in words]


def _word_matches(a, b):
    return a == b or (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b))


def _one_typo(a, b):
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    if len(a) > len(b):
        a, b = b, a
    return any(a == b[:i] + b[i + 1:] for i in range(len(b)))


def _name_matches(a, b, *, typo=False):
    if not a or not b:
        return False
    if len(a) > len(b):
        a, b = b, a
    if len(a) == 1:
        # A full first name may match uniquely; a lone initial may not.
        full_words = [word for word in b if len(word) > 1]
        first = full_words[0] if full_words else ''
        return len(a[0]) > 1 and (a[0] == first or (typo and _one_typo(a[0], first)))
    # Do not find an arbitrary matching name fragment in the middle of an
    # unrelated full name (Edy Suryadi must not match Hargo Wahono Edy.S).
    # A leading Muhammad/M. may be omitted, but other leading names may not.
    anchors = [b[0]]
    if b[0] in {'muhammad', 'm'} and len(b) > 1:
        anchors.append(b[1])
    if not any(_word_matches(a[0], token) or (typo and _one_typo(a[0], token)) for token in anchors):
        return False
    position = 0
    for token in a:
        while position < len(b) and not (_word_matches(token, b[position]) or (typo and _one_typo(token, b[position]))):
            position += 1
        if position == len(b):
            return False
        position += 1
    return True


def match_employee(name, employees, employee_id=''):
    """Return one deterministic candidate, or flag ambiguity without guessing."""
    if employee_id:
        ids = [e for e in employees if e.get('employee_id') == employee_id]
        if len(ids) == 1:
            return ids[0], 'employee_id'
        if ids:
            return None, 'ambiguous'
    eligible = [e for e in employees if not employee_id or not e.get('employee_id') or e['employee_id'] == employee_id]
    tokens = name_tokens(name)
    if not tokens:
        return None, 'unmatched'
    exact = [e for e in eligible if name_tokens(e.get('name', '')) == tokens]
    candidates = exact or [e for e in eligible if _name_matches(tokens, name_tokens(e.get('name', '')))]
    typo_candidates = []
    if not candidates:
        typo_candidates = [e for e in eligible if _name_matches(tokens, name_tokens(e.get('name', '')), typo=True)]
        candidates = typo_candidates
    if len(candidates) == 1:
        return candidates[0], 'exact' if exact else ('spelling_variant' if typo_candidates else 'name_variant')
    return None, 'ambiguous' if candidates else 'unmatched'


def reconcile_timesheet(preview, baseline):
    """Use timesheet statuses where a name exists; otherwise retain Daily rows."""
    result = copy.deepcopy(dict(preview))
    days = baseline.get('daily', [])
    if not any('people' in day for day in days):
        if days:
            result.setdefault('warnings', []).append({'code': 'daily_people_unavailable', 'severity': 'warning',
                'message': 'Recompile the Daily sources to enable per-person Daily fallback; this older draft has totals only.'})
        return result
    employees = result.setdefault('employees', [])
    source_employees = list(employees)
    fallback = {}
    audit = []
    warnings = result.setdefault('warnings', [])
    for day in days:
        date = day.get('date', '')
        for person in day.get('people', []):
            match, method = match_employee(person.get('name', ''), source_employees, person.get('employee_id', ''))
            if match:
                audit.append({'daily_name': person['name'], 'timesheet_name': match['name'], 'date': date, 'method': method})
                status = next((s.get('status') for s in match.get('statuses', []) if s.get('date') == date), 'missing')
                if status != 'present':
                    warnings.append({'code': 'daily_timesheet_attendance_difference', 'severity': 'warning', 'date': date,
                        'message': f"{date}: {person['name']} is listed in the Daily Report but timesheet status is {status}. Timesheet takes precedence."})
                continue
            if method == 'ambiguous':
                result.setdefault('unresolved', []).append({'name': person['name'], 'date': date, 'reason': 'ambiguous_daily_name'})
                warnings.append({'code': 'ambiguous_daily_name', 'severity': 'warning', 'date': date,
                    'message': f"{date}: {person['name']} matches multiple timesheet workers. Daily entry retained separately pending identity review; headcount and hours may be counted twice. Verify the full name or employee ID before Final issue."})
            key = 'daily:' + (person.get('employee_id') or ' '.join(name_tokens(person.get('name', ''))))
            if key not in fallback:
                fallback[key] = {'employee_key': key, 'name': person.get('name', ''), 'employee_id': person.get('employee_id', ''),
                    'role': person.get('role') or 'Unspecified', 'section': person.get('section', 'direct'),
                    'statuses': [], 'source': 'daily_fallback'}
            entry = fallback[key]
            hours = person.get('hours')
            existing = next((s for s in entry['statuses'] if s['date'] == date), None)
            if existing:
                existing['physical_manhours'] = max(existing['physical_manhours'], hours or 0)
            else:
                entry['statuses'].append({'date': date, 'status': 'present', 'physical_manhours': hours or 0,
                    'hours_missing': hours is None, 'sources': ['daily_report']})
            if hours is None:
                result.setdefault('unresolved', []).append({'name': person['name'], 'date': date, 'reason': 'daily_hours_missing'})
    employees.extend(fallback.values())
    result['identity_matches'] = audit
    result['daily_fallback_count'] = len(fallback)
    if fallback:
        warnings.append({'code': 'daily_person_fallback', 'severity': 'warning',
            'message': 'Workers absent from the timesheet roster retain Daily attendance/hours: ' + ', '.join(e['name'] for e in fallback.values())})
    variants = [a for a in audit if a['method'] in {'name_variant', 'spelling_variant'}]
    if variants:
        warnings.append({'code': 'matched_name_variants', 'severity': 'warning',
            'message': 'Unique name variants matched: ' + '; '.join(sorted({a['daily_name'] + ' = ' + a['timesheet_name'] for a in variants}))})

    dates = {row['date'] for row in result.get('daily_totals', [])} | {day['date'] for day in days}
    daily = {date: {'date': date, 'present_count': 0, 'physical_manhours': 0,
                    'hours_complete': True, 'status_counts': Counter(), 'present_by_section': Counter(), 'hours_by_section': Counter()} for date in sorted(dates)}
    roles = {}
    by_section = defaultdict(lambda: {'employee_count': 0, 'present_person_days': 0, 'physical_manhours': 0})
    for employee in employees:
        present = [s for s in employee.get('statuses', []) if s.get('status') == 'present' and s.get('date') in daily]
        employee['present_days'] = len(present)
        employee['physical_manhours'] = sum(s.get('physical_manhours', 10) for s in present)
        section = employee.get('section', 'direct')
        role = employee.get('role') or 'Unspecified'
        if present:
            by_section[section]['employee_count'] += 1
            r = roles.setdefault(role, {'role': role, 'employee_count': 0, 'present_person_days': 0, 'physical_manhours': 0})
            r['employee_count'] += 1
            r['present_person_days'] += len(present)
            r['physical_manhours'] += employee['physical_manhours']
        for status in employee.get('statuses', []):
            row = daily.get(status.get('date'))
            if row is None:
                continue
            row['status_counts'][status['status']] += 1
            if status.get('hours_missing'):
                row['hours_complete'] = False
            if status['status'] == 'present':
                hours = status.get('physical_manhours', 10)
                row['present_count'] += 1
                row['physical_manhours'] += hours
                row['present_by_section'][section] += 1
                row['hours_by_section'][section] += hours
                by_section[section]['present_person_days'] += 1
                by_section[section]['physical_manhours'] += hours
    result['daily_totals'] = list(daily.values())
    result['roles'] = list(roles.values())
    result['totals'] = {'employee_count': len(employees), 'present_person_days': sum(r['present_count'] for r in daily.values()),
        'physical_manhours': sum(r['physical_manhours'] for r in daily.values()),
        'peak_present_count': max((r['present_count'] for r in daily.values()), default=0),
        'by_section': dict(by_section)}
    return result
