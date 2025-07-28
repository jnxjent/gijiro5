# test_pipeline.py

import asyncio
from kowake import load_keywords_from_file, transcribe_and_correct

# キーワードロード
load_keywords_from_file()

# Windowsパスは raw 文字列で記述
SRC = r"C:\Users\021213\OneDrive - 株式会社ミダック\ドキュメント\AI研究会\議事録アプリ関連\音源サンプル\ジュピターアドバイザーズShorten.mp4"

# 非同期パイプライン実行
if __name__ == "__main__":
    result = asyncio.run(transcribe_and_correct(SRC))
    print("▶ processed text:\n", result)
