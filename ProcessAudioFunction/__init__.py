import logging

# ─── ロガー初期化 ─────────────────────────────────────────
logger = logging.getLogger("ProcessAudioFunction")
logger.setLevel(logging.INFO)
logger.info("▶▶ Module import start")

import os
import json
import base64
import tempfile
import subprocess
import uuid
from pathlib import Path
import shutil
import stat

from pydub import AudioSegment
import azure.functions as func

from storage import download_blob, upload_to_blob
from kowake import transcribe_and_correct
from extraction import extract_meeting_info_and_speakers
from docwriter import process_document

# ─────────────────────────────────────────────────────────
# ffmpeg/ffprobe 解決（/tmp フォールバック）
#   1) 環境変数の値を最優先
#   2) 既知パス（ProcessAudioFunction/tools 等）を候補に
#   3) 実行不可(noexec)なら /tmp にコピーして実行
#     - /tmp は揮発。起動時に存在+サイズチェックで必要時のみコピー
# ─────────────────────────────────────────────────────────

TMP_DIR = tempfile.gettempdir()
SRC_CANDIDATES = [
    # Environment first
    lambda: os.environ.get("FFMPEG_BINARY"),
    lambda: "/home/site/wwwroot/ProcessAudioFunction/tools/ffmpeg",
    lambda: "/home/site/ffmpeg-bin/bin/ffmpeg",
    lambda: "/home/site/wwwroot/ffmpeg",
]

PROBE_CANDIDATES = [
    lambda: os.environ.get("FFPROBE_BINARY"),
    lambda: "/home/site/wwwroot/ProcessAudioFunction/tools/ffprobe",
    lambda: "/home/site/ffmpeg-bin/bin/ffprobe",
    lambda: "/home/site/wwwroot/ffprobe",
]

TMP_FFMPEG = os.path.join(TMP_DIR, "ffmpeg")
TMP_FFPROBE = os.path.join(TMP_DIR, "ffprobe")


def _is_exec(path: str) -> bool:
    try:
        return path and os.path.isfile(path) and os.access(path, os.X_OK)
    except Exception:
        return False


def _same_size(a: str, b: str) -> bool:
    try:
        return os.path.getsize(a) == os.path.getsize(b)
    except Exception:
        return False


def _ensure_tmp(src: str, dst: str) -> str:
    """
    src から /tmp の dst へ必要時のみコピー。実行ビット付与。
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst) or not _same_size(src, dst):
        # 原子的に置換
        tmp_dst = dst + ".tmp"
        shutil.copyfile(src, tmp_dst)
        os.chmod(tmp_dst, os.stat(tmp_dst).st_mode | stat.S_IEXEC)
        os.replace(tmp_dst, dst)
    return dst


def _pick_first(candidates) -> str:
    for g in candidates:
        p = g()
        if _is_exec(p):
            return p
    return ""


def _exec_succeeds(bin_path: str) -> bool:
    try:
        subprocess.check_output([bin_path, "-version"], stderr=subprocess.STDOUT, timeout=5)
        return True
    except Exception:
        return False


def resolve_ffmpeg_and_ffprobe():
    # 1) 候補から掴む
    ff_src = _pick_first(SRC_CANDIDATES)
    fp_src = _pick_first(PROBE_CANDIDATES)

    # 2) 見つからなければ明示エラー
    if not ff_src or not os.path.isfile(ff_src):
        logger.error("FFMPEG binary not found in env/candidates.")
        raise RuntimeError("FFMPEG binary not found.")
    if not fp_src or not os.path.isfile(fp_src):
        logger.error("FFPROBE binary not found in env/candidates.")
        raise RuntimeError("FFPROBE binary not found.")

    # 3) そのまま実行できるか試す（noexec 検出）
    if _exec_succeeds(ff_src) and _exec_succeeds(fp_src):
        ff_bin, fp_bin = ff_src, fp_src
    else:
        # 4) 実行不可なら /tmp に退避
        ff_bin = _ensure_tmp(ff_src, TMP_FFMPEG)
        fp_bin = _ensure_tmp(fp_src, TMP_FFPROBE)
        # 念のため実行確認
        if not (_exec_succeeds(ff_bin) and _exec_succeeds(fp_bin)):
            logger.error("ffmpeg/ffprobe could not execute even after /tmp fallback.")
            raise RuntimeError("ffmpeg/ffprobe not executable")

    # pydub / 環境変数へ反映
    os.environ["FFMPEG_BINARY"] = ff_bin
    os.environ["FFPROBE_BINARY"] = fp_bin
    # PATH 先頭に追加（必要に応じて）
    bin_dir = str(Path(ff_bin).parent)
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

    # pydub に明示設定
    AudioSegment.converter = ff_bin
    AudioSegment.ffprobe = fp_bin

    logger.info(f"▶▶ Using FFMPEG_BINARY  : {ff_bin}")
    logger.info(f"▶▶ Using FFPROBE_BINARY : {fp_bin}")
    return ff_bin, fp_bin


# 解決を実行（モジュール import 時に一度だけ）
FFMPEG_BIN, FFPROBE_BIN = resolve_ffmpeg_and_ffprobe()
logger.info("▶▶ Module import success")


# ─── Function 本体 ────────────────────────────────────────
async def main(msg: func.QueueMessage) -> None:
    logger.info("▶▶ Function invoked")

    raw = msg.get_body().decode("utf-8", errors="replace")
    logger.info("▶▶ RAW payload: %s", raw)

    # /tmp（揮発）の再確認：スケールイン/コールドスタート直後に備えて
    # ※ 極力軽く：サイズ差のみでコピー
    try:
        if os.path.isfile(FFMPEG_BIN) and os.path.isfile(os.environ.get("FFMPEG_BINARY", FFMPEG_BIN)):
            # noop: 既に解決済み
            pass
        else:
            # 万一消えていたら再配置
            resolve_ffmpeg_and_ffprobe()
    except Exception:
        logger.exception("▶▶ Re-ensure ffmpeg/ffprobe failed")

    try:
        # JSON / Base64 自動判定
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            decoded = base64.b64decode(raw).decode("utf-8")
            body = json.loads(decoded)

        job_id            = body["job_id"]
        blob_url          = body["blob_url"]
        template_blob_url = body["template_blob_url"]
        logger.info(f"Received job {job_id}, blob: {blob_url}, template: {template_blob_url}")

        # 1. 音声を /tmp にダウンロード
        local_audio = os.path.join(TMP_DIR, f"{uuid.uuid4()}.mp4")
        logger.info(f"▶▶ STEP1-1: Downloading audio from {blob_url}")
        download_blob(blob_url, local_audio)
        logger.info(f"▶▶ STEP1-2: Audio downloaded to {local_audio}")

        # 2. Fast-Start（※ FFMPEG_BIN を使用）
        fixed_audio = os.path.join(TMP_DIR, f"{uuid.uuid4()}_fixed.mp4")
        subprocess.run(
            [
                FFMPEG_BIN,
                "-y",
                "-i", local_audio,
                "-c", "copy",
                "-movflags", "+faststart",
                fixed_audio,
            ],
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
        blob_docx  = f"processed/{job_id}.docx"
        process_document(template_path, local_docx, meeting_info)
        logger.info("▶▶ STEP5-2: Document processed")

        with open(local_docx, "rb") as fp:
            upload_to_blob(blob_docx, fp, add_audio_prefix=False)
        logger.info(f"Job {job_id} completed, saved to {blob_docx}")

    except Exception:
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
