"""Filename extraction and safe username suffix generation."""

import os
import re
import unicodedata


_OLD_NAME = re.compile(r"old\s+file\s+name\s*(?:[:：]\s*[-–]?\s*|[-–]\s*)(.+)", re.IGNORECASE)
_INVALID = set('<>:\\|?*"')


def extract_original_filename(response_text: str | None, fallback: str | None = None) -> str | None:
    """Extract the filename from the rename bot's visible prompt."""
    if response_text:
        match = _OLD_NAME.search(response_text)
        if match:
            candidate = match.group(1).strip().strip("`*_ ")
            candidate = candidate.splitlines()[0].strip().strip("`*_ ")
            candidate = candidate.replace("\\", "/").rsplit("/", 1)[-1]
            return candidate or None
    if fallback:
        candidate = os.path.basename(fallback.replace("\\", "/")).strip()
        return candidate or None
    return None


def sanitize_filename(filename: str) -> str:
    """Remove path components and unsafe/control characters while retaining Unicode."""
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = []
    for character in filename:
        category = unicodedata.category(character)
        if character in _INVALID or category in {"Cc", "Cs"}:
            continue
        cleaned.append(character)
    result = "".join(cleaned).strip().strip(".")
    return result or "renamed_file"


def append_username(filename: str, username: str) -> str:
    """Insert a configured username before the extension, once only."""
    clean_name = sanitize_filename(filename)
    tag = sanitize_filename(username.strip()) if username.strip() else ""
    if not tag:
        return clean_name

    stem, extension = os.path.splitext(clean_name)
    if tag.casefold() in stem.casefold():
        return clean_name
    return f"{stem} {tag}{extension}"
