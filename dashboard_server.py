#!/usr/bin/env python3
"""
Short Drama Production Studio Dashboard Server
- Google Minimal Aesthetic (Zero purple, zero neon)
- Bi-directional bridge with Antigravity / Gemini CLI
- Project switcher, Stage 1~6 visual orchestrator, multi-model Higgsfield selector
"""

import os
import sys
import json
import time
import base64
import subprocess
import threading
import tempfile
from pathlib import Path
from PIL import Image
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

from pipeline.common import load_config, ensure_dir, read_json, write_json, resolve_binary
from pipeline.stage1_bible import parse_screenplay, generate_bible_prompt, save_bible
from pipeline.stage2_assets import prepare_assets, slice_master_sheet, resolve_asset_image, validate_master_assets, slice_project_master_assets, write_character_sheet_manifest, character_sheet_manifest_path, character_sheet_pending_path
from pipeline.stage3_breakdown import generate_breakdown_prompt, save_breakdown, read_breakdown
from pipeline.stage4_keyframes import prepare_keyframe_tasks, resolve_segment_keyframe, render_state_reference_keyframe
from pipeline.stage5_higgsfield import prepare_video_tasks, estimate_credits, load_continuity_modes, save_continuity_modes, resolve_take_start_image, validated_video_duration, build_continuity_video_inputs, continuity_video_args, continuity_video_instruction, continuity_frame_status
from pipeline.continuity import ensure_continuity_plan, load_continuity_plan, assert_continuity_ready
from pipeline.stage_audio import prepare_dialogue_audio_tasks
from pipeline.stage6_cut import build_final_cut, review_single_take
from pipeline.story_review import review_breakdown
from pipeline.proposal_review import review_proposals
from pipeline.story_runtime import normalize_target_duration, save_target_duration, load_target_duration
from pipeline.antigravity_bridge import (
    find_antigravity_cli,
    process_synopsis_request,
    process_full_script_request,
    process_script_feedback_request,
    process_bible_and_breakdown_request,
    process_breakdown_feedback_request,
)

PORT = 8899
BASE_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = BASE_DIR / "outputs"
BRIDGE_DIR = BASE_DIR / "outputs" / ".antigravity_bridge"
STORY_GUIDANCE = (BASE_DIR / "templates" / "story-guidance.md").read_text(encoding="utf-8")
ensure_dir(BRIDGE_DIR)

BRIDGE_LOCK = threading.Lock()
BRIDGE_REQUESTS = {}

# Global in-memory job queue for serial generation
JOB_STATUS = {
    "current_job": None,
    "progress": 0,
    "logs": [],
    "error": None,
    "error_id": None,
    "recent_bridge_request": None
}


def build_full_script_request(body, guidance=STORY_GUIDANCE, timestamp=None):
    """Preserve the dashboard's runtime choice in the external script request."""
    target_duration = normalize_target_duration(body.get("target_duration", 60))
    now = time.time() if timestamp is None else timestamp
    return {
        "id": f"full_script_{int(now)}",
        "type": "generate_full_script",
        "project": body.get("project"),
        "synopsis_index": body.get("synopsis_index", 0),
        "feedback": body.get("feedback", ""),
        "target_duration": target_duration,
        "story_guidance": f"이 대본의 목표 길이: 약 {target_duration}초. 사건 수와 대사량을 이 길이에 맞추세요.\n\n{guidance}",
        "status": "pending",
        "timestamp": now,
    }

def execute_higgsfield_with_retry(cmd, max_retries=3, backoff_seconds=5):
    """Higgsfield CLI 실행 및 일시적 520, 502, 503, upload_url 오류 시 자동 재시도"""
    last_res = None
    for attempt in range(1, max_retries + 1):
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(BASE_DIR))
        last_res = res
        JOB_STATUS["logs"].extend([l.strip() for l in res.stdout.splitlines() if l.strip()])
        if res.returncode == 0:
            return res
        is_transient = any(err in res.stdout for err in ["520", "502", "503", "504", "failed to PUT", "upload_url", "Connection reset", "timed out"])
        if is_transient and attempt < max_retries:
            wait_time = backoff_seconds * attempt
            JOB_STATUS["logs"].append(f"⚠️ 일시적 Higgsfield 업로드/서버 오류 감지 ({res.stdout.strip()[:100]}). {wait_time}초 후 자동 재시도합니다... (시도 {attempt}/{max_retries})")
            time.sleep(wait_time)
            continue
        break
    return last_res

class DashboardHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Serve Dashboard HTML directly at root
        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html_path = BASE_DIR / "dashboard" / "index.html"
            if html_path.exists():
                self.wfile.write(html_path.read_bytes())
            else:
                self.wfile.write(b"<h1>Dashboard HTML not found</h1>")
            return

        # Static media files inside outputs/
        if path.startswith("/outputs/"):
            rel_path = urllib.parse.unquote(path[len("/outputs/"):].split("?")[0])
            file_path = (OUTPUTS_DIR / rel_path).resolve()
            outputs_root = OUTPUTS_DIR.resolve()
            if str(file_path).startswith(str(outputs_root)) and file_path.exists() and file_path.is_file():
                import mimetypes
                mime_type, _ = mimetypes.guess_type(str(file_path))
                self.send_response(200)
                self.send_header("Content-Type", mime_type or "application/octet-stream")
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-cache, must-revalidate")
                self.end_headers()
                with open(file_path, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        # API Endpoints
        if path == "/api/projects":
            self.send_json_response(self.get_projects())
            return
        elif path == "/api/project_data":
            project = query.get("project", ["sample_drama"])[0]
            self.send_json_response(self.get_project_data(project))
            return
        elif path == "/api/models":
            self.send_json_response(self.get_available_models())
            return
        elif path == "/api/job_status":
            self.send_json_response(JOB_STATUS)
            return
        elif path == "/api/bridge/status":
            req_id = query.get("id", [None])[0]
            cli = find_antigravity_cli()
            inbox_path = BRIDGE_DIR / "inbox.json"
            inbox_data = read_json(inbox_path) if inbox_path.exists() else None
            target_req = BRIDGE_REQUESTS.get(req_id) if req_id else JOB_STATUS.get("recent_bridge_request")
            if not target_req and inbox_data and (not req_id or inbox_data.get("id") == req_id):
                target_req = inbox_data
            self.send_json_response({
                "connected": bool(cli),
                "cli": cli,
                "request": target_req,
                "inbox": inbox_data,
            })
            return

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(length) if length > 0 else b'{}'
        try:
            body = json.loads(post_data.decode('utf-8'))
        except Exception:
            body = {}

        if path == "/api/switch_project":
            project_name = body.get("project")
            self.send_json_response({"success": True, "project": project_name})
            return

        elif path == "/api/create_project":
            project_name = body.get("name", "").strip().replace(" ", "_")
            if not project_name:
                self.send_json_response({"success": False, "error": "Invalid name"}, status=400)
                return
            p_dir = OUTPUTS_DIR / project_name
            try:
                p_dir.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                self.send_json_response({"success": False, "error": "같은 이름의 프로젝트가 이미 있습니다. 다른 이름을 사용해 주세요."}, status=409)
                return
            (p_dir / "screenplay.fountain").write_text("Title: New Short Drama\n\nEXT. STREET - NIGHT\n\n", encoding="utf-8")
            self.send_json_response({"success": True, "project": project_name})
            return

        elif path == "/api/save_screenplay":
            project = body.get("project")
            content = body.get("content", "")
            p_dir = OUTPUTS_DIR / project
            if p_dir.exists():
                screenplay = p_dir / "screenplay.fountain"
                if screenplay.exists():
                    previous = screenplay.read_text(encoding="utf-8")
                    if previous != content:
                        backup_dir = ensure_dir(p_dir / ".screenplay_backups")
                        (backup_dir / f"screenplay_{time.time_ns()}.fountain").write_text(previous, encoding="utf-8")
                screenplay.write_text(content, encoding="utf-8")
                self.send_json_response({"success": True})
            else:
                self.send_json_response({"success": False, "error": "Project not found"}, status=404)
            return

        elif path == "/api/save_bible":
            project = body.get("project")
            bible_data = body.get("bible_data", {})
            p_dir = OUTPUTS_DIR / project
            if p_dir.exists():
                save_bible(p_dir, bible_data)
                self.send_json_response({"success": True})
            else:
                self.send_json_response({"success": False, "error": "Project not found"}, status=404)
            return

        elif path == "/api/save_breakdown":
            project = body.get("project")
            episode = body.get("episode", "EP001")
            breakdown_data = body.get("breakdown_data", {})
            p_dir = OUTPUTS_DIR / project
            if p_dir.exists():
                save_breakdown(p_dir, episode, breakdown_data)
                self.send_json_response({"success": True})
            else:
                self.send_json_response({"success": False, "error": "Project not found"}, status=404)
            return

        elif path == "/api/bridge/request_synopsis":
            # Handled via Antigravity Bridge!
            url = body.get("url", "")
            project = body.get("project", "sample_drama")
            notes = body.get("notes", "")
            bridge_req = {
                "id": f"synopsis_{int(time.time() * 1000)}",
                "type": "synopsis_proposal",
                "url": url,
                "project": project,
                "notes": notes,
                "story_guidance": STORY_GUIDANCE,
                "status": "pending",
                "timestamp": time.time()
            }
            write_json(BRIDGE_DIR / "inbox.json", bridge_req)
            BRIDGE_REQUESTS[bridge_req["id"]] = dict(bridge_req)
            JOB_STATUS["recent_bridge_request"] = dict(bridge_req)
            threading.Thread(
                target=self.execute_bridge_task,
                args=(bridge_req,),
                daemon=True,
            ).start()
            self.send_json_response({"success": True, "message": "Dispatched to Antigravity Bridge", "request": bridge_req})
            return

        elif path == "/api/bridge/apply_synopsis":
            # User picks synopsis option 1, 2, or 3
            bridge_req = build_full_script_request(body)
            project_name = bridge_req.get("project")
            if project_name:
                project_dir = OUTPUTS_DIR / str(project_name)
                if project_dir.is_dir():
                    save_target_duration(project_dir, bridge_req["target_duration"])
            write_json(BRIDGE_DIR / "inbox.json", bridge_req)
            BRIDGE_REQUESTS[bridge_req["id"]] = dict(bridge_req)
            JOB_STATUS["recent_bridge_request"] = dict(bridge_req)
            threading.Thread(
                target=self.execute_bridge_task,
                args=(bridge_req,),
                daemon=True,
            ).start()
            self.send_json_response({"success": True, "message": "Dispatched to Antigravity Bridge", "request": bridge_req})
            return

        elif path == "/api/bridge/feedback":
            project_name = body.get("project", "sample_drama")
            feedback = body.get("feedback", "").strip()
            if not feedback:
                self.send_json_response({"success": False, "error": "피드백 내용을 입력해 주세요."}, status=400)
                return
            proj_dir = OUTPUTS_DIR / str(project_name)
            target_duration = load_target_duration(proj_dir) if proj_dir.is_dir() else 60
            now = time.time()
            bridge_req = {
                "id": f"feedback_{int(now * 1000)}",
                "type": "script_feedback",
                "project": project_name,
                "feedback": feedback,
                "target_duration": target_duration,
                "story_guidance": STORY_GUIDANCE,
                "status": "pending",
                "timestamp": now,
            }
            write_json(BRIDGE_DIR / "inbox.json", bridge_req)
            BRIDGE_REQUESTS[bridge_req["id"]] = dict(bridge_req)
            JOB_STATUS["recent_bridge_request"] = dict(bridge_req)
            threading.Thread(
                target=self.execute_bridge_task,
                args=(bridge_req,),
                daemon=True,
            ).start()
            self.send_json_response({"success": True, "message": "Dispatched feedback to Antigravity Bridge", "request": bridge_req})
            return

        elif path == "/api/bridge/generate_bible_breakdown":
            project_name = body.get("project", "sample_drama")
            episode_id = body.get("episode", "EP001")
            now = time.time()
            bridge_req = {
                "id": f"bible_breakdown_{int(now * 1000)}",
                "type": "generate_bible_breakdown",
                "project": project_name,
                "episode": episode_id,
                "status": "pending",
                "timestamp": now,
            }
            write_json(BRIDGE_DIR / "inbox.json", bridge_req)
            BRIDGE_REQUESTS[bridge_req["id"]] = dict(bridge_req)
            JOB_STATUS["recent_bridge_request"] = dict(bridge_req)
            threading.Thread(
                target=self.execute_bridge_task,
                args=(bridge_req,),
                daemon=True,
            ).start()
            self.send_json_response({"success": True, "message": "Dispatched Bible & Breakdown generation to Antigravity Bridge", "request": bridge_req})
            return

        elif path == "/api/bridge/breakdown_feedback":
            project_name = body.get("project", "sample_drama")
            episode_id = body.get("episode", "EP001")
            feedback = body.get("feedback", "").strip()
            if not feedback:
                self.send_json_response({"success": False, "error": "수정 피드백 내용을 입력해 주세요."}, status=400)
                return
            now = time.time()
            bridge_req = {
                "id": f"breakdown_fb_{int(now * 1000)}",
                "type": "breakdown_feedback",
                "project": project_name,
                "episode": episode_id,
                "feedback": feedback,
                "status": "pending",
                "timestamp": now,
            }
            write_json(BRIDGE_DIR / "inbox.json", bridge_req)
            BRIDGE_REQUESTS[bridge_req["id"]] = dict(bridge_req)
            JOB_STATUS["recent_bridge_request"] = dict(bridge_req)
            threading.Thread(
                target=self.execute_bridge_task,
                args=(bridge_req,),
                daemon=True,
            ).start()
            self.send_json_response({"success": True, "message": "Dispatched breakdown feedback to Antigravity Bridge", "request": bridge_req})
            return

        elif path == "/api/continuity":
            project = body.get("project", "")
            segment_id = body.get("segment_id", "")
            mode = body.get("mode")
            proj_dir = OUTPUTS_DIR / project
            breakdown_file = proj_dir / "episodes" / "EP001" / "breakdown.json"
            try:
                segments = read_json(breakdown_file).get("segments", [])
                ids = [seg.get("id") for seg in segments]
                if segment_id not in ids or ids.index(segment_id) == 0:
                    raise ValueError("연결할 이전 장면이 없습니다.")
                if mode not in ("auto", "keyframe", "previous_frame", "state_reference"):
                    raise ValueError("연결 방식이 올바르지 않습니다.")
                modes = load_continuity_modes(proj_dir, "EP001")
                if mode == "auto":
                    modes.pop(segment_id, None)
                else:
                    modes[segment_id] = mode
                save_continuity_modes(proj_dir, "EP001", modes)
                self.send_json_response({"success": True})
            except Exception as e:
                self.send_json_response({"success": False, "error": str(e)})
            return

        elif path == "/api/run_stage":
            project = body.get("project")
            stage = str(body.get("stage"))
            model = body.get("model")
            image_model = body.get("image_model")
            skip_existing = bool(body.get("skip_existing", False))
            threading.Thread(
                target=self.execute_stage_task,
                args=(project, stage, model, skip_existing, image_model),
                daemon=True,
            ).start()
            mode_label = " (미생성 항목만 이어하기)" if skip_existing else ""
            self.send_json_response({"success": True, "message": f"Stage {stage} execution queued{mode_label}"})
            return

        elif path == "/api/run_auto_pipeline":
            project = body.get("project")
            img_model = body.get("img_model")
            vid_model = body.get("vid_model")
            threading.Thread(target=self.execute_auto_pipeline_task, args=(project, img_model, vid_model), daemon=True).start()
            self.send_json_response({"success": True, "message": "Full automated pipeline execution queued"})
            return

        elif path == "/api/slice_master_sheet":
            project = body.get("project")
            asset_type = body.get("asset_type", "characters")
            proj_dir = OUTPUTS_DIR / project
            bible_file = proj_dir / "bible.json"
            if not bible_file.exists():
                self.send_json_response({"success": False, "error": "No bible.json found"}, status=400)
                return
            bible = read_json(bible_file)
            items = bible.get(asset_type, [])
            master_img = proj_dir / "assets" / f"master_{asset_type}_sheet.png"
            if not master_img.exists():
                self.send_json_response({"success": False, "error": f"{master_img.name} does not exist"}, status=400)
                return
            try:
                res = slice_project_master_assets(proj_dir, bible, asset_type)
                self.send_json_response({"success": True, "sliced": res})
            except Exception as exc:
                self.send_json_response({"success": False, "error": str(exc)}, status=400)
            return

        elif path == "/api/override_asset":
            project = body.get("project")
            asset_id = body.get("asset_id")
            asset_type = body.get("asset_type", "characters")
            data_url = body.get("image_data", "")
            if not project or not asset_id or not data_url:
                self.send_json_response({"success": False, "error": "필수 파라미터가 누락되었습니다."}, status=400)
                return
            try:
                raw_b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
                img_bytes = base64.b64decode(raw_b64)
                target_dir = ensure_dir(OUTPUTS_DIR / project / "assets" / asset_type / asset_id)
                filename = "base.png" if asset_type == "scenes" else "sheet.png"
                target_path = target_dir / filename
                target_path.write_bytes(img_bytes)
                if asset_type == "characters":
                    # Also keep face.png up to date when user manually overrides character sheet
                    (target_dir / "face.png").write_bytes(img_bytes)
                self.send_json_response({"success": True, "path": str(target_path)})
            except Exception as exc:
                self.send_json_response({"success": False, "error": str(exc)}, status=500)
            return

        elif path == "/api/reroll_keyframe":
            project = body.get("project")
            segment_id = body.get("segment_id")
            model = body.get("model")
            if not project or not segment_id:
                self.send_json_response({"success": False, "error": "project와 segment_id가 필요합니다."}, status=400)
                return
            threading.Thread(
                target=self.execute_single_keyframe_task,
                args=(project, segment_id, model),
                daemon=True,
            ).start()
            self.send_json_response({"success": True, "message": f"{segment_id} 키프레임 단독 리롤이 시작되었습니다."})
            return

        elif path == "/api/rerender_take":
            project = body.get("project")
            segment_id = body.get("segment_id")
            model = body.get("model")
            image_model = body.get("image_model")
            if not project or not segment_id:
                self.send_json_response({"success": False, "error": "project와 segment_id가 필요합니다."}, status=400)
                return
            threading.Thread(
                target=self.execute_single_take_task,
                args=(project, segment_id, model, image_model),
                daemon=True,
            ).start()
            self.send_json_response({"success": True, "message": f"{segment_id} 비디오 단독 렌더링이 시작되었습니다."})
            return

        self.send_json_response({"error": "Not Found"}, status=404)

    def default_serializer(self, obj):
        if isinstance(obj, Path):
            return str(obj)
        raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

    def send_json_response(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=self.default_serializer).encode('utf-8'))

    def get_projects(self):
        projects = []
        if OUTPUTS_DIR.exists():
            for d in sorted(OUTPUTS_DIR.iterdir()):
                if d.is_dir() and not d.name.startswith("."):
                    projects.append(d.name)
        return projects

    def get_available_models(self):
        return {
            "default_video": load_config().get("video", {}).get("model", "seedance_2_0_mini"),
            "video": [
                {"id": "seedance_2_0_mini", "name": "Seedance 2.0 Mini", "badge": "기본 / 4~15초 가성비", "credits": "5 credits", "tier": "budget"},
                {"id": "seedance_2_0", "name": "Seedance 2.0", "badge": "고화질 / 4~15초", "credits": "요금 확인 필요", "tier": "flagship"},
                {"id": "wan3_0", "name": "Wan 3.0", "badge": "오디오 지원", "credits": "요금 확인 필요", "tier": "standard"},
                {"id": "seedance_2_5", "name": "Seedance 2.5", "badge": "얼굴 고정 최강 (Omni Ref)", "credits": "10 credits", "tier": "flagship"},
                {"id": "wan2_6", "name": "Wan 2.6 Video", "badge": "저크레딧 가성비", "credits": "8 credits", "tier": "budget"},
                {"id": "kling_4_0_flash", "name": "Kling 4.0 Flash", "badge": "고속 저비용", "credits": "6 credits", "tier": "budget"},
                {"id": "veo3_1_lite", "name": "Google Veo 3.1 Lite", "badge": "구글 경량 모델", "credits": "7 credits", "tier": "budget"},
                {"id": "grok_video_v15_lite", "name": "Grok 1.5 Lite", "badge": "경량 프리뷰", "credits": "6 credits", "tier": "budget"},
                {"id": "kling4_0", "name": "Kling 4.0", "badge": "시네마틱 물리 플래그십", "credits": "12 credits", "tier": "flagship"},
                {"id": "veo3_1", "name": "Google Veo 3.1", "badge": "구글 플래그십", "credits": "15 credits", "tier": "flagship"},
                {"id": "minimax_h3", "name": "MiniMax H3 (Hailuo)", "badge": "인물 감정 특화", "credits": "10 credits", "tier": "flagship"}
            ],
            "image": [
                {"id": "nano_banana_2_1", "name": "Nano Banana 2.1", "badge": "현재 기본 / 멀티 레퍼런스", "credits": "2 credits", "tier": "standard"},
                {"id": "nano_banana_pro", "name": "Nano Banana Pro", "badge": "고화질 디테일 극대화", "credits": "4 credits", "tier": "flagship"},
                {"id": "nano_banana_2_lite", "name": "Nano Banana 2 Lite", "badge": "최저가 프리뷰", "credits": "1 credit", "tier": "budget"},
                {"id": "nano_banana_flash", "name": "Nano Banana 2 Flash", "badge": "초고속 시안", "credits": "1 credit", "tier": "budget"},
                {"id": "seedream_5_0_flash", "name": "Seedream 5.0 Flash", "badge": "바이트댄스 저비용", "credits": "1 credit", "tier": "budget"},
                {"id": "seedream_v5_lite", "name": "Seedream 5.0 Lite", "badge": "경량 실사", "credits": "1 credit", "tier": "budget"},
                {"id": "seedream_5_5", "name": "Seedream 5.5", "badge": "고해상도 실사", "credits": "3 credits", "tier": "flagship"},
                {"id": "flux_3_image", "name": "FLUX 3 Image", "badge": "현존 최고 텍스처", "credits": "4 credits", "tier": "flagship"},
                {"id": "gpt_image_2_6", "name": "GPT Image 2.6", "badge": "OpenAI 구도 이해 최상", "credits": "4 credits", "tier": "flagship"},
                {"id": "text2image_soul_v2", "name": "Higgsfield Soul 2.0", "badge": "시네마틱 인물 특화", "credits": "3 credits", "tier": "flagship"}
            ]
        }

    def get_project_data(self, project_name):
        proj_dir = OUTPUTS_DIR / project_name
        if not proj_dir.exists():
            return {"error": "Project not found"}

        # Screenplay
        screenplay_text = ""
        for ext in [".fountain", ".txt", ".md"]:
            cand = proj_dir / f"screenplay{ext}"
            if cand.exists():
                screenplay_text = cand.read_text(encoding="utf-8")
                break

        # Bible
        bible = {}
        bible_file = proj_dir / "bible.json"
        if bible_file.exists():
            bible = read_json(bible_file)

        def _versioned_url(url_path: str, file_path: Path) -> str:
            try:
                mtime_ms = int(file_path.stat().st_mtime * 1000)
                return f"{url_path}?v={mtime_ms}"
            except OSError:
                return url_path

        # Assets
        assets_res = prepare_assets(proj_dir, bible) if bible else {"characters": {}, "scenes": {}, "props": {}, "master_sheets": {}}
        # Inject versioned web URL paths (cache-busted by file mtime)
        for cat, items in assets_res.items():
            if cat == "master_sheets":
                continue
            for item_id, item_data in items.items():
                target_key = "base_status" if cat == "scenes" else "sheet_status"
                if item_data.get(target_key, {}).get("exists"):
                    p = Path(item_data[target_key]["path"])
                    try:
                        rel = p.relative_to(BASE_DIR)
                        base_url = f"/{rel.as_posix()}"
                    except ValueError:
                        rel = p.relative_to(OUTPUTS_DIR)
                        base_url = f"/outputs/{rel.as_posix()}"
                    item_data["web_url"] = _versioned_url(base_url, p)
                # Character face url
                if cat == "characters" and item_data.get("face_status", {}).get("exists"):
                    fp = Path(item_data["face_status"]["path"])
                    try:
                        rel = fp.relative_to(BASE_DIR)
                        base_url = f"/{rel.as_posix()}"
                    except ValueError:
                        rel = fp.relative_to(OUTPUTS_DIR)
                        base_url = f"/outputs/{rel.as_posix()}"
                    item_data["face_web_url"] = _versioned_url(base_url, fp)

        # Master sheet URLs
        master_urls = {}
        for cat in ["characters", "scenes", "props"]:
            ms_file = proj_dir / "assets" / f"master_{cat}_sheet.png"
            if ms_file.exists():
                master_urls[cat] = _versioned_url(f"/outputs/{project_name}/assets/{ms_file.name}", ms_file)
        char_pages = (assets_res.get('master_sheets', {}).get('characters') or {}).get('pages', [])
        first_master = proj_dir / 'assets' / 'master_characters_sheet.png'
        if char_pages and (not first_master.exists() or character_sheet_manifest_path(first_master).exists()):
            master_urls['character_pages'] = [(_versioned_url(f"/outputs/{project_name}/assets/{Path(page['target_path']).name}", Path(page['target_path'])) if Path(page['target_path']).exists() else None) for page in char_pages]

        # Breakdown (EP001)
        ep_dir = proj_dir / "episodes" / "EP001"
        breakdown = {}
        breakdown_file = ep_dir / "breakdown.json"
        if breakdown_file.exists():
            breakdown = read_json(breakdown_file)

        # Keyframes
        keyframes = []
        continuity_modes = load_continuity_modes(proj_dir, "EP001")
        loaded_plan = load_continuity_plan(proj_dir, "EP001")
        continuity_plan = (loaded_plan or {}).get("decisions", {})
        if breakdown.get("segments"):
            kf_tasks = prepare_keyframe_tasks(proj_dir, "EP001")
            for kf_index, t in enumerate(kf_tasks):
                seg_id = t["segment_id"]
                kf_cand = ep_dir / "keyframes" / f"{seg_id}.png"
                web_url = _versioned_url(f"/outputs/{project_name}/episodes/EP001/keyframes/{seg_id}.png", kf_cand) if kf_cand.exists() else None
                effective_mode = continuity_modes.get(seg_id, continuity_plan.get(seg_id, {}).get('mode')) if kf_index else 'keyframe'
                previous_id = kf_tasks[kf_index-1]['segment_id'] if kf_index else None
                frame_status = continuity_frame_status(proj_dir, 'EP001', seg_id, previous_id) if previous_id and effective_mode == 'previous_frame' else {'state': 'not_applicable', 'path': None}
                frame_url = _versioned_url(f"/outputs/{project_name}/episodes/EP001/keyframes/continuity/{seg_id}_from_{previous_id}.png", frame_status['path']) if frame_status['path'] else None
                keyframes.append({
                    "segment_id": seg_id,
                    "exists": kf_cand.exists(),
                    "web_url": web_url,
                    "prompt": t.get("prompt", ""),
                    "refs": t.get("image_references", []),
                    "continuity_mode": continuity_modes.get(seg_id, 'auto'),
                    "continuity_auto": continuity_plan.get(seg_id),
                    "effective_mode": effective_mode,
                    "previous_segment_id": previous_id,
                    "continuity_frame_url": frame_url,
                    "continuity_frame_status": frame_status['state'],
                    "continuity_plan_pending": bool(kf_index and effective_mode is None)
                })

        # Takes & Final Cut
        takes = []
        take_reviews_path = ep_dir / 'takes' / 'dialogue_reviews.json'
        take_reviews = read_json(take_reviews_path) if take_reviews_path.exists() else {}
        if breakdown.get("segments"):
            for seg_index, seg in enumerate(breakdown["segments"]):
                seg_id = seg["id"]
                take_cand = ep_dir / "takes" / f"{seg_id}_take01.mp4"
                review = take_reviews.get(seg_id)
                if review and (not take_cand.exists() or review.get('take_mtime_ns') != take_cand.stat().st_mtime_ns):
                    review = None
                takes.append({
                    "segment_id": seg_id,
                    "exists": take_cand.exists(),
                    "web_url": _versioned_url(f"/outputs/{project_name}/episodes/EP001/takes/{seg_id}_take01.mp4", take_cand) if take_cand.exists() else None,
                    "duration": seg.get("duration", 5),
                    "continuity_mode": continuity_modes.get(seg_id, "auto"),
                    "continuity_auto": continuity_plan.get(seg_id),
                    "dialogue_review": review,
                    "previous_segment_id": breakdown["segments"][seg_index - 1]["id"] if seg_index else None
                })

        final_cut_url = None
        fc_path = ep_dir / "cut" / "final_cut.mp4"
        if fc_path.exists():
            final_cut_url = _versioned_url(f"/outputs/{project_name}/episodes/EP001/cut/final_cut.mp4", fc_path)

        clean_cut_url = None
        fcc_path = ep_dir / "cut" / "final_cut_clean.mp4"
        if fcc_path.exists():
            clean_cut_url = _versioned_url(f"/outputs/{project_name}/episodes/EP001/cut/final_cut_clean.mp4", fcc_path)
        dialogue_review_path = ep_dir / "cut" / "dialogue_review.json"
        dialogue_review = read_json(dialogue_review_path) if dialogue_review_path.exists() else []

        # Check bridge proposals
        proposals_file = BRIDGE_DIR / f"{project_name}_proposals.json"
        proposals = read_json(proposals_file) if proposals_file.exists() else None

        return {
            "project": project_name,
            "screenplay": screenplay_text,
            "bible": bible,
            "assets": assets_res,
            "master_urls": master_urls,
            "breakdown": breakdown,
            "story_review": review_breakdown(breakdown, target_duration=load_target_duration(proj_dir)),
            "keyframes": keyframes,
            "takes": takes,
            "final_cut_url": final_cut_url,
            "clean_cut_url": clean_cut_url,
            "dialogue_review": dialogue_review,
            "proposals": proposals,
            "proposal_review": review_proposals(proposals or {})
        }

    def execute_bridge_task(self, bridge_req):
        req_id = bridge_req.get("id")
        req_type = bridge_req.get("type")
        project = bridge_req.get("project", "sample_drama")

        def append_log(msg):
            JOB_STATUS["logs"].append(msg)
            if len(JOB_STATUS["logs"]) > 100:
                JOB_STATUS["logs"].pop(0)
            print(msg, flush=True)

        with BRIDGE_LOCK:
            running_state = dict(bridge_req, status="running", updated_at=time.time())
            BRIDGE_REQUESTS[req_id] = running_state
            JOB_STATUS["recent_bridge_request"] = running_state
            JOB_STATUS["current_job"] = f"Antigravity Bridge ({req_type}): {project}"
            JOB_STATUS["progress"] = 20
            try:
                if req_type == "synopsis_proposal":
                    res = process_synopsis_request(bridge_req, BRIDGE_DIR, OUTPUTS_DIR, log_fn=append_log)
                elif req_type == "generate_full_script":
                    res = process_full_script_request(bridge_req, BRIDGE_DIR, OUTPUTS_DIR, log_fn=append_log)
                elif req_type == "script_feedback":
                    res = process_script_feedback_request(bridge_req, BRIDGE_DIR, OUTPUTS_DIR, log_fn=append_log)
                elif req_type == "generate_bible_breakdown":
                    res = process_bible_and_breakdown_request(bridge_req, BRIDGE_DIR, OUTPUTS_DIR, log_fn=append_log)
                elif req_type == "breakdown_feedback":
                    res = process_breakdown_feedback_request(bridge_req, BRIDGE_DIR, OUTPUTS_DIR, log_fn=append_log)
                else:
                    raise ValueError(f"Unsupported bridge request type: {req_type}")

                final_state = res["state"]
                BRIDGE_REQUESTS[req_id] = final_state
                JOB_STATUS["recent_bridge_request"] = final_state
                JOB_STATUS["progress"] = 100
            except Exception as exc:
                err_state = dict(bridge_req, status="error", error=str(exc), updated_at=time.time())
                BRIDGE_REQUESTS[req_id] = err_state
                JOB_STATUS["recent_bridge_request"] = err_state
                JOB_STATUS["progress"] = 0
            finally:
                JOB_STATUS["current_job"] = None

    def execute_auto_pipeline_task(self, project, img_model=None, vid_model=None):
        JOB_STATUS["current_job"] = f"전체 자동 파이프라인 시작: {project}"
        JOB_STATUS["progress"] = 5
        JOB_STATUS["logs"] = [f"🚀 [원클릭 자동 제작] 프로젝트 '{project}' 3단계 -> 4단계 -> 5단계 -> 최종 컷 순차 실행 시작..."]
        try:
            # 1. Stage 2: Master Sheets & Slicing
            JOB_STATUS["logs"].append("▶️ 1/3단계: 마스터 시트 3종 생성 및 스마트 슬라이싱 시작...")
            if self.execute_stage_task(project, "2", model_override=img_model) is False:
                return
            
            # 2. Stage 4: Keyframes Generation
            JOB_STATUS["logs"].append("▶️ 2/3단계: 전체 세그먼트 키프레임 생성 시작...")
            if self.execute_stage_task(project, "4", model_override=img_model) is False:
                return

            # 3. Stage 5 & Final Cut
            JOB_STATUS["logs"].append("▶️ 3/3단계: 비디오 테이크 렌더링 & 24fps 최종 컷 인코딩 시작...")
            if self.execute_stage_task(project, "5", model_override=vid_model, image_model_override=img_model) is False:
                return

            JOB_STATUS["progress"] = 100
            JOB_STATUS["logs"].append("🎉🎉 [전체 자동 제작 완료] 1화 최종 완성본 영상이 준비되었습니다!")
        except Exception as e:
            JOB_STATUS["logs"].append(f"❌ 자동 파이프라인 에러: {str(e)}")
        finally:
            JOB_STATUS["current_job"] = None

    def execute_stage_task(self, project, stage, model_override=None, skip_existing=False, image_model_override=None):
        JOB_STATUS["error"] = None
        JOB_STATUS["error_id"] = None
        JOB_STATUS["current_job"] = f"Stage {stage} on {project}"
        JOB_STATUS["progress"] = 10
        JOB_STATUS["logs"] = [f"▶️ Starting Stage {stage} for project '{project}'{' (미생성만 이어하기)' if skip_existing else ''}..."]
        try:
            if str(stage) == "6":
                JOB_STATUS["current_job"] = "STT 자막 생성 및 최종 컷 합성 중..."
                cut_res = build_final_cut(OUTPUTS_DIR / project, "EP001", burn_subtitles=True)
                if not cut_res.get("success"):
                    raise RuntimeError(f"최종 컷 합성 실패: {cut_res.get('error', '알 수 없는 오류')}")
                JOB_STATUS["progress"] = 100
                JOB_STATUS["logs"].append(f"🎉 자막 합본 완료: {cut_res.get('final_cut_path')}")
                return
            # Special Handling for Stage 1 / Stage 3: Real Antigravity Bible & Breakdown Generation
            if str(stage) in ["1", "3"]:
                now = time.time()
                bridge_req = {
                    "id": f"bible_breakdown_{int(now * 1000)}",
                    "type": "generate_bible_breakdown",
                    "project": project,
                    "episode": "EP001",
                    "status": "pending",
                    "timestamp": now,
                }
                self.execute_bridge_task(bridge_req)
                return

            # Special Handling for Stage 2: Real Higgsfield Master Sheets Generation (3 sheets)
            if str(stage) == "2":
                proj_dir = OUTPUTS_DIR / project
                bible_file = proj_dir / "bible.json"
                if bible_file.exists():
                    prepare_assets(proj_dir, read_json(bible_file))
                plan_file = proj_dir / "assets" / "master_sheets_plan.json"
                if plan_file.exists():
                    plan = read_json(plan_file)
                    img_model = model_override or load_config().get("image", {}).get("model", "nano_banana_2_1")
                    flattened = []
                    for category, meta in plan.items():
                        pages = meta.get('pages') or [meta]
                        if category == 'characters' and skip_existing and Path(meta['target_path']).is_file() and not character_sheet_manifest_path(meta['target_path']).exists():
                            # A legacy sheet covers the entire old cast; do not reinterpret it as page 1.
                            pages = [meta]
                        flattened.extend((category, page) for page in pages)
                    total_sheets = len(flattened)
                    done_count = 0

                    for cat, sheet_meta in flattened:
                        target_path = Path(sheet_meta["target_path"])
                        prompt = sheet_meta["prompt"]
                        rec_res = sheet_meta.get("layout_spec", {}).get("recommended_resolution", "1k")
                        done_count += 1
                        if skip_existing and target_path.exists() and target_path.stat().st_size > 0:
                            JOB_STATUS["progress"] = int((done_count / total_sheets) * 90)
                            JOB_STATUS["logs"].append(f"⏭️ [{done_count}/{total_sheets}] {target_path.name} 이미 존재하여 건너뜁니다.")
                            continue
                        JOB_STATUS["current_job"] = f"마스터 시트 생성 중 ({done_count}/{total_sheets}): {cat}"
                        JOB_STATUS["progress"] = int((done_count / total_sheets) * 90)
                        JOB_STATUS["logs"].append(f"🎨 [{done_count}/{total_sheets}] 힉스필드 {img_model}({rec_res})로 '{cat}' 16:9 마스터 시트 생성 시작...")
                        
                        # Call higgsfield CLI with automatic 1x retry on transient failure
                        cmd = [
                            resolve_binary("higgsfield"), "generate", "create", img_model,
                            "--prompt", prompt,
                            "--aspect-ratio", "16:9",
                            "--resolution", rec_res,
                            "--wait", "--wait-timeout", "10m"
                        ]
                        import re, urllib.request
                        urls = []
                        for attempt in range(1, 3):
                            if attempt > 1:
                                JOB_STATUS["logs"].append(f"🔁 '{cat}' 마스터 시트 일시 실패 — 3초 후 자동 재시도 ({attempt}/2)...")
                                time.sleep(3)
                            JOB_STATUS["logs"].append(f"$ higgsfield generate create {img_model} --aspect-ratio 16:9 --resolution {rec_res} (cat={cat}, try={attempt})")
                            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(BASE_DIR))
                            JOB_STATUS["logs"].extend([l.strip() for l in res.stdout.splitlines() if l.strip()])
                            urls = re.findall(r'https?://[^\s<>"\']+\.(?:png|jpg|jpeg|webp)(?:\?[^\s<>"\']*)?', res.stdout)
                            if res.returncode == 0 and urls:
                                break
                            urls = []

                        if urls:
                            img_url = urls[-1]
                            JOB_STATUS["logs"].append(f"📥 다운로드 중: {img_url} -> {target_path.name}")
                            temp_path = None
                            try:
                                with tempfile.NamedTemporaryFile(dir=target_path.parent, suffix='.download', delete=False) as tmp:
                                    temp_path = Path(tmp.name)
                                urllib.request.urlretrieve(img_url, str(temp_path))
                                with Image.open(temp_path) as image:
                                    image.load()
                                    image.convert('RGB').save(temp_path, format='PNG')
                                if cat == 'characters':
                                    character_sheet_pending_path(target_path).write_text('pending', encoding='utf-8')
                                os.replace(temp_path, target_path)
                                if cat == 'characters':
                                    write_character_sheet_manifest(target_path, sheet_meta.get('item_ids', []))
                                    character_sheet_pending_path(target_path).unlink()
                            finally:
                                if temp_path and temp_path.exists():
                                    temp_path.unlink()
                            JOB_STATUS["logs"].append(f"✅ {target_path.name} 저장 완료!")
                        else:
                            raise RuntimeError(f"{target_path.name} 마스터 시트 생성이 2회 모두 실패했습니다. 기존 파일은 보존했습니다. {res.stdout.strip() or '결과 URL 없음'}")

                    # Auto-trigger Smart Slicing immediately! (Step 4 통합)
                    JOB_STATUS["current_job"] = "마스터 시트 원클릭 스마트 슬라이싱 중..."
                    JOB_STATUS["logs"].append("✂️ 마스터 시트 생성 결과 확인 및 스마트 슬라이싱 준비 중...")
                    bible = read_json(proj_dir / "bible.json")
                    missing_sheets = [Path(meta['target_path']).name for cat, meta in flattened if not Path(meta['target_path']).is_file() or Path(meta['target_path']).stat().st_size == 0]
                    if missing_sheets:
                        raise RuntimeError('마스터 시트 생성 실패: ' + ', '.join(missing_sheets) + '. 미생성 시트를 생성해 주세요.')
                    for a_type in ("characters", "scenes", "props"):
                        if bible.get(a_type):
                            slice_project_master_assets(proj_dir, bible, a_type)
                            JOB_STATUS["logs"].append(f"  ✅ {a_type} 슬라이싱 및 고해상도 face.png 추출 완료")

                    validate_master_assets(proj_dir)

                    JOB_STATUS["progress"] = 100
                    JOB_STATUS["logs"].append("🎉 마스터 시트 3종 생성 및 스마트 슬라이싱이 모두 완료되었습니다! (Step 3 & 4 완료)")
                    return

            # Special Handling for Stage 4: Real Higgsfield Multi-Reference Keyframe Generation
            if str(stage) == "4":
                proj_dir = OUTPUTS_DIR / project
                ep_id = "EP001"
                validate_master_assets(proj_dir)
                JOB_STATUS["current_job"] = "이미지 생성 전 스토리 연결 자동 판별 중..."
                plan = ensure_continuity_plan(proj_dir, ep_id, log_fn=JOB_STATUS['logs'].append)
                continuity_modes = load_continuity_modes(proj_dir, ep_id)
                assert_continuity_ready(plan, continuity_modes)
                tasks = prepare_keyframe_tasks(proj_dir, ep_id)
                if tasks:
                    img_model = model_override or load_config().get("image", {}).get("model", "nano_banana_2_1")
                    total_kf = len(tasks)
                    done_count = 0

                    generated_count = 0
                    linked_count = 0
                    referenced_count = 0
                    for task_index, task in enumerate(tasks):
                        seg_id = task["segment_id"]
                        prompt = task["prompt"]
                        refs = task.get("image_references", [])
                        target_kf_path = proj_dir / "episodes" / ep_id / "keyframes" / f"{seg_id}.png"

                        done_count += 1
                        mode = continuity_modes.get(seg_id, plan['decisions'].get(seg_id, {}).get('mode', 'keyframe')) if task_index else 'keyframe'
                        if mode in ('previous_frame', 'state_reference'):
                            linked_count += mode == 'previous_frame'
                            referenced_count += mode == 'state_reference'
                            previous_id = tasks[task_index - 1]['segment_id']
                            JOB_STATUS["progress"] = int((done_count / total_kf) * 90)
                            message = '마지막 프레임과 이전 영상을 참고해 이어집니다' if mode == 'previous_frame' else '이전 영상과 고정 장소·인물 기준을 참고해 영상 단계에서 새 구도를 생성합니다'
                            JOB_STATUS["logs"].append(f"🔗 [{done_count}/{total_kf}] {seg_id}: {previous_id} 이전 영상의 {message}. 이미지 단계에서 생성 생략.")
                            continue
                        if skip_existing and target_kf_path.exists() and target_kf_path.stat().st_size > 0:
                            JOB_STATUS["progress"] = int((done_count / total_kf) * 90)
                            JOB_STATUS["logs"].append(f"⏭️ [{done_count}/{total_kf}] {target_kf_path.name} 이미 존재하여 건너뜁니다.")
                            continue
                        JOB_STATUS["current_job"] = f"키프레임 생성 중 ({done_count}/{total_kf}): {seg_id}"
                        JOB_STATUS["progress"] = int((done_count / total_kf) * 90)
                        JOB_STATUS["logs"].append(f"🖼️ [{done_count}/{total_kf}] 힉스필드 {img_model}로 {seg_id} 키프레임 생성 시작 (레퍼런스 {len(refs)}장 결합)...")

                        cmd = [
                            resolve_binary("higgsfield"), "generate", "create", img_model,
                            "--prompt", prompt,
                            "--aspect-ratio", "9:16",
                            "--wait", "--wait-timeout", "10m"
                        ]
                        for ref_path in refs:
                            if Path(ref_path).exists():
                                cmd.extend(["--image", str(ref_path)])

                        JOB_STATUS["logs"].append(f"$ higgsfield generate create {img_model} --aspect-ratio 9:16 (seg={seg_id}, refs={len(refs)})")
                        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(BASE_DIR))
                        JOB_STATUS["logs"].extend([l.strip() for l in res.stdout.splitlines() if l.strip()])
                        if res.returncode != 0:
                            raise RuntimeError(f"{seg_id} 키프레임 생성 실패: {res.stdout.strip() or res.returncode}")

                        # Extract image url and download
                        import re, urllib.request
                        urls = re.findall(r'https?://[^\s<>"\']+\.(?:png|jpg|jpeg|webp)(?:\?[^\s<>"\']*)?', res.stdout)
                        if urls:
                            img_url = urls[-1]
                            JOB_STATUS["logs"].append(f"📥 다운로드 중: {img_url} -> {target_kf_path.name}")
                            urllib.request.urlretrieve(img_url, str(target_kf_path))
                            generated_count += 1
                            JOB_STATUS["logs"].append(f"✅ {target_kf_path.name} 저장 완료!")
                        else:
                            raise RuntimeError(f"{seg_id} 키프레임 생성 실패: 결과 이미지 URL을 찾지 못했습니다.")

                    JOB_STATUS["progress"] = 100
                    JOB_STATUS["logs"].append(f"🎉 이미지 단계 완료: 새로 생성 {generated_count}장, 이전 영상 직접 연결 {linked_count}씬, 이전 영상 참고 새 구도 대기 {referenced_count}씬. 새 구도는 영상 단계에서 직접 생성합니다.")
                    return

            # Special Handling for Stage 5: Real Higgsfield Video (I2V Takes) Generation + Auto Final Cut
            if str(stage) == "5":
                proj_dir = OUTPUTS_DIR / project
                ep_id = "EP001"
                takes_dir = ensure_dir(proj_dir / "episodes" / ep_id / "takes")
                tasks = prepare_video_tasks(proj_dir, ep_id)
                if tasks:
                    vid_model = model_override or load_config().get("video", {}).get("model", "seedance_2_0")
                    for item in tasks:
                        item['duration'] = validated_video_duration(vid_model, item.get('duration', 5), item['segment_id'])
                    first_id = tasks[0]['segment_id']
                    first_take = takes_dir / f'{first_id}_take01.mp4'
                    first_keyframe = proj_dir / 'episodes' / ep_id / 'keyframes' / f'{first_id}.png'
                    already_done = skip_existing and first_take.is_file() and first_take.stat().st_size > 0
                    if not already_done and (not first_keyframe.is_file() or first_keyframe.stat().st_size == 0):
                        raise RuntimeError(f'{first_id} 키프레임이 없습니다. 먼저 4단계 키프레임을 생성해 주세요.')
                    JOB_STATUS["current_job"] = "스토리와 장면 연결 자동 판별 중..."
                    plan = ensure_continuity_plan(proj_dir, ep_id, log_fn=JOB_STATUS['logs'].append)
                    for seg_id, decision in plan['decisions'].items():
                        JOB_STATUS['logs'].append(f"🔗 {seg_id}: {decision['mode']} — {decision['reason']}")
                    continuity_modes = load_continuity_modes(proj_dir, ep_id)
                    assert_continuity_ready(plan, continuity_modes)
                    image_model = image_model_override or load_config().get('image', {}).get('model', 'nano_banana_2_1')
                    keyframe_tasks = None
                    total_vids = len(tasks)
                    done_count = 0

                    for task_index, task in enumerate(tasks):
                        seg_id = task["segment_id"]
                        prompt = task["prompt"]
                        duration = task.get("duration", 5)
                        target_take_path = takes_dir / f"{seg_id}_take01.mp4"

                        if skip_existing and target_take_path.exists() and target_take_path.stat().st_size > 0:
                            done_count += 1
                            JOB_STATUS["progress"] = int((done_count / total_vids) * 85)
                            JOB_STATUS["logs"].append(f"⏭️ [{done_count}/{total_vids}] {target_take_path.name} 이미 생성됨 — 크레딧 절약을 위해 건너뜁니다.")
                            continue

                        # Dynamically resolve real generated keyframe on disk to guarantee 100% I2V visual consistency!
                        mode = continuity_modes.get(seg_id, plan['decisions'].get(seg_id, {}).get('mode', 'keyframe')) if task_index else 'keyframe'
                        image_task = None
                        if mode == 'state_reference':
                            validate_master_assets(proj_dir)
                            if keyframe_tasks is None:
                                keyframe_tasks = {t['segment_id']: t for t in prepare_keyframe_tasks(proj_dir, ep_id)}
                            image_task = keyframe_tasks.get(seg_id)
                        inputs = build_continuity_video_inputs(proj_dir, ep_id, tasks, task_index, mode, vid_model, image_task)
                        prompt += continuity_video_instruction(mode, inputs)

                        done_count += 1
                        JOB_STATUS["current_job"] = f"비디오 테이크 렌더링 중 ({done_count}/{total_vids}): {seg_id}"
                        JOB_STATUS["progress"] = int((done_count / total_vids) * 85)
                        JOB_STATUS["logs"].append(f"🎥 [{done_count}/{total_vids}] 힉스필드 {vid_model}로 {seg_id} 비디오(I2V) 렌더링 시작...")
                        JOB_STATUS["logs"].append(f"🔗 {seg_id}: {mode}, 시작 이미지 {inputs['start_image'].name if inputs['start_image'] else '고정 안 함'}, 영상 참고 {len(inputs['video_references'])}개, 이미지 참고 {len(inputs['image_references'])}개")

                        cmd = [
                            resolve_binary("higgsfield"), "generate", "create", vid_model,
                            "--prompt", prompt,
                            "--duration", str(duration),
                            "--aspect-ratio", "9:16",
                            *continuity_video_args(inputs),
                            "--wait", "--wait-timeout", "20m"
                        ]

                        JOB_STATUS["logs"].append(f"$ higgsfield generate create {vid_model} --duration {duration} --aspect-ratio 9:16 (seg={seg_id}, mode={mode})")
                        res = execute_higgsfield_with_retry(cmd)

                        # Extract video url and download
                        if res.returncode != 0:
                            detail = res.stdout.strip() or f"종료 코드 {res.returncode}"
                            raise RuntimeError(f"{seg_id} 영상 생성 실패: {detail}")
                        import re, urllib.request
                        urls = re.findall(r'https?://[^\s<>"\']+\.(?:mp4|webm|mov)(?:\?[^\s<>"\']*)?', res.stdout)
                        if not urls:
                            # Fallback: any https url with video extension substring
                            urls = re.findall(r'https?://[^\s<>"\']+(?:mp4|webm|mov)[^\s<>"\']*', res.stdout)
                        if urls:
                            vid_url = urls[-1]
                            JOB_STATUS["logs"].append(f"📥 비디오 다운로드 중: {vid_url} -> {target_take_path.name}")
                            urllib.request.urlretrieve(vid_url, str(target_take_path))
                            JOB_STATUS["logs"].append(f"✅ {target_take_path.name} 저장 완료!")
                            JOB_STATUS["current_job"] = f"{seg_id} 대사 STT 검수 중..."
                            review = review_single_take(proj_dir, ep_id, seg_id)
                            if review and review.get('needs_review'):
                                detail = '영상 끝 발화 잘림 의심' if review.get('end_cut_risk') else ('STT 실행 실패' if review.get('stt_error') else '대본과 발화 차이')
                                JOB_STATUS['logs'].append(f"⚠️ {seg_id} 대사 확인 필요: {detail}. 영상을 듣고 확인해 주세요.")
                            elif review:
                                JOB_STATUS['logs'].append(f"✅ {seg_id} 대사 STT 비교 완료 (청취 확인 권장).")
                        else:
                            raise RuntimeError(f"{seg_id} 영상 생성 실패: 비디오 결과 URL을 찾지 못했습니다. {res.stdout.strip()}")

                    # Auto assemble final cut with subtitles (Step 5 완성)
                    JOB_STATUS["current_job"] = "최종 컷 자동 합성 중 (FFmpeg 24fps)..."
                    JOB_STATUS["logs"].append("🎬 모든 비디오 테이크 완성! 24fps 시네마틱 + 40px 와이드 1줄 자막 자동 결합 중...")
                    cut_res = build_final_cut(proj_dir, ep_id, burn_subtitles=True)
                    if not cut_res.get("success"):
                        raise RuntimeError(f"최종 컷 합성 실패: {cut_res.get('error', '알 수 없는 오류')}")
                    JOB_STATUS["logs"].append(f"🎉 24fps 최종 완성본 인코딩 완료: {cut_res.get('final_cut_path')}")

                    JOB_STATUS["progress"] = 100
                    JOB_STATUS["logs"].append(f"🏁 Step 5 전체 비디오 렌더링 및 최종 완성본 합본이 100% 완료되었습니다!")
                    return

            cmd = [sys.executable, str(BASE_DIR / "run_pipeline.py"), "--project", project, "--stage", stage]
            if stage in ["5", "all"]:
                cmd.append("--confirm-credits")
            JOB_STATUS["logs"].append(f"$ {' '.join(cmd)}")
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(BASE_DIR))
            for line in iter(p.stdout.readline, ''):
                if line:
                    JOB_STATUS["logs"].append(line.strip())
                    if len(JOB_STATUS["logs"]) > 100:
                        JOB_STATUS["logs"].pop(0)
            p.wait()
            if p.returncode != 0:
                raise RuntimeError(f"Stage {stage} 실행 실패 (종료 코드 {p.returncode}). " + (JOB_STATUS['logs'][-1] if JOB_STATUS['logs'] else ''))

            # If Stage 5 (Video Takes) completes, automatically trigger Stage 6 (Final Cut) so user gets the final video in one go!
            if str(stage) == "5" and p.returncode == 0:
                JOB_STATUS["current_job"] = "최종 컷 자동 합성 중 (FFmpeg 24fps)..."
                JOB_STATUS["logs"].append("🎬 비디오 테이크 렌더링 완료! 24fps 최종 컷 및 자막 인코딩 자동 실행 중...")
                cut_res = build_final_cut(OUTPUTS_DIR / project, "EP001", burn_subtitles=True)
                if cut_res.get("success"):
                    JOB_STATUS["logs"].append(f"🎉 24fps 와이드 1줄 자막 최종 완성본 생성 완료: {cut_res.get('final_cut_path')}")

            JOB_STATUS["progress"] = 100
            JOB_STATUS["logs"].append(f"🏁 Stage {stage} complete with code {p.returncode}")
        except Exception as e:
            JOB_STATUS["error"] = str(e)
            JOB_STATUS["error_id"] = str(time.time_ns())
            JOB_STATUS["logs"].append(f"❌ Error: {str(e)}")
            return False
        finally:
            JOB_STATUS["current_job"] = None

    def execute_single_keyframe_task(self, project, segment_id, model_override=None):
        JOB_STATUS["error"] = None
        JOB_STATUS["error_id"] = None
        JOB_STATUS["current_job"] = f"키프레임 단독 리롤 중: {segment_id}"
        JOB_STATUS["progress"] = 20
        JOB_STATUS["logs"].append(f"🔄 [단독 리롤] {segment_id} 키프레임 재생성 시작...")
        try:
            proj_dir = OUTPUTS_DIR / project
            ep_id = "EP001"
            validate_master_assets(proj_dir)
            plan = ensure_continuity_plan(proj_dir, ep_id, log_fn=JOB_STATUS['logs'].append)
            overrides = load_continuity_modes(proj_dir, ep_id)
            assert_continuity_ready(plan, overrides, segment_id)
            mode = overrides.get(segment_id, plan['decisions'].get(segment_id, {}).get('mode', 'keyframe'))
            if mode in ('previous_frame', 'state_reference'):
                raise RuntimeError(f'{segment_id}은 이전 영상 상태가 필요한 씬입니다. 이미지 탭이 아닌 영상 렌더링 단계에서 생성됩니다.')
            # Always rebuild keyframe plan so latest template & breakdown edits take effect immediately
            tasks = prepare_keyframe_tasks(proj_dir, ep_id)
            task = next((t for t in tasks if t.get("segment_id") == segment_id), None)
            if not task:
                raise ValueError(f"{segment_id} 키프레임 작업 정보를 찾을 수 없습니다.")

            img_model = model_override or load_config().get("image", {}).get("model", "nano_banana_2_1")
            refs = task.get("image_references", [])
            target_kf_path = ensure_dir(proj_dir / "episodes" / ep_id / "keyframes") / f"{segment_id}.png"

            cmd = [
                resolve_binary("higgsfield"), "generate", "create", img_model,
                "--prompt", task["prompt"],
                "--aspect-ratio", "9:16",
                "--wait", "--wait-timeout", "10m"
            ]
            for ref_path in refs:
                if Path(ref_path).exists():
                    cmd.extend(["--image", str(ref_path)])

            res = execute_higgsfield_with_retry(cmd)

            if res.returncode != 0:
                raise RuntimeError(f"{segment_id} 키프레임 생성 실패: {res.stdout.strip() or res.returncode}")

            import re, urllib.request
            urls = re.findall(r'https?://[^\s<>"\']+\.(?:png|jpg|jpeg|webp)(?:\?[^\s<>"\']*)?', res.stdout)
            if not urls:
                raise RuntimeError(f"{segment_id} 키프레임 결과 이미지 URL을 찾지 못했습니다.")
            urllib.request.urlretrieve(urls[-1], str(target_kf_path))
            JOB_STATUS["progress"] = 100
            JOB_STATUS["logs"].append(f"✅ [단독 리롤 완료] {target_kf_path.name} 교체 완료!")
        except Exception as e:
            JOB_STATUS["error"] = str(e)
            JOB_STATUS["error_id"] = str(time.time_ns())
            JOB_STATUS["logs"].append(f"❌ [단독 리롤 에러] {segment_id}: {str(e)}")
        finally:
            JOB_STATUS["current_job"] = None

    def execute_single_take_task(self, project, segment_id, model_override=None, image_model_override=None):
        JOB_STATUS["error"] = None
        JOB_STATUS["error_id"] = None
        JOB_STATUS["current_job"] = f"비디오 단독 렌더링 중: {segment_id}"
        JOB_STATUS["progress"] = 20
        JOB_STATUS["logs"].append(f"🎬 [단독 렌더링] {segment_id} 비디오(I2V) 생성 시작...")
        try:
            proj_dir = OUTPUTS_DIR / project
            ep_id = "EP001"
            takes_dir = ensure_dir(proj_dir / "episodes" / ep_id / "takes")
            tasks = prepare_video_tasks(proj_dir, ep_id)
            JOB_STATUS["current_job"] = "스토리와 장면 연결 자동 판별 중..."
            plan = ensure_continuity_plan(proj_dir, ep_id, log_fn=JOB_STATUS['logs'].append)
            overrides = load_continuity_modes(proj_dir, ep_id)
            assert_continuity_ready(plan, overrides, segment_id)
            task = next((t for t in tasks if t.get("segment_id") == segment_id), None)
            if not task:
                raise ValueError(f"{segment_id} 비디오 작업 정보를 찾을 수 없습니다.")

            vid_model = model_override or load_config().get("video", {}).get("model", "seedance_2_0")
            task['duration'] = validated_video_duration(vid_model, task.get('duration', 5), segment_id)

            task_index = tasks.index(task)
            mode = overrides.get(segment_id, plan['decisions'].get(segment_id, {}).get('mode', 'keyframe')) if task_index else 'keyframe'
            image_task = None
            if mode == 'state_reference':
                validate_master_assets(proj_dir)
                image_task = next((t for t in prepare_keyframe_tasks(proj_dir, ep_id) if t['segment_id'] == segment_id), None)
            inputs = build_continuity_video_inputs(proj_dir, ep_id, tasks, task_index, mode, vid_model, image_task)
            prompt = task['prompt'] + continuity_video_instruction(mode, inputs)
            JOB_STATUS['logs'].append(f"🔗 {segment_id}: {mode}, 시작 이미지 {inputs['start_image'].name if inputs['start_image'] else '고정 안 함'}, 영상 참고 {len(inputs['video_references'])}개, 이미지 참고 {len(inputs['image_references'])}개")

            target_take_path = takes_dir / f"{segment_id}_take01.mp4"

            cmd = [
                resolve_binary("higgsfield"), "generate", "create", vid_model,
                "--prompt", prompt,
                "--duration", str(task.get("duration", 5)),
                "--aspect-ratio", "9:16",
                *continuity_video_args(inputs),
                "--wait", "--wait-timeout", "20m"
            ]
            res = execute_higgsfield_with_retry(cmd)

            if res.returncode != 0:
                raise RuntimeError(f"{segment_id} 영상 생성 실패: {res.stdout.strip()}")

            import re, urllib.request
            urls = re.findall(r'https?://[^\s<>"\']+\.(?:mp4|webm|mov)(?:\?[^\s<>"\']*)?', res.stdout)
            if not urls:
                urls = re.findall(r'https?://[^\s<>"\']+(?:mp4|webm|mov)[^\s<>"\']*', res.stdout)
            if not urls:
                raise RuntimeError(f"{segment_id} 비디오 결과 URL을 찾지 못했습니다.")
            urllib.request.urlretrieve(urls[-1], str(target_take_path))
            JOB_STATUS["current_job"] = f"{segment_id} 대사 STT 검수 중..."
            review = review_single_take(proj_dir, ep_id, segment_id)
            if review and review.get('needs_review'):
                JOB_STATUS['logs'].append(f"⚠️ {segment_id} 대사 확인 필요: {'영상 끝 잘림 의심' if review.get('end_cut_risk') else '대본과 발화 차이 또는 STT 실패'}.")
            JOB_STATUS["progress"] = 100
            JOB_STATUS["logs"].append(f"✅ [단독 렌더링 완료] {target_take_path.name} 교체 완료!")
        except Exception as e:
            JOB_STATUS["error"] = str(e)
            JOB_STATUS["error_id"] = str(time.time_ns())
            JOB_STATUS["logs"].append(f"❌ [단독 렌더링 에러] {segment_id}: {str(e)}")
        finally:
            JOB_STATUS["current_job"] = None

def run_server():
    server_address = ('', PORT)
    httpd = ThreadingHTTPServer(server_address, DashboardHandler)
    httpd.daemon_threads = True
    print(f"🎬 [Short Drama Studio] Dashboard running at http://localhost:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard server...")
        httpd.server_close()

if __name__ == '__main__':
    run_server()

