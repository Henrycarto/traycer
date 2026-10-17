"""Writing rules, enforced in CI.

Two rules across the whole repository, in code comments, documentation, UI
copy and commit ready files alike:

1. No em dashes. They are the single clearest tell of unedited generated
   prose, and a comma or a full stop always works instead.
2. No filler vocabulary. The list below is not a matter of taste. Each word
   is one that reads as marketing when it appears in a technical document, and
   a clinician or an investor reading this repository is entitled to prose
   that says what it means.

Word boundaries are checked on both sides, so domain terms that happen to
contain a listed word are not flagged.

One word gets special handling. An unlock is a real phone event and
`unlock_count` is a real sensor feature, so a flat ban on the token would
force the SDK to rename a measurement to satisfy a writing rule. The banned
forms are the marketing constructions instead: "unlock the", "unlock your",
"unlocking value" and their variants.

Lock files are skipped. They carry dependency descriptions written by other
people, and rewriting those is neither possible nor the point.

This file exempts itself, because a checker that trips on its own word list is
a checker nobody runs twice.
"""

from __future__ import annotations

import pathlib
import re
import sys

SKIP_DIRS = {
    ".git",
    ".github",
    ".next",
    ".turbo",
    ".venv",
    "__pycache__",
    "coverage",
    "dist",
    "node_modules",
    "venv",
}

SUFFIXES = {".ts", ".tsx", ".py", ".md", ".json", ".yml", ".yaml", ".css", ".sql", ".tf"}

SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}

SELF = pathlib.Path(__file__).name

BANNED_WORDS = (
    "robust",
    "seamless",
    "leverage",
    "leverages",
    "leveraging",
    "cutting-edge",
    "revolutionize",
    "revolutionizes",
    "powerful",
    "comprehensive",
    "streamline",
    "streamlines",
    "streamlined",
)

BANNED_PHRASES = (
    r"unlock(?:s|ing)?\s+(?:the|your|its|new|real|full|hidden)\b",
    r"unlock(?:s|ing)?\s+\w+\s+(?:potential|value|insight)",
)

EM_DASH = "2014"

BANNED_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(word) for word in BANNED_WORDS) + r")\b",
    re.IGNORECASE,
)

PHRASE_PATTERN = re.compile("|".join(BANNED_PHRASES), re.IGNORECASE)


def scan(root: pathlib.Path) -> list[str]:
    failures: list[str] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name == SELF or path.name in SKIP_FILES:
            continue

        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if EM_DASH in line:
                failures.append(f"{path}:{number} em dash")

            word_match = BANNED_PATTERN.search(line)
            if word_match:
                failures.append(f"{path}:{number} banned word: {word_match.group(1)}")

            phrase_match = PHRASE_PATTERN.search(line)
            if phrase_match:
                failures.append(f"{path}:{number} banned phrase: {phrase_match.group(0)}")

    return failures


def main() -> int:
    failures = scan(pathlib.Path("."))

    if failures:
        for failure in failures[:80]:
            print(failure)
        if len(failures) > 80:
            print(f"... and {len(failures) - 80} more")
        print(f"\n{len(failures)} writing rule violations")
        return 1

    print("writing rules pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
