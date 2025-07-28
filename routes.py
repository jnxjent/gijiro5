from flask import request, render_template, jsonify, redirect, send_file
import logging
import os
import time
import uuid
from pathlib import Path
from azure.storage.blob import BlobClient
from storage import generate_upload_sas, enqueue_processing, upload_to_blob
from kowake import (
    load_keywords_from_file,
    get_all_keywords,
    add_keyword,
    delete_keyword_by_id,
    get_keyword_by_id,
    update_keyword_by_id,
)

def setup_routes(app):
    logger = logging.getLogger("routes")
    logging.basicConfig(level=logging.INFO)
    logger.info("✔ setup_routes() 開始")
    load_keywords_from_file()   # ← これを追加するだけ
    # ─── トップページ ───────────────────────────
    @app.route("/", methods=["GET"])
    def index():
        logger.info("✔ / にアクセスされました")
        return render_template("index.html")

    # ─── ヘルスチェック ───────────────────────
    @app.route("/health", methods=["GET"])
    def health():
        logger.info("✔ /health にアクセス")
        return jsonify({"status": "OK"}), 200

    @app.route("/results/<job_id>", methods=["GET"])
    def result_page(job_id):
        return render_template("result.html", job_id=job_id)

    # ─── Azure AD コールバック ───────────────
    @app.route("/api/auth/callback/azure-ad", methods=["GET", "POST"])
    def azure_ad_callback():
        try:
            code = request.args.get("code")
            state = request.args.get("state")
            error = request.args.get("error")

            if error:
                logger.error(f"認証エラー: {error}")
                return jsonify({"error": error}), 400
            if not code:
                logger.error("認証コードがありません")
                return jsonify({"error": "認証コードがありません"}), 400

            logger.info(f"Azure AD 認証成功！code={code}, state={state}")
            return jsonify({
                "message": "Azure AD 認証成功！",
                "code": code,
                "state": state
            })
        except Exception as e:
            logger.error(f"エラー発生: {e}")
            return jsonify({"error": f"エラー発生: {e}"}), 500

    # ─── Blob SAS 発行 ────────────────────────
    @app.route("/api/blob/sas", methods=["GET"])
    def api_blob_sas():
        blob_name = request.args.get("name")
        if not blob_name:
            logger.error("SAS URL 生成エラー: name パラメーターがありません")
            return jsonify({"error": "name parameter is required"}), 400
        sas_info = generate_upload_sas(blob_name)
        return jsonify(sas_info)

    # ─── 非同期ジョブ登録 ─────────────────────
    @app.route("/api/process", methods=["POST"])
    def api_process():
        data = request.get_json()
        blob_url = data.get("blobUrl")
        template_blob_url = data.get("templateBlobUrl")

        if not blob_url or not template_blob_url:
            logger.error("ジョブ登録エラー: blobUrl または templateBlobUrl が不足")
            return jsonify({"error": "blobUrl and templateBlobUrl are required"}), 400

        job_id = uuid.uuid4().hex
        enqueue_processing(blob_url, template_blob_url, job_id)
        logger.info(f"✔ ジョブ登録完了: job_id={job_id}")
        return jsonify({"jobId": job_id}), 202

    @app.route("/api/process/<job_id>/status", methods=["GET"])
    def api_status(job_id):
        result_blob = f"processed/{job_id}.docx"
        blob_client = BlobClient.from_connection_string(
            os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
            os.getenv("AZURE_STORAGE_CONTAINER_NAME"),
            result_blob
        )
        try:
            if blob_client.exists():
                return jsonify({"status": "Completed", "resultUrl": blob_client.url})
            else:
                return jsonify({"status": "Processing"}), 202
        except Exception as e:
            logger.error(f"ステータス確認中にエラー: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/process/<job_id>/wait", methods=["GET"])
    def api_wait_for_result(job_id):
        max_wait_sec = 600
        interval_sec = 5
        result_blob = f"processed/{job_id}.docx"
        blob_client = BlobClient.from_connection_string(
            os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
            os.getenv("AZURE_STORAGE_CONTAINER_NAME"),
            result_blob
        )

        elapsed = 0
        while elapsed < max_wait_sec:
            if blob_client.exists():
                local_path = Path("downloads") / f"{job_id}.docx"
                with open(local_path, "wb") as f:
                    download_stream = blob_client.download_blob()
                    f.write(download_stream.readall())
                return send_file(local_path, as_attachment=True)

            time.sleep(interval_sec)
            elapsed += interval_sec

        return jsonify({"error": "処理が完了しませんでした"}), 504

    # ─── キーワード管理 ────────────────────────
    @app.route("/keywords", methods=["GET"])
    def keywords_page():
        keywords = get_all_keywords()
        print(f"🟡 /keywords loaded = {len(keywords)}")  # ★ログ①
        return render_template("keywords.html", keywords=keywords)

    @app.route("/register_keyword", methods=["POST"])
    def register_keyword():
        reading = request.form.get("reading")
        wrong_examples = request.form.get("wrong_examples")
        keyword = request.form.get("keyword")

        before = len(get_all_keywords())
        print(f"🟢 register before = {before}")          # ★ログ②

        add_keyword(reading, wrong_examples, keyword)

        after = len(get_all_keywords())
        print(f"🟢 register after  = {after}")           # ★ログ③
        return redirect("/keywords")

    @app.route("/delete_keyword", methods=["POST"])
    def delete_keyword():
        keyword_id = request.form.get("id")

        before = len(get_all_keywords())
        print(f"🔴 delete  before = {before}")           # ★ログ④

        delete_keyword_by_id(keyword_id)

        after = len(get_all_keywords())
        print(f"🔴 delete  after  = {after}")            # ★ログ⑤
        return redirect("/keywords")

    @app.route("/edit_keyword", methods=["GET"])
    def edit_keyword():
        keyword_id = request.args.get("id")
        keyword = get_keyword_by_id(keyword_id)
        return render_template("edit_keyword.html", keyword=keyword)

    @app.route("/update_keyword", methods=["POST"])
    def update_keyword():
        keyword_id = request.form.get("id")
        reading = request.form.get("reading")
        wrong_examples = request.form.get("wrong_examples")
        keyword_text = request.form.get("keyword")

        update_keyword_by_id(keyword_id, reading, wrong_examples, keyword_text)
        return redirect("/keywords")
