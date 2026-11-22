import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from pipeline.common import ensure_dir, load_config, read_json, write_json

def prepare_dialogue_audio_tasks(project_dir: Union[str, Path], episode_id: str) -> List[Dict[str, Any]]:
    """
    Extracts all dialogue lines from episode breakdown and matches with character voice profiles
    to generate consistent TTS audio tasks (Qwen Audio TTS).
    """
    config = load_config()
    audio_cfg = config.get("audio", {})
    voice_profiles = config.get("voice_profiles", {})

    proj = Path(project_dir)
    ep_dir = proj / "episodes" / episode_id
    breakdown_file = ep_dir / "breakdown.json"
    if not breakdown_file.exists():
        raise FileNotFoundError(f"Breakdown not found: {breakdown_file}")

    breakdown = read_json(breakdown_file)
    audio_dir = ensure_dir(ep_dir / "audio")

    tasks = []
    line_counter = 1

    for seg in breakdown.get("segments", []):
        seg_id = seg.get("id")
        for idx, d in enumerate(seg.get("dialogue", []), 1):
            speaker_raw = d.get("speaker", "unknown")
            speaker = speaker_raw.lower().replace(" ", "_")
            line_text = d.get("line", "")

            # Match voice profile or fallback
            profile = voice_profiles.get(speaker, {
                "voice_id": "default_korean_voice",
                "instruction": "자연스러운 한국어 대화체",
                "speech_rate": 1.0,
                "pitch_rate": 1.0
            })

            target_audio_file = audio_dir / f"{seg_id}_line{idx:02d}_{speaker}.mp3"

            payload = {
                "task_index": line_counter,
                "segment_id": seg_id,
                "speaker": speaker,
                "model": audio_cfg.get("model", "qwen_audio_tts"),
                "prompt": line_text,
                "language": audio_cfg.get("language", "ko"),
                "voice_id": profile.get("voice_id", "default_voice"),
                "voice_type": "preset",
                "instruction": profile.get("instruction", ""),
                "speech_rate": profile.get("speech_rate", 1.0),
                "pitch_rate": profile.get("pitch_rate", 1.0),
                "format": audio_cfg.get("format", "mp3"),
                "target_audio_path": str(target_audio_file)
            }
            tasks.append(payload)
            line_counter += 1

    write_json(audio_dir / "audio_tasks_plan.json", tasks)
    return tasks

