```diff
--- a/kowake.py
+++ b/kowake.py
@@
-def _apply_keyword_replacements(text: str) -> str:
-    total_hit = 0  # 何語置換できたか集計
-    for kw in _KEYWORDS_DB:
-        corr = kw["keyword"]
-        # 半角 , 全角 ， 読点 、 をいずれも区切り文字として split
-        tgts = [kw["reading"]] + [
-            e.strip() for e in re.split(r"[,，、]", kw.get("wrong_examples", "")) if e.strip()
-        ]
-        for t in tgts:
-            pat = re.compile(re.escape(t), flags=re.IGNORECASE)
-            text, n_hits = pat.subn(corr, text)  # 置換してヒット数取得
-            total_hit += n_hits
-    print(f"[DEBUG] keyword replace hit = {total_hit}")
-    return text
+def _apply_keyword_replacements(text: str) -> tuple[str, int]:
+    """
+    テキストに対してキーワード置換を実行し、置換後テキストとヒット数を返します。
+    """
+    total_hit = 0
+    for kw in _KEYWORDS_DB:
+        corr = kw["keyword"]
+        tgts = [kw["reading"]] + [
+            e.strip() for e in re.split(r"[,\uFF0C\u3001]", kw.get("wrong_examples", "")) if e.strip()
+        ]
+        for t in tgts:
+            pat = re.compile(re.escape(t), flags=re.IGNORECASE)
+            text, n_hits = pat.subn(corr, text)
+            total_hit += n_hits
+    # 標準エラーに置換ヒット数を出力
+    print(f"[DEBUG] keyword replace hit = {total_hit}", file=sys.stderr, flush=True)
+    # 置換後テキストとヒット数を返す
+    return text, total_hit
```
