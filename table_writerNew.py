if norm_label == normalize_text("議事"):
    raw_value = re.sub(
        r"【出力形式】[\s\S]*?---\s*",  # 先頭〜"---" + 改行までマッチ
        "",
        str(raw_value),
        flags=re.MULTILINE
    )
