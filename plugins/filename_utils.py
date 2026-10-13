"""Filename extraction, safe suffixes, and episode/quality metadata detection."""

import os
import re
import unicodedata

_OLD_NAME = re.compile(r"old\s+file\s+name\s*(?:[:：]\s*[-–]?\s*|[-–]\s*)(.+)", re.IGNORECASE)
_INVALID = set('<>:\\|?*"')
_SEASON_EPISODE = re.compile(
    r"\bS(?:EASON)?[\s._:-]*(\d{1,2})[\s._:-]*E(?:P(?:ISODE)?)?[\s._:-]*(\d{1,3})(?!\d)",
    re.IGNORECASE,
)
_SEASON_EPISODE_WORDS = re.compile(
    r"\bSEASON[\s._:-]*(\d{1,2})[\s._:-]*EPISODE[\s._:-]*(\d{1,3})(?!\d)",
    re.IGNORECASE,
)
_EPISODE_ONLY = re.compile(r"\b(?:EP|E)[\s._:-]*(\d{1,3})(?!\d)", re.IGNORECASE)
_QUALITY = re.compile(r"(?<!\d)(2160|1440|1080|720|480)\s*p?(?!\d)|\b4\s*k\b", re.IGNORECASE)
_QUALITY_VALUES = {"480": 480, "720": 720, "1080": 1080, "1440": 1440, "2160": 2160, "4k": 2160}
_QUALITY_ORDER = {480: 0, 720: 1, 1080: 2, 1440: 3, 2160: 4, None: 5}


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


def extract_video_metadata(filename: str | None) -> dict:
    """Detect season, episode, video quality and a quality-insensitive group key."""
    name = os.path.basename((filename or "").replace("\\", "/"))
    stem = os.path.splitext(name)[0]
    episode_match = _SEASON_EPISODE_WORDS.search(stem) or _SEASON_EPISODE.search(stem)
    season = episode = None
    if episode_match:
        season, episode = int(episode_match.group(1)), int(episode_match.group(2))
        normalized_episode = f" S{season:02d}E{episode:02d} "
        group_stem = stem[:episode_match.start()] + normalized_episode + stem[episode_match.end():]
    else:
        episode_match = _EPISODE_ONLY.search(stem)
        group_stem = stem
        if episode_match:
            episode = int(episode_match.group(1))
            normalized_episode = f" E{episode:02d} "
            group_stem = stem[:episode_match.start()] + normalized_episode + stem[episode_match.end():]

    quality_matches = [
        match for match in _QUALITY.finditer(stem)
        if not episode_match or match.end() <= episode_match.start() or match.start() >= episode_match.end()
    ]
    quality_match = quality_matches[0] if quality_matches else None
    quality = None
    if quality_match:
        token = re.sub(r"\s+", "", quality_match.group(0)).casefold()
        token = token.removesuffix("p")
        quality = _QUALITY_VALUES.get(token)

    episode_delta = (
        len(normalized_episode) - (episode_match.end() - episode_match.start())
        if episode_match else 0
    )
    for match in reversed(quality_matches):
        start, end = match.start(), match.end()
        if episode_match and start >= episode_match.end():
            start += episode_delta
            end += episode_delta
        group_stem = group_stem[:start] + " " + group_stem[end:]
    group_key = " ".join(re.findall(r"[^\W_]+", group_stem.casefold(), flags=re.UNICODE))
    return {
        "season": season,
        "episode": episode,
        "quality": quality,
        "group_key": group_key,
    }


def quality_priority(metadata: dict | None) -> int:
    """Return queue priority (lower is better) for known video qualities."""
    return _QUALITY_ORDER.get((metadata or {}).get("quality"), 5)
