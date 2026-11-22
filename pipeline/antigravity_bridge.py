"""Antigravity CLI bridge consumer for synopsis, full script, and script revision."""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pipeline.common import ensure_dir, read_json, write_json
from pipeline.stage1_bible import generate_bible_prompt, parse_screenplay, save_bible
from pipeline.stage2_assets import prepare_assets
from pipeline.stage3_breakdown import generate_breakdown_prompt, save_breakdown
from pipeline.story_runtime import breakdown_brief, load_target_duration

BASE_DIR = Path(__file__).resolve().parent.parent
SKILLS_WRITER_PATH = Path.home() / ".gemini" / "config" / "skills" / "shorts-drama-writer" / "SKILL.md"


def find_antigravity_cli() -> Optional[Dict[str, str]]:
    """Locate an available non-interactive Antigravity / Gemini CLI binary."""
    env_bin = os.environ.get("ANTIGRAVITY_CLI_BIN", "").strip()
    candidates: List[tuple[str, Path]] = []
    if env_bin:
        p = Path(env_bin)
        kind = "gemini" if "gemini" in p.name.lower() and p.name.lower() != "agy" else "agy"
        candidates.append((kind, p))

    agy_which = shutil.which("agy")
    if agy_which:
        candidates.append(("agy", Path(agy_which)))
    candidates.append(("agy", Path.home() / ".gemini" / "bin" / "agy"))

    gemini_which = shutil.which("gemini")
    if gemini_which:
        candidates.append(("gemini", Path(gemini_which)))
    candidates.append(("gemini", Path("/opt/homebrew/bin/gemini")))

    seen = set()
    for kind, path in candidates:
        resolved = str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.exists() and os.access(path, os.X_OK):
            return {"kind": kind, "path": resolved}
    return None


def find_yt_dlp() -> Optional[str]:
    """Locate yt-dlp binary if installed."""
    for cand in (shutil.which("yt-dlp"), "/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"):
        if cand and Path(cand).exists() and os.access(cand, os.X_OK):
            return cand
    return None


def _clean_vtt_transcript(raw_vtt: str, max_chars: int = 3200) -> str:
    """Strip WebVTT timestamps/tags and collapse duplicate rolling captions."""
    lines: List[str] = []
    for raw_line in raw_vtt.splitlines():
        line = re.sub(r"<[^>]+>", "", raw_line)
        line = html.unescape(line).strip()
        if not line:
            continue
        if line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or "-->" in line:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def fetch_reference_context(url: str, timeout: int = 20) -> Dict[str, Any]:
    """Fetch reference video title, metadata, and subtitles via yt-dlp when possible."""
    ctx: Dict[str, Any] = {
        "url": url,
        "title": "",
        "uploader": "",
        "duration": None,
        "description": "",
        "transcript": "",
        "fetch_status": "skipped",
    }
    if not url or not url.strip():
        return ctx

    yt_dlp = find_yt_dlp()
    if not yt_dlp:
        ctx["fetch_status"] = "yt_dlp_missing"
        return ctx

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_tpl = str(Path(tmp_dir) / "ref.%(ext)s")
            cmd = [
                yt_dlp,
                "--skip-download",
                "--write-auto-subs",
                "--write-subs",
                "--sub-langs", "ko-orig,ko,en",
                "--sub-format", "vtt",
                "--no-warnings",
                "--print-json",
                "-o", out_tpl,
                url.strip(),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

            # Parse metadata JSON from the first valid JSON line in stdout
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        meta = json.loads(line)
                        ctx["title"] = str(meta.get("title") or "").strip()
                        ctx["uploader"] = str(meta.get("uploader") or meta.get("channel") or "").strip()
                        ctx["duration"] = meta.get("duration")
                        ctx["description"] = str(meta.get("description") or "").strip()[:1000]
                        break
                    except Exception:
                        continue

            # Pick best available VTT subtitle file (Korean original > Korean > English > any)
            vtt_files = sorted(Path(tmp_dir).glob("*.vtt"))
            chosen_vtt: Optional[Path] = None
            for pref in (".ko-orig.vtt", ".ko.vtt", ".en.vtt"):
                for cand in vtt_files:
                    if cand.name.endswith(pref):
                        chosen_vtt = cand
                        break
                if chosen_vtt:
                    break
            if not chosen_vtt and vtt_files:
                chosen_vtt = vtt_files[0]

            if chosen_vtt:
                raw_vtt = chosen_vtt.read_text(encoding="utf-8", errors="ignore")
                ctx["transcript"] = _clean_vtt_transcript(raw_vtt)

            ctx["fetch_status"] = "ok" if (ctx["title"] or ctx["transcript"]) else "partial"
    except subprocess.TimeoutExpired:
        ctx["fetch_status"] = "timeout"
    except Exception as exc:
        ctx["fetch_status"] = f"error:{exc}"
    return ctx


def extract_json_object(raw_text: str) -> Dict[str, Any]:
    """Extract and parse a JSON object from raw model output (tolerates markdown fences)."""
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, flags=re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        parsed = json.loads(cleaned[start : end + 1])
        if isinstance(parsed, dict):
            return parsed

    raise ValueError(f"Antigravity 응답에서 유효한 JSON 객체를 찾을 수 없습니다: {cleaned[:240]}")


def strip_code_fences(raw_text: str) -> str:
    """Remove surrounding markdown code fences from generated Fountain text."""
    text = raw_text.strip()
    match = re.match(r"^```[a-zA-Z0-9_-]*\n(.*)\n```$", text, flags=re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    return text + "\n"


def _openai_setting(name: str, default: str = "") -> str:
    """Read one setting at request time, so editing .env needs no restart."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = BASE_DIR / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, val = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() == name:
                return val.strip().strip("\"'")
    return default


def run_openai_prompt(prompt: str, timeout: int = 600) -> str:
    key = _openai_setting("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OpenAI API 대체 실행 불가: .env에 OPENAI_API_KEY를 입력해 주세요.")
    model = _openai_setting("OPENAI_MODEL", "gpt-4.1") or "gpt-4.1"
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=timeout, max_retries=1)
        response = client.responses.create(model=model, input=prompt, max_output_tokens=16384)
        output = (response.output_text or "").strip()
        if getattr(response, "status", None) == "incomplete":
            raise RuntimeError("OpenAI 응답이 완료되지 않았습니다. 출력 한도 또는 모델 상태를 확인해 주세요.")
        if not output:
            raise RuntimeError("OpenAI API가 빈 응답을 반환했습니다.")
        return output
    except Exception as exc:
        message = str(exc).replace(key, "[REDACTED]")
        raise RuntimeError(f"OpenAI API 대체 실행 실패 ({model}): {message}") from exc


def run_antigravity_prompt(prompt: str, timeout: int = 600, log_fn: Optional[Callable[[str], None]] = None) -> str:
    """Execute a single headless prompt via Antigravity CLI (`agy`) or Gemini CLI fallback."""
    cli = find_antigravity_cli()
    if not cli:
        raise RuntimeError(
            "Antigravity CLI(agy) 또는 Gemini CLI를 찾을 수 없습니다. "
            "~/.gemini/bin/agy 또는 gemini 설치 상태를 확인해 주세요."
        )

    env = os.environ.copy()
    extra_paths = [str(Path.home() / ".gemini" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = ":".join(extra_paths + [env.get("PATH", "")])

    if cli["kind"] == "agy":
        cmd = [
            cli["path"],
            "-p", prompt,
            "--output-format", "text",
            "--disable-slash-commands",
            "--print-timeout", f"{timeout}s",
        ]
    else:
        cmd = [
            cli["path"],
            "-p", prompt,
            "-o", "text",
        ]

    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout + 15,
        cwd=str(BASE_DIR),
        env=env,
    )
    output = (res.stdout or "").strip()
    if res.returncode != 0:
        err = (res.stderr or output or f"exit code {res.returncode}").strip()
        normalized = err.lower()
        if "individual quota reached" in normalized and ("resource_exhausted" in normalized or "429" in normalized):
            if log_fn:
                log_fn("⚠️ Antigravity 개인 할당량 소진(429): OpenAI API로 전환합니다. API 사용 요금이 발생할 수 있습니다.")
            return run_openai_prompt(prompt, timeout=timeout)
        raise RuntimeError(f"Antigravity CLI 실행 실패 ({cli['kind']}): {err[:400]}")
    if not output:
        raise RuntimeError(f"Antigravity CLI ({cli['kind']})가 빈 응답을 반환했습니다.")
    return output


def _load_writer_skill_excerpt() -> str:
    if SKILLS_WRITER_PATH.exists():
        text = SKILLS_WRITER_PATH.read_text(encoding="utf-8", errors="ignore")
        return text[:3500]
    return ""


def build_synopsis_prompt(bridge_req: Dict[str, Any], ref_ctx: Dict[str, Any]) -> str:
    """Build the Antigravity prompt for generating 3 distinct synopsis options."""
    guidance = bridge_req.get("story_guidance", "").strip()
    url = bridge_req.get("url", "").strip()
    notes = bridge_req.get("notes", "").strip()
    project = bridge_req.get("project", "").strip()

    ref_parts = [f"- Reference URL: {url or '(none)'}"]
    if ref_ctx.get("title"):
        ref_parts.append(f"- Reference Title: {ref_ctx['title']}")
    if ref_ctx.get("uploader"):
        ref_parts.append(f"- Reference Channel: {ref_ctx['uploader']}")
    if ref_ctx.get("duration"):
        ref_parts.append(f"- Reference Duration: {ref_ctx['duration']}s")
    if ref_ctx.get("description"):
        ref_parts.append(f"- Reference Description: {ref_ctx['description']}")
    if ref_ctx.get("transcript"):
        ref_parts.append(f"- Reference Transcript:\n{ref_ctx['transcript']}")
    else:
        ref_parts.append("- Reference Transcript: (자막 추출 불가 - URL과 제목, 기획 요청사항을 토대로 창작)")

    ref_block = "\n".join(ref_parts)

    return f"""너는 숏폼 드라마 전문 작가(`shorts-drama-writer`)다.
중요 실행 규칙:
- 어떤 파일도 읽거나 수정하지 말고, 어떤 쉘 명령어나 도구도 호출하지 마라.
- 아래에 제공된 레퍼런스 정보와 지침만 사용해 즉시 응답하라.
- 응답은 반드시 유효한 순수 JSON 객체 하나만 출력하라. 설명 문장이나 마크다운 코드펜스를 붙이지 마라.
- 기존 예시 소재(치매 어머니, 고물상, 200원 젖은 폐지 박스 등)는 절대 재사용하지 마라.
- 모든 텍스트 값은 자연스러운 한국어로 작성하되, `genre_engine`과 `hook_type`만 영문 snake_case 라벨로 작성하라.

[프로젝트 이름]
{project}

[레퍼런스 영상 정보]
{ref_block}

[사용자 추가 요청사항 / 장르 메모]
{notes or '(없음 - 레퍼런스 구조를 참고해 서로 다른 매력의 3안을 창의적으로 제안)'}

[필수 스토리 작성 지침 (story_guidance)]
{guidance}

[출력 JSON 스키마 - 정확히 이 키 구조를 지켜라]
{{
  "reference_analysis": "레퍼런스 영상의 후킹/갈등/반전/보상 구조를 2~3문장으로 분석한 요약",
  "items": [
    {{
      "index": 0,
      "title": "옵션 1: 구체적이고 흥미로운 한국어 제목",
      "genre_engine": "13개 패턴 중 하나 (예: self_inflicted_consequence)",
      "hook_type": "6개 후킹 중 하나 (예: confrontation)",
      "logline": "한두 문장의 선명한 로그라인",
      "hook": "첫 0~3초 화면의 구체적 물리적 행동과 짧은 대사",
      "bridge": "시청자의 의문을 대변하는 질문 또는 상황 전환 고리",
      "aha": "앞선 복선/단서로 인과적으로 납득되는 반전 진실",
      "payoff": "감정적 보상 및 클라이맥스 여운",
      "protagonist_choice": "주인공이 화면에서 손해나 위험을 감수하고 직접 내리는 선택",
      "reveal_evidence": "반전을 믿게 만드는 초반의 관찰 가능한 단서나 증거",
      "payoff_action": "반전 이후 인물이 물리적으로 다르게 행동하는 구체적 마지막 행동"
    }},
    {{
      "index": 1,
      "title": "옵션 2: ...",
      "genre_engine": "옵션 1과 다른 snake_case 패턴",
      "hook_type": "옵션 1과 다른 snake_case 후킹 방식",
      "logline": "...",
      "hook": "...",
      "bridge": "...",
      "aha": "...",
      "payoff": "...",
      "protagonist_choice": "...",
      "reveal_evidence": "...",
      "payoff_action": "..."
    }},
    {{
      "index": 2,
      "title": "옵션 3: ...",
      "genre_engine": "옵션 1, 2와 모두 다른 snake_case 패턴",
      "hook_type": "옵션 1, 2와 모두 다른 snake_case 후킹 방식",
      "logline": "...",
      "hook": "...",
      "bridge": "...",
      "aha": "...",
      "payoff": "...",
      "protagonist_choice": "...",
      "reveal_evidence": "...",
      "payoff_action": "..."
    }}
  ]
}}

검증 체크:
1. `items`는 반드시 정확히 3개여야 한다.
2. 세 옵션의 `genre_engine`은 서로 모두 달라야 한다.
3. 세 옵션의 `hook_type`은 서로 모두 달라야 한다. (`confrontation`, `strange_action`, `missing_object`, `unfair_choice`, `visual_contrast`, `interrupted_payoff` 중 3개 선택)
4. 모든 옵션에 `title`, `logline`, `hook`, `bridge`, `aha`, `payoff`, `protagonist_choice`, `reveal_evidence`, `payoff_action`이 비어있지 않고 구체적으로 채워져야 한다.
"""


def build_full_script_prompt(
    bridge_req: Dict[str, Any],
    selected_item: Dict[str, Any],
    reference_analysis: str = "",
) -> str:
    """Build the Antigravity prompt for expanding a chosen synopsis into a Fountain screenplay."""
    guidance = bridge_req.get("story_guidance", "").strip()
    target_duration = bridge_req.get("target_duration", 60)
    feedback = bridge_req.get("feedback", "").strip()
    skill_excerpt = _load_writer_skill_excerpt()

    return f"""너는 9:16 숏폼 드라마 전문 각본가다.
중요 실행 규칙:
- 어떤 파일도 읽거나 수정하지 말고, 어떤 쉘 명령어나 도구도 호출하지 마라.
- 응답은 설명 없이 **Fountain 포맷 한국어 대본 원문만** 바로 출력하라.

[목표 길이]
약 {target_duration}초 (9:16 세로형 쇼츠 드라마)

[선택된 시놉시스]
{json.dumps(selected_item, ensure_ascii=False, indent=2)}

[레퍼런스 구조 메모]
{reference_analysis or '(없음)'}

[사용자 추가 피드백]
{feedback or '(없음)'}

[필수 스토리 작성 지침 (story_guidance)]
{guidance}

[숏드라마 작가 규칙 요약]
{skill_excerpt[:1500]}

[Fountain 대본 출력 형식 규칙]
- 첫 3줄은 헤더로 시작:
  Title: 작품 제목
  Credit: 숏드라마 파이프라인 전용 각본
  Format: 9:16 Vertical Short Drama (약 {target_duration}초)
- 씬 헤딩은 반드시 `EXT. 장소 - 시간대` 또는 `INT. 장소 - 시간대` 형식 사용.
- 캐릭터 대사는 캐릭터 이름을 단독 줄에 쓰고, 필요 시 괄호 지문 후, 짧은 구어체 한국어 대사를 작성.
- 0~2초 첫 프레임에서 바로 시각적 갈등/이상 행동이 보이게 Cold Open으로 시작.
- 초반 단서(`reveal_evidence`)를 반드시 앞 씬에 시각적으로 심고, 반전 후 표정/반응과 구체적 마지막 행동(`payoff_action`)으로 마무리.
- 설교성 내레이션이나 "좋아요/구독 눌러주세요" CTA 금지.
- 대사 분량은 실제 한국어 발화 속도로 약 {target_duration}초 안에 자연스럽게 소화되도록 절제할 것.
"""


def build_feedback_prompt(
    current_screenplay: str,
    feedback: str,
    story_guidance: str,
    target_duration: int = 60,
) -> str:
    """Build the Antigravity prompt for revising an existing Fountain screenplay."""
    return f"""너는 9:16 숏폼 드라마 전문 각본가다.
중요 실행 규칙:
- 어떤 파일도 읽거나 수정하지 말고, 어떤 쉘 명령어나 도구도 호출하지 마라.
- 아래 기존 Fountain 대본에 사용자 피드백을 정확히 반영해 **수정된 전체 Fountain 대본 원문만** 출력하라.

[목표 길이]
약 {target_duration}초

[사용자 수정 요청사항]
{feedback}

[기존 대본]
{current_screenplay}

[스토리 품질 지침]
{story_guidance}
"""


def _normalize_proposals_payload(
    parsed: Dict[str, Any],
    bridge_req: Dict[str, Any],
    ref_ctx: Dict[str, Any],
) -> Dict[str, Any]:
    raw_items = parsed.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("생성된 시놉시스에 'items' 목록이 비어 있습니다.")

    items: List[Dict[str, Any]] = []
    for idx, raw in enumerate(raw_items[:3]):
        if not isinstance(raw, dict):
            continue
        items.append({
            "index": idx,
            "title": str(raw.get("title") or f"옵션 {idx + 1}").strip(),
            "genre_engine": str(raw.get("genre_engine") or "").strip(),
            "hook_type": str(raw.get("hook_type") or "").strip(),
            "logline": str(raw.get("logline") or "").strip(),
            "hook": str(raw.get("hook") or "").strip(),
            "bridge": str(raw.get("bridge") or "").strip(),
            "aha": str(raw.get("aha") or "").strip(),
            "payoff": str(raw.get("payoff") or "").strip(),
            "protagonist_choice": str(raw.get("protagonist_choice") or "").strip(),
            "reveal_evidence": str(raw.get("reveal_evidence") or "").strip(),
            "payoff_action": str(raw.get("payoff_action") or "").strip(),
        })

    if not items:
        raise ValueError("유효한 시놉시스 항목을 파싱하지 못했습니다.")

    return {
        "request_id": bridge_req.get("id"),
        "project": bridge_req.get("project"),
        "url": bridge_req.get("url", ""),
        "reference_title": ref_ctx.get("title", ""),
        "reference_analysis": str(parsed.get("reference_analysis") or "").strip(),
        "generated_at": time.time(),
        "items": items,
    }


def _update_inbox_state(bridge_dir: Path, bridge_req: Dict[str, Any], status: str, **extra: Any) -> Dict[str, Any]:
    updated = dict(bridge_req)
    updated["status"] = status
    updated["updated_at"] = time.time()
    updated.update(extra)
    ensure_dir(bridge_dir)
    write_json(bridge_dir / "inbox.json", updated)
    return updated


def process_synopsis_request(
    bridge_req: Dict[str, Any],
    bridge_dir: Path,
    outputs_dir: Path,
    log_fn: Optional[Callable[[str], None]] = None,
    runner: Optional[Callable[[str], str]] = None,
    reference_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run Antigravity synopsis generation and persist `{project}_proposals.json`."""
    log = log_fn or (lambda msg: None)
    run_llm = runner or partial(run_antigravity_prompt, log_fn=log)
    fetch_ref = reference_fetcher or fetch_reference_context

    project = str(bridge_req.get("project") or "sample_drama")
    url = str(bridge_req.get("url") or "")

    state = _update_inbox_state(bridge_dir, bridge_req, "running")
    try:
        log(f"🔎 [Antigravity Bridge] 레퍼런스 영상 분석 중: {url}")
        ref_ctx = fetch_ref(url)
        if ref_ctx.get("title"):
            log(f"📺 레퍼런스 확인: '{ref_ctx['title']}' (자막 {len(ref_ctx.get('transcript', ''))}자 추출)")
        else:
            log(f"ℹ️ 레퍼런스 메타데이터 제한({ref_ctx.get('fetch_status')}) - URL 및 요청사항 기준으로 기획 진행")

        prompt = build_synopsis_prompt(bridge_req, ref_ctx)
        log("🤖 [Antigravity Bridge] Antigravity CLI로 3색 시놉시스 생성 요청 중...")
        raw_output = run_llm(prompt)
        parsed = extract_json_object(raw_output)
        proposals = _normalize_proposals_payload(parsed, bridge_req, ref_ctx)

        ensure_dir(bridge_dir)
        proposals_path = bridge_dir / f"{project}_proposals.json"
        write_json(proposals_path, proposals)

        state = _update_inbox_state(
            bridge_dir,
            bridge_req,
            "completed",
            result_path=str(proposals_path),
            item_count=len(proposals["items"]),
        )
        log(f"✅ [Antigravity Bridge] 3색 시놉시스 생성 완료! ({proposals_path.name})")
        return {"state": state, "proposals": proposals}
    except Exception as exc:
        state = _update_inbox_state(bridge_dir, bridge_req, "error", error=str(exc))
        log(f"❌ [Antigravity Bridge] 시놉시스 생성 실패: {exc}")
        raise


def process_full_script_request(
    bridge_req: Dict[str, Any],
    bridge_dir: Path,
    outputs_dir: Path,
    log_fn: Optional[Callable[[str], None]] = None,
    runner: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """Expand the selected synopsis into `outputs/{project}/screenplay.fountain`."""
    log = log_fn or (lambda msg: None)
    run_llm = runner or partial(run_antigravity_prompt, log_fn=log)

    project = str(bridge_req.get("project") or "sample_drama")
    synopsis_idx = int(bridge_req.get("synopsis_index", 0))
    proposals_path = bridge_dir / f"{project}_proposals.json"

    state = _update_inbox_state(bridge_dir, bridge_req, "running")
    try:
        if not proposals_path.exists():
            raise FileNotFoundError(f"시놉시스 제안 파일({proposals_path.name})을 찾을 수 없습니다. 먼저 시놉시스를 요청해 주세요.")

        proposals = read_json(proposals_path)
        items = proposals.get("items") or []
        if not (0 <= synopsis_idx < len(items)):
            raise IndexError(f"선택한 시놉시스 인덱스({synopsis_idx})가 범위를 벗어났습니다.")

        selected = items[synopsis_idx]
        prompt = build_full_script_prompt(
            bridge_req,
            selected,
            reference_analysis=str(proposals.get("reference_analysis") or ""),
        )
        log(f"✍️ [Antigravity Bridge] '{selected.get('title', '선택안')}' 기반으로 {bridge_req.get('target_duration', 60)}초 대본 생성 중...")
        raw_script = run_llm(prompt)
        screenplay = strip_code_fences(raw_script)

        proj_dir = ensure_dir(outputs_dir / project)
        script_path = proj_dir / "screenplay.fountain"
        script_path.write_text(screenplay, encoding="utf-8")

        state = _update_inbox_state(
            bridge_dir,
            bridge_req,
            "completed",
            result_path=str(script_path),
        )
        log(f"✅ [Antigravity Bridge] 전체 대본 생성 완료! ({script_path.name}, {len(screenplay)}자)")
        return {"state": state, "screenplay": screenplay}
    except Exception as exc:
        state = _update_inbox_state(bridge_dir, bridge_req, "error", error=str(exc))
        log(f"❌ [Antigravity Bridge] 대본 생성 실패: {exc}")
        raise


def process_script_feedback_request(
    bridge_req: Dict[str, Any],
    bridge_dir: Path,
    outputs_dir: Path,
    log_fn: Optional[Callable[[str], None]] = None,
    runner: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """Revise an existing screenplay using user feedback via Antigravity CLI."""
    log = log_fn or (lambda msg: None)
    run_llm = runner or partial(run_antigravity_prompt, log_fn=log)

    project = str(bridge_req.get("project") or "sample_drama")
    feedback = str(bridge_req.get("feedback") or "").strip()
    target_duration = int(bridge_req.get("target_duration") or 60)

    state = _update_inbox_state(bridge_dir, bridge_req, "running")
    try:
        if not feedback:
            raise ValueError("수정 요청 내용이 비어 있습니다.")
        proj_dir = ensure_dir(outputs_dir / project)
        script_path = proj_dir / "screenplay.fountain"
        current_script = script_path.read_text(encoding="utf-8") if script_path.exists() else ""

        prompt = build_feedback_prompt(
            current_screenplay=current_script,
            feedback=feedback,
            story_guidance=str(bridge_req.get("story_guidance") or ""),
            target_duration=target_duration,
        )
        log(f"💬 [Antigravity Bridge] 대본 피드백 반영 중: {feedback[:60]}")
        raw_script = run_llm(prompt)
        updated_script = strip_code_fences(raw_script)
        script_path.write_text(updated_script, encoding="utf-8")

        state = _update_inbox_state(
            bridge_dir,
            bridge_req,
            "completed",
            result_path=str(script_path),
        )
        log(f"✅ [Antigravity Bridge] 대본 수정 반영 완료! ({len(updated_script)}자)")
        return {"state": state, "screenplay": updated_script}
    except Exception as exc:
        state = _update_inbox_state(bridge_dir, bridge_req, "error", error=str(exc))
        log(f"❌ [Antigravity Bridge] 대본 수정 실패: {exc}")
        raise



_ALLOWED_SIZES = {
    "extreme wide shot": "extreme wide shot",
    "ews": "extreme wide shot",
    "wide shot": "wide shot",
    "wide": "wide shot",
    "ws": "wide shot",
    "full shot": "wide shot",
    "medium shot": "medium shot",
    "ms": "medium shot",
    "medium": "medium shot",
    "medium close-up": "medium close-up",
    "medium close up": "medium close-up",
    "mcu": "medium close-up",
    "close-up": "close-up",
    "close up": "close-up",
    "cu": "close-up",
    "extreme close-up": "extreme close-up",
    "extreme close up": "extreme close-up",
    "ecu": "extreme close-up",
}

_ALLOWED_ANGLES = {
    "eye level": "eye level",
    "eye-level": "eye level",
    "high angle": "high angle",
    "high-angle": "high angle",
    "low angle": "low angle",
    "low-angle": "low angle",
    "overhead shot": "overhead shot",
    "overhead": "overhead shot",
    "bird's eye": "overhead shot",
}

_ALLOWED_MOVEMENTS = {
    "locked-off camera",
    "push in",
    "pull out",
    "pull back",
    "pan",
    "tilt up",
    "tilt down",
    "track",
    "follow",
    "orbit",
    "dive",
    "crane",
    "roll",
}

_ALLOWED_TECHNIQUES = {
    "handheld",
    "first-person perspective",
    "aerial perspective",
    "FPV",
    "long take",
    "dolly zoom",
    "bullet time",
    "speed ramp",
}


def _slug_id(raw: Any, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9_]+", "_", str(raw or "").strip().lower()).strip("_")
    if not text:
        text = fallback
    if not text[0].isalpha():
        text = f"x_{text}"
    return text


def _dedupe_slugs(values: Any) -> List[str]:
    result: List[str] = []
    if not isinstance(values, list):
        return result
    for item in values:
        slug = _slug_id(item, "")
        if slug and slug != "x_" and slug not in result:
            result.append(slug)
    return result


def normalize_bible_data(raw: Dict[str, Any], fallback_title: str = "Short Drama", line_count: int = 20) -> Dict[str, Any]:
    """Sanitize LLM output so it strictly satisfies schemas/bible.schema.json."""
    title = str(raw.get("title") or fallback_title).strip() or fallback_title
    logline = str(raw.get("logline") or f"{title} 숏폼 드라마").strip()
    tone = str(raw.get("tone") or "Cinematic Korean 9:16 vertical short drama").strip()

    characters: List[Dict[str, Any]] = []
    seen_char_ids = set()
    for idx, ch in enumerate(raw.get("characters") or []):
        if not isinstance(ch, dict):
            continue
        cid = _slug_id(ch.get("id") or ch.get("name"), f"char_{idx + 1}")
        while cid in seen_char_ids:
            cid = f"{cid}_{idx + 1}"
        seen_char_ids.add(cid)

        name = str(ch.get("name") or cid).strip() or cid
        aliases = [str(a).strip() for a in (ch.get("aliases") or []) if str(a).strip()]
        observable = bool(ch.get("observable", True))
        entry: Dict[str, Any] = {
            "id": cid,
            "name": name,
            "aliases": aliases,
            "role": str(ch.get("role") or "주연").strip(),
            "personality": str(ch.get("personality") or "진중한 성격").strip(),
            "appearance": str(ch.get("appearance") or "Korean adult, natural facial features").strip(),
            "voice": str(ch.get("voice") or "자연스러운 한국어 어조").strip(),
            "observable": observable,
        }
        if observable:
            entry["look"] = str(ch.get("look") or entry["appearance"] or "Everyday Korean attire").strip()
        else:
            entry["reason"] = str(ch.get("reason") or "Offscreen character").strip()
        characters.append(entry)

    if not characters:
        characters.append({
            "id": "lead_character",
            "name": "주인공",
            "aliases": [],
            "role": "주인공",
            "personality": "진정성 있는 인물",
            "appearance": "30-year-old Korean adult, expressive eyes, natural hair",
            "voice": "차분하고 또렷한 한국어 목소리",
            "observable": True,
            "look": "Casual everyday Korean clothing",
        })

    scenes: List[Dict[str, Any]] = []
    seen_scene_ids = set()
    for idx, sc in enumerate(raw.get("scenes") or []):
        if not isinstance(sc, dict):
            continue
        sid = _slug_id(sc.get("id") or sc.get("name"), f"scene_{idx + 1}")
        while sid in seen_scene_ids:
            sid = f"{sid}_{idx + 1}"
        seen_scene_ids.add(sid)

        setting_raw = str(sc.get("setting") or "interior").strip().lower()
        setting = "exterior" if "ext" in setting_raw or "out" in setting_raw else "interior"
        scene_entry: Dict[str, Any] = {
            "id": sid,
            "name": str(sc.get("name") or sid).strip() or sid,
            "description": str(sc.get("description") or "Realistic modern Korean location, cinematic lighting").strip(),
            "setting": setting,
        }
        time_val = str(sc.get("time") or "").strip().capitalize()
        if time_val in ("Day", "Night"):
            scene_entry["time"] = time_val
        scenes.append(scene_entry)

    if not scenes:
        scenes.append({
            "id": "main_location",
            "name": "주요 장소",
            "description": "Realistic modern Korean setting with natural cinematic lighting",
            "setting": "interior",
        })

    props: List[Dict[str, Any]] = []
    seen_prop_ids = set()
    for idx, pr in enumerate(raw.get("props") or []):
        if not isinstance(pr, dict):
            continue
        pid = _slug_id(pr.get("id") or pr.get("name"), f"prop_{idx + 1}")
        while pid in seen_prop_ids:
            pid = f"{pid}_{idx + 1}"
        seen_prop_ids.add(pid)
        prop_entry: Dict[str, Any] = {
            "id": pid,
            "name": str(pr.get("name") or pid).strip() or pid,
            "description": str(pr.get("description") or "Detailed cinematic prop on neutral studio surface").strip(),
        }
        if str(pr.get("text") or "").strip():
            prop_entry["text"] = str(pr["text"]).strip()
        props.append(prop_entry)

    beats: List[Dict[str, Any]] = []
    max_line = max(1, int(line_count or 1))
    for idx, bt in enumerate(raw.get("beats") or []):
        if not isinstance(bt, dict):
            continue
        raw_lines = bt.get("lines")
        if isinstance(raw_lines, list) and len(raw_lines) >= 2:
            try:
                l1 = max(1, int(raw_lines[0]))
                l2 = max(l1, int(raw_lines[1]))
            except Exception:
                l1, l2 = 1, max_line
        else:
            l1, l2 = 1, max_line
        try:
            secs = max(0.0, float(bt.get("seconds", 8.0)))
        except Exception:
            secs = 8.0
        beats.append({
            "lines": [l1, l2],
            "seconds": secs,
            "event": str(bt.get("event") or f"Beat {idx + 1}").strip(),
        })
    if not beats:
        beats.append({"lines": [1, max_line], "seconds": 10.0, "event": logline})

    episodes: List[Dict[str, Any]] = []
    for idx, ep in enumerate(raw.get("episodes") or []):
        if not isinstance(ep, dict):
            continue
        ep_id = str(ep.get("id") or f"EP{idx + 1:03d}").strip().upper()
        if not re.fullmatch(r"EP[0-9]{3}", ep_id):
            ep_id = f"EP{idx + 1:03d}"
        episodes.append({
            "id": ep_id,
            "title": str(ep.get("title") or title).strip() or title,
            "summary": str(ep.get("summary") or logline).strip() or logline,
            "end_hook": str(ep.get("end_hook") or "강렬한 여운의 결말").strip(),
        })
    if not episodes:
        episodes.append({"id": "EP001", "title": title, "summary": logline, "end_hook": "강렬한 여운의 결말"})

    red_lines = [str(x).strip() for x in (raw.get("red_line_issues") or []) if str(x).strip()]

    bible_out: Dict[str, Any] = {
        "title": title,
        "logline": logline,
        "tone": tone,
        "characters": characters,
        "scenes": scenes,
        "beats": beats,
        "episodes": episodes,
        "red_line_issues": red_lines,
    }
    if props:
        bible_out["props"] = props
    return bible_out


def _normalize_size(val: Any) -> str:
    key = str(val or "").strip().lower()
    if key in _ALLOWED_SIZES:
        return _ALLOWED_SIZES[key]
    for token, mapped in _ALLOWED_SIZES.items():
        if token in key:
            return mapped
    return "medium shot"


def _normalize_angle(val: Any) -> str:
    key = str(val or "").strip().lower()
    if key in _ALLOWED_ANGLES:
        return _ALLOWED_ANGLES[key]
    for token, mapped in _ALLOWED_ANGLES.items():
        if token in key:
            return mapped
    return "eye level"


def _normalize_time_range(val: Any, seg_duration: float) -> str:
    raw = str(val or "").strip()
    if re.fullmatch(r"[0-9]+-[0-9]+s", raw):
        return raw
    nums = re.findall(r"\d+", raw)
    if len(nums) >= 2:
        return f"{nums[0]}-{nums[1]}s"
    return f"0-{max(1, int(round(seg_duration)))}s"


def normalize_breakdown_data(
    raw: Dict[str, Any],
    bible_data: Dict[str, Any],
    episode_id: str = "EP001",
) -> Dict[str, Any]:
    """Sanitize LLM breakdown output so it strictly satisfies schemas/breakdown.schema.json."""
    ep_id = str(raw.get("episode") or episode_id).strip().upper()
    if not re.fullmatch(r"EP[0-9]{3}", ep_id):
        ep_id = episode_id

    title = str(raw.get("title") or bible_data.get("title") or "Short Drama").strip() or "Short Drama"

    valid_char_ids = [c["id"] for c in (bible_data.get("characters") or []) if c.get("id")]
    char_name_to_id: Dict[str, str] = {}
    for c in bible_data.get("characters") or []:
        cid = c.get("id")
        if not cid:
            continue
        char_name_to_id[cid.lower()] = cid
        if c.get("name"):
            char_name_to_id[str(c["name"]).strip().lower()] = cid
        for alias in c.get("aliases") or []:
            if str(alias).strip():
                char_name_to_id[str(alias).strip().lower()] = cid

    valid_scene_ids = [s["id"] for s in (bible_data.get("scenes") or []) if s.get("id")]
    scene_name_to_id: Dict[str, str] = {}
    for s in bible_data.get("scenes") or []:
        sid = s.get("id")
        if not sid:
            continue
        scene_name_to_id[sid.lower()] = sid
        if s.get("name"):
            scene_name_to_id[str(s["name"]).strip().lower()] = sid

    valid_prop_ids = {p["id"] for p in (bible_data.get("props") or []) if p.get("id")}

    def resolve_char(val: Any) -> str:
        s = str(val or "").strip()
        if s.lower() in char_name_to_id:
            return char_name_to_id[s.lower()]
        slug = _slug_id(s, "")
        if slug in char_name_to_id:
            return char_name_to_id[slug]
        return slug or (valid_char_ids[0] if valid_char_ids else "speaker")

    def resolve_scene(val: Any) -> str:
        s = str(val or "").strip()
        if s.lower() in scene_name_to_id:
            return scene_name_to_id[s.lower()]
        slug = _slug_id(s, "")
        if slug in scene_name_to_id:
            return scene_name_to_id[slug]
        return valid_scene_ids[0] if valid_scene_ids else (slug or "main_location")

    segments: List[Dict[str, Any]] = []
    for idx, seg in enumerate(raw.get("segments") or []):
        if not isinstance(seg, dict):
            continue
        seg_id = str(seg.get("id") or f"seg{idx + 1:02d}").strip().lower()
        if not re.fullmatch(r"seg[0-9]+[a-z]*", seg_id):
            seg_id = f"seg{idx + 1:02d}"

        try:
            dur = float(seg.get("duration", 6.0))
            if dur <= 0:
                dur = 6.0
        except Exception:
            dur = 6.0

        location = resolve_scene(seg.get("location"))
        scene_heading = str(seg.get("scene_heading") or f"INT. {location.upper()} - DAY").strip()
        first_state = str(seg.get("first_state") or "Character stands in frame as tension begins.").strip()
        exit_state = str(seg.get("exit_state") or "Character reacts as the beat concludes.").strip()

        seg_chars: List[str] = []
        for c in (seg.get("characters") or []):
            cid = resolve_char(c)
            if cid and cid not in seg_chars:
                seg_chars.append(cid)

        dialogue: List[Dict[str, Any]] = []
        for d in (seg.get("dialogue") or []):
            if not isinstance(d, dict):
                continue
            line_text = str(d.get("line") or "").strip()
            if not line_text:
                continue
            speaker = resolve_char(d.get("speaker"))
            if speaker not in seg_chars and speaker in char_name_to_id.values():
                seg_chars.append(speaker)
            d_entry: Dict[str, Any] = {"speaker": speaker, "line": line_text}
            if isinstance(d.get("offscreen"), bool):
                d_entry["offscreen"] = d["offscreen"]
            dialogue.append(d_entry)

        shots: List[Dict[str, Any]] = []
        for sh in (seg.get("shots") or []):
            if not isinstance(sh, dict):
                continue
            sh_chars: List[str] = []
            for c in (sh["characters"] if isinstance(sh.get("characters"), list) else seg_chars):
                cid = resolve_char(c)
                if cid and cid not in sh_chars:
                    sh_chars.append(cid)
            shot_entry: Dict[str, Any] = {
                "time": _normalize_time_range(sh.get("time"), dur),
                "size": _normalize_size(sh.get("size")),
                "angle": _normalize_angle(sh.get("angle")),
                "camera": str(sh.get("camera") or "50mm F2.0, steady cinematic framing").strip(),
                "visual": str(sh.get("visual") or first_state).strip(),
                "audio": str(sh.get("audio") or "Natural room ambience").strip(),
                "characters": sh_chars,
            }
            mov = str(sh.get("movement") or "").strip().lower()
            if mov in _ALLOWED_MOVEMENTS:
                shot_entry["movement"] = mov
            techs = [t for t in (sh.get("techniques") or []) if t in _ALLOWED_TECHNIQUES]
            if techs:
                shot_entry["techniques"] = list(dict.fromkeys(techs))
            shots.append(shot_entry)

        if not shots:
            shots.append({
                "time": f"0-{max(1, int(round(dur)))}s",
                "size": "medium shot",
                "angle": "eye level",
                "camera": "50mm F2.0, natural push in",
                "visual": first_state,
                "audio": "Ambient sound",
                "characters": seg_chars,
            })

        must_hit = [str(m).strip() for m in (seg.get("must_hit") or []) if str(m).strip()]
        if not must_hit:
            must_hit = [shots[0]["visual"]]

        seg_entry: Dict[str, Any] = {
            "id": seg_id,
            "duration": dur,
            "location": location,
            "scene_heading": scene_heading,
            "characters": seg_chars,
            "first_state": first_state,
            "shots": shots,
            "dialogue": dialogue,
            "must_hit": must_hit,
            "exit_state": exit_state,
        }

        if seg.get("time") and str(seg["time"]).strip():
            seg_entry["time"] = str(seg["time"]).strip()
        raw_states = seg.get('states')
        if isinstance(raw_states, dict):
            states = {}
            for raw_id, state in raw_states.items():
                char_id = resolve_char(raw_id)
                if char_id in seg_chars and isinstance(state, str) and state.strip():
                    states[char_id] = state.strip()
            if states:
                seg_entry['states'] = states
        seg_props = [p for p in _dedupe_slugs(seg.get("props")) if not valid_prop_ids or p in valid_prop_ids]
        if seg_props:
            seg_entry["props"] = seg_props
        if isinstance(seg.get("light_audio"), str) and seg["light_audio"].strip():
            seg_entry["light_audio"] = seg["light_audio"].strip()

        segments.append(seg_entry)

    if not segments:
        raise ValueError("생성된 브레이크다운에 유효한 segments가 없습니다.")

    return {
        "episode": ep_id,
        "title": title,
        "segments": segments,
    }


def build_bible_cli_prompt(screenplay_text: str, title: str, target_duration: Optional[int] = 60) -> str:
    """Wrap templates/bible.md with strict JSON-only CLI execution instructions."""
    base_prompt = generate_bible_prompt(
        screenplay_text,
        title=title,
        brief=(f"9:16 vertical Korean short drama targeting ~{target_duration} seconds." if target_duration else "9:16 vertical Korean short drama; follow the screenplay without inventing a target runtime."),
    )
    return f"""너는 숏폼 드라마 파이프라인의 Stage 1 스토리 바이블 분석가다.
중요 실행 규칙:
- 어떤 파일도 읽거나 수정하지 말고, 어떤 쉘 명령어나 도구도 호출하지 마라.
- 아래 대본을 정확히 분석해서 **오직 유효한 순수 JSON 객체 하나만** 출력하라. 설명 문장이나 마크다운 코드펜스를 붙이지 마라.
- 모든 `id`는 영문 소문자 snake_case(`^[a-z][a-z0-9_]*$`, 예: `ji_hoon`, `cafe_counter`, `broken_watch`)로 작성하라.
- `characters[].appearance`, `characters[].look`, `scenes[].description`, `props[].description`은 힉스필드 이미지 생성 프롬프트에 직접 들어가므로 **구체적이고 시각적인 영어(English)**로 작성하라. (인물 나이·한국인 외모·헤어·의상 색/재질 포함)
- **공간·물리 배치 필수 규칙 (`scenes[].description`, `props[].description` 공통):** 어떤 소품·배경이든 반드시 **상/중/하(`Top -> Middle -> Bottom`), 좌/중/우(`Left -> Center -> Right`), 실제 크기 비례, 현실 물리적 순서**를 영어로 명시하라. (예: 키 눈금·계측기면 큰 숫자가 위·작은 숫자가 아래, 문서·영수증이면 상단 제목→중단 본문→하단 합계, 방·공간이면 좌/중/우 가구 배치와 동선)
- `title`, `logline`, `characters[].name`, `characters[].role`, `characters[].personality`, `characters[].voice`, `scenes[].name`, `props[].name`, `beats[].event`, `episodes[].summary`, `episodes[].end_hook`은 자연스러운 한국어로 작성하라.
- `scenes[].setting`은 반드시 `"interior"` 또는 `"exterior"` 중 하나만 사용하라.
- `characters[].observable`은 화면에 등장하는 인물이면 `true`로 두고 `look`을 반드시 채워라.

[출력 JSON 최상위 구조 (추가 키 금지)]
{{
  "title": "작품 제목",
  "logline": "한두 문장 로그라인",
  "tone": "Visual & emotional tone in English",
  "characters": [
    {{
      "id": "english_snake_case_id",
      "name": "한국어 이름",
      "aliases": ["별칭"],
      "role": "역할 설명",
      "personality": "성격",
      "appearance": "Detailed English physical appearance for image generation",
      "voice": "목소리 톤",
      "observable": true,
      "look": "Detailed English wardrobe and outfit description"
    }}
  ],
  "scenes": [
    {{
      "id": "english_snake_case_scene_id",
      "name": "한국어 장소명",
      "description": "Detailed English environment, lighting, props, mood",
      "setting": "interior",
      "time": "Day"
    }}
  ],
  "props": [
    {{
      "id": "english_snake_case_prop_id",
      "name": "핵심 소품 이름",
      "description": "Detailed English visual description of the key prop"
    }}
  ],
  "beats": [
    {{"lines": [1, 10], "seconds": 8, "event": "핵심 사건 요약"}}
  ],
  "episodes": [
    {{"id": "EP001", "title": "1화 제목", "summary": "요약", "end_hook": "엔딩 여운"}}
  ],
  "red_line_issues": []
}}

[Stage 1 템플릿 지침]
{base_prompt}
"""


def build_breakdown_cli_prompt(
    episode_id: str,
    screenplay_text: str,
    bible_data: Dict[str, Any],
    brief: str,
    target_duration: Optional[int] = 60,
    feedback: str = "",
    existing_breakdown: Optional[Dict[str, Any]] = None,
) -> str:
    """Wrap templates/breakdown.md with strict JSON-only CLI execution instructions."""
    base_prompt = generate_breakdown_prompt(
        episode_id=episode_id,
        script_text=screenplay_text,
        bible_data=bible_data,
        brief=brief,
        min_seconds=5,
        max_seconds=15,
        feedback=feedback,
    )
    existing_block = ""
    if existing_breakdown:
        existing_block = f"\n[기존 브레이크다운 (피드백을 반영해 수정하라)]\n{json.dumps(existing_breakdown, ensure_ascii=False, indent=2)}\n"

    char_ids = [c["id"] for c in bible_data.get("characters", [])]
    scene_ids = [s["id"] for s in bible_data.get("scenes", [])]
    prop_ids = [p["id"] for p in bible_data.get("props", [])]
    runtime_instruction = (f"전체 길이는 목표 약 {target_duration}초 (±15% 이내)에 맞추되 대사를 삭제하거나 시간을 억지로 채우지 마라." if target_duration else "전체 길이 목표는 지정되지 않았다. 대본의 대사와 사건을 보존하고 자연스러운 길이로 구성하라.")

    return f"""너는 숏폼 드라마 파이프라인의 Stage 3 에피소드 브레이크다운 감독이다.
중요 실행 규칙:
- 어떤 파일도 읽거나 수정하지 말고, 어떤 쉘 명령어나 도구도 호출하지 마라.
- **오직 유효한 순수 JSON 객체 하나만** 출력하라. 설명 문장이나 마크다운 코드펜스를 붙이지 마라.
- Seedance 2.0 I2V: 5~8초를 출발점으로 검토하되 고정하지 마라. 자연스러운 대사·반응·의미 있는 구도 변화에 더 필요할 때만 9~15초를 사용하라. 침묵이나 의미 없는 동작으로 시간을 채우지 말고, 말이 끝나기 전에 자르거나 급하게 말하게 하지 마라. `duration`은 `5`~`15`초 정수이며 15초를 초과하지 마라. {runtime_instruction}
- `location`은 반드시 다음 `bible.scenes` ID 중 하나만 사용하라: {scene_ids}
- `characters`와 `dialogue[].speaker`는 반드시 다음 `bible.characters` ID 중 하나만 사용하라: {char_ids}
- 대사 정확도를 위해 짧고 완결된 발화를 우선하되 자연스러운 시간 여유가 있다면 짧은 대화나 화면 밖 대사와 리액션도 허용한다. 화자와 발화 구간을 명확히 표시하고 음성을 겹치지 마라. 여러 대사나 행동이 과밀하면 생략·반복 없이 문장 경계에서 나눠라.
- 의미적으로 한 호흡이 중요한 두 사람의 대화는 무조건 인물별로 자르지 말고 9~15초 또는 필요한 실제 길이로 한 세그먼트에 배치하는 선택지를 적극 검토하라. 두 사람의 짧고 완결된 대사를 순서대로 배치하고 화자별 발화 시간, 듣는 사람의 표정·시선, 투샷/클로즈업/반응 컷과 필요 시 push in을 지정하라. 길이는 대사와 반응에 맞추고 15초를 채우려고 늘리지 마라. 화자·입모양·발화 누락 위험이 높거나 대사가 과밀하면 문장 경계에서 나누며 후속 STT와 실제 영상을 검수하라.
- 발화가 없는 샷·독립 행동 시간과 말 시작 및 끝의 여유(합계 약 1초 이상)를 빼고 남은 실제 발화 가능 시간으로 대사 분량을 추정하라. 예를 들어 6초 중 앞 2초가 무대사 행동이면 전체 6초가 아니라 나머지 4초에서 여유를 뺀 시간을 기준으로 한다. 발화 시작을 지나치게 늦추거나 마지막 단어를 영상 끝에 붙이지 말고, 분량이 불안하면 대사를 생략하지 말고 문장 경계에서 씬을 나눠라. `shots[].audio`에 어느 구간에서 누가 발화하는지 기록하라.
- 매 세그먼트마다 무조건 컷을 바꾸지 말고 연속 촬영과 새 구도를 섞어라. 하나의 행동이나 같은 화자의 연속 발화가 자연스럽게 이어지는 경우 인접 세그먼트의 실제 화면 인물, 샷 크기·각도·카메라 시점이 같게 설계하고 앞 exit_state를 뒤 first_state로 물리적으로 이어라. 반응 컷, 증거 클로즈업, 장소·시점 전환에는 새 구도를 사용하되 소품과 인물의 상태는 이어라. 임의로 연속 컷을 억지로 만들지 마라.
- `shots[0].characters`에는 이 컷의 **첫 프레임에 실제로 얼굴이 보이는 인물만** 정확히 적어라.
- 같은 장소·시간의 인물이 이어지면 `states`에 등장인물 ID별 시작 자세, 양손, 쥔 소품과 문서 위치·복장 상태를 구체적으로 기록하고 직전 `exit_state`와 맞춰라. 중요한 손·소품은 다른 앵글에서도 확인할 수 있게 첫 프레임에 포함하라. 손을 뗄 계획이면 그 행동을 명시하고, 장소·시간이 바뀌면 상황에 맞는 새 상태를 써라.
- 펜·도장, 서류·봉투·테이프처럼 손으로 조작하는 소품은 집기, 내려놓기, 교체, 닫기 등 물리적 접촉과 순서를 명확히 쓰고, 5~15초 씬에 여러 정교한 조작이 몰리면 필요한 만큼 연속 씬으로 나눠라. 화면 밖에서 갑자기 소품이 바뀌거나 저절로 움직이는 지시는 피하라.
- 드라마 연출: 대치 인물과 가구의 공간 위치, 카메라 위치·높이, 상대를 향한 시선(eyeline)과 좌우 화면 방향을 명시하라. 구도를 바꿔도 공간축을 유지하고 대치에 맞는 투샷·어깨너머·클로즈업을 선택하라. 말 이후 작은 표정 변화와 반응 시간을 확보하고 불필요한 카메라 움직임은 지시하지 마라.
- 각 샷에 화면 속 인물의 좌우·전후 배치와 상대 및 고정 배경물까지의 거리, 몸의 방향, 시선 대상을 구체적으로 쓰고 관계와 감정에 맞게 투샷·어깨너머·클로즈업을 고르라. 클로즈업의 화면 밖 상대를 향한 시선도 유지하라. 시작 표정과 시선 → 대사나 행동 → 상대 또는 본인의 작은 표정 변화와 반응을 시간에 따라 묘사하라. 역구도에서도 공간축과 인물 배치를 유지하고 축을 넘을 때는 전환을 명시하라. 한 영상에 여러 무관한 카메라 전환을 몰지 마라.
- 첫 장면을 반드시 와이드로 시작하지 말고 갈등이 가장 잘 보이는 구도를 선택하라. 한 인물이 말하는 중에도 감정 변화가 필요하면 서서히 push in, pull back, pan, follow 또는 명확한 반응 컷을 계획하라. 표정이 중요한 장면에는 얼굴 close-up을 적극 검토하고, 전체를 고정 카메라나 미디엄 클로즈업만 반복하지 마라. 과장된 무빙·필요 없는 컷은 피하라.
- 화자가 보이지 않아도 상대의 표정이 중요하면 듣는 사람의 클로즈업 위로 화면 밖 대사를 사용하라. 이 대사는 `dialogue`에 실제 화자 ID와 `offscreen: true`를 넣고 정확히 한 번만 기록하며, `shots[].characters`에는 화면에 실제 보이는 인물만 적어라. `shots[].audio`에 화면 밖 화자와 발화 시간을, `visual`에 듣는 사람의 시선과 표정 변화를 명시하라. 화면 속 인물이 대신 말하거나 대사가 겹치지 않게 하라. 한 영상 안의 구도 전환은 시간 구간과 연결 관계를 명확히 쓰고 대사 중복·누락을 피하라.
- `props`는 다음 `bible.props` ID 중에서만 선택하되, **이 컷의 첫 프레임(`first_state` / `shots[0].visual`)에서 실제로 손에 들리거나 화면 중심에 포커스되는 핵심 소품만 최대 1~2개** 넣어라. (이미 배경 장소에 기본 배치된 가구·가전이나, 이 컷에서 직접 쓰이지 않는 소품, 아직 정체가 공개되기 전인 스포일러 소품은 절대 `props`에 넣지 마라!)
- `shots[].size`는 반드시 `"extreme wide shot"`, `"wide shot"`, `"medium shot"`, `"medium close-up"`, `"close-up"`, `"extreme close-up"` 중 하나.
- `shots[].angle`은 반드시 `"eye level"`, `"high angle"`, `"low angle"`, `"overhead shot"` 중 하나.
- `shots[].time`은 반드시 `"0-6s"` 같은 `숫자-숫자s` 형식.
- `first_state`, `exit_state`, `shots[].visual`, `shots[].camera`, `shots[].audio`, `must_hit`은 키프레임/영상 생성용으로 **구체적이고 시각적인 영어(English)**로 작성하고, `dialogue[].line`은 자연스러운 **한국어 대사**로 작성하라. (각 세그먼트 길이에 맞게 한국어 발화 분량 과밀 방지!)

[출력 JSON 최상위 구조 (추가 키 금지)]
{{
  "episode": "{episode_id}",
  "title": "{bible_data.get('title', 'Short Drama')}",
  "segments": [
    {{
      "id": "seg01",
      "duration": 8,
      "location": "{scene_ids[0] if scene_ids else 'main_location'}",
      "scene_heading": "INT. LOCATION - DAY",
      "characters": {json.dumps(char_ids[:2], ensure_ascii=False)},
      "states": {{"{char_ids[0] if char_ids else 'lead'}": "Opening body, hands, held props and clothing state"}},
      "props": {json.dumps(prop_ids[:1], ensure_ascii=False)},
      "first_state": "Concrete opening visual frame state in English",
      "shots": [
        {{
          "time": "0-8s",
          "size": "medium close-up",
          "angle": "eye level",
          "movement": "push in",
          "camera": "Slow motivated push in at eye level as the expression changes",
          "visual": "Concrete visual action in English",
          "audio": "Ambient audio and foley in English",
          "characters": {json.dumps(char_ids[:2], ensure_ascii=False)}
        }}
      ],
      "dialogue": [
        {{"speaker": "{char_ids[0] if char_ids else 'lead'}", "line": "짧고 임팩트 있는 한국어 대사"}}
      ],
      "must_hit": ["Key visual beat that must appear in this segment"],
      "exit_state": "Concrete end frame visual state for smooth handoff to next segment"
    }}
  ]
}}
{existing_block}
[Stage 3 템플릿 지침]
{base_prompt}
"""


def process_bible_and_breakdown_request(
    bridge_req: Dict[str, Any],
    bridge_dir: Path,
    outputs_dir: Path,
    log_fn: Optional[Callable[[str], None]] = None,
    runner: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """Generate both Stage 1 bible.json and Stage 3 breakdown.json from screenplay via Antigravity CLI."""
    log = log_fn or (lambda msg: None)
    run_llm = runner or partial(run_antigravity_prompt, log_fn=log)

    project = str(bridge_req.get("project") or "sample_drama")
    episode_id = str(bridge_req.get("episode") or "EP001")
    proj_dir = outputs_dir / project

    state = _update_inbox_state(bridge_dir, bridge_req, "running")
    try:
        screenplay_file = None
        for ext in (".fountain", ".txt", ".md"):
            cand = proj_dir / f"screenplay{ext}"
            if cand.exists():
                screenplay_file = cand
                break
        if not screenplay_file:
            raise FileNotFoundError(f"대본 파일(screenplay.fountain)을 찾을 수 없습니다: {proj_dir}")

        title, script_text, line_count = parse_screenplay(screenplay_file)
        if not script_text.strip():
            raise ValueError("대본 내용이 비어 있습니다. 먼저 대본을 생성하거나 작성해 주세요.")

        target_duration = load_target_duration(proj_dir)

        # 1. Stage 1: Story Bible
        log(f"📖 [Antigravity Bridge] 1/2단계: '{title}' 대본 분석 및 스토리 바이블(인물/장소/소품) 생성 중...")
        bible_prompt = build_bible_cli_prompt(script_text, title=title, target_duration=target_duration)
        (proj_dir / "stage1_bible_prompt.md").write_text(bible_prompt, encoding="utf-8")

        raw_bible = run_llm(bible_prompt)
        bible_parsed = extract_json_object(raw_bible)
        bible_data = normalize_bible_data(bible_parsed, fallback_title=title, line_count=line_count)
        bible_path = save_bible(proj_dir, bible_data)
        log(
            f"✅ [Stage 1 완료] 바이블 저장 ({bible_path.name}): "
            f"인물 {len(bible_data['characters'])}명, 장소 {len(bible_data['scenes'])}곳, 소품 {len(bible_data.get('props', []))}개"
        )

        # Prepare asset templates right away so Tab 3 is immediately ready
        prepare_assets(proj_dir, bible_data)

        # 2. Stage 3: Episode Breakdown
        duration_label = f'약 {target_duration}초 목표' if target_duration else '전체 목표 길이 미지정'
        log(f"🎬 [Antigravity Bridge] 2/2단계: {duration_label} 에피소드 브레이크다운(5~15초 컷 분할) 생성 중...")
        ep_dir = ensure_dir(proj_dir / "episodes" / episode_id)
        brief = breakdown_brief(proj_dir)
        breakdown_prompt = build_breakdown_cli_prompt(
            episode_id=episode_id,
            screenplay_text=script_text,
            bible_data=bible_data,
            brief=brief,
            target_duration=target_duration,
        )
        (ep_dir / "stage3_breakdown_prompt.md").write_text(breakdown_prompt, encoding="utf-8")

        raw_breakdown = run_llm(breakdown_prompt)
        breakdown_parsed = extract_json_object(raw_breakdown)
        breakdown_data = normalize_breakdown_data(breakdown_parsed, bible_data, episode_id=episode_id)
        breakdown_path = save_breakdown(proj_dir, episode_id, breakdown_data)
        total_sec = sum(float(s.get("duration", 0)) for s in breakdown_data["segments"])
        log(
            f"✅ [Stage 3 완료] 브레이크다운 저장 ({breakdown_path.name}): "
            f"총 {len(breakdown_data['segments'])}세그먼트 (약 {total_sec:.0f}초)"
        )

        state = _update_inbox_state(
            bridge_dir,
            bridge_req,
            "completed",
            bible_path=str(bible_path),
            breakdown_path=str(breakdown_path),
            segment_count=len(breakdown_data["segments"]),
        )
        return {"state": state, "bible": bible_data, "breakdown": breakdown_data}
    except Exception as exc:
        state = _update_inbox_state(bridge_dir, bridge_req, "error", error=str(exc))
        log(f"❌ [Antigravity Bridge] 바이블/브레이크다운 생성 실패: {exc}")
        raise


def process_breakdown_feedback_request(
    bridge_req: Dict[str, Any],
    bridge_dir: Path,
    outputs_dir: Path,
    log_fn: Optional[Callable[[str], None]] = None,
    runner: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """Revise episodes/<ep>/breakdown.json using user feedback via Antigravity CLI."""
    log = log_fn or (lambda msg: None)
    run_llm = runner or partial(run_antigravity_prompt, log_fn=log)

    project = str(bridge_req.get("project") or "sample_drama")
    episode_id = str(bridge_req.get("episode") or "EP001")
    feedback = str(bridge_req.get("feedback") or "").strip()
    proj_dir = outputs_dir / project

    state = _update_inbox_state(bridge_dir, bridge_req, "running")
    try:
        if not feedback:
            raise ValueError("브레이크다운 수정 피드백 내용이 비어 있습니다.")

        bible_path = proj_dir / "bible.json"
        if not bible_path.exists():
            raise FileNotFoundError("bible.json이 없습니다. 먼저 바이블 & 브레이크다운 자동 생성을 실행해 주세요.")
        bible_data = read_json(bible_path)

        screenplay_text = ""
        for ext in (".fountain", ".txt", ".md"):
            cand = proj_dir / f"screenplay{ext}"
            if cand.exists():
                _, screenplay_text, _ = parse_screenplay(cand)
                break

        ep_dir = ensure_dir(proj_dir / "episodes" / episode_id)
        bd_path = ep_dir / "breakdown.json"
        existing_bd = read_json(bd_path) if bd_path.exists() else None
        target_duration = load_target_duration(proj_dir)

        log(f"🔄 [Antigravity Bridge] 브레이크다운 피드백 반영 재생성 중: {feedback[:60]}")
        prompt = build_breakdown_cli_prompt(
            episode_id=episode_id,
            screenplay_text=screenplay_text,
            bible_data=bible_data,
            brief=breakdown_brief(proj_dir),
            target_duration=target_duration,
            feedback=feedback,
            existing_breakdown=existing_bd,
        )
        raw_bd = run_llm(prompt)
        parsed_bd = extract_json_object(raw_bd)
        breakdown_data = normalize_breakdown_data(parsed_bd, bible_data, episode_id=episode_id)
        saved_path = save_breakdown(proj_dir, episode_id, breakdown_data)

        state = _update_inbox_state(
            bridge_dir,
            bridge_req,
            "completed",
            breakdown_path=str(saved_path),
            segment_count=len(breakdown_data["segments"]),
        )
        log(f"✅ [Antigravity Bridge] 브레이크다운 수정 반영 완료! ({len(breakdown_data['segments'])}세그먼트)")
        return {"state": state, "breakdown": breakdown_data}
    except Exception as exc:
        state = _update_inbox_state(bridge_dir, bridge_req, "error", error=str(exc))
        log(f"❌ [Antigravity Bridge] 브레이크다운 수정 실패: {exc}")
        raise
