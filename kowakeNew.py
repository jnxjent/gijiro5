# kowake.py
import os
import sys
import platform
import shutil
import subprocess
import tempfile
import uuid
import re
import json
import asyncio
import random
import time
from pathlib import Path
from urllib.parse import urlparse, quote
from dotenv import load_dotenv
from deepgram import Deepgram
import openai
from storage import upload_to_blob, download_blob

# ──────────────────────────────────────────────────────────────
# 構造化ログ（KQLで parse_json(message) しやすい1行JSON）
# ──────────────────────────────────────────────────────────────
JOB_ID = str(uuid.uuid4())

def log_evt(evt: str, **kw):
    payload = {"evt": evt, "ts": time.time(), "job_id": JOB_ID}
    payload.update(kw or {})
    print(json.dumps(payload, ensure_ascii=False), flush=True)

# ──────────────────────────────────────────────────────────────
# ffmpeg / ffprobe パス検出
# ──────────────────────────────────────────────────────────────
ffmpeg_path = os.getenv("FFMPEG_PATH")
ffprobe_path = os.getenv("FFPROBE_PATH")

if not (ffmpeg_path and ffprobe_path):
    BASE_DIR = os.path.dirname(__file__)
    BIN_ROOT = os.getenv("FFMPEG_HOME", os.path.join(BASE_DIR, "ffmpeg", "bin"))
    if platform.system() == "Windows":
        tb = os.path.join(BIN_ROOT, "win")
        ffmpeg_path = os.path.join(tb, "ffmpeg.exe")
        ffprobe_path = os.path.join(tb, "ffprobe.exe")
    else:
        tb = os.path.join(BIN_ROOT, "linux")
        ffmpeg_path = os.path.join(tb, "ffmpeg")
        ffprobe_path = os.path.join(tb, "ffprobe")

if not os.path.isfile(ffmpeg_path):
    ffmpeg_path = shutil.which("ffmpeg") or ffmpeg_path
if not os.path.isfile(ffprobe_path):
    ffprobe_path = shutil.which("ffprobe") or ffprobe_path

os.environ["PATH"] = os.path.dirname(ffmpeg_path) + os.pathsep + os.environ.get("PATH", "")
os.environ["FFMPEG_BINARY"] = ffmpeg_path
os.environ["FFPROBE_BINARY"] = ffprobe_path

print(f"[INFO] Using ffmpeg:  {ffmpeg_path}")
print(f"[INFO] Using ffprobe: {ffprobe_path}")

# ──────────────────────────────────────────────────────────────
# 環境変数読み込み & 設定（安全側の既定値）
# ──────────────────────────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE")
DEPLOYMENT_ID = os.getenv("DEPLOYMENT_ID")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
TEMPERATURE = float(os.getenv("TEMPERATURE", 0.7))

# Deepgram / 分割 / 並列・リトライ
DG_TIMEOUT_TOTAL_SEC = int(os.getenv("DG_TIMEOUT_TOTAL_SEC", "240"))  # 1チャンク合計待ち上限
DG_MAX_RETRIES       = int(os.getenv("DG_MAX_RETRIES", "1"))          # 失敗時のリトライ回数
DG_BACKOFF_BASE_SEC  = float(os.getenv("DG_BACKOFF_BASE_SEC", "2"))   # 指数バックオフ基点
DG_PARALLEL          = int(os.getenv("DG_PARALLEL", "3"))             # 同時投げ本数
CHUNK_LEN_SEC        = int(os.getenv("CHUNK_LEN_SEC", "480"))         # チャンク長（秒）
CHUNK_OVERLAP_SEC    = int(os.getenv("CHUNK_OVERLAP_SEC", "30"))      # オーバーラップ（秒）

openai.api_key = OPENAI_API_KEY
openai.api_base = OPENAI_API_BASE
openai.api_type = "azure"
openai.api_version = "2024-08-01-preview"

deepgram_client = Deepgram(DEEPGRAM_API_KEY)
TMP_DIR = tempfile.gettempdir()

def _log_runtime_knobs():
    log_evt("knobs", MAX_CONTENT_LENGTH_BYTES=os.getenv("MAX_CONTENT_LENGTH_BYTES"),
            DG_TIMEOUT_TOTAL_SEC=DG_TIMEOUT_TOTAL_SEC,
            DG_MAX_RETRIES=DG_MAX_RETRIES,
            DG_BACKOFF_BASE_SEC=DG_BACKOFF_BASE_SEC,
            DG_PARALLEL=DG_PARALLEL,
            CHUNK_LEN_SEC=CHUNK_LEN_SEC,
            CHUNK_OVERLAP_SEC=CHUNK_OVERLAP_SEC)
_log_runtime_knobs()

# ──────────────────────────────────────────────────────────────
# 例外（DG タイムアウトを上位に通知するため）
# ──────────────────────────────────────────────────────────────
class DGTimeoutError(TimeoutError):
    def __init__(self, chunk_idx: int, attempts: int, request_id: str | None = None, msg: str = "Deepgram timeout"):
        super().__init__(msg)
        self.chunk_idx = chunk_idx
        self.attempts = attempts
        self.request_id = request_id

# ──────────────────────────────────────────────────────────────
# Deepgram 呼び出し（timeout / retry 包装）
# ──────────────────────────────────────────────────────────────
async def _dg_prerecorded_with_retry(buf: bytes, chunk_idx: int) -> dict:
    rid: str | None = None
    for attempt in range(1, DG_MAX_RETRIES + 2):  # 初回 + リトライ回数
        try:
            t_attempt_start = time.monotonic()

            # thundering herd 回避の微小ジッター／指数バックオフ
            if attempt == 1:
                await asyncio.sleep(random.uniform(0.02, 0.12))
            else:
                backoff = min(DG_BACKOFF_BASE_SEC * (2 ** (attempt - 1)), 10.0)  # ← 上限クリップ
                jitter = random.uniform(0, 0.3 * backoff)
                wait = backoff + jitter
                log_evt("dg_retry_wait", chunk_idx=chunk_idx, wait_sec=round(wait, 2), attempt=attempt)
                await asyncio.sleep(wait)

            # 総待機をアプリ側で制御
            resp = await asyncio.wait_for(
                deepgram_client.transcription.prerecorded(
                    {"buffer": buf, "mimetype": "audio/wav"},
                    {
                        "model": "nova-2-general",
                        "detect_language": True,
                        "diarize": True,
                        "utterances": True,
                    },
                ),
                timeout=DG_TIMEOUT_TOTAL_SEC,
            )

            # 応答メタと計測
            try:
                rid = (resp.get("metadata") or {}).get("request_id")
            except Exception:
                rid = None

            dur_ms = int((time.monotonic() - t_attempt_start) * 1000)
            log_evt("dg_ok", chunk_idx=chunk_idx, attempt=attempt, dur_ms=dur_ms, request_id=rid)
            return resp

        except asyncio.TimeoutError:
            log_evt("dg_timeout", chunk_idx=chunk_idx, attempt=attempt, timeout_sec=DG_TIMEOUT_TOTAL_SEC)
            # 続けてリトライ（ループ先頭へ）
        except Exception as e:
            log_evt("dg_error", chunk_idx=chunk_idx, attempt=attempt, error=f"{type(e).__name__}: {str(e)}")
            # 続けてリトライ

    # 失敗確定（実試行回数を attempts として記録：初回 + リトライ）
    raise DGTimeoutError(chunk_idx=chunk_idx, attempts=(DG_MAX_RETRIES + 1), request_id=rid)

# ──────────────────────────────────────────────────────────────
# 音声 → Deepgram → OpenAI 整形
# ──────────────────────────────────────────────────────────────
async def _transcribe_chunk(idx: int, chunk_path: str) -> str:
    t0 = time.monotonic()
    log_evt("chunk_start", chunk_idx=idx, path=chunk_path)

    wav_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}_chunk_{idx}.wav")
    try:
        # 16kHz/mono 変換（安定）
        subprocess.run(
            [ffmpeg_path, "-y", "-i", chunk_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
            check=True,
        )
        with open(wav_path, "rb") as f:
            buf = f.read()
    finally:
        # 中間ファイルは確実に掃除
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass
        try:
            if os.path.exists(chunk_path):
                os.remove(chunk_path)
        except Exception:
            pass

    # Deepgram（timeout / retry 付き）
    resp = await _dg_prerecorded_with_retry(buf, idx)

    uts = (resp.get("results") or {}).get("utterances") or []
    out = "\n".join(f"[Speaker {u.get('speaker')}] {u.get('transcript')}" for u in uts)

    log_evt("chunk_end", chunk_idx=idx, dur_ms=int((time.monotonic()-t0)*1000), utterances=len(uts))
    return out

async def transcribe_and_correct(source: str) -> str:
    log_evt("process_start", source=source)
    downloaded = False
    local_audio = source

    try:
        # 1) URL判定 & ダウンロード
        if source.lower().startswith("http"):
            parsed = urlparse(source)
            safe_path = "/".join(quote(p) for p in parsed.path.split("/"))
            safe_url = f"{parsed.scheme}://{parsed.netloc}{safe_path}"
            if parsed.query:
                safe_url += f"?{parsed.query}"

            ext = os.path.splitext(parsed.path)[1]
            local_audio = os.path.join(TMP_DIR, f"{uuid.uuid4()}{ext}")
            log_evt("download_start", url=safe_url, save_to=local_audio)
            download_blob(safe_url, local_audio)
            downloaded = True
            try:
                sz = os.path.getsize(local_audio)
            except Exception:
                sz = None
            log_evt("download_done", path=local_audio, bytes=sz)

        # 2) Fast-Start 適用
        ext = os.path.splitext(local_audio)[1]
        fixed = os.path.join(TMP_DIR, f"{uuid.uuid4()}_fixed{ext}")
        log_evt("faststart_start", src=local_audio, dst=fixed)
        subprocess.run(
            [ffmpeg_path, "-y", "-i", local_audio, "-c", "copy", "-movflags", "+faststart", fixed],
            check=True,
        )
        log_evt("faststart_done", dst=fixed)

        # 3) 長さ取得 (秒)
        cmd = [
            ffprobe_path,
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            fixed,
        ]
        duration = float(subprocess.check_output(cmd).strip())
        log_evt("audio_info", duration_sec=duration, chunk_len_sec=CHUNK_LEN_SEC, overlap_sec=CHUNK_OVERLAP_SEC)

        # チャンク設定
        chunk_len = CHUNK_LEN_SEC
        overlap = CHUNK_OVERLAP_SEC
        step = max(1, chunk_len - overlap)

        # 4) ffmpeg でチャンク分割
        chunk_paths: list[tuple[int, str]] = []
        start = 0.0
        cidx = 0
        while start < duration:
            out_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}_seg_{cidx}.mp4")
            seg_len = max(1.0, min(chunk_len, duration - start))
            subprocess.run(
                [ffmpeg_path, "-y", "-ss", str(start), "-t", str(seg_len), "-i", fixed, "-c", "copy", out_path],
                check=True,
            )
            chunk_paths.append((cidx, out_path))
            cidx += 1
            start += step
        log_evt("chunk_split_done", chunks=len(chunk_paths))

        # 5) 並列送信（安全重視で同時 DG_PARALLEL 本、バッチ送信）
        corrected: list[str] = []
        for i in range(0, len(chunk_paths), DG_PARALLEL):
            batch = chunk_paths[i : i + DG_PARALLEL]
            # 微小ジッターで投入
            tasks = []
            for (cidx, path) in batch:
                await asyncio.sleep(random.uniform(0.01, 0.12))
                tasks.append(_transcribe_chunk(cidx, path))

            results = await asyncio.gather(*tasks)
            for text in results:
                prompt = (
                    "以下の音声書き起こしを自然な日本語にしてください。\n\n"
                    f"{text}\n\n"
                    "【出力形式】\n[Speaker X] 発話内容\n[Speaker X] 発話内容\n"
                )
                t_oa = time.monotonic()
                resp = openai.ChatCompletion.create(
                    engine=DEPLOYMENT_ID,
                    messages=[
                        {"role": "system", "content": "あなたは日本語整形アシスタントです。"},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    max_tokens=4000,
                )
                log_evt("oa_done", dur_ms=int((time.monotonic()-t_oa)*1000),
                        model="azure_openai", engine=DEPLOYMENT_ID, max_tokens=4000)
                corrected.append(resp.choices[0].message.content)

        full = "\n".join(corrected)

        # 置換ステップ
        replaced_text, hit = _apply_keyword_replacements(full)
        log_evt("keyword_replace", hits=hit)

        log_evt("process_end", status="ok", chunks=len(chunk_paths))
        return replaced_text

    except Exception as e:
        # 例外終了も記録
        log_evt("process_end", status="error", error=f"{type(e).__name__}: {str(e)}")
        raise

    finally:
        # クリーンアップ
        try:
            if downloaded and os.path.exists(local_audio):
                os.remove(local_audio)
        except Exception:
            pass
        try:
            if 'fixed' in locals() and os.path.exists(fixed):
                os.remove(fixed)
        except Exception:
            pass

# ─── キーワード管理 / Blob 連携 ────────────────────────────────
_KEYWORDS_DB: list[dict] = []
BLOB_JSON_PATH = "settings/keywords.json"

def _apply_keyword_replacements(text: str) -> tuple[str, int]:
    """
    テキストに対してキーワード置換を実行し、置換後テキストとヒット数を返します。
    """
    total_hit = 0
    for kw in _KEYWORDS_DB:
        corr = kw["keyword"]
        tgts = [kw["reading"]] + [
            e.strip() for e in re.split(r"[,\uFF0C\u3001]", kw.get("wrong_examples", "")) if e.strip()
        ]
        for t in tgts:
            pat = re.compile(re.escape(t), flags=re.IGNORECASE)
            text, n_hits = pat.subn(corr, text)
            total_hit += n_hits
    print(f"[DEBUG] keyword replace hit = {total_hit}", file=sys.stderr, flush=True)
    return text, total_hit

# -------------------- CRUD + ログ ---------------------------------

def get_all_keywords():
    return _KEYWORDS_DB

def get_keyword_by_id(id):
    # バグ修正: != → ==（一致するIDを返す）
    return next((k for k in _KEYWORDS_DB if k["id"] == id), None)

def add_keyword(reading, wrong_examples, keyword):
    before = len(_KEYWORDS_DB)
    _KEYWORDS_DB.append({
        "id": str(uuid.uuid4()),
        "reading": reading,
        "wrong_examples": wrong_examples,
        "keyword": keyword,
    })
    after = len(_KEYWORDS_DB)
    print(f"[ADD] keywords {before} → {after}")
    _save_keywords_to_blob()

def delete_keyword_by_id(id):
    global _KEYWORDS_DB
    before = len(_KEYWORDS_DB)
    _KEYWORDS_DB = [k for k in _KEYWORDS_DB if k["id"] != id]
    after = len(_KEYWORDS_DB)
    print(f"[DEL] keywords {before} → {after}")
    _save_keywords_to_blob()

def update_keyword_by_id(id, reading, wrong_examples, keyword):
    for k in _KEYWORDS_DB:
        if k["id"] == id:
            k["reading"] = reading
            k["wrong_examples"] = wrong_examples
            k["keyword"] = keyword
            print(f"[UPDATE] keyword id={id} を更新しました")
            break
    _save_keywords_to_blob()

def load_keywords_from_file():
    global _KEYWORDS_DB
    try:
        tmp = os.path.join(TMP_DIR, "keywords.json")
        Path(tmp).parent.mkdir(exist_ok=True, parents=True)
        try:
            download_blob(BLOB_JSON_PATH, tmp)
            print(f"[INFO] Blob からダウンロード完了 → {tmp}")
        except Exception as e:
            print(f"[INFO] Blob 取得スキップ: {e}")

        local_json = os.path.abspath("keywords.json")
        candidate = local_json if os.path.exists(local_json) else tmp

        with open(candidate, encoding="utf-8") as f:
            _KEYWORDS_DB = json.load(f)

        print(f"[INFO] キーワード {len(_KEYWORDS_DB)} 件ロード ({candidate})")
        print("[DEBUG] SAMPLE:", _KEYWORDS_DB[:3])
    except Exception as e:
        print(f"[WARN] キーワード読込失敗: {e}")
        _KEYWORDS_DB = []

def _save_keywords_to_blob():
    try:
        tmp = os.path.join(TMP_DIR, "keywords.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_KEYWORDS_DB, f, ensure_ascii=False, indent=2)
        with open(tmp, "rb") as f:
            upload_to_blob(BLOB_JSON_PATH, f)
        print("[INFO] キーワード保存完了")
    except Exception as e:
        print(f"[ERROR] キーワード保存失敗: {e}"