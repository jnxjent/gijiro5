# routes.py  (最小パッチ)
def setup_routes(app):
    logger = logging.getLogger("routes")
    logging.basicConfig(level=logging.INFO)
    logger.info("✔ setup_routes() 開始")

    load_keywords_from_file()   # ← これを追加するだけ
