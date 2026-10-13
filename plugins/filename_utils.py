"""Filename extraction, standard target naming, and tolerant video metadata matching."""

import os
import re
import unicodedata

_OLD_NAME = re.compile(r"old\s+file\s+name\s*(?:[:：]\s*[-–]?\s*|[-–]\s*)(.+)", re.IGNORECASE)
_INVALID = set('<>:\\|?*"')
_SEASON_EPISODE = re.compile(
    r"(?<![A-Za-z])S(?:EASON)?[\s._:-]*(\d{1,2})[\s._:-]*E(?:P(?:ISODE)?)?[\s._:-]*(\d{1,3})(?!\d)",
    re.IGNORECASE,
)
_SEASON_EPISODE_WORDS = re.compile(
    r"(?<![A-Za-z])SEASON[\s._:-]*(\d{1,2})[\s._:-]*EPISODE[\s._:-]*(\d{1,3})(?!\d)",
    re.IGNORECASE,
)
_EPISODE_ONLY = re.compile(r"(?<![A-Za-z])(?:EPISODE|EP|E)[\s._:-]*(\d{1,3})(?!\d)", re.IGNORECASE)
_QUALITY = re.compile(r"(?<!\d)(2160|1440|1080|720|480)\s*p?(?!\d)|\b4\s*k\b", re.IGNORECASE)
_QUALITY_VALUES = {"480": 480, "720": 720, "1080": 1080, "1440": 1440, "2160": 2160, "4k": 2160}
_QUALITY_ORDER = {480: 0, 720: 1, 1080: 2, 1440: 3, 2160: 4, None: 5}
_TITLE_STOPWORDS = {
    "a", "an", "and", "audio", "dubbed", "dual", "episode", "ep", "for", "hindi",
    "in", "multi", "of", "season", "subbed", "the", "with",
}


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


def _parse_filename(filename: str | None) -> dict:
    name = os.path.basename((filename or "").replace("\\", "/"))
    stem = os.path.splitext(name)[0]
    season_match = _SEASON_EPISODE_WORDS.search(stem) or _SEASON_EPISODE.search(stem)
    episode_match = season_match or _EPISODE_ONLY.search(stem)
    season = episode = None
    if season_match:
        season, episode = int(season_match.group(1)), int(season_match.group(2))
        normalized_episode = f" S{season:02d}E{episode:02d} "
    elif episode_match:
        episode = int(episode_match.group(1))
        normalized_episode = f" E{episode:02d} "
    else:
        normalized_episode = None

    quality_matches = [
        match for match in _QUALITY.finditer(stem)
        if not episode_match or match.end() <= episode_match.start() or match.start() >= episode_match.end()
    ]
    quality = None
    if quality_matches:
        token = re.sub(r"\s+", "", quality_matches[0].group(0)).casefold().removesuffix("p")
        quality = _QUALITY_VALUES.get(token)

    group_stem = stem
    if episode_match and normalized_episode:
        group_stem = stem[:episode_match.start()] + normalized_episode + stem[episode_match.end():]
    episode_delta = len(normalized_episode) - (episode_match.end() - episode_match.start()) if episode_match else 0
    for match in reversed(quality_matches):
        start, end = match.start(), match.end()
        if episode_match and start >= episode_match.end():
            start += episode_delta
            end += episode_delta
        group_stem = group_stem[:start] + " " + group_stem[end:]

    # Title matching intentionally removes bracketed prefixes and all episode/quality
    # annotations, so episode-only coincidences cannot route a file by themselves.
    title_stem = re.sub(r"\[[^\]]*\]", " ", stem)
    title_match = _SEASON_EPISODE_WORDS.search(title_stem) or _SEASON_EPISODE.search(title_stem)
    title_match = title_match or _EPISODE_ONLY.search(title_stem)
    if title_match:
        title_stem = title_stem[:title_match.start()] + " " + title_stem[title_match.end():]
    title_stem = _QUALITY.sub(" ", title_stem)
    title_tokens = [
        token for token in re.findall(r"[^\W_]+", title_stem.casefold(), flags=re.UNICODE)
        if token not in _TITLE_STOPWORDS
    ]
    return {
        "season": season,
        "episode": episode,
        "quality": quality,
        "group_key": " ".join(re.findall(r"[^\W_]+", group_stem.casefold(), flags=re.UNICODE)),
        "title_key": " ".join(title_tokens),
    }


def extract_video_metadata(filename: str | None, caption: str | None = None) -> dict:
    """Detect episode/quality/title from a filename, using caption only as fallback."""
    result = _parse_filename(filename)
    if caption:
        caption_metadata = _parse_filename(caption)
        for field in ("season", "episode", "quality"):
            if result[field] is None:
                result[field] = caption_metadata[field]
        if not result["title_key"]:
            result["title_key"] = caption_metadata["title_key"]
        if not result["group_key"]:
            result["group_key"] = caption_metadata["group_key"]
    return result


def format_standardized_filename(username: str | None, metadata: dict | None, original_filename: str | None) -> str | None:
    """Format only metadata that was actually detected; retain the original extension."""
    original = sanitize_filename(original_filename) if original_filename else ""
    _, extension = os.path.splitext(original)
    metadata = metadata or {}
    parts = []
    prefix = sanitize_filename(username.strip()) if username and username.strip() else ""
    if prefix:
        parts.append(f"[{prefix}]")
    season, episode, quality = metadata.get("season"), metadata.get("episode"), metadata.get("quality")
    if season is not None:
        parts.append(f"S{int(season)}")
    if episode is not None:
        parts.append(f"Ep{int(episode)}")
    if quality is not None:
        parts.append(f"{int(quality)}p")
    if season is None and episode is None and quality is None:
        return append_username(original, username or "") if original else None
    return sanitize_filename(" ".join(parts) + extension)


def normalize_filename_key(filename: str | None) -> str:
    """Normalize punctuation/case for exact-name comparison across separator variants."""
    basename = os.path.basename((filename or "").replace("\\", "/"))
    stem, extension = os.path.splitext(basename)
    episode_match = _SEASON_EPISODE_WORDS.search(stem) or _SEASON_EPISODE.search(stem)
    episode_match = episode_match or _EPISODE_ONLY.search(stem)
    if episode_match:
        season_match = _SEASON_EPISODE_WORDS.search(stem) or _SEASON_EPISODE.search(stem)
        if season_match:
            episode_token = f"S{int(season_match.group(1))}E{int(season_match.group(2))}"
        else:
            episode_number = int(episode_match.group(1))
            episode_token = f"E{episode_number}"
        stem = stem[:episode_match.start()] + episode_token + stem[episode_match.end():]
    metadata = _parse_filename(basename)
    if metadata["quality"] is not None:
        stem = _QUALITY.sub(f"{metadata['quality']}p", stem)
    compact = "".join(re.findall(r"[^\W_]+", stem.casefold(), flags=re.UNICODE))
    return compact + extension.casefold()


def title_similarity(left: str | None, right: str | None) -> tuple[float, int]:
    """Return Jaccard title similarity and overlap count, excluding common media words."""
    left_tokens, right_tokens = set((left or "").split()), set((right or "").split())
    if not left_tokens or not right_tokens:
        return 0.0, 0
    overlap = left_tokens & right_tokens
    return len(overlap) / len(left_tokens | right_tokens), len(overlap)


def quality_priority(metadata: dict | None) -> int:
    """Return queue priority (lower is better) for known video qualities."""
    return _QUALITY_ORDER.get((metadata or {}).get("quality"), 5)
