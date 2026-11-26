"""Deterministic, opt-in suggestions. Never execute external tools or change goals."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from .proactive import may_suggest
from .vault import now
from .spaces import normalize_space


def candidates(state, rule: dict[str, Any], instant, scheduled=True):
    rule = {**rule, 'space': normalize_space(rule['space'])}
    local = datetime.fromisoformat(instant).astimezone(ZoneInfo(rule['timezone']))
    if scheduled and (local.hour < rule['hour'] or (rule['kind'] == 'weekly_review' and local.weekday() != rule['weekday'])):
        return []
    records = records_all(state, rule['space'])
    if rule['kind'] == 'weekly_review':
        records = sorted([r for r in records if r['kind'] == 'project' and r['data']['status'] == 'active'], key=lambda r:r['id'])[:1]
    else:
        records = [r for r in records if r['kind'] == ('task' if rule['kind'] == 'task_due' else 'project') and r['data']['status'] == 'active']
    results = []
    for r in records:
        d = r['data']
        if rule['kind'] == 'task_due':
            if not d.get('due_on') or d['due_on'] > local.date().isoformat(): continue
            text, reason = '看看这项到期任务的下一步', '目标日期：' + d['due_on']
        elif rule['kind'] == 'project_stalled':
            related = [r] + [t for t in records_all(state, rule['space']) if t['kind'] == 'task' and t['data']['parent_id'] == r['id']]
            latest = max(datetime.fromisoformat(t['updated_at']) for t in related)
            if datetime.fromisoformat(instant) - latest < timedelta(days=rule['days']): continue
            text, reason = '给这个项目安排一个小行动', f"已 {rule['days']} 天没有项目或任务更新"
        else:
            text, reason = '为这一周留一份复盘', '根据本周实际完成的任务和习惯，写下下一步'
        results.append({'id':f"rule:{rule['id']}:{local.date().isoformat()}:{r['id']}", 'source_id':r['id'],
            'space':rule['space'], 'text':text, 'reason':d['title'] + ' · ' + reason,
            'action':'weekly_review' if rule['kind']=='weekly_review' else 'review', 'state':'new', 'created_at':instant,
            'expires_at':(datetime.fromisoformat(instant)+timedelta(days=2)).isoformat(),
            'sources':[{'id':r['id'], 'revision':r['revision']}], 'rule_id':rule['id']})
    return results


def records_all(state, space):
    return [r for r in state['records'].values() if normalize_space(r['space']) == normalize_space(space)]


def run_rules(vault, instant=None):
    instant = instant or now()
    with vault.view() as state:
        marker = (state.store_revision, instant[:16])
        if getattr(vault, '_rules_check', None) == marker:
            return
        if not state.get('automation_rules') and not any(b.get('status') == 'pending' and b.get('expires_at', '~') <= instant
                for b in state.get('connector_batches', {}).values()):
            vault._rules_check = marker
            return
        # Evaluate against an isolated read view first; idle polls never take a writer lock.
        from .vault import canonical
        before = canonical([dict(state['suggestions']), state.get('connector_batches', {}), state['seq']])
        evaluate_rules(state, instant)
        changed = before != canonical([dict(state['suggestions']), state.get('connector_batches', {}), state['seq']])
    if not changed:
        vault._rules_check = marker
        return
    with vault.transaction() as state:
        evaluate_rules(state, instant)
    vault._rules_check = marker


def evaluate_rules(state, instant):
    from .connector_gateway import expire_batches
    expire_batches(state, instant)
    for rule in state.get('automation_rules', {}).values():
        eligible = {s['source_id'] for s in candidates(state, rule, instant, scheduled=False)} if rule['enabled'] else set()
        for old in state['suggestions'].values():
            if old.get('rule_id') == rule['id'] and old['state'] in {'new','snoozed'} and old['source_id'] not in eligible:
                old['state'] = 'expired'
        if not rule['enabled'] or not may_suggest(state, instant): continue
        for suggestion in candidates(state, rule, instant):
            if not may_suggest(state, instant): break
            if suggestion['id'] not in state['suggestions']:
                state['suggestions'][suggestion['id']] = suggestion
                state['seq'] += 1
