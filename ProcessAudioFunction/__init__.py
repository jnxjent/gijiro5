# ProcessAudioFunction/__init__.py
import os
import re
import json
import base64
import tempfile
import subprocess
import uuid
from pathlib import Path
import shutil
import stat
import datetime
import logging

from typing import List
from pydub import AudioSegment
import azure.functions as func

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

from storage import download_blob, upload_to_blob
# kowake は ffmpeg 解決後に import（遅延）
from extraction import extract_meeting_info_and_speakers
from docwriter import process_document

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# 一時ディレクトリなど
# ─────────────────────────────────────────────────────────
TMP_DIR = tempfile.gettempdir()
TMP_FFMPEG = os.path.join(TMP_DIR, "ffmpeg")
TMP_FFPROBE = os.path.join(TMP_DIR, "ffprobe")


def _account_name_from_cs(cs: str) -> str:
    try:
        m = re.search(r"AccountName=([^;]+)", cs or "")
        return m.group(1) if m else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


# ─────────────────────────────────────────────────────────
# ffmpeg/ffprobe 解決（/tmp フォールバック）
#   - import 時に例外は投げない
#   - 実行不可(noexec)なら /tmp に実体コピーして実行確認
# ─────────────────────────────────────────────────────────
def _is_file(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path)
    except Exception:
        return False


def _exec_succeeds(bin_path: str) -> bool:
    try:
        subprocess.check_output([bin_path, "-version"], stderr=subprocess.STDOUT, timeout=5)
        return True
    except Exception:
        return False


def _same_size(a: str, b: str) -> bool:
    try:
        return os.path.getsize(a) == os.path.getsize(b)
    except Exception:
        return False


def _ensure_tmp(src: str, dst: str) -> str:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst) or not _same_size(src, dst):
        tmp_dst = dst + ".tmp"
        shutil.copyfile(src, tmp_dst)
        os.chmod(tmp_dst, os.stat(tmp_dst).st_mode | stat.S_IEXEC)
        os.replace(tmp_dst, dst)
    return dst


def _pick_first_existing(paths: List[str]) -> str:
    for p in paths:
        if _is_file(p):
            return p
    return ""


def _candidate_paths_from_env():
    env_ff = os.environ.get("FFMPEG_BINARY") or os.environ.get("FFMPEG_PATH") or ""
    env_fp = os.environ.get("FFPROBE_BINARY") or os.environ.get("FFPROBE_PATH") or ""

    ffmpeg_candidates = [
        env_ff,
        "/home/site/wwwroot/ProcessAudioFunction/tools/ffmpeg",
        "/home/site/wwwroot/tools/ffmpeg",
        "/home/site/ffmpeg-bin/bin/ffmpeg",
        "/home/site/wwwroot/ffmpeg",
    ]
    ffprobe_candidates = [
        env_fp,
        "/home/site/wwwroot/ProcessAudioFunction/tools/ffprobe",
        "/home/site/wwwroot/tools/ffprobe",
        "/home/site/ffmpeg-bin/bin/ffprobe",
        "/home/site/wwwroot/ffprobe",
    ]
    # 空と重複を除去
    ffmpeg_candidates = [p for p in dict.fromkeys(ffmpeg_candidates) if p]
    ffprobe_candidates = [p for p in dict.fromkeys(ffprobe_candidates) if p]
    return ffmpeg_candidates, ffprobe_candidates


def resolve_ffmpeg_and_ffprobe():
    ff_candidates, fp_candidates = _candidate_paths_from_env()
    logger.info(f"[ffmpeg-check] candidates ffmpeg={ff_candidates}")
    logger.info(f"[ffmpeg-check] candidates ffprobe={fp_candidates}")

    ff_src = _pick_first_existing(ff_candidates)
    fp_src = _pick_first_existing(fp_candidates)

    if not ff_src:
        logger.error("FFMPEG binary not found in candidates.")
        raise RuntimeError("FFMPEG binary not found.")
    if not fp_src:
        logger.error("FFPROBE binary not found in candidates.")
        raise RuntimeError("FFPROBE binary not found.")

    # 直接実行できる？
    if _exec_succeeds(ff_src) and _exec_succeeds(fp_src):
        ff_bin, fp_bin = ff_src, fp_src
        logger.info("[ffmpeg-check] direct execute OK")
    else:
        logger.warning("[ffmpeg-check] direct execute failed -> fallback to /tmp")
        ff_bin = _ensure_tmp(ff_src, TMP_FFMPEG)
        fp_bin = _ensure_tmp(fp_src, TMP_FFPROBE)
        if not (_exec_succeeds(ff_bin) and _exec_succeeds(fp_bin)):
            logger.error("[ffmpeg-check] /tmp fallback also failed")
            raise RuntimeError("ffmpeg/ffprobe not executable even after /tmp fallback")

    # pydub / 環境変数を更新（/tmp 実体で統一）
    os.environ["FFMPEG_BINARY"] = ff_bin
    os.environ["FFPROBE_BINARY"] = fp_bin
    os.environ["FFMPEG_PATH"] = ff_bin
    os.environ["FFPROBE_PATH"] = fp_bin

    AudioSegment.converter = ff_bin
    AudioSegment.ffprobe = fp_bin

    # PATH 先頭に追加
    bin_dir = str(Path(ff_bin).parent)
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

    # 版表示（デバッグ）
    try:
        out = (
            subprocess.check_output([ff_bin, "-version"], stderr=subprocess.STDOUT, timeout=5)
            .decode("utf-8", "ignore")
            .splitlines()[0]
        )
        logger.info(f"[ffmpeg-check] ffmpeg -version: {out}")
    except Exception:
        logger.warning("[ffmpeg-check] could not print ffmpeg version")

    logger.info(f"▶▶ Using FFMPEG:  {ff_bin}")
    logger.info(f"▶▶ Using FFPROBE: {fp_bin}")
    return ff_bin, fp_bin


logger.info("▶▶ Module import success (ffmpeg resolution will run at invocation)")


# ─── Function 本体 ────────────────────────────────────────
# ※ Azure Functions の Queue Trigger は sync 推奨ですが、
#   既存実装に合わせて async を維持します。
async def main(msg: func.QueueMessage) -> None:
    logger.info("▶▶ Function invoked")

    # ===== BOOT / STORAGE TARGET CHECK =====
    cs = os.environ.get("AzureWebJobsStorage", "")
    logger.warning(f"[BOOT] AzureWebJobsStorage.AccountName={_account_name_from_cs(cs)}")
    logger.warning(f"[BOOT] FUNCTIONS_WORKER_RUNTIME={os.environ.get('FUNCTIONS_WORKER_RUNTIME')}")
    logger.warning(f"[BOOT] WEBSITE_SITE_NAME={os.environ.get('WEBSITE_SITE_NAME')}")
    logger.warning(f"[BOOT] msg.id={getattr(msg, 'id', None)} dequeue={getattr(msg, 'dequeue_count', None)}")
    try:
        b = msg.get_body()
        logger.warning(f"[BOOT] msg.get_body type={type(b)} len={len(b) if b else 0}")
        if b:
            logger.warning(f"[BOOT] msg.get_body head(200)={b[:200]!r}")
    except Exception:
        logger.exception("[BOOT] failed to read msg.get_body()")

    raw = msg.get_body().decode("utf-8", errors="replace")
    logger.info("▶▶ RAW payload: %s", raw)

    # JSON / Base64 自動判定
    try:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            decoded = base64.b64decode(raw).decode("utf-8")
            body = json.loads(decoded)
    except Exception:
        logger.exception("payload parse failed")
        raise

    # === 処理ロック（同じ job の二重実行防止） ===
    job_id = (body.get("job_id") or body.get("jobId") or "").strip()
    if not job_id:
        logger.warning("job_id が無いためロック不能 → 通常処理（推奨は job_id 必須）")
        bc = None
    else:
        conn = os.environ["AzureWebJobsStorage"]
        container = os.environ.get("SENTFLAGS_CONTAINER", "sentflags-function-test")
        bsc = BlobServiceClient.from_connection_string(conn)
        cc = bsc.get_container_client(container)
        bc = cc.get_blob_client(f"{job_id}.lock")
        try:
            bc.upload_blob(
                b"lock",
                overwrite=False,
                if_none_match="*",
                metadata={"status": "processing", "firstUtc": datetime.datetime.utcnow().isoformat() + "Z"},
            )
            logger.info(f"[LOCK ACQUIRED] job_id={job_id}")
        except (ResourceExistsError, ResourceModifiedError):
            logger.warning(f"[SKIP: DUPLICATE EXECUTION] job_id={job_id}")
            return

    # ffmpeg/ffprobe を起動時に毎回保証（コールドスタート・スケールアウト耐性）
    try:
        FFMPEG_BIN, FFPROBE_BIN = resolve_ffmpeg_and_ffprobe()
    except Exception:
        logger.exception("▶▶ FFMPEG/FFPROBE resolution failed")
        # ロック取得済みなら status=error で刻む（任意）
        if bc is not None:
            try:
                bc.set_blob_metadata({"status": "error_ffmpeg", "at": datetime.datetime.utcnow().isoformat() + "Z"})
            except Exception:
                pass
        raise

    # ★ 遅延 import：ffmpeg 環境が整った後にロード
    from kowake import transcribe_and_correct

    try:
        blob_url = body["blob_url"]
        template_blob_url = body["template_blob_url"]
        logger.info(f"Received job {job_id}, blob: {blob_url}, template: {template_blob_url}")

        # 1. 音声を /tmp にダウンロード
        local_audio = os.path.join(TMP_DIR, f"{uuid.uuid4()}.mp4")
        logger.info(f"▶▶ STEP1-1: Downloading audio from {blob_url}")
        download_blob(blob_url, local_audio)
        logger.info(f"▶▶ STEP1-2: Audio downloaded to {local_audio}")

        # 2. Fast-Start
        fixed_audio = os.path.join(TMP_DIR, f"{uuid.uuid4()}_fixed.mp4")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", local_audio, "-c", "copy", "-movflags", "+faststart", fixed_audio],
            check=True,
            timeout=60,
        )
        logger.info(f"▶▶ STEP2: Faststart applied: {fixed_audio}")

        # 3. 文字起こし
        logger.info("▶▶ STEP3-1: Starting transcription")
        transcript = await transcribe_and_correct(fixed_audio)
        logger.info("▶▶ STEP3-2: Transcription completed")

        # 4. テンプレート DL
        template_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}_template.docx")
        logger.info(f"▶▶ STEP4-1: Downloading template from {template_blob_url}")
        download_blob(template_blob_url, template_path)
        logger.info(f"▶▶ STEP4-2: Template downloaded to {template_path}")

        # 5. 情報抽出 → Word
        logger.info("▶▶ STEP5-1: Starting document processing")
        meeting_info = await extract_meeting_info_and_speakers(transcript, template_path)
        local_docx = os.path.join(TMP_DIR, f"{job_id}.docx")
        blob_docx = f"processed/{job_id}.docx"
        process_document(template_path, local_docx, meeting_info)
        logger.info("▶▶ STEP5-2: Document processed")

        with open(local_docx, "rb") as fp:
            upload_to_blob(blob_docx, fp, add_audio_prefix=False)
        logger.info(f"Job {job_id} completed, saved to {blob_docx}")

        # === 正常終了 → 完了フラグ ===
        if bc is not None:
            try:
                bc.set_blob_metadata({"status": "done", "doneUtc": datetime.datetime.utcnow().isoformat() + "Z"})
                logger.info(f"[LOCK DONE] job_id={job_id}")
            except Exception:
                pass

    except Exception:
        # 失敗時：ロックは status=processing のまま（設計どおり）
        logger.exception("Error processing job")
        raise

    finally:
        # 後片付け
        for var in ("local_audio", "fixed_audio", "template_path", "local_docx"):
            path = locals().get(var)
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
