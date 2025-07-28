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
from pathlib import Path
from urllib.parse import urlparse, quote
from dotenv import load_dotenv
from deepgram import Deepgram
import openai
from storage import upload_to_blob, download_blob

# ── 1) ffmpeg / ffprobe パス検出 ───────────────────────────────
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

# ── 2) 環境変数読み込み ─────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE")
DEPLOYMENT_ID = os.getenv("DEPLOYMENT_ID")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
TEMPERATURE = float(os.getenv("TEMPERATURE", 0.7))

openai.api_key = OPENAI_API_KEY
openai.api_base = OPENAI_API_BASE
openai.api_type = "azure"
openai.api_version = "2024-08-01-preview"

deepgram_client = Deepgram(DEEPGRAM_API_KEY)
TMP_DIR = tempfile.gettempdir()

# ──────────────────────────────────────────────────────────────
# 音声 → Deepgram → OpenAI 整形
# ──────────────────────────────────────────────────────────────
async def _transcribe_chunk(idx: int, chunk_path: str) -> str:
    wav_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}_chunk_{idx}.wav")
    subprocess.run(
        [ffmpeg_path, "-y", "-i", chunk_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
        check=True,
    )
    with open(wav_path, "rb") as f:
        buf = f.read()
    os.remove(wav_path)
    os.remove(chunk_path)

    resp = await deepgram_client.transcription.prerecorded(
        {"buffer": buf, "mimetype": "audio/wav"},
        {"model": "nova-2-general", "detect_language": True, "diarize": True, "utterances": True},
    )
    return "\n".join(
        f"[Speaker {u['speaker']}] {u['transcript']}" for u in resp["results"]["utterances"]
    )

async def transcribe_and_correct(source: str) -> str:
    # 1) URL判定 & ダウンロード
    if source.lower().startswith("http"):
        parsed = urlparse(source)
        safe_path = "/".join(quote(p) for p in parsed.path.split("/"))
        safe_url = f"{parsed.scheme}://{parsed.netloc}{safe_path}"
        if parsed.query:
            safe_url += f"?{parsed.query}"
        ext = os.path.splitext(parsed.path)[1]
        local_audio = os.path.join(TMP_DIR, f"{uuid.uuid4()}{ext}")
        download_blob(safe_url, local_audio)
    else:
        local_audio = source

    # 2) Fast‑Start 適用
    ext = os.path.splitext(local_audio)[1]
    fixed = os.path.join(TMP_DIR, f"{uuid.uuid4()}_fixed{ext}")
    subprocess.run(
        [ffmpeg_path, "-y", "-i", local_audio, "-c", "copy", "-movflags", "+faststart", fixed],
        check=True,
    )

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
    chunk_len = 10 * 60  # 10分
    overlap = 1 * 60  # 1分
    step = chunk_len - overlap

    # 4) ffmpeg でチャンク分割
    chunk_paths = []
    start = 0.0
    idx = 0
    while start < duration:
        out_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}_seg_{idx}.mp4")
        subprocess.run(
            [ffmpeg_path, "-y", "-ss", str(start), "-t", str(min(chunk_len, duration - start)), "-i", fixed, "-c", "copy", out_path],
            check=True,
        )
        chunk_paths.append((idx, out_path))
        idx += 1
        start += step

    # 5) 並列送信 → 整形AI
    corrected = []
    for i in range(0, len(chunk_paths), 6):
        tasks = [_transcribe_chunk(idx, path) for idx, path in chunk_paths[i : i + 6]]
        results = await asyncio.gather(*tasks)
        for text in results:
            prompt = (
                "以下の音声書き起こしを自然な日本語にしてください。\n\n"
                f"{text}\n\n"
                "【出力形式】\n[Speaker X] 発話内容\n[Speaker X] 発話内容\n"
            )
            resp = openai.ChatCompletion.create(
                engine=DEPLOYMENT_ID,
                messages=[
                    {"role": "system", "content": "あなたは日本語整形アシスタントです。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=4000,
            )
            corrected.append(resp.choices[0].message.content)

    full = "\n".join(corrected)

    # クリーンアップ
    if source.lower().startswith("http"):
        os.remove(local_audio)
    os.remove(fixed)

    # 置換ステップ追加
    replaced_text, hit = _apply_keyword_replacements(full)
    return replaced_text

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
            print(f"[INFO] Blob 取得ス킥: {e}")

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
        print(f"[ERROR] キーワード保存失敗: {e}")
