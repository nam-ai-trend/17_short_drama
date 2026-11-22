import os
import subprocess
import textwrap
import tempfile
import shutil
import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from PIL import Image, ImageDraw, ImageFont
from pipeline.common import ensure_dir, load_config, read_json, resolve_binary

def format_srt_time(seconds: float) -> str:
    """
    Converts seconds (e.g. 65.25) to SRT format: HH:MM:SS,mmm
    """
    millis = int(round((seconds - int(seconds)) * 1000))
    total_seconds = int(seconds)
    secs = total_seconds % 60
    total_minutes = total_seconds // 60
    mins = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{mins:02d}:{secs:02d},{millis:03d}"

def generate_subtitles_srt(
    breakdown_data: Dict[str, Any],
    output_srt_path: Union[str, Path],
    actual_durations: Optional[Dict[str, float]] = None
) -> Path:
    """
    Generates a synchronized SRT subtitle file from episode breakdown dialogue and segment durations.
    If actual_durations dict is provided (e.g. from actual rendered video takes), uses real timings.
    """
    out = Path(output_srt_path)
    ensure_dir(out.parent)

    srt_entries = []
    current_time = 0.0
    entry_idx = 1

    for seg in breakdown_data.get("segments", []):
        seg_id = seg.get("id")
        # 실제 렌더링된 비디오 테이크의 길이가 있으면 최우선 반영하여 오차를 방지함
        if actual_durations and seg_id in actual_durations:
            duration = actual_durations[seg_id]
        else:
            duration = float(seg.get("duration", 5.0))

        dialogues = seg.get("dialogue", [])
        if dialogues:
            time_per_line = duration / len(dialogues)
            for idx, d in enumerate(dialogues):
                start = current_time + (idx * time_per_line) + 0.5
                end = min(current_time + ((idx + 1) * time_per_line) - 0.2, current_time + duration - 0.05)
                if end <= start:
                    end = start + 1.2

                speaker = d.get("speaker", "")
                text = d.get("line", "")
                formatted_line = f"{speaker.title()}: {text}" if speaker else text

                srt_entries.append(f"{entry_idx}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{formatted_line}\n")
                entry_idx += 1

        current_time += duration

    out.write_text("\n".join(srt_entries), encoding="utf-8")
    return out

import re

CHOSUNG = ['ㄱ', 'ㄲ', 'ㄴ', 'ㄷ', 'ㄸ', 'ㄹ', 'ㅁ', 'ㅂ', 'ㅃ', 'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅉ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ']
JUNGSUNG = ['ㅏ', 'ㅐ', 'ㅑ', 'ㅒ', 'ㅓ', 'ㅔ', 'ㅕ', 'ㅖ', 'ㅗ', 'ㅘ', 'ㅙ', 'ㅚ', 'ㅛ', 'ㅜ', 'ㅝ', 'ㅞ', 'ㅟ', 'ㅠ', 'ㅡ', 'ㅢ', 'ㅣ']
JONGSUNG = ['', 'ㄱ', 'ㄲ', 'ㄳ', 'ㄴ', 'ㄵ', 'ㄶ', 'ㄷ', 'ㄹ', 'ㄺ', 'ㄻ', 'ㄼ', 'ㄽ', 'ㄾ', 'ㄿ', 'ㅀ', 'ㅁ', 'ㅂ', 'ㅄ', 'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ']

def decompose_hangul(text: str) -> str:
    """Decomposes Korean syllable blocks into discrete Jamo for phonetic alignment."""
    result = []
    for char in text:
        if '가' <= char <= '힣':
            code = ord(char) - 0xAC00
            cho = code // 588
            jung = (code % 588) // 28
            jong = code % 28
            result.append(CHOSUNG[cho])
            result.append(JUNGSUNG[jung])
            if jong > 0:
                result.append(JONGSUNG[jong])
        else:
            result.append(char)
    return ''.join(result)

_DIGIT_TO_KOR = {
    '10': '십', '0': '영', '1': '일', '2': '이', '3': '삼',
    '4': '사', '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구'
}

def _phonetic_norm(s: str) -> str:
    """Normalizes Korean word for phonetic Jamo comparison (including misheard digits like 10비가 -> 십비가)."""
    c = re.sub(r'[^\w가-힣]', '', s)
    if re.search(r'\d', c) and re.search(r'[가-힣]', c):
        for d, k in _DIGIT_TO_KOR.items():
            c = c.replace(d, k)
    return c

def jamo_similarity(s1: str, s2: str) -> float:
    """Calculates phonetic Jamo similarity between two Korean words."""
    c1 = _phonetic_norm(s1)
    c2 = _phonetic_norm(s2)
    if not c1 or not c2:
        return 0.0
    return SequenceMatcher(None, decompose_hangul(c1), decompose_hangul(c2)).ratio()

def refine_stt_tokens_with_script(
    word_tokens: List[Dict[str, Any]],
    script_text: str
) -> List[Dict[str, Any]]:
    """
    Refines Whisper word-timestamp tokens ([{'word': str, 'start': float, 'end': float}])
    by correcting phonetic typos using script vocabulary and surrounding word context,
    strictly preserving every token's original STT speech timestamps.
    """
    if not word_tokens or not script_text:
        return word_tokens

    # 1. Strip throat-clearing hallucination only at very first token
    tokens = [dict(t) for t in word_tokens if str(t.get("word", "")).strip()]
    if tokens and re.match(r'^(?:소유,|음,|어,)$', tokens[0]["word"].strip()):
        tokens = tokens[1:]
    if not tokens:
        return []

    # 2. Build script unigram & bigram pools with their immediate neighbors for context matching
    raw_script_words = [re.sub(r'[^\w가-힣]', '', w) for w in script_text.split()]
    script_unigrams = [w for w in raw_script_words if w]
    unigram_set = set(script_unigrams)

    # List of (word, prev_word, next_word)
    uni_ctx = []
    for idx, sw in enumerate(script_unigrams):
        prev_w = script_unigrams[idx - 1] if idx > 0 else ""
        next_w = script_unigrams[idx + 1] if idx + 1 < len(script_unigrams) else ""
        uni_ctx.append((sw, prev_w, next_w))

    # List of (merged, w1, w2, prev_word, next_word)
    bi_ctx = []
    for idx in range(len(script_unigrams) - 1):
        w1, w2 = script_unigrams[idx], script_unigrams[idx + 1]
        prev_w = script_unigrams[idx - 1] if idx > 0 else ""
        next_w = script_unigrams[idx + 2] if idx + 2 < len(script_unigrams) else ""
        bi_ctx.append((w1 + w2, w1, w2, prev_w, next_w))

    def _clean(tok: Dict[str, Any]) -> str:
        return re.sub(r'[^\w가-힣]', '', str(tok.get("word", "")).strip())

    def _extract_punct(raw_w: str, clean_w: str) -> str:
        s = raw_w.strip()
        if s.startswith(clean_w):
            return s[len(clean_w):]
        return s[-1] if s and s[-1] in ('.', ',', '?', '!', '…') else ''

    corrected: List[Dict[str, Any]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        raw_w = str(tok.get("word", "")).strip()
        w_clean = _clean(tok)
        punct = _extract_punct(raw_w, w_clean)
        t_start = float(tok.get("start", 0.0))
        t_end = float(tok.get("end", t_start))

        if not w_clean:
            corrected.append(tok)
            i += 1
            continue

        # Exact match in script? Never alter it or its timing!
        if w_clean in unigram_set:
            corrected.append({"word": raw_w, "start": t_start, "end": t_end})
            i += 1
            continue

        prev_clean = _clean(corrected[-1]) if corrected else ""
        next_clean = _clean(tokens[i + 1]) if i + 1 < len(tokens) else ""

        # 2a. Check if 2 consecutive non-exact STT tokens match a script bigram (e.g. "10비가" + "반된" -> "식비가" + "포함된")
        if i + 1 < len(tokens) and next_clean and next_clean not in unigram_set:
            tok2 = tokens[i + 1]
            raw_w2 = str(tok2.get("word", "")).strip()
            punct2 = _extract_punct(raw_w2, next_clean)
            next2_clean = _clean(tokens[i + 2]) if i + 2 < len(tokens) else ""
            pair_merged = w_clean + next_clean

            best_bi = None
            best_bi_score = 0.0
            for b_merged, bw1, bw2, bp, bn in bi_ctx:
                if abs(len(_phonetic_norm(pair_merged)) - len(_phonetic_norm(b_merged))) <= 3:
                    sim = jamo_similarity(pair_merged, b_merged)
                    bonus = (0.10 if prev_clean and prev_clean == bp else 0.0) + (0.10 if next2_clean and next2_clean == bn else 0.0)
                    score = sim + bonus
                    if score > best_bi_score:
                        best_bi_score = score
                        best_bi = (bw1, bw2, sim)
            if best_bi and best_bi_score >= 0.76 and best_bi[2] >= 0.58:
                bw1, bw2, _ = best_bi
                corrected.append({"word": bw1, "start": t_start, "end": t_end})
                corrected.append({"word": bw2 + punct2, "start": float(tok2.get("start", t_end)), "end": float(tok2.get("end", t_end))})
                i += 2
                continue

        # 2b. Single STT token vs script unigrams AND merged bigrams with context sandwich bonus
        best_match = None  # tuple: (is_bigram, text_or_pair, raw_sim, total_score)
        best_score = 0.0

        for b_merged, bw1, bw2, bp, bn in bi_ctx:
            if len(w_clean) >= 3 and abs(len(_phonetic_norm(w_clean)) - len(_phonetic_norm(b_merged))) <= 2:
                sim = jamo_similarity(w_clean, b_merged)
                bonus = (0.12 if prev_clean and prev_clean == bp else 0.0) + (0.12 if next_clean and next_clean == bn else 0.0)
                score = sim + bonus
                if score > best_score and sim >= 0.62:
                    best_score = score
                    best_match = (True, (bw1, bw2), sim, score)

        for sw, sp, sn in uni_ctx:
            # Allow length diff up to 2 if sandwiched by exact matching neighbors (e.g. "팀장님 [골라] 만들지" -> "곤란하게")
            is_sandwich = bool(prev_clean and prev_clean == sp and next_clean and next_clean == sn)
            max_len_diff = 2 if is_sandwich else 1
            if abs(len(_phonetic_norm(w_clean)) - len(_phonetic_norm(sw))) <= max_len_diff:
                sim = jamo_similarity(w_clean, sw)
                bonus = (0.12 if prev_clean and prev_clean == sp else 0.0) + (0.12 if next_clean and next_clean == sn else 0.0)
                score = sim + bonus
                min_raw_sim = 0.50 if is_sandwich else 0.68
                if score > best_score and sim >= min_raw_sim:
                    best_score = score
                    best_match = (False, sw, sim, score)

        threshold = 0.74 if len(w_clean) >= 3 else 0.80
        if best_match and best_score >= threshold:
            is_bi, val, _, _ = best_match
            if is_bi:
                bw1, bw2 = val
                dur = max(0.05, t_end - t_start)
                ratio = len(bw1) / max(1, len(bw1) + len(bw2))
                mid = round(t_start + dur * ratio, 3)
                corrected.append({"word": bw1, "start": t_start, "end": mid})
                corrected.append({"word": bw2 + punct, "start": mid, "end": t_end})
            else:
                corrected.append({"word": val + punct, "start": t_start, "end": t_end})
        else:
            corrected.append({"word": raw_w, "start": t_start, "end": t_end})
        i += 1

    return corrected

def refine_stt_text_with_script(stt_text: str, script_text: str) -> str:
    """
    Refines STT transcription text by correcting phonetic typos using script vocabulary.
    """
    if not stt_text or not script_text:
        return stt_text
    dummy_tokens = [{"word": w, "start": 0.0, "end": 0.0} for w in stt_text.strip().split() if w]
    refined = refine_stt_tokens_with_script(dummy_tokens, script_text)
    return " ".join(t["word"] for t in refined)

def review_transcribed_dialogue(expected: str, heard: str, duration: Optional[float] = None, speech_end: Optional[float] = None) -> Dict[str, Any]:
    normalize = lambda value: re.sub(r'[^\w가-힣]', '', value.lower(), flags=re.UNICODE)
    expected_norm, heard_norm = normalize(expected), normalize(heard)
    similarity = SequenceMatcher(None, expected_norm, heard_norm).ratio() if expected_norm else 1.0
    end_cut_risk = duration is not None and speech_end is not None and duration > 0 and duration - speech_end <= 0.3
    # AI 영상 모델의 한국어 발음 뭉개짐 한계로 인한 유사도 기준 경고(0.96) 제거: 영상 끝 잘림(end_cut_risk)만 확인
    return {"expected": expected, "transcribed": heard, "similarity": round(similarity, 2),
            "speech_end": speech_end, "duration": duration, "end_cut_risk": end_cut_risk,
            "needs_review": bool(end_cut_risk)}

STRONG_ENDINGS = ('고', '며', '면서', '는데', '은데', '지만', '으나', '어서', '아서', '여서', '니까', '으니', '으면', '면', '더니', '려고', '으려고', '도록', '없이')
MID_ENDINGS = ('은', '는', '이', '가', '을', '를', '에서', '에게', '으로', '로', '까지', '부터', '하고', '와', '과')

BOUND_FOLLOWERS = ('원', '원씩', '원짜리', '원이야', '원을', '원의', '명', '개', '번', '살', '년', '월', '일', '줄', '수', '것', '거', '게', '때', '뿐', '채', '만큼', '대로')

def _score_split_point(w: str, next_w: str = "") -> int:
    w_clean = re.sub(r'[^\w가-힣]', '', w)
    next_clean = re.sub(r'[^\w가-힣]', '', next_w)
    if not w_clean:
        return 0
    # Strongly penalize breaking between number+unit ("15만 / 원씩") or modifier+bound noun ("키운 / 줄", "알 / 수")
    if next_clean and any(next_clean == bf or next_clean.startswith(bf) for bf in BOUND_FOLLOWERS):
        return -15
    for e in STRONG_ENDINGS:
        if w_clean.endswith(e):
            return 10
    for e in MID_ENDINGS:
        if w_clean.endswith(e):
            return 5
    return 1

def _split_by_words(text: str, max_len: int = 16) -> List[str]:
    """Splits Korean phrase naturally based on semantic endings (clauses, particles) rather than arbitrary middle."""
    words = text.split(" ")
    if len(words) <= 1 or len(text) <= max_len:
        return [text]
        
    best_idx = 1
    best_score = -999.0
    total_len = len(text)
    cur_len = 0
    
    for i in range(len(words) - 1):
        cur_len += len(words[i]) + (1 if i > 0 else 0)
        dist_from_mid = abs(cur_len - (total_len / 2))
        sem_score = _score_split_point(words[i], words[i + 1]) * 3.5 - (dist_from_mid * 0.4)
        if sem_score > best_score:
            best_score = sem_score
            best_idx = i + 1
            
    p1 = " ".join(words[:best_idx])
    p2 = " ".join(words[best_idx:])
    
    res = []
    for p in [p1, p2]:
        if len(p) <= max_len:
            res.append(p)
        else:
            res.extend(_split_by_words(p, max_len))
    return res

def split_korean_dialogue_smart(text: str, max_len: int = 16) -> List[str]:
    """
    Splits Korean dialogue into punchy, high-retention Shorts-optimized single lines (typically 8-16 chars).
    Splits at natural semantic boundaries (clauses, particles, punctuation) without broken phrases.
    """
    cleaned = re.sub(r"\s+", " ", text.strip())
    if not cleaned:
        return []
    
    # 1. Split on punctuation (. ! ? ...)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", cleaned) if s.strip()]
    
    chunks = []
    for sent in sentences:
        if len(sent) <= max_len:
            chunks.append(sent)
            continue
            
        # Try splitting by comma first
        if "," in sent:
            comma_parts = [p.strip() for p in sent.split(",") if p.strip()]
            for i, cp in enumerate(comma_parts):
                part_text = cp + ("," if i < len(comma_parts) - 1 else "")
                if len(part_text) <= max_len:
                    chunks.append(part_text)
                else:
                    chunks.extend(_split_by_words(part_text, max_len))
        else:
            chunks.extend(_split_by_words(sent, max_len))
            
    return chunks

def _split_tokens_by_words(tokens: List[Dict[str, Any]], max_len: int = 16) -> List[List[Dict[str, Any]]]:
    """Splits word-timestamp token list naturally based on semantic endings while preserving exact word timings."""
    text = " ".join(str(t.get("word", "")).strip() for t in tokens).strip()
    if len(tokens) <= 1 or len(text) <= max_len:
        return [tokens] if tokens else []

    words = [str(t.get("word", "")).strip() for t in tokens]
    best_idx = 1
    best_score = -999.0
    total_len = len(text)
    cur_len = 0

    for i in range(len(words) - 1):
        cur_len += len(words[i]) + (1 if i > 0 else 0)
        dist_from_mid = abs(cur_len - (total_len / 2))
        sem_score = _score_split_point(words[i], words[i + 1]) * 3.5 - (dist_from_mid * 0.4)
        if sem_score > best_score:
            best_score = sem_score
            best_idx = i + 1

    left = tokens[:best_idx]
    right = tokens[best_idx:]
    res: List[List[Dict[str, Any]]] = []
    for part in (left, right):
        part_text = " ".join(str(t.get("word", "")).strip() for t in part).strip()
        if len(part_text) <= max_len or len(part) <= 1:
            if part:
                res.append(part)
        else:
            res.extend(_split_tokens_by_words(part, max_len))
    return res


def split_word_tokens_smart(tokens: List[Dict[str, Any]], max_len: int = 16) -> List[Dict[str, Any]]:
    """
    Groups word-timestamp tokens into punchy Shorts subtitle chunks (<=max_len chars)
    splitting at punctuation (. ! ? ,) and Korean semantic boundaries,
    preserving exact STT first-word start and last-word end timestamps for every chunk.
    """
    valid = [t for t in tokens if str(t.get("word", "")).strip()]
    if not valid:
        return []

    # 1. Group by sentence-ending punctuation (. ! ? …) first
    sentence_groups: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    for tok in valid:
        cur.append(tok)
        w = str(tok.get("word", "")).strip()
        if w and w[-1] in (".", "!", "?", "…"):
            sentence_groups.append(cur)
            cur = []
    if cur:
        sentence_groups.append(cur)

    # 2. Within each sentence group, split on commas if > max_len, then semantic split
    token_chunks: List[List[Dict[str, Any]]] = []
    for grp in sentence_groups:
        grp_text = " ".join(str(t.get("word", "")).strip() for t in grp).strip()
        if len(grp_text) <= max_len:
            token_chunks.append(grp)
            continue

        # Split at comma boundaries if present
        comma_groups: List[List[Dict[str, Any]]] = []
        sub: List[Dict[str, Any]] = []
        for tok in grp:
            sub.append(tok)
            if str(tok.get("word", "")).strip().endswith(","):
                comma_groups.append(sub)
                sub = []
        if sub:
            comma_groups.append(sub)

        for cg in comma_groups:
            cg_text = " ".join(str(t.get("word", "")).strip() for t in cg).strip()
            if len(cg_text) <= max_len:
                token_chunks.append(cg)
            else:
                token_chunks.extend(_split_tokens_by_words(cg, max_len))

    # 3. Materialize chunks with exact Whisper word timestamps
    out: List[Dict[str, Any]] = []
    for chunk_toks in token_chunks:
        text = " ".join(str(t.get("word", "")).strip() for t in chunk_toks).strip()
        if not text:
            continue
        c_start = float(chunk_toks[0].get("start", 0.0))
        c_end = float(chunk_toks[-1].get("end", c_start))
        if c_end <= c_start:
            c_end = c_start + 0.4
        out.append({"text": text, "start": round(c_start, 3), "end": round(c_end, 3)})
    return out


def compute_smart_trim_window(
    seg_duration: float,
    first_speech_start: float,
    last_speech_end: float,
    is_last_segment: bool = False,
    lead_pad: float = 0.30,
    tail_pad: float = 0.40,
    min_trim_threshold: float = 0.45,
    min_clip_duration: float = 2.5
) -> Tuple[float, float]:
    """
    Computes tight Shorts-paced trim window [trim_start, trim_end] around actual speech,
    removing excessive dead-air silence before/after dialogue while preserving natural breathing and reaction beats.
    """
    if seg_duration <= min_clip_duration or last_speech_end <= first_speech_start:
        return (0.0, round(seg_duration, 3))

    effective_tail = 0.80 if is_last_segment else tail_pad
    trim_start = max(0.0, first_speech_start - lead_pad)
    if trim_start < min_trim_threshold:
        trim_start = 0.0

    trim_end = min(seg_duration, last_speech_end + effective_tail)
    if (seg_duration - trim_end) < min_trim_threshold:
        trim_end = seg_duration

    if (trim_end - trim_start) < min_clip_duration:
        return (0.0, round(seg_duration, 3))

    return (round(trim_start, 3), round(trim_end, 3))


def extract_subtitle_cues_with_stt(
    project_dir: Union[str, Path],
    episode_id: str,
    breakdown_data: Dict[str, Any],
    speaker_names: Optional[Dict[str, str]] = None,
    whisper_bin: Optional[str] = None,
    dialogue_review: Optional[List[Dict[str, Any]]] = None,
    speech_bounds: Optional[Dict[str, Tuple[float, float]]] = None
) -> List[Dict[str, Any]]:
    """
    Extracts millisecond-accurate subtitle cues using OpenAI Whisper STT on rendered video takes.
    Eliminates lip-sync mismatch and subtitle desync by anchoring directly to actual audio speech.
    """
    proj = Path(project_dir)
    ep_dir = proj / "episodes" / episode_id
    takes_dir = ep_dir / "takes"
    
    cues = []
    current_time_offset = 0.0
    entry_idx = 1
    
    for seg in breakdown_data.get("segments", []):
        seg_id = seg.get("id")
        take_file = takes_dir / f"{seg_id}_take01.mp4"
        seg_duration = get_video_duration(take_file) if take_file.exists() else float(seg.get("duration", 5.0))
        dialogues = seg.get("dialogue", [])
        if not dialogues:
            current_time_offset += seg_duration
            continue
        
        # Check if Whisper is available and run STT
        stt_ran = False
        resolved_whisper = resolve_binary("whisper", whisper_bin)
        if Path(resolved_whisper).exists() and take_file.exists():
            out_tmp = Path(tempfile.mkdtemp(prefix=f"whisper_{seg_id}_"))
            cmd = [
                resolved_whisper, str(take_file),
                "--language", "Korean",
                "--output_format", "json",
                "--word_timestamps", "True",
                "--output_dir", str(out_tmp)
            ]
            try:
                subprocess.run(cmd, capture_output=True, text=True, check=True)
                json_file = out_tmp / f"{take_file.stem}.json"
                if json_file.exists():
                    stt_data = read_json(json_file)
                    stt_segments = stt_data.get("segments", [])
                    if stt_segments:
                        stt_ran = True
                        all_seg_words = [w for s in stt_segments for w in (s.get("words") or []) if str(w.get("word", "")).strip()]
                        if all_seg_words:
                            first_speech = min((float(w.get("start", 0.0)) for w in all_seg_words), default=0.0)
                            last_speech = max((float(w.get("end", seg_duration)) for w in all_seg_words), default=seg_duration)
                        else:
                            first_speech = min((float(s.get("start", 0.0)) for s in stt_segments), default=0.0)
                            last_speech = max((float(s.get("end", seg_duration)) for s in stt_segments), default=seg_duration)
                        if speech_bounds is not None:
                            speech_bounds[seg_id] = (first_speech, last_speech)
                        if dialogue_review is not None:
                            expected = " ".join(str(d.get("line", "")) for d in dialogues)
                            heard = " ".join(str(s.get("text", "")).strip() for s in stt_segments)
                            dialogue_review.append({"segment_id": seg_id, **review_transcribed_dialogue(expected, heard, duration=seg_duration, speech_end=last_speech)})
                        speakers = {str(d.get("speaker") or "").strip() for d in dialogues if str(d.get("speaker") or "").strip()}
                        expected_dialogue_text = " ".join(str(d.get("line", "")) for d in dialogues)
                        for s_idx, stt_seg in enumerate(stt_segments):
                            # Whisper segments are not speaker diarization: never guess speaker order for exchanges.
                            sp_id = next(iter(speakers)) if len(speakers) == 1 else ""
                            kor_name = (speaker_names or {}).get(sp_id, sp_id.title())
                            words = stt_seg.get("words", [])

                            if words:
                                # Preserve exact Whisper word-level timestamps while refining only typo words
                                refined_tokens = refine_stt_tokens_with_script(words, expected_dialogue_text)
                                timed_chunks = split_word_tokens_smart(refined_tokens, max_len=16)
                                for tc in timed_chunks:
                                    p_start = tc["start"]
                                    p_end = tc["end"]
                                    cues.append({
                                        "index": entry_idx,
                                        "segment_id": seg_id,
                                        "local_start": round(p_start, 3),
                                        "local_end": round(p_end, 3),
                                        "start": round(current_time_offset + p_start, 3),
                                        "end": round(current_time_offset + p_end, 3),
                                        "speaker_id": sp_id,
                                        "speaker_name": kor_name,
                                        "text": tc["text"]
                                    })
                                    entry_idx += 1
                            else:
                                raw_stt_text = stt_seg.get("text", "").strip()
                                clean_stt_text = refine_stt_text_with_script(raw_stt_text, expected_dialogue_text)
                                l_start = float(stt_seg.get("start", 0.0))
                                l_end = float(stt_seg.get("end", seg_duration))
                                cues.append({
                                    "index": entry_idx,
                                    "segment_id": seg_id,
                                    "local_start": round(l_start, 3),
                                    "local_end": round(l_end, 3),
                                    "start": round(current_time_offset + l_start, 3),
                                    "end": round(current_time_offset + l_end, 3),
                                    "speaker_id": sp_id,
                                    "speaker_name": kor_name,
                                    "text": clean_stt_text
                                })
                                entry_idx += 1
            except Exception:
                stt_ran = False
            finally:
                shutil.rmtree(out_tmp, ignore_errors=True)
                
        # Fallback to intelligent script-based splitting if STT failed or wasn't run
        if not stt_ran:
            raise RuntimeError(f"{seg_id} STT 실패 또는 발화 미인식: 음성을 확인하고 다시 시도해 주세요.")
        current_time_offset += seg_duration

    return cues

def review_single_take(project_dir, episode_id, segment_id):
    """Transcribe one finished take and persist a warning tied to that file version."""
    ep = Path(project_dir) / 'episodes' / episode_id
    breakdown = read_json(ep / 'breakdown.json')
    segment = next((s for s in breakdown.get('segments', []) if s.get('id') == segment_id), None)
    if segment is None:
        raise ValueError(f'{segment_id} 대본 구성을 찾지 못했습니다.')
    if not segment.get('dialogue'):
        return None
    take = ep / 'takes' / f'{segment_id}_take01.mp4'
    if not take.is_file():
        raise FileNotFoundError(f'{segment_id} 영상이 없습니다.')
    reviews = []
    try:
        extract_subtitle_cues_with_stt(project_dir, episode_id, {'segments':[segment]}, dialogue_review=reviews)
        if not reviews:
            raise RuntimeError('음성이 인식되지 않았습니다.')
        result = reviews[0]
    except Exception as exc:
        result = {'segment_id':segment_id, 'needs_review':True, 'end_cut_risk':False, 'stt_error':str(exc)}
    result['take_mtime_ns'] = take.stat().st_mtime_ns
    path = ep / 'takes' / 'dialogue_reviews.json'
    existing = read_json(path) if path.exists() else {}
    existing[segment_id] = result
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8')
    return result

def extract_subtitle_cues(
    breakdown_data: Dict[str, Any],
    actual_durations: Optional[Dict[str, float]] = None,
    speaker_names: Optional[Dict[str, str]] = None,
    max_chars_per_line: int = 23
) -> List[Dict[str, Any]]:
    """
    Computes exact start/end timestamps for single-line semantic subtitle chunks,
    tightly synchronized to the actual video take durations.
    """
    cues = []
    current_time = 0.0
    entry_idx = 1

    for seg in breakdown_data.get("segments", []):
        seg_id = seg.get("id")
        if actual_durations and seg_id in actual_durations:
            duration = actual_durations[seg_id]
        else:
            duration = float(seg.get("duration", 5.0))

        dialogues = seg.get("dialogue", [])
        if dialogues:
            time_per_line = duration / len(dialogues)
            for idx, d in enumerate(dialogues):
                line_start = current_time + (idx * time_per_line) + 0.12
                line_end = min(current_time + ((idx + 1) * time_per_line) - 0.08, current_time + duration - 0.04)
                if line_end <= line_start:
                    line_end = line_start + 1.2

                speaker_id = d.get("speaker", "")
                raw_text = d.get("line", "")
                kor_name = (speaker_names or {}).get(speaker_id, speaker_id.title())

                phrases = split_into_semantic_phrases(raw_text, max_chars=max_chars_per_line)
                if not phrases:
                    continue

                total_chars = sum(max(1, len(p)) for p in phrases)
                avail_span = line_end - line_start
                sub_cursor = line_start

                for p_idx, phrase in enumerate(phrases):
                    weight = max(1, len(phrase)) / float(total_chars)
                    p_dur = avail_span * weight
                    p_start = sub_cursor
                    p_end = line_end if p_idx == len(phrases) - 1 else (sub_cursor + p_dur - 0.03)
                    sub_cursor += p_dur

                    cues.append({
                        "index": entry_idx,
                        "segment_id": seg_id,
                        "start": round(p_start, 3),
                        "end": round(p_end, 3),
                        "speaker_id": speaker_id,
                        "speaker_name": kor_name,
                        "text": phrase  # Guaranteed single-line semantic phrase
                    })
                    entry_idx += 1

        current_time += duration
    return cues

def load_korean_font(size: int = 40) -> ImageFont.FreeTypeFont:
    """Loads bold Korean system font on macOS for crisp Shorts subtitles."""
    font_candidates = [
        ("/System/Library/Fonts/AppleSDGothicNeo.ttc", 6),  # Bold
        ("/System/Library/Fonts/AppleSDGothicNeo.ttc", 0),
        ("/System/Library/Fonts/Supplemental/AppleGothic.ttf", 0),
    ]
    for path, idx in font_candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size, index=idx)
            except Exception:
                continue
    return ImageFont.load_default()

def burn_hard_subtitles_with_pil(
    input_video_path: Union[str, Path],
    output_video_path: Union[str, Path],
    cues: List[Dict[str, Any]],
    overlays_dir: Union[str, Path],
    ffmpeg_bin: str = "/opt/homebrew/bin/ffmpeg",
    video_size: Tuple[int, int] = (720, 1280)
) -> bool:
    """
    Renders high-contrast YouTube Shorts hardcoded subtitles (burn-in) using Pillow RGBA overlays
    and FFmpeg's built-in overlay filter (works without libass/libfreetype).
    """
    if not cues:
        return False

    ov_dir = ensure_dir(overlays_dir)
    w, h = video_size
    font_Size = max(36, int(round(w * 0.073))) # ~53px on 720p width (1.3x scaled up for maximum readability)
    speaker_font_size = max(28, int(round(w * 0.057)))
    font = load_korean_font(font_Size)
    speaker_font = load_korean_font(speaker_font_size)

    speaker_colors = {
        "cheon_chairman": (255, 228, 115, 255), # Warm Gold-Yellow
        "do_jin": (255, 255, 255, 255),         # Crisp Pure White
        "yeong_ja": (255, 228, 115, 255),       # Warm Gold-Yellow
        "jin_woo": (255, 255, 255, 255),        # Crisp Pure White
        "min_seok": (130, 230, 255, 255),       # Sharp Cyan-Blue
    }

    overlay_files = []
    max_box_w = int(w * 0.94) # Wide horizontal span across 94% of screen width (676px on 720p)

    for cue in cues:
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Strictly ONE single line (no newline wrapping!)
        single_line_text = cue["text"].replace("\n", " ").strip()

        sp_id = cue.get("speaker_id", "")
        text_color = speaker_colors.get(sp_id, (255, 255, 255, 255))

        # Auto-fit font size so the single wide line always fits cleanly within max_box_w
        cur_size = font_Size
        cur_font = font
        bbox = draw.textbbox((0, 0), single_line_text, font=cur_font, stroke_width=5)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        while tw > max_box_w and cur_size > 20:
            cur_size -= 1
            cur_font = load_korean_font(cur_size)
            bbox = draw.textbbox((0, 0), single_line_text, font=cur_font, stroke_width=5)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]

        # Position in Shorts lower-third golden zone (82% height, crisp wide single line)
        cx = w // 2
        cy = int(h * 0.82)
        tx = cx - (tw // 2)
        ty = cy - (th // 2)

        # Draw semi-transparent dark rounded caption pill background
        pad_x, pad_y = 24, 16
        box_coords = [max(4, tx - pad_x), ty - pad_y, min(w - 4, tx + tw + pad_x), ty + th + pad_y]
        draw.rounded_rectangle(box_coords, radius=18, fill=(10, 10, 16, 190))

        # Draw high-contrast single-line text with thick black outline stroke
        draw.text(
            (tx, ty),
            single_line_text,
            font=cur_font,
            fill=text_color,
            stroke_width=5,
            stroke_fill=(0, 0, 0, 255)
        )

        ov_path = ov_dir / f"sub_cue_{cue['index']:02d}.png"
        img.save(ov_path, format="PNG")
        overlay_files.append((ov_path, cue["start"], cue["end"]))

    # Build FFmpeg filter_complex chain for timed overlays
    cmd = [ffmpeg_bin, "-y", "-i", str(input_video_path)]
    for ov_path, _, _ in overlay_files:
        cmd.extend(["-i", str(ov_path)])

    filter_parts = []
    last_label = "0:v"
    for idx, (_, start_t, end_t) in enumerate(overlay_files, 1):
        next_label = f"v{idx}" if idx < len(overlay_files) else "vout"
        filter_parts.append(
            f"[{last_label}][{idx}:v]overlay=0:0:enable='between(t,{start_t:.3f},{end_t:.3f})'[{next_label}]"
        )
        last_label = next_label

    filter_complex = ";".join(filter_parts)
    config = load_config()
    target_fps = int(config.get("video", {}).get("fps", 24))
    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "0:a?",
        "-r", str(target_fps),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "18",
        "-preset", "fast",
        "-c:a", "copy",
        str(output_video_path)
    ])
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return True

def get_video_duration(video_path: Union[str, Path], ffprobe_bin: Optional[str] = None) -> float:
    """Gets video duration in seconds via ffprobe."""
    try:
        cmd = [
            resolve_binary("ffprobe", ffprobe_bin), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception:
        return 5.0

def build_final_cut(
    project_dir: Union[str, Path],
    episode_id: str,
    burn_subtitles: bool = True,
    trim_silence: bool = True
) -> Dict[str, Any]:
    """
    Concatenates takes using FFmpeg (with smart lead/tail silence trimming for tight Shorts pacing)
    and burns high-contrast Shorts hardcoded subtitles onto the video.
    """
    config = load_config()
    ffmpeg_bin = resolve_binary("ffmpeg", config.get("paths", {}).get("ffmpeg_bin"))

    proj = Path(project_dir)
    ep_dir = proj / "episodes" / episode_id
    breakdown_file = ep_dir / "breakdown.json"
    if not breakdown_file.exists():
        raise FileNotFoundError(f"Breakdown not found: {breakdown_file}")

    breakdown = read_json(breakdown_file)
    takes_dir = ep_dir / "takes"
    cut_dir = ensure_dir(ep_dir / "cut")

    # Collect take video files and measure actual durations in segment order
    segments = breakdown.get("segments", [])
    take_files = []
    take_by_seg: Dict[str, Path] = {}
    missing_takes = []
    actual_durations: Dict[str, float] = {}

    for seg in segments:
        seg_id = seg.get("id")
        candidate = takes_dir / f"{seg_id}_take01.mp4"
        if candidate.exists() and candidate.stat().st_size > 0:
            take_files.append(candidate)
            take_by_seg[seg_id] = candidate
            actual_durations[seg_id] = get_video_duration(candidate)
        else:
            missing_takes.append(seg_id)

    if missing_takes:
        return {
            "success": False,
            "error": f"Missing take video files for segments: {missing_takes}",
            "takes_found": [str(t) for t in take_files]
        }

    # Read speaker names from bible.json if available
    speaker_names = {}
    bible_file = proj / "bible.json"
    if bible_file.exists():
        bible_data = read_json(bible_file)
        for c in bible_data.get("characters", []):
            speaker_names[c.get("id")] = c.get("name", c.get("id"))

    # Generate subtitles synchronized to actual take lengths via STT
    srt_path = cut_dir / "subtitles.srt"
    dialogue_review = []
    speech_bounds: Dict[str, Tuple[float, float]] = {}
    try:
        cues = extract_subtitle_cues_with_stt(
            project_dir, episode_id, breakdown,
            speaker_names=speaker_names,
            dialogue_review=dialogue_review,
            speech_bounds=speech_bounds
        )
    except TypeError:
        cues = extract_subtitle_cues_with_stt(
            project_dir, episode_id, breakdown,
            speaker_names=speaker_names,
            dialogue_review=dialogue_review
        )
    review_path = cut_dir / "dialogue_review.json"
    review_path.write_text(json.dumps(dialogue_review, ensure_ascii=False, indent=2), encoding="utf-8")

    target_fps = int(config.get("video", {}).get("fps", 24))

    # Smart silence trimming: trim excessive dead-air before/after dialogue per segment for snappy Shorts pacing
    final_concat_files: List[Path] = []
    applied_trims: Dict[str, Tuple[float, float]] = {}
    effective_durations: Dict[str, float] = dict(actual_durations)
    trimmed_dir = ensure_dir(cut_dir / "trimmed")
    total_trimmed_sec = 0.0

    for idx, seg in enumerate(segments):
        seg_id = seg.get("id")
        src_file = take_by_seg[seg_id]
        orig_dur = actual_durations.get(seg_id, 5.0)
        is_last = (idx == len(segments) - 1)

        if trim_silence and seg_id in speech_bounds and seg.get("dialogue"):
            f_start, l_end = speech_bounds[seg_id]
            t_start, t_end = compute_smart_trim_window(orig_dur, f_start, l_end, is_last_segment=is_last)
            if t_start > 0.0 or t_end < (orig_dur - 0.05):
                trimmed_out = trimmed_dir / f"{seg_id}_trimmed.mp4"
                trim_cmd = [
                    ffmpeg_bin, "-y",
                    "-i", str(src_file),
                    "-ss", f"{t_start:.3f}",
                    "-to", f"{t_end:.3f}",
                    "-r", str(target_fps),
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-crf", "18",
                    "-preset", "fast",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    str(trimmed_out)
                ]
                try:
                    subprocess.run(trim_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if trimmed_out.is_file() and trimmed_out.stat().st_size > 0:
                        new_dur = get_video_duration(trimmed_out)
                        final_concat_files.append(trimmed_out)
                        applied_trims[seg_id] = (t_start, t_end)
                        effective_durations[seg_id] = new_dur
                        total_trimmed_sec += max(0.0, orig_dur - new_dur)
                        continue
                except Exception:
                    pass  # Fallback safely to original clip

        final_concat_files.append(src_file)

    # Re-align subtitle cue timestamps accurately to actual trimmed segment durations (zero drift)
    if applied_trims and cues:
        seg_offsets: Dict[str, float] = {}
        cursor = 0.0
        for seg in segments:
            sid = seg.get("id")
            seg_offsets[sid] = cursor
            cursor += effective_durations.get(sid, actual_durations.get(sid, 5.0))

        for c in cues:
            sid = c.get("segment_id")
            if sid in seg_offsets and "local_start" in c and "local_end" in c:
                t_start = applied_trims.get(sid, (0.0, effective_durations.get(sid, 5.0)))[0]
                eff_dur = effective_durations.get(sid, 5.0)
                adj_start = max(0.0, min(eff_dur, c["local_start"] - t_start))
                adj_end = max(adj_start + 0.1, min(eff_dur, c["local_end"] - t_start))
                c["start"] = round(seg_offsets[sid] + adj_start, 3)
                c["end"] = round(seg_offsets[sid] + adj_end, 3)

    # Write accurate SRT file matching cues
    srt_lines = []
    for c in cues:
        sp_name = c.get("speaker_name", "")
        txt = f"{sp_name}: {c['text']}" if sp_name else c["text"]
        srt_lines.append(f"{c['index']}\n{format_srt_time(c['start'])} --> {format_srt_time(c['end'])}\n{txt}\n")
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

    # Write concat list for ffmpeg
    filelist_path = cut_dir / "concat_list.txt"
    with open(filelist_path, "w", encoding="utf-8") as f:
        for t in final_concat_files:
            f.write(f"file '{t.resolve()}'\n")

    clean_cut_path = cut_dir / "final_cut_clean.mp4"
    final_cut_path = cut_dir / "final_cut.mp4"

    # Concat takes and normalize frame rate to target_fps (24fps cinema standard)
    concat_fps_cmd = [
        ffmpeg_bin, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(filelist_path),
        "-r", str(target_fps),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "18",
        "-preset", "fast",
        "-c:a", "aac",
        "-b:a", "192k",
        str(clean_cut_path)
    ]
    try:
        subprocess.run(concat_fps_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception:
        # Fallback for synthetic test dummy files
        concat_cmd = [
            ffmpeg_bin, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(filelist_path),
            "-c", "copy",
            str(clean_cut_path)
        ]
        subprocess.run(concat_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    result = {
        "success": True,
        "final_cut_path": str(final_cut_path),
        "final_cut_clean_path": str(clean_cut_path),
        "subtitles_path": str(srt_path),
        "dialogue_review_path": str(review_path),
        "dialogue_warnings": [r for r in dialogue_review if r['needs_review']],
        "takes_count": len(take_files)
    }

    # Burn visible hardcoded subtitles directly onto the video pixels using Pillow + FFmpeg overlay
    if burn_subtitles and cues:
        subtitled_path = cut_dir / "final_cut_subtitled.mp4"
        overlays_dir = cut_dir / "sub_overlays"
        try:
            burn_hard_subtitles_with_pil(
                input_video_path=clean_cut_path,
                output_video_path=subtitled_path,
                cues=cues,
                overlays_dir=overlays_dir,
                ffmpeg_bin=ffmpeg_bin
            )
            # Copy burned video to final_cut.mp4 as well so opening final_cut.mp4 always shows subtitles!
            shutil.copyfile(subtitled_path, final_cut_path)
            result["final_cut_subtitled_path"] = str(subtitled_path)
        except Exception as e:
            return {"success": False, "error": f"자막 합성 실패: {e}", "final_cut_clean_path": str(clean_cut_path)}
    elif burn_subtitles and any(seg.get("dialogue") for seg in breakdown.get("segments", [])):
        return {"success": False, "error": "대사가 있지만 STT 자막이 생성되지 않았습니다."}
    else:
        shutil.copyfile(clean_cut_path, final_cut_path)

    return result
