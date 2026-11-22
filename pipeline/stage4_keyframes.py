import os
import re
import json
import hashlib
import subprocess
import tempfile
import urllib.request
from PIL import Image
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from pipeline.common import ensure_dir, render_template, read_json, write_json, resolve_binary
from pipeline.stage2_assets import resolve_asset_image
from pipeline.scene_state import planned_state_context

def download_reference_image(url, destination):
    with urllib.request.urlopen(url, timeout=60) as source, open(destination, 'wb') as target:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            target.write(chunk)

def render_state_reference_keyframe(project_dir, episode_id, task, previous_id, image_model):
    """Render a fresh angle using the actual preceding take as one reference."""
    from pipeline.stage5_higgsfield import extract_previous_end_frame
    ep = Path(project_dir) / 'episodes' / episode_id
    seg_id = task['segment_id']
    previous = ep / 'takes' / f'{previous_id}_take01.mp4'
    if not previous.is_file() or previous.stat().st_size == 0:
        raise RuntimeError(f'{seg_id}: 이전 영상 {previous_id}이 없어 상태 참고 이미지를 만들 수 없습니다.')
    refs = [Path(p) for p in task.get('image_references', [])]
    missing_refs = [p for p in refs if not p.is_file()]
    if missing_refs:
        raise RuntimeError(f'{seg_id}: 필요한 인물·배경·소품 레퍼런스가 없어 이미지 생성을 중단합니다.')
    def digest(path):
        h = hashlib.sha256()
        with open(path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()
    previous_digest = digest(previous)
    fingerprint = hashlib.sha256(json.dumps({'previous': previous_digest, 'prompt':task['prompt'],
        'refs':[(str(p), digest(p)) for p in refs], 'model':image_model}, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    out_dir = ensure_dir(ep / 'keyframes' / 'state_reference')
    target = out_dir / f'{seg_id}_from_{previous_id}.png'
    manifest = out_dir / f'{seg_id}_from_{previous_id}.json'
    if target.is_file() and target.stat().st_size and manifest.is_file():
        try:
            if read_json(manifest).get('fingerprint') == fingerprint:
                with Image.open(target) as image: image.verify()
                return target
        except (OSError, ValueError, KeyError):
            pass
    previous_frame = extract_previous_end_frame(project_dir, episode_id, seg_id, previous_id)
    prior_number = len(refs) + 1
    prompt = (task['prompt'] + f'\nImage {prior_number} is the ACTUAL final frame of the preceding shot. '
              'Use it as a continuity reference for the shared characters’ hands, held objects, clothing, '
              'and fixed background layout. Keep permanent architectural landmarks spatially consistent '
              '(walls, doors, windows and furniture), while allowing realistic perspective changes and '
              'character movement appropriate to the new camera angle. Render the requested new camera angle, not a duplicate frame. '
              'Keep only the people specified for the current shot; do not add people from the reference. '
              'Honor explicitly described state changes in the new scene.')
    cmd = [resolve_binary('higgsfield'), 'generate', 'create', image_model, '--prompt', prompt,
           '--aspect-ratio', '9:16', '--wait', '--wait-timeout', '10m']
    for ref in refs + [previous_frame]: cmd.extend(['--image', str(ref)])
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'{seg_id} 상태 참고 이미지 생성 실패: {result.stdout.strip() or result.returncode}')
    urls = re.findall(r'https?://[^\s<>"\']+\.(?:png|jpg|jpeg|webp)(?:\?[^\s<>"\']*)?', result.stdout)
    if not urls:
        raise RuntimeError(f'{seg_id} 상태 참고 이미지 생성 실패: 결과 URL이 없습니다.')
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=out_dir, suffix='.download', delete=False) as temp:
            temp_path = Path(temp.name)
        download_reference_image(urls[-1], temp_path)
        with Image.open(temp_path) as image:
            image.load()
            if image.width < 1 or image.height < 1:
                raise ValueError('이미지 크기가 올바르지 않습니다.')
            image.convert('RGB').save(temp_path, format='PNG')
        if digest(previous) != previous_digest:
            raise RuntimeError(f'{seg_id}: 이미지 생성 중 이전 영상이 변경되어 결과를 사용하지 않습니다. 다시 시도해 주세요.')
        os.replace(temp_path, target)
        write_json(manifest, {'fingerprint':fingerprint, 'previous_segment_id':previous_id, 'image_model':image_model})
    finally:
        if temp_path and temp_path.exists(): temp_path.unlink()
    return target


def select_keyframe_props(
    seg: Dict[str, Any],
    first_shot: Dict[str, Any],
    prop_meta_map: Dict[str, Dict[str, Any]],
    max_props: int = 2,
) -> List[str]:
    """
    Pick only the hero props actually visible/active in this segment's opening frame
    (up to `max_props`) so unrelated props don't hallucinate into the keyframe.
    """
    raw_props = list(dict.fromkeys(seg.get("props") or []))
    if not raw_props:
        return []

    opening_text = f"{seg.get('first_state', '')} {first_shot.get('visual', '')}".lower()
    scored: List[tuple[int, int, str]] = []
    for idx, prop_id in enumerate(raw_props):
        tokens = [t for t in re.split(r"[^a-z0-9가-힣]+", prop_id.lower()) if len(t) >= 3]
        meta = prop_meta_map.get(prop_id) or {}
        name_tokens = [t for t in re.split(r"[^a-z0-9가-힣]+", str(meta.get("name", "")).lower()) if len(t) >= 2]
        hits = 0
        for t in set(tokens + name_tokens):
            if re.fullmatch(r"[a-z0-9]+", t):
                if re.search(rf"\b{re.escape(t)}s?\b", opening_text):
                    hits += 1
            elif t in opening_text:
                hits += 1
        scored.append((hits, -idx, prop_id))

    matched = [pid for hits, _, pid in sorted(scored, reverse=True) if hits > 0]
    if matched:
        return matched[:max_props]
    # If breakdown had only 1 explicit prop and text used a synonym, keep that single prop
    if len(raw_props) == 1:
        return raw_props
    return []


def prepare_keyframe_tasks(project_dir: Union[str, Path], episode_id: str) -> List[Dict[str, Any]]:
    """
    Parses episode breakdown and generates keyframe (First Frame) prompt metadata for each segment.
    """
    proj = Path(project_dir)
    ep_dir = proj / "episodes" / episode_id
    breakdown_file = ep_dir / "breakdown.json"
    if not breakdown_file.exists():
        raise FileNotFoundError(f"Breakdown file not found: {breakdown_file}")

    breakdown = read_json(breakdown_file)
    segments = breakdown.get("segments", [])
    keyframes_dir = ensure_dir(ep_dir / "keyframes")

    # Load bible metadata once for wardrobe look text & prop matching
    bible_file = proj / "bible.json"
    char_look_map: Dict[str, str] = {}
    char_name_map: Dict[str, str] = {}
    prop_meta_map: Dict[str, Dict[str, Any]] = {}
    if bible_file.exists():
        bible_data = read_json(bible_file)
        for c in bible_data.get("characters", []):
            if c.get("id"):
                char_look_map[c["id"]] = str(c.get("look") or "").strip()
                char_name_map[c["id"]] = str(c.get("name") or c["id"])
        for p in bible_data.get("props", []):
            if p.get("id"):
                prop_meta_map[p["id"]] = p

    tasks = []
    for seg_index, seg in enumerate(segments):
        seg_id = seg.get("id")
        shots = seg.get("shots", [])
        if not shots:
            continue
        first_shot = shots[0]

        # Gather reference images for characters, location, and props for Nano Banana consistency
        ref_descriptions = []
        image_ref_paths = []
        img_counter = 1

        # 1. Characters actually visible in the opening shot (fallback to segment characters)
        shot_chars = list(dict.fromkeys(first_shot['characters'] if isinstance(first_shot.get('characters'), list) else (seg.get('characters') or [])))
        shot_size = first_shot.get("size", "medium shot").lower()
        is_close_shot = "close" in shot_size
        prefer_face_only = is_close_shot or len(shot_chars) >= 3

        for char_id in shot_chars:
            face_res = resolve_asset_image(project_dir, "characters", char_id, "face")
            sheet_res = resolve_asset_image(project_dir, "characters", char_id, "sheet")
            look_note = f", wearing {char_look_map[char_id]}" if char_look_map.get(char_id) else ""
            if prefer_face_only and face_res["exists"] and face_res["path"]:
                ref_descriptions.append(
                    f"Image {img_counter}: Master Character '{char_id}' High-Res Face Reference "
                    f"(lock exact facial bone structure, eyes, nose, and hair{look_note});"
                )
                image_ref_paths.append(str(face_res["path"]))
                img_counter += 1
            elif sheet_res["exists"] and sheet_res["path"]:
                ref_descriptions.append(f"Image {img_counter}: Master Character '{char_id}' (maintain exact facial identity, hair, and clothing{look_note});")
                image_ref_paths.append(str(sheet_res["path"]))
                img_counter += 1
                if face_res["exists"] and face_res["path"]:
                    ref_descriptions.append(f"Image {img_counter}: Master Character '{char_id}' Close-Up Face Lock (match exact facial identity from this close-up portrait);")
                    image_ref_paths.append(str(face_res["path"]))
                    img_counter += 1

        # 2. Scene location reference
        loc_id = seg.get("location")
        if loc_id:
            loc_resolved = resolve_asset_image(project_dir, "scenes", loc_id, "base")
            if loc_resolved["exists"] and loc_resolved["path"]:
                ref_descriptions.append(f"Image {img_counter}: Environment '{loc_id}' (replicate exact spatial layout, wall/ground textures, and atmospheric lighting);")
                image_ref_paths.append(str(loc_resolved["path"]))
                img_counter += 1

        # 3. Hero props (ONLY props relevant to the opening frame, max 2)
        selected_props = select_keyframe_props(seg, first_shot, prop_meta_map, max_props=2)
        for prop_id in selected_props:
            prop_resolved = resolve_asset_image(project_dir, "props", prop_id, "sheet")
            if prop_resolved["exists"] and prop_resolved["path"]:
                ref_descriptions.append(f"Image {img_counter}: Hero Prop '{prop_id}' (replicate exact object texture, weathering, and shape);")
                image_ref_paths.append(str(prop_resolved["path"]))
                img_counter += 1

        ctx = {
            "shot": 1,
            "shots": len(shots),
            "size": first_shot.get("size", "medium shot"),
            "angle": first_shot.get("angle", "eye level"),
            "camera": first_shot.get("camera", "static camera"),
            "before": f"At start: {seg.get('first_state', '')}",
            "visual": first_shot.get("visual", ""),
            "people": ", ".join(f"{char_name_map.get(cid, cid)} ({cid})" for cid in shot_chars),
            "continuity_context": planned_state_context(segments[seg_index - 1] if seg_index else None, seg, char_name_map),
            "has_references": len(ref_descriptions) > 0,
            "references": " ".join(ref_descriptions) if ref_descriptions else "",
            "feedback": ""
        }

        kf_prompt = render_template("keyframe.md", ctx)
        status = resolve_segment_keyframe(project_dir, episode_id, seg_id)

        task = {
            "segment_id": seg_id,
            "location_id": loc_id,
            "prompt": kf_prompt,
            "status": status,
            "image_references": image_ref_paths,
            "metadata": {
                "first_shot": first_shot,
                "first_state": seg.get("first_state", ""),
                "characters": seg.get("characters", []),
                "continuity_context": ctx["continuity_context"]
            }
        }
        tasks.append(task)

    # Save keyframe plan summary
    write_json(keyframes_dir / "keyframes_plan.json", tasks)
    return tasks

def resolve_segment_keyframe(project_dir: Union[str, Path], episode_id: str, segment_id: str) -> Dict[str, Any]:
    """
    Checks if a keyframe image exists for the given segment (manual drop-in or previously generated).
    """
    proj = Path(project_dir)
    target_dir = proj / "episodes" / episode_id / "keyframes"
    if not target_dir.exists():
        return {"exists": False, "source": "needs_generation", "path": None}

    extensions = [".png", ".jpg", ".jpeg", ".webp"]
    for ext in extensions:
        candidate = target_dir / f"{segment_id}{ext}"
        if candidate.exists() and candidate.stat().st_size > 0:
            return {
                "exists": True,
                "source": "user_override",
                "path": candidate,
                "filename": candidate.name
            }

    return {"exists": False, "source": "needs_generation", "path": None}

