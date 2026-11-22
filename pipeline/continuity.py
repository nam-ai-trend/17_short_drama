"""Classify and validate adjacent shots before any paid media generation."""
import hashlib
import json
import re
from pathlib import Path

from pipeline.antigravity_bridge import extract_json_object, run_antigravity_prompt
from pipeline.common import read_json, write_json

_RESPONSE_SCHEMA = {
    'type': 'object',
    'properties': {
        'previous_id': {'type': 'string'}, 'current_id': {'type': 'string'},
        'previous_visible': {'type': 'array', 'items': {'type': 'string'}},
        'current_visible': {'type': 'array', 'items': {'type': 'string'}},
        'temporal_relation': {'type': 'string', 'enum': ['continuous_time', 'time_jump', 'unknown']},
        'mode': {'type': 'string', 'enum': ['previous_frame', 'state_reference', 'keyframe', 'needs_review']},
    },
    'required': ['previous_id', 'current_id', 'previous_visible', 'current_visible', 'temporal_relation', 'mode'],
}

def _digest(breakdown):
    raw = json.dumps(breakdown.get('segments', []), ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()

def load_continuity_plan(project_dir, episode_id):
    ep = Path(project_dir) / 'episodes' / episode_id
    breakdown_path, plan_path = ep / 'breakdown.json', ep / 'continuity_plan.json'
    if not breakdown_path.exists() or not plan_path.exists():
        return None
    plan = read_json(plan_path)
    return plan if plan.get('policy_version') == 7 and plan.get('breakdown_hash') == _digest(read_json(breakdown_path)) else None

def _visible(shot):
    return list(dict.fromkeys(shot.get('characters') or []))

def _pair(previous, current):
    previous_shots, current_shots = previous.get('shots') or [], current.get('shots') or []
    last = previous_shots[-1] if previous_shots else {}
    first = current_shots[0] if current_shots else {}
    return {'previous_id': previous['id'], 'current_id': current['id'],
            'previous': {'location':previous.get('location'), 'heading':previous.get('scene_heading'), 'visible':_visible(last), 'ending':previous.get('exit_state'), 'shot':last},
            'current': {'location':current.get('location'), 'heading':current.get('scene_heading'), 'time':current.get('time'), 'visible':_visible(first), 'starting':current.get('first_state'), 'shot':first}}

def _has_time_jump_evidence(before, after):
    """Find explicit cues, never treat their absence as proof of continuity."""
    previous_heading = str(before.get('heading') or '').casefold()
    current_heading = str(after.get('heading') or '').casefold()
    day_night = re.compile(r'\b(day|night|dawn|dusk|morning|evening)\b|낮|밤|새벽|저녁', re.I)
    prev_times = {m.group(0).casefold() for m in day_night.finditer(previous_heading)}
    curr_times = {m.group(0).casefold() for m in day_night.finditer(current_heading)}
    if prev_times and curr_times and prev_times != curr_times:
        return True
    shot = after.get('shot') or {}
    text = ' '.join(str(v or '') for v in (after.get('time'), after.get('starting'), shot.get('visual')))
    return bool(re.search(r'\b(flashback|flash forward|time jump|next (?:day|morning|night|week|year)|(?:hours?|days?|weeks?|months?|years?) later|the previous (?:day|night)|earlier that (?:day|morning|night))\b|회상|다음\s*날|다음\s*날\s*아침|전날|며칠\s*후|몇\s*(?:시간|분|달|년)\s*(?:후|뒤)', text, re.I))

def _validate_pair(pair, result):
    if not isinstance(result, dict) or result.get('previous_id') != pair['previous_id'] or result.get('current_id') != pair['current_id']:
        return '이전·현재 장면 번호가 일치하지 않습니다.'
    for key, side in [('previous_visible','previous'), ('current_visible','current')]:
        values = result.get(key)
        if not isinstance(values, list) or any(not isinstance(x, str) for x in values) or sorted(values) != sorted(pair[side]['visible']):
            return f"{pair[side]['id'] if 'id' in pair[side] else pair[side + '_id']} 화면 인물 근거가 일치하지 않습니다."
    relation, mode = result.get('temporal_relation'), result.get('mode')
    if relation not in ('continuous_time','time_jump','unknown') or mode not in ('previous_frame','state_reference','keyframe','needs_review'):
        return '시간 관계 또는 연결 방식이 올바르지 않습니다.'
    if relation == 'unknown' and mode != 'needs_review':
        return '시간 관계가 불확실하지만 확인 없이 연결하려 했습니다.'
    before, after = pair['previous'], pair['current']
    same_place = bool(before.get('location') and before.get('location') == after.get('location'))
    if not same_place and mode not in ('keyframe','needs_review'):
        return '서로 다른 장소를 연결하려 했습니다.'
    if relation == 'time_jump' and mode not in ('keyframe','needs_review'):
        return 'AI가 시간 전환과 이전 영상 연결을 함께 선택해 판정이 모순됩니다.'
    if same_place and relation == 'time_jump' and mode == 'keyframe' and not _has_time_jump_evidence(before, after):
        return '같은 장소에서 시간 전환이라고 판정했지만 명시된 시간 전환 근거를 찾을 수 없습니다.'
    if same_place and relation == 'continuous_time' and mode == 'keyframe':
        return '같은 장소·시각인데 독립 키프레임으로 분류했습니다.'
    if mode in ('previous_frame','state_reference') and relation != 'continuous_time':
        return '같은 시각인지 확인하지 못했습니다.'
    if mode in ('previous_frame','state_reference') and (not before['shot'] or not after['shot']):
        return '이전 마지막 샷 또는 현재 첫 샷이 없어 연결 근거가 부족합니다.'
    if mode in ('previous_frame','state_reference') and any(not isinstance(side['shot'].get('characters'), list) for side in (before, after)):
        return '화면 인물 목록이 누락되어 연결 근거를 검증할 수 없습니다.'
    if mode == 'previous_frame':
        old, new = before['shot'], after['shot']
        if not all(old.get(k) and new.get(k) for k in ('size','angle')):
            return '샷 크기 또는 각도 정보가 없어 동일 구도인지 확인할 수 없습니다.'
        if sorted(before['visible']) != sorted(after['visible']) or any(old.get(k) and new.get(k) and old[k] != new[k] for k in ('size','angle')):
            return '화면 인물 또는 구도가 달라 마지막 프레임을 그대로 사용할 수 없습니다.'
    return None

def _prompt(pair, correction=''):
    return ('인접한 오직 한 쌍의 이전 장면 마지막 샷과 현재 장면 첫 샷을 비교한다. '
            'previous_id, current_id와 previous_visible/current_visible은 입력에서 정확히 복사한다. '
            'temporal_relation은 continuous_time(대화·사건이 시간 경과 없이 바로 이어짐), time_jump(시간 경과·회상·날짜 변경), unknown(불확실) 중 하나다. '
            '카메라 구도, 화면 인물 또는 대사 화자가 바뀌는 것은 시간 전환이 아니다. '
            '같은 장소·같은 시각의 같은 화면이 실제로 바로 이어질 때만 previous_frame, '
            '같은 장소·같은 시각에 화면 인물이나 구도가 바뀌면 state_reference를 선택한다. '
            '장소 또는 시각이 바뀌면 keyframe, 판단이 불확실하면 needs_review를 선택한다. '
            '비슷한 DAY 표기만으로 같은 시각이라 단정하지 말고 앞뒤 동작을 확인한다. '
            '같은 장소·동일 시각인데 화면 인물만 바뀌었다는 이유로 keyframe을 선택하지 않는다. '
            '같은 장소에서 time_jump는 회상·다음날·몇 시간 후 등 명시된 시간 경과 근거가 있을 때만 사용한다. 근거가 없다면 unknown과 needs_review를 선택한다. '
            '선행 장면이 여러 샷이면 마지막 샷을 비교한다. 아래 JSON 스키마에 맞는 객체 하나만 반환한다. '
            + 'RESPONSE_SCHEMA:' + json.dumps(_RESPONSE_SCHEMA, ensure_ascii=False) + ' '
            + (f'이전 응답 오류: {correction} 입력을 다시 비교해 수정하라. ' if correction else '')
            + 'PAIR_JSON:' + json.dumps(pair, ensure_ascii=False))

def ensure_continuity_plan(project_dir, episode_id, classify=None, log_fn=None):
    existing = load_continuity_plan(project_dir, episode_id)
    if existing is not None:
        return existing
    ep = Path(project_dir) / 'episodes' / episode_id
    breakdown = read_json(ep / 'breakdown.json')
    segments = breakdown.get('segments', [])
    decisions = {}
    for previous, current in zip(segments, segments[1:]):
        pair = _pair(previous, current)
        if log_fn:
            log_fn(f'🔎 장면 연결 판정: {previous["id"]} → {current["id"]}')
        error, result = None, None
        for _ in range(2):
            try:
                prompt = _prompt(pair, error or '')
                raw = classify(prompt) if classify else run_antigravity_prompt(prompt, log_fn=log_fn)
                result = extract_json_object(raw)
                error = _validate_pair(pair, result)
                if error is None:
                    break
            except RuntimeError:
                raise
            except (ValueError, TypeError, KeyError) as exc:
                error = f'판정 응답을 읽을 수 없습니다: {exc}'
        if error:
            mode, reason = 'needs_review', f'{pair["previous_id"]} → {pair["current_id"]}: {error}'
        else:
            mode = result['mode']
            prev_visible = ', '.join(pair['previous']['visible']) or '화면 인물 없음'
            next_visible = ', '.join(pair['current']['visible']) or '화면 인물 없음'
            mode_label = {'previous_frame':'마지막 프레임 연결', 'state_reference':'이전 영상 참고 새 구도', 'keyframe':'독립 키프레임', 'needs_review':'수동 확인 필요'}[mode]
            reason = f'{pair["previous_id"]} 끝 화면({prev_visible}) → {pair["current_id"]} 시작 화면({next_visible}): {mode_label}.'
        decisions[current['id']] = {'mode':mode, 'reason':reason, 'previous_segment_id':previous['id']}
    plan = {'policy_version':7, 'breakdown_hash':_digest(breakdown), 'decisions':decisions}
    write_json(ep / 'continuity_plan.json', plan)
    return plan

def assert_continuity_ready(plan, overrides, segment_id=None):
    decisions = plan.get('decisions', {})
    unresolved = [seg_id for seg_id, decision in decisions.items()
                  if (segment_id is None or seg_id == segment_id)
                  and overrides.get(seg_id, decision.get('mode')) == 'needs_review']
    if unresolved:
        details = '; '.join(f"{seg}: {decisions[seg].get('reason', '자동 판정 불확실')}" for seg in unresolved)
        raise RuntimeError('장면 연결 판정을 확인해 주세요. 이미지 또는 영상 탭의 시작 화면에서 연결 방식을 직접 선택한 후 다시 실행해 주세요. ' + details)
