import os
import json
import subprocess
import tempfile
from PIL import Image, ImageStat, ImageFilter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from pipeline.common import ensure_dir, load_config, render_template, read_json, write_json
from pipeline.stage4_keyframes import resolve_segment_keyframe
from pipeline.continuity import load_continuity_plan
from pipeline.scene_state import planned_state_context

def load_continuity_modes(project_dir, episode_id):
    path = Path(project_dir) / 'episodes' / episode_id / 'continuity.json'
    return read_json(path) if path.exists() else {}

def save_continuity_modes(project_dir, episode_id, modes):
    if not isinstance(modes, dict) or any(mode not in ('keyframe', 'previous_frame', 'state_reference') for mode in modes.values()):
        raise ValueError('잘못된 장면 연결 방식입니다.')
    path = Path(project_dir) / 'episodes' / episode_id / 'continuity.json'
    ensure_dir(path.parent)
    write_json(path, modes)

def validate_continuity_frame(frame):
    with Image.open(frame) as im:
        gray = im.convert('L').resize((128, 128))
        luminance = ImageStat.Stat(gray).mean[0]
        edges = gray.filter(ImageFilter.FIND_EDGES).crop((1, 1, 127, 127))
        edge_strength = ImageStat.Stat(edges).mean[0]
    if luminance < 8:
        raise RuntimeError('이전 영상의 마지막 프레임이 검은 화면 또는 페이드아웃입니다. 이전 테이크를 확인해 주세요.')
    if edge_strength < 0.5:
        raise RuntimeError('이전 영상의 마지막 프레임이 지나치게 흐리거나 단색입니다. 이전 테이크를 확인해 주세요.')

def continuity_frame_status(project_dir, episode_id, segment_id, previous_id):
    ep = Path(project_dir) / 'episodes' / episode_id
    previous = ep / 'takes' / f'{previous_id}_take01.mp4'
    frame = ep / 'keyframes' / 'continuity' / f'{segment_id}_from_{previous_id}.png'
    if not frame.is_file() or frame.stat().st_size == 0:
        return {'state': 'missing', 'path': None}
    if not previous.is_file() or previous.stat().st_size == 0:
        return {'state': 'stale', 'path': None}
    try:
        metadata = read_json(frame.with_suffix('.json'))
        stat = previous.stat()
        if metadata.get('previous_size') != stat.st_size or metadata.get('previous_mtime_ns') != stat.st_mtime_ns:
            return {'state': 'stale', 'path': None}
        return {'state': 'ready', 'path': frame}
    except (OSError, ValueError, TypeError):
        return {'state': 'unverified', 'path': None}

def extract_previous_end_frame(project_dir, episode_id, segment_id, previous_id):
    ep_dir = Path(project_dir) / 'episodes' / episode_id
    previous = ep_dir / 'takes' / f'{previous_id}_take01.mp4'
    if not previous.is_file() or previous.stat().st_size == 0:
        raise RuntimeError(f'{segment_id}: 이전 영상 {previous_id}이 없어 연결할 수 없습니다.')
    out = ensure_dir(ep_dir / 'keyframes' / 'continuity') / f'{segment_id}_from_{previous_id}.png'
    ffmpeg = load_config().get('paths', {}).get('ffmpeg_bin', '/opt/homebrew/bin/ffmpeg')
    try:
        previous_stat = previous.stat()
        ffprobe = str(Path(ffmpeg).with_name('ffprobe'))
        probe = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(previous)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        duration = float(probe.stdout.strip())
        if duration <= 0:
            raise RuntimeError('이전 영상의 영상 스트림 길이를 확인할 수 없습니다.')
        with tempfile.TemporaryDirectory(dir=str(out.parent)) as tmp:
            frame = Path(tmp) / 'last.png'
            subprocess.run([ffmpeg, '-v', 'error', '-ss', str(max(0.0, duration - 0.5)), '-i', str(previous), '-map', '0:v:0', '-vf', 'reverse', '-frames:v', '1', '-y', str(frame)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if not frame.is_file() or frame.stat().st_size == 0:
                raise RuntimeError('프레임 추출 결과가 비었습니다.')
            validate_continuity_frame(frame)
            current_stat = previous.stat()
            if (current_stat.st_mtime_ns, current_stat.st_size) != (previous_stat.st_mtime_ns, previous_stat.st_size):
                raise RuntimeError('프레임 추출 중 이전 영상이 변경되었습니다. 다시 시도해 주세요.')
            frame.replace(out)
            write_json(out.with_suffix('.json'), {'previous_size': current_stat.st_size, 'previous_mtime_ns': current_stat.st_mtime_ns})
    except Exception as exc:
        raise RuntimeError(f'{segment_id}: {previous_id} 마지막 프레임 추출 실패: {exc}') from exc
    return out

def resolve_take_start_image(project_dir, episode_id, tasks, index):
    ep_dir = Path(project_dir) / 'episodes' / episode_id
    seg_id = tasks[index]['segment_id']
    overrides = load_continuity_modes(project_dir, episode_id)
    plan = load_continuity_plan(project_dir, episode_id) or {}
    mode = overrides.get(seg_id, plan.get('decisions', {}).get(seg_id, {}).get('mode', 'keyframe'))
    if mode == 'keyframe':
        keyframe = ep_dir / 'keyframes' / f'{seg_id}.png'
        if not keyframe.is_file():
            raise RuntimeError(f'키프레임({keyframe.name})이 없습니다. Step 4를 먼저 완료해 주세요.')
        return keyframe
    if mode == 'state_reference':
        raise RuntimeError(f'{seg_id}: 이전 영상 상태를 반영한 새 이미지가 아직 준비되지 않았습니다.')
    if index == 0:
        raise RuntimeError(f'{seg_id}: 첫 세그먼트에는 이전 영상이 없습니다.')
    return extract_previous_end_frame(project_dir, episode_id, seg_id, tasks[index - 1]['segment_id'])

def build_all_segment_image_references(project_dir: Union[str, Path], segment: Dict[str, Any], episode_id: str = "EP001", max_refs: int = 9) -> List[Path]:
    """Gather all character (face + sheet), location, and prop reference images; switch to sheet.png only when over max_refs."""
    from pipeline.stage2_assets import resolve_asset_image
    proj = Path(project_dir)
    
    # If segment dict lacks shots/characters, try loading from breakdown.json
    seg_id = segment.get("segment_id") or segment.get("id")
    if not segment.get("shots") and seg_id:
        breakdown_file = proj / "episodes" / episode_id / "breakdown.json"
        if breakdown_file.exists():
            try:
                b_data = read_json(breakdown_file)
                for s in b_data.get("segments", []):
                    if s.get("id") == seg_id:
                        segment = s
                        break
            except Exception:
                pass

    shots = segment.get("shots", [])
    
    # 1. Collect all characters across the entire segment (not just the first shot)
    all_chars = list(dict.fromkeys(
        (segment.get("characters") or []) +
        [c for s in shots for c in (s.get("characters") or [])]
    ))
    
    # Priority sorting: Prioritize characters NOT in the starting frame/shot so they never get dropped by the 9-image limit
    first_shot_chars = set(shots[0].get("characters", [])) if shots else set()
    unseen_chars = [c for c in all_chars if c not in first_shot_chars]
    seen_chars = [c for c in all_chars if c in first_shot_chars]
    prioritized_chars = unseen_chars + seen_chars
    
    loc_refs: List[Path] = []
    loc_id = segment.get("location")
    if loc_id:
        loc_res = resolve_asset_image(proj, "scenes", loc_id, "base")
        if loc_res.get("exists") and loc_res.get("path") and Path(loc_res["path"]).is_file():
            loc_refs.append(Path(loc_res["path"]))

    char_full_refs: List[Path] = []
    char_sheet_only_refs: List[Path] = []
    for cid in prioritized_chars:
        face_res = resolve_asset_image(proj, "characters", cid, "face")
        sheet_res = resolve_asset_image(proj, "characters", cid, "sheet")
        face_p = Path(face_res["path"]) if face_res.get("exists") and face_res.get("path") and Path(face_res["path"]).is_file() else None
        sheet_p = Path(sheet_res["path"]) if sheet_res.get("exists") and sheet_res.get("path") and Path(sheet_res["path"]).is_file() else None

        if face_p and face_p not in char_full_refs:
            char_full_refs.append(face_p)
        if sheet_p and sheet_p not in char_full_refs:
            char_full_refs.append(sheet_p)

        # Compact mode (when over limit): use sheet.png (full body + face turnaround) only; fallback to face.png if missing
        compact_p = sheet_p or face_p
        if compact_p and compact_p not in char_sheet_only_refs:
            char_sheet_only_refs.append(compact_p)

    # 4. Add prop sheets ONLY when the segment actually features an explicit prop insert/macro shot
    # (Prevents Seedance Reference Leakage where passing a flat document sheet makes the AI shove it into frame at 0.0s)
    is_prop_showcase = any(
        (not (s.get("characters") or [])) or
        any(k in (f"{s.get('size', '')} {s.get('camera', '')}").lower() for k in ("macro", "insert", "extreme close-up", "detail shot", "close-up on"))
        for s in shots
    ) if shots else True

    prop_refs: List[Path] = []
    if is_prop_showcase:
        for pid in segment.get("props", []):
            prop_res = resolve_asset_image(proj, "props", pid, "sheet")
            if prop_res.get("exists") and prop_res.get("path") and Path(prop_res["path"]).is_file():
                p = Path(prop_res["path"])
                if p not in prop_refs:
                    prop_refs.append(p)

    full_refs = list(dict.fromkeys(loc_refs + char_full_refs + prop_refs))
    if len(full_refs) <= max_refs:
        return full_refs

    # Over slot limit: use character sheet.png only so all characters + location + props fit cleanly
    compact_refs = list(dict.fromkeys(loc_refs + char_sheet_only_refs + prop_refs))
    return compact_refs[:max_refs]

def get_muted_video_reference(video_path: Path) -> Path:
    """Extract or return video without audio track to prevent audio hallucination / repetition in Seedance."""
    if not video_path.is_file():
        return video_path
    muted_path = video_path.parent / f"{video_path.stem}_muted.mp4"
    if muted_path.is_file() and muted_path.stat().st_mtime >= video_path.stat().st_mtime:
        return muted_path
    ffmpeg = load_config().get('paths', {}).get('ffmpeg_bin', '/opt/homebrew/bin/ffmpeg')
    try:
        subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(video_path), "-an", "-c:v", "copy", "-y", str(muted_path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if muted_path.is_file() and muted_path.stat().st_size > 0:
            return muted_path
    except Exception:
        pass
    return video_path

def build_continuity_video_inputs(project_dir, episode_id, tasks, index, mode, model, image_task):
    """Build explicit Seedance inputs; never silently drop a required reference."""
    def _gather_refs(max_refs: int) -> List[Path]:
        r = build_all_segment_image_references(project_dir, tasks[index], episode_id, max_refs=max_refs)
        if not r and image_task:
            r = [Path(p) for p in image_task.get('image_references', []) if Path(p).is_file()]
        return r[:max_refs]

    if index == 0 or mode == 'keyframe':
        start_img = resolve_take_start_image(project_dir, episode_id, tasks, index)
        max_refs = 8 if start_img else 9  # Seedance counts start_image toward the 9-image limit
        return {
            'start_image': start_img,
            'video_references': [],
            'image_references': _gather_refs(max_refs)
        }
    seg_id = tasks[index]['segment_id']
    if mode not in ('previous_frame', 'state_reference'):
        raise ValueError(f'{seg_id}: 알 수 없는 연결 방식: {mode}')
    if model not in ('seedance_2_0', 'seedance_2_0_mini'):
        raise RuntimeError(f'{seg_id}: 이전 영상 참조는 현재 Seedance 2.0 및 Mini에서만 지원합니다. 모델 또는 연결 방식을 변경해 주세요.')
    previous_id = tasks[index - 1]['segment_id']
    previous = Path(project_dir) / 'episodes' / episode_id / 'takes' / f'{previous_id}_take01.mp4'
    if not previous.is_file() or previous.stat().st_size == 0:
        raise RuntimeError(f'{seg_id}: 이전 영상 {previous_id}이 없어 연결할 수 없습니다.')
    
    # Strip audio from previous video to prevent audio hallucination / dialogue repetition
    previous_ref = get_muted_video_reference(previous)

    if mode == 'previous_frame':
        start_img = extract_previous_end_frame(project_dir, episode_id, seg_id, previous_id)
        max_refs = 8 if start_img else 9
        return {
            'start_image': start_img,
            'video_references': [previous_ref],
            'image_references': _gather_refs(max_refs)
        }

    # In state_reference mode, anchor start_image using keyframe if exists on disk
    ep_dir = Path(project_dir) / 'episodes' / episode_id
    keyframe = ep_dir / 'keyframes' / f'{seg_id}.png'
    start_img = keyframe if keyframe.is_file() and keyframe.stat().st_size > 0 else None
    max_refs = 8 if start_img else 9
    refs = _gather_refs(max_refs)

    if any(not p.is_file() or p.stat().st_size == 0 for p in refs):
        raise RuntimeError(f'{seg_id}: 인물·장소 레퍼런스 파일이 없어 영상 생성을 중단합니다.')
    
    location = tasks[index].get('location') or (image_task.get('location_id') if image_task else None)
    if not location:
        breakdown_file = Path(project_dir) / "episodes" / episode_id / "breakdown.json"
        if breakdown_file.exists():
            try:
                for s in read_json(breakdown_file).get("segments", []):
                    if s.get("id") == seg_id:
                        location = s.get("location")
                        break
            except Exception:
                pass
    scene_dir = Path(project_dir) / 'assets' / 'scenes' / str(location)
    if not location or not any(p.parent == scene_dir for p in refs):
        raise RuntimeError(f'{seg_id}: 고정 장소 기준 이미지가 없어 영상 생성을 중단합니다. 마스터 시트를 확인해 주세요.')
    
    return {'start_image': start_img, 'video_references': [previous_ref], 'image_references': refs}

def continuity_video_args(inputs):
    args = []
    if inputs['start_image']:
        args.extend(['--start-image', str(inputs['start_image'].resolve())])
    for path in inputs['image_references']:
        args.extend(['--image-references', str(path.resolve())])
    for path in inputs['video_references']:
        args.extend(['--video-references', str(path.resolve())])
    return args

def continuity_video_instruction(mode, inputs):
    if mode == 'previous_frame':
        return '\nContinuity: Start from the exact supplied start image. The reference video is the preceding shot; use it only to understand immediately preceding movement and character state. Do not repeat earlier action or dialogue.'
    if mode == 'state_reference':
        labels = []
        for i, path in enumerate(inputs['image_references'], 1):
            category = path.parent.parent.name
            if category == 'scenes':
                labels.append(f'Image {i}: fixed location anchor ({path.parent.name}); preserve walls, windows, doors and furniture spatial relationships.')
            elif category == 'characters':
                labels.append(f'Image {i}: current character identity and appearance ({path.parent.name}).')
            elif category == 'props':
                labels.append(f'Image {i}: prop reference ({path.parent.name}).')
        return ('\nContinuity: Reference video 1 is the immediately preceding shot. Preserve current character positions, hands, props and action state while creating the requested new camera angle. Do not copy its camera angle, repeat earlier dialogue, or reproduce people not specified in this shot. '
                'Use the fixed location image as the architectural anchor; preserve its landmarks with the changed perspective.\n' + '\n'.join(labels))
    return ''

def validated_video_duration(model: str, duration: Any, segment_id: str):
    """Check Seedance 2.0 duration before any billable media request."""
    if model != "seedance_2_0":
        return duration
    try:
        value = float(duration)
        if not value.is_integer() or not 4 <= value <= 15:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{segment_id}: Seedance 2.0 영상 길이는 4~15초의 정수여야 합니다. 입력: {duration}")
    return int(value)


CINEMATIC_CAMERA_LIBRARY = {
    "push_in": "Smooth, steady cinematic push-in (dolly in) along the room axis, progressively closing in on the confrontation with natural optical depth",
    "pull_back": "Smooth purposeful pull-back (dolly out) expanding from the subject to reveal surrounding reactions and spatial isolation",
    "whip_pan": "Sharp motivated whip pan snapping focus between opposing characters to connect accusation and immediate defensive reaction",
    "pan": "Smooth horizontal tracking pan traveling across the confrontation space to follow the shifting emotional balance",
    "tilt_up": "Purposeful vertical tilt up from held physical evidence or tense hands to character's resolute eyes and facial reaction",
    "tilt_down": "Dramatic vertical tilt down from authoritative gaze to the revealed document, receipt, or physical consequence",
    "ots_push": "Over-the-shoulder framing slowly pushing in past foreground shoulder, establishing intense spatial tension and dominance",
    "handheld": "Subtle grounded handheld camera energy with organic micro-movements conveying suppressed rage, vulnerability, or raw confrontation stakes",
    "handheld_tension": "Subtle grounded tension handheld camera energy with organic micro-movements conveying suppressed rage and high stakes",
    "static": "Rock-solid locked-off camera stillness capturing intense frozen shock, dignified composure, or heavy silence",
    "arc_orbit": "Slow cinematic arc orbiting along the confrontation axis to dramatize shifting power dynamics between characters",
    "macro_insert": "Extreme tight macro optical focus pushing directly onto the physical document, receipt numbers, or held prop with crisp tactile detail",
    "low_angle_hero": "Authoritative low-angle perspective looking slightly upward, conveying absolute executive power, composure, and courtroom-like gravity",
    "high_angle_oppression": "Vulnerable high-angle perspective looking downward, emphasizing cornered guilt, pressure, and psychological isolation",
    "follow_track": "Motivated tracking camera matching character's physical gestures, step-forward, and decisive movement in the space",
    "reaction_micro_push": "Subtle optical micro-push directly into character's unwavering eyes and micro-expressions at the key moment of revelation"
}

# High-End Seedance 2.5 Emotion Acting Performance Library (25 Archetypes)
CINEMATIC_EMOTION_LIBRARY = {
    "rage": "The eyebrows slam down and inward, the upper lip peels back from the teeth, the nostrils flare, the neck tendons tighten, and the head pushes forward. The snarl holds as hard breaths move through the nose.",
    "shock": "The eyes snap wide with white visible above the iris, the eyebrows shoot up, the jaw drops fully open, and the head jerks back. The face freezes in that expression before a single blink.",
    "terror": "The eyebrows pull up and together, the eyes lock wide open without blinking, the mouth stretches open, the chin tucks back, and the chest rises and falls with fast, shallow breaths.",
    "disgust": "The nose wrinkles hard and pulls the upper lip upward, the eyes squint nearly shut, the chin draws in, and the head recoils and turns away. The revolted expression holds.",
    "crying": "The eyes and nose flush red, tears spill over the lower lid, the breath catches in visible stutters, the mouth pulls into a square shape, and the chin crumples and trembles. The shoulders begin to shake.",
    "pain": "The eyes clamp shut, the teeth bare in a hard grimace, the head snaps to one side, and one shoulder rises toward the ear. The face stays contracted before easing only slightly.",
    "eye_roll": "The eyes roll in a full, slow arc while the head tilts with the movement. Air pushes out through the nose, the eyes return with lowered lids, and a flat stare holds before the gaze turns away.",
    "suspicion": "The chin drops while the eyes stay lifted. One eyebrow rises higher, the head turns slightly so the gaze lands sideways, and the mouth tightens at one corner. The stare holds without blinking.",
    "flirtation": "The chin lowers, the gaze returns with a slow, deliberate blink, and a warm, playful half-smile grows at one corner of the mouth. The expression holds without rushing or breaking eye contact.",
    "smug": "The eyelids lower, one corner of the mouth pulls into a slow smirk, the eyebrows rise once and settle, and the chin lifts slightly. The smirk holds through unbroken eye contact.",
    "boredom": "The eyelids grow heavy, the gaze drifts and loses focus, a slow blink lasts too long, the jaw slackens, and a long sigh lowers the chest. The head sinks and the body settles into stillness.",
    "confusion": "The eyebrows become asymmetric, one raised and one lowered. The eyes search rapidly, the head tilts sharply, and the mouth hangs slightly open. The puzzled expression deepens instead of resolving.",
    "realization": "A blank thinking expression breaks as the eyes widen, the eyebrows jump, the lips part on a silent breath, and focus snaps back. A slow nod follows while the knowing, slightly stunned look holds.",
    "awe": "The eyes widen gradually without tension in the brow, the mouth opens little by little, the head tilts upward, and the body leans forward. The open, reverent expression never drops.",
    "determination": "The eyes lift and lock forward, a deep breath expands the chest, the jaw sets visibly at the hinge, the eyes narrow, and the shoulders roll back. One sharp nod completes the change.",
    "frustration": "The eyes clamp shut, the jaw slides from side to side, a sharp breath pushes through the nose, and the head shakes once. The head tips back as one long defeated breath leaves the tension in the face.",
    "anxiety": "The eyebrows stay knitted, the eyes flick rapidly from side to side, the lower lip pulls between the teeth, and breathing remains shallow and quick. The hands grip together as the weight keeps shifting.",
    "sadness": "The inner corners of the eyebrows pull up and together, the mouth corners drag down, the chin trembles once, and the gaze sinks as the head lowers. A slow blink and hard swallow fail to change the expression.",
    "guilt": "The mouth opens but no words come. The eyes slide down and away, the head lowers, a hard swallow moves through the throat, and one hand reaches to the back of the neck. The gaze stays on the floor.",
    "embarrassment": "Color rises in the cheeks, the eyes dart down and to the side, and an awkward pressed-lip half-smile appears. The head ducks and turns away while one hand rises near the mouth. The eyes stay lowered.",
    "exhaustion": "The eyelids drag downward, a long blink stays closed too long, the head drifts down and lifts again slowly, the jaw hangs slack, and one long breath empties out. The eyes reopen only halfway.",
    "relief": "A large visible exhale empties the chest, the eyes close, the raised eyebrows drop to neutral, and the shoulders collapse downward. One hand rises to the forehead. A small, shaky smile appears only after the breath finishes.",
    "pride": "The chin lifts, the chest expands, and a closed-lip smile spreads slowly and evenly. The shoulders roll back, followed by one slow, satisfied blink. The smile remains as the arms fold.",
    "fake_smile": "The mouth stretches into a smile while the eyes stay flat, with no crinkling at the corners. Blinking speeds up, the throat makes a hard swallow, and the gaze drops before the strained smile snaps back into place.",
    "laughter": "The eyes squeeze into crinkled slits, the mouth opens wide showing teeth, the head drops forward and tips back, and the shoulders bounce with each breath. The laugh settles into a wide lingering grin."
}

EMOTION_KEYWORD_MAP = {
    "분노": "rage", "격분": "rage", "화남": "rage", "rage": "rage", "angry": "rage",
    "충격": "shock", "경악": "shock", "shock": "shock", "disbelief": "shock",
    "공포": "terror", "겁": "terror", "terror": "terror", "panic": "terror",
    "혐오": "disgust", "역겨움": "disgust", "disgust": "disgust", "recoil": "disgust",
    "오열": "crying", "눈물": "crying", "울음": "crying", "crying": "crying", "weep": "crying",
    "고통": "pain", "비명": "pain", "pain": "pain", "wince": "pain",
    "한심": "eye_roll", "눈돌림": "eye_roll", "eye roll": "eye_roll",
    "의심": "suspicion", "수상": "suspicion", "suspicion": "suspicion", "doubt": "suspicion",
    "유혹": "flirtation", "플러팅": "flirtation", "flirtation": "flirtation",
    "비웃음": "smug", "조소": "smug", "거만": "smug", "smug": "smug", "gloating": "smug",
    "지루": "boredom", "하품": "boredom", "boredom": "boredom",
    "혼란": "confusion", "당황": "confusion", "confusion": "confusion", "puzzled": "confusion",
    "깨달음": "realization", "각성": "realization", "realization": "realization",
    "경탄": "awe", "감탄": "awe", "awe": "awe", "wonder": "awe",
    "결의": "determination", "단호": "determination", "비장": "determination", "사이다": "determination", "determination": "determination", "resolve": "determination",
    "좌절": "frustration", "답답": "frustration", "frustration": "frustration",
    "불안": "anxiety", "초조": "anxiety", "anxiety": "anxiety", "nervous": "anxiety",
    "슬픔": "sadness", "비통": "sadness", "sadness": "sadness", "sorrow": "sadness",
    "죄책감": "guilt", "미안": "guilt", "후회": "guilt", "guilt": "guilt",
    "수치": "embarrassment", "부끄러움": "embarrassment", "창피": "embarrassment", "embarrassment": "embarrassment",
    "탈진": "exhaustion", "피로": "exhaustion", "지침": "exhaustion", "exhaustion": "exhaustion",
    "안도": "relief", "안심": "relief", "relief": "relief",
    "자부심": "pride", "뿌듯": "pride", "pride": "pride",
    "억지미소": "fake_smile", "가짜미소": "fake_smile", "눈치": "fake_smile", "비굴": "fake_smile", "fake smile": "fake_smile",
    "웃음": "laughter", "폭소": "laughter", "laughter": "laughter", "joy": "laughter"
}

def resolve_cinematic_emotion(text: str) -> Optional[str]:
    """Detect emotion keywords in description and return acting-only physical direction."""
    if not text:
        return None
    lower_text = text.lower()
    for kw, emotion_key in EMOTION_KEYWORD_MAP.items():
        if kw in lower_text or kw in text:
            return CINEMATIC_EMOTION_LIBRARY.get(emotion_key)
    return None

def resolve_cinematic_lens_optics(size: str = "", shot_type: str = "") -> str:
    """Map shot size to cinematic cinema prime lens and aperture."""
    s = (size or "").lower()
    if any(k in s for k in ("macro", "insert", "extreme close-up")):
        return "100mm T2.8 macro cinema lens, extreme razor-sharp detail on evidence"
    elif any(k in s for k in ("close-up", "tight", "face")):
        return "85mm prime lens at F1.4, shallow cinematic depth of field with rich organic bokeh"
    elif any(k in s for k in ("medium", "bust", "waist")):
        return "50mm prime lens at F2.0, natural eye-level perspective with crisp subject separation"
    else:
        return "35mm anamorphic wide prime at F2.8, cinematic spatial depth capturing character and environment"

def sanitize_camera_text(text: str) -> str:
    """Remove contradictory static/locked-off words to allow active camera motion."""
    import re
    cleaned = re.sub(r'\b(static|locked[\s_-]*off|stationary)\b', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip(' ;,.-')
    return cleaned

CAMERA_NATURAL_ALIASES = [
    ("ots_push", ("over-the-shoulder", "over the shoulder", "ots")),
    ("whip_pan", ("whip pan", "whip-pan", "snap pan")),
    ("pull_back", ("pull back", "pull-back", "dolly out", "widening", "pulling back")),
    ("arc_orbit", ("arc", "orbit", "circling")),
    ("macro_insert", ("macro", "insert", "extreme close-up", "ecu")),
    ("tilt_up", ("tilt up", "tilting up", "tilt-up")),
    ("tilt_down", ("tilt down", "tilting down", "tilt-down")),
    ("low_angle_hero", ("low angle", "low-angle", "looking up")),
    ("high_angle_oppression", ("high angle", "high-angle", "looking down")),
    ("handheld", ("handheld", "hand-held")),
    ("static", ("static", "locked-off", "still camera", "rock-solid")),
    ("follow_track", ("tracking", "follow", "steadicam")),
    ("pan", ("pan across", "panning", "horizontal pan")),
    ("push_in", ("push in", "push-in", "dolly in", "pushing in")),
]

def resolve_cinematic_camera_preset(shot: Dict[str, Any], seg_id: str = "") -> str:
    """Enrich camera movement with cinematic library techniques and lens optics respecting scene context."""
    raw_camera = str(shot.get("camera") or "").strip()
    movement = str(shot.get("movement") or "").strip().lower()
    size = str(shot.get("size") or "").strip()
    angle = str(shot.get("angle") or "").strip()
    
    # 1. Match specific technique prioritizing specific/rich setups over generic push_in
    # Use word-boundary regex so substrings like "executive" never false-match "ecu" (macro_insert)
    import re
    def _matches_alias(text_lower: str, alias: str) -> bool:
        return bool(re.search(r'\b' + re.escape(alias) + r'\b', text_lower))

    matched_technique = None
    if movement:
        for key, aliases in CAMERA_NATURAL_ALIASES:
            if key == movement or _matches_alias(movement, key.replace('_', ' ')) or any(_matches_alias(movement, a) for a in aliases):
                matched_technique = CINEMATIC_CAMERA_LIBRARY.get(key)
                break
        if not matched_technique:
            for key, desc in CINEMATIC_CAMERA_LIBRARY.items():
                if key == movement or _matches_alias(movement, key.replace('_', ' ')):
                    matched_technique = desc
                    break
    if not matched_technique and raw_camera:
        cam_lower = raw_camera.lower()
        for key, aliases in CAMERA_NATURAL_ALIASES:
            if any(_matches_alias(cam_lower, a) for a in aliases):
                matched_technique = CINEMATIC_CAMERA_LIBRARY.get(key)
                break

    # 2. DO NOT force push_in if raw_camera already provides detailed framing/staging (>15 chars)
    # Only fallback to push_in if neither camera description nor movement exists at all
    if not matched_technique and not movement and len(raw_camera) < 15:
        matched_technique = CINEMATIC_CAMERA_LIBRARY["push_in"]

    optics = resolve_cinematic_lens_optics(size, raw_camera)
    
    parts = []
    if raw_camera:
        parts.append(raw_camera)
    if matched_technique and matched_technique.lower() not in raw_camera.lower():
        parts.append(f"active camera movement: {matched_technique}")
    elif movement and movement not in raw_camera.lower():
        parts.append(f"active camera movement: {movement}")
    parts.append(f"optics: {optics}")
    
    return "; ".join(parts)

def build_primary_camera_directive(shots: List[Dict[str, Any]], has_start_image: bool = True) -> str:
    """Build prominent top-level camera directive ensuring start-frame anchor or fresh angle staging."""
    if not shots:
        return "Dynamic eye-level framing; 50mm prime lens at F2.0."
    first = shots[0]
    size = first.get("size", "medium shot")
    angle = first.get("angle", "eye level")
    cam = resolve_cinematic_camera_preset(first)
    anchor_rule = (
        "Anchor the first frame strictly to the start image composition"
        if has_start_image
        else "Establish a FRESH, DISTINCT camera angle matching the specified spatial placement—do NOT copy the previous clip's camera framing"
    )
    
    if len(shots) == 1:
        return f"{size}, {angle}, {cam}. MANDATORY DIRECTION: {anchor_rule}, then execute smooth, purposeful cinematic camera movement matching the emotional subtext without sudden jarring jumps or frame tearing."
    
    # Multi-shot sequence: highlight spatial blocking across all cuts
    seq_parts = []
    for idx, s in enumerate(shots, 1):
        s_time = s.get("time", "")
        s_sz = s.get("size", "")
        s_cam = s.get("camera") or s.get("movement") or "cinematic framing"
        seq_parts.append(f"Shot {idx} ({s_time}: {s_sz}, {s_cam})")
    seq_summary = " -> ".join(seq_parts)
    
    return f"Multi-cut sequence ({len(shots)} shots: {seq_summary}). Opening: {size}, {angle}, {cam}. MANDATORY DIRECTION: {anchor_rule}, execute purposeful cinematic camera cuts matching the shot framing and 180-degree axis, without sudden jarring frame tearing or erratic shaking."

def build_speech_window_directive(segment: Dict[str, Any], character_names: Optional[Dict[str, str]] = None) -> str:
    """Build mathematical speech windowing and audio constraints per character."""
    import re
    shots = segment.get("shots", [])
    dialogues = segment.get("dialogue", [])
    all_chars = list(dict.fromkeys(
        (segment.get("characters") or []) +
        [c for s in shots for c in (s.get("characters") or [])]
    ))
    
    # Track speech windows per character: char_id -> list of (start_sec, end_sec)
    speech_windows = {cid: [] for cid in all_chars}
    
    time_pat = re.compile(r'([0-9.]+)\s*s?\s*(?:to|-)\s*([0-9.]+)\s*s', re.I)
    for s in shots:
        aud = s.get("audio", "")
        for cid in all_chars:
            cname = character_names.get(cid, cid) if character_names else cid
            if cid.lower() in aud.lower() or cname.lower() in aud.lower():
                matches = time_pat.findall(aud)
                for start_s, end_s in matches:
                    try:
                        speech_windows[cid].append((float(start_s), float(end_s)))
                    except ValueError:
                        pass
                        
    # If no explicit window found in audio description but character has dialogue, infer from shot
    if not any(speech_windows.values()) and dialogues:
        for d in dialogues:
            spk = d.get("speaker")
            if spk in speech_windows and not speech_windows[spk]:
                for s in shots:
                    st = s.get("time", "")
                    m = time_pat.search(st)
                    if m:
                        try:
                            start_t = float(m.group(1)) + 0.5
                            end_t = max(start_t, float(m.group(2)) - 0.5)
                            speech_windows[spk].append((start_t, end_t))
                        except ValueError:
                            pass

    directives = []
    for cid in all_chars:
        name = character_names.get(cid, cid) if character_names else cid
        windows = speech_windows.get(cid, [])
        if windows:
            count = len(windows)
            min_start = min(w[0] for w in windows)
            max_end = max(w[1] for w in windows)
            directives.append(f"{name} ({cid}) speaks aloud {count} time{'s' if count > 1 else ''} between {min_start:.1f}s and {max_end:.1f}s and never again; silent before {min_start:.1f}s and from {max_end:.1f}s to the end.")
        else:
            directives.append(f"{name} ({cid}) never speaks aloud in this clip; remains silent throughout.")
            
    return " ".join(directives) if directives else "Natural diegetic room sound only; strictly no background music or score."

def assemble_video_prompt(segment: Dict[str, Any], references: Optional[List[str]] = None, continuity_context: str = "", character_names: Optional[Dict[str, str]] = None, has_start_image: bool = True) -> str:
    """
    Assembles a video generation prompt according to templates/video-prompts.md
    incorporating Higgsfield MCSLA + Camera-Emotion Sync + I2V motion rules.
    """
    seg_id = segment.get("id", "")
    shots = segment.get("shots", [])
    shot_descriptions = []
    for idx, s in enumerate(shots, 1):
        s_time = s.get("time", "")
        s_size = s.get("size", "")
        s_angle = s.get("angle", "")
        s_cam = s.get("camera", "")
        s_vis = s.get("visual", "")
        s_aud = s.get("audio", "")
        cam_preset = resolve_cinematic_camera_preset(s, seg_id)
        line = f"Shot {idx} ({s_time}): [{s_size}, {s_angle}, {cam_preset}] {s_vis}"
        if isinstance(s.get('characters'), list):
            shown = ', '.join(f"{character_names[cid]} ({cid})" if character_names and cid in character_names else cid for cid in s['characters']) or 'no people'
            line += f" (Visible in shot: {shown})"
        if s_aud:
            line += f" (Audio: {s_aud})"
        shot_descriptions.append(line)

    dialogue_lines = []
    for d in segment.get("dialogue", []):
        speaker = d.get('speaker', '')
        name = f"{character_names[speaker]} ({speaker})" if character_names and speaker in character_names else speaker
        position = ' [off-screen voice; show the listener, not the speaker]' if d.get('offscreen') is True else ''
        dialogue_lines.append(f"{name}{position}: \"{d.get('line', '')}\"")

    first_shot = shots[0] if shots else {}
    visible_people = first_shot.get('characters') if isinstance(first_shot.get('characters'), list) else segment.get('characters', [])
    camera_directive = build_primary_camera_directive(shots, has_start_image=has_start_image)
    speech_window_directive = build_speech_window_directive(segment, character_names)

    # Detect high-end emotion performance acting direction
    combined_acting_texts = [
        segment.get("first_state", ""),
        segment.get("exit_state", ""),
        " ".join(segment.get("must_hit", []))
    ] + [s.get("visual", "") for s in shots]
    matched_emotion = None
    for txt in combined_acting_texts:
        res = resolve_cinematic_emotion(txt)
        if res:
            matched_emotion = res
            break

    ctx = {
        "camera_directive": camera_directive,
        "emotion_directive": matched_emotion or "",
        "speech_window_directive": speech_window_directive,
        "references": "\n".join(references) if references else "",
        "people": "On-screen characters (first frame): " + ", ".join(f"{character_names[cid]} ({cid})" if character_names and cid in character_names else cid for cid in visible_people),
        "subjects": f"Location: {segment.get('location', '')}, Scene: {segment.get('scene_heading', '')}",
        "continuity_context": continuity_context,
        "blocking": f"Key action: {', '.join(segment.get('must_hit', []))}",
        "first_state": segment.get("first_state", ""),
        "shot_list_header": "Shot sequence:",
        "shot_list": "\n".join(shot_descriptions),
        "dialogue": "Dialogue:\n" + "\n".join(dialogue_lines) if dialogue_lines else "",
        "exit_state": segment.get("exit_state", ""),
        "negative": "Negative prompt: low quality, blurry, deformed anatomy, distorted faces, watermark"
    }

    return render_template("video-prompts.md", ctx)

def estimate_credits(segments_count: int, model: str = "seedance_2_5") -> Optional[float]:
    """
    Estimated Higgsfield credit cost calculation.
    seedance_2_5 typically costs ~10-15 credits per 5s video take.
    """
    if model == 'seedance_2_0':
        return None  # Actual pricing depends on settings and billing; do not invent a fixed credit rate.
    rate_map = {
        "seedance_2_5": 10.0,
        "kling4_0": 12.0,
        "wan2_6": 8.0,
        "wan3_0": 10.0
    }
    cost_per_take = rate_map.get(model, 10.0)
    return segments_count * cost_per_take

def prepare_video_tasks(project_dir: Union[str, Path], episode_id: str) -> List[Dict[str, Any]]:
    """
    Prepares Higgsfield video generation payloads for all segments in an episode.
    """
    config = load_config()
    model = config.get("video", {}).get("model", "seedance_2_5")
    aspect_ratio = config.get("video", {}).get("aspect_ratio", "9:16")
    resolution = config.get("video", {}).get("resolution", "720p")

    proj = Path(project_dir)
    ep_dir = proj / "episodes" / episode_id
    breakdown_file = ep_dir / "breakdown.json"
    if not breakdown_file.exists():
        raise FileNotFoundError(f"Breakdown file not found: {breakdown_file}")

    breakdown = read_json(breakdown_file)
    segments = breakdown.get("segments", [])
    takes_dir = ensure_dir(ep_dir / "takes")

    tasks = []
    bible_path = proj / 'bible.json'
    bible = read_json(bible_path) if bible_path.exists() else {}
    names = {c['id']: c.get('name') or c['id'] for c in bible.get('characters', []) if c.get('id')}
    for seg_index, seg in enumerate(segments):
        seg_id = seg.get("id")
        kf_res = resolve_segment_keyframe(project_dir, episode_id, seg_id)
        start_image = str(kf_res["path"].resolve()) if kf_res["exists"] and kf_res["path"] else None

        v_prompt = assemble_video_prompt(seg, continuity_context=planned_state_context(segments[seg_index-1] if seg_index else None, seg, names), character_names=names, has_start_image=bool(start_image))
        payload = {
            "segment_id": seg_id,
            "characters": seg.get("characters", []),
            "location": seg.get("location"),
            "shots": seg.get("shots", []),
            "props": seg.get("props", []),
            "model": model,
            "prompt": v_prompt,
            "duration": seg.get("duration", config.get("video", {}).get("default_duration", 5)),
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "start_image": start_image,
            "generate_audio": config.get("video", {}).get("generate_audio", True),
            "keyframe_status": kf_res,
            "target_take_path": str(takes_dir / f"{seg_id}_take01.mp4")
        }
        tasks.append(payload)

    write_json(takes_dir / "video_tasks_plan.json", tasks)
    return tasks

