"""Text statistics for skill `text-stats`.

Usage:
    python count.py --text "your text here"

Outputs JSON to stdout, diagnostics to stderr.
Exit code: 0 on success, 1 on bad input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys


_CHINESE_RE = re.compile(r"[一-鿿]")


def stats(text: str) -> dict:
    chars = len(text)
    chars_no_space = len(re.sub(r"\s", "", text))
    words = len(text.split())
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0) if text else 0
    paragraphs = len([p for p in re.split(r"\n\s*\n", text) if p.strip()])
    chinese_chars = len(_CHINESE_RE.findall(text))
    chinese_ratio = round(chinese_chars / chars, 4) if chars else 0.0
    return {
        "chars": chars,
        "chars_no_space": chars_no_space,
        "words": words,
        "lines": lines,
        "paragraphs": paragraphs,
        "chinese_chars": chinese_chars,
        "chinese_ratio": chinese_ratio,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Text statistics: chars/words/lines/paragraphs/chinese ratio.")
    ap.add_argument("--text", required=True, help="text to analyze (wrap in quotes)")
    args = ap.parse_args()
    if args.text is None:
        print("error: --text is required", file=sys.stderr)
        return 1
    result = stats(args.text)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
