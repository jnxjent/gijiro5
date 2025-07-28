# test_word.py

import re
from kowake import load_keywords_from_file, _KEYWORDS_DB, _apply_keyword_replacements

# 1) キーワード DB を読み込み
load_keywords_from_file()

# 2) テスト対象のサンプル文字列
sample = "演習在籍 演習採石、演習 在籍 をテストします。"

# 3) 各パターンがマッチするかを確認
print("=== パターンマッチ確認 ===")
for k in _KEYWORDS_DB:
    tgts = [k["reading"]] + [
        e.strip()
        for e in re.split(r"[,\uFF0C\u3001]", k.get("wrong_examples", ""))
        if e.strip()
    ]
    for t in tgts:
        matched = bool(re.search(re.escape(t), sample, flags=re.IGNORECASE))
        print(f"pattern={t!r} → {'MATCH' if matched else 'NO MATCH'}")

# 4) 置換前後の比較とヒット数ログ
print("\n=== 置換前後比較 ===")
print("before:", sample)
after = _apply_keyword_replacements(sample)
print(" after:", after)
