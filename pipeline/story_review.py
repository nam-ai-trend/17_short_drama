"""Nonblocking checks for breakdowns before media credits are spent."""

import re
from typing import Any, Dict, List, Optional


def review_breakdown(breakdown: Dict[str, Any], target_duration: Optional[int] = None) -> Dict[str, Any]:
    findings: List[Dict[str, str]] = []
    segments = breakdown.get("segments", [])

    if segments and target_duration:
        total_duration = sum(float(segment.get("duration") or 0) for segment in segments)
        if total_duration < target_duration * 0.75 or total_duration > target_duration * 1.25:
            findings.append({
                "segment_id": "전체",
                "code": "duration_mismatch",
                "message": f"목표 {target_duration}초에 비해 장면 합계가 {total_duration:g}초입니다. 사건과 대사 호흡을 다시 확인해 주세요.",
            })

    for index, segment in enumerate(segments):
        segment_id = str(segment.get("id", ""))

        def add(code: str, message: str) -> None:
            findings.append({"segment_id": segment_id, "code": code, "message": message})

        duration = float(segment.get("duration") or 0)
        if not str(segment.get("first_state", "")).strip():
            add("missing_first_state", "시작 화면 상태가 비어 있습니다.")
        if not str(segment.get("exit_state", "")).strip():
            add("missing_exit_state", "다음 장면으로 넘길 마지막 상태가 비어 있습니다.")
        if not segment.get("must_hit") or not segment.get("shots"):
            add("missing_visual_action", "이 장면에서 반드시 보여줄 행동이나 샷이 없습니다.")

        if index:
            previous = segments[index - 1]
            before_heading = str(previous.get('scene_heading') or '').strip().lower()
            after_heading = str(segment.get('scene_heading') or '').strip().lower()
            same_scene = previous.get('location') and previous.get('location') == segment.get('location') and (not before_heading or not after_heading or before_heading == after_heading)
            opening = (segment.get('shots') or [{}])[0]
            shared = set(previous.get('characters') or []) & set(opening.get('characters') or segment.get('characters') or [])
            before = str(previous.get('exit_state') or '')
            after = str(segment.get('first_state') or '') + ' ' + ' '.join(str(v) for v in (segment.get('states') or {}).values())
            if same_scene and shared and re.search(r'\b(hand|hands|손)\b', before, re.I) and re.search(r'\b(document|paper|contract|page|서류|계약서)\b', before, re.I) and not re.search(r'\b(hand|hands|document|paper|contract|page|손|서류|계약서)\b', after, re.I):
                add('handoff_state_review', '이전 씬에서 손과 서류의 관계가 중요하지만 이번 첫 상태에는 나타나지 않습니다. 같은 인물의 손 위치와 서류 상태가 유지되는지 확인하세요.')

        action_patterns = (
            r'\b(signs?|signing|signature|scrawls?)\b|서명',
            r'\b(stamps?|stamping|seal)\b|도장',
            r'\b(pushes?|shoves?|slides?|sliding|inserts?|encloses?)\b|밀어|넣',
            r'\b(folds?|folding|closes?|closing)\b|접|닫',
            r'\b(tape|tapes|taping|adhesive)\b|테이프',
        )
        for shot in segment.get('shots') or []:
            visual = str(shot.get('visual') or '')
            if sum(bool(re.search(pattern, visual, re.I)) for pattern in action_patterns) >= 3:
                add('complex_object_action', '한 샷에 서명·소품 교체·봉투·테이프 등 여러 조작이 겹칩니다. 실제 손의 접촉과 집기·놓기 순서를 확인하고 필요하면 씬을 나눠 주세요.')
                break

        dialogue = segment.get("dialogue") or []
        spoken_chars = sum(len(re.sub(r"[\s\W_]", "", str(line.get("line", "")))) for line in dialogue)
        if duration > 0 and spoken_chars / duration >= 5:
            add("dense_dialogue", f"대사 {spoken_chars}글자 / {duration:g}초입니다. 말보다 행동으로 보여주거나 장면 시간을 늘려 보세요.")
        # A planning estimate, not a measurement of generated speech. If the
        # shot list specifies where speech occurs, reserve its other shots for action.
        if dialogue and duration > 0:
            shots = segment.get('shots') or []
            windows = []
            for shot in shots:
                audio = str(shot.get('audio') or '')
                if not re.search(r'voice|speak|speech|speaks|talk|dialogue|speaking|말하|목소리|대사', audio, re.I):
                    continue
                match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)s\s*', str(shot.get('time') or ''), re.I)
                if match:
                    start, end = float(match.group(1)), float(match.group(2))
                    windows.append(max(0.0, min(end, duration) - max(0.0, start)))
            speech_window = min(duration, sum(windows)) if windows else duration
            available = max(0.5, speech_window - 1.0)
            if spoken_chars / available >= 5:
                add('speech_window_risk', f'대사 {spoken_chars}글자에 추정 발화 가능 시간 {available:.1f}초입니다. 행동·시작과 끝의 여유를 고려해 문장을 나누거나 시간을 늘려 주세요. 실제 길이는 렌더 후 확인해야 합니다.')

    return {"segment_count": len(segments), "findings": findings}
