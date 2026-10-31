"""Pure-Python helpers for the Discord X video bot (no Discord dependency)."""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# Extract candidates broadly, then strictly validate them in normalize_x_url().
_URL_CANDIDATE_RE = re.compile(r"https?://[^\s<>\[\]\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,!?;:)]}、。，！？；：」』】》〉"
_STATUS_ID_RE = re.compile(r"^[0-9]{1,25}$")
_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,80}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_PIXIV_ID_RE = re.compile(r"^[0-9]{1,25}$")
_PIXIV_HOSTS = {"pixiv.net", "www.pixiv.net", "touch.pixiv.net"}
_PIXIV_LOCALES = {"en", "ja", "zh", "zh-tw", "ko", "th", "id", "de", "fr", "es", "vi"}

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpeg", ".mpg"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".mka", ".aiff", ".alac"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS


SUPPORTED_COOKIE_BROWSERS = {"edge", "chrome", "chromium", "firefox", "brave", "vivaldi", "opera", "safari", "whale"}

def normalize_cookie_browser(value: object) -> str:
    """Validate an optional browser name used to read the user's existing cookies.

    Only the browser name is accepted (not arbitrary arguments or file paths).
    Empty means that no browser cookies are used.
    """
    if value is None:
        return ""
    browser = str(value).strip().lower()
    if not browser:
        return ""
    if browser not in SUPPORTED_COOKIE_BROWSERS:
        choices = ", ".join(sorted(SUPPORTED_COOKIE_BROWSERS))
        raise ValueError(f"cookies_from_browser は次のいずれかにしてください: {choices} (空欄ならCookieを使いません)")
    return browser


def normalize_cookie_browsers(value: object) -> list[str]:
    """Validate an ordered list of browsers to try for authenticated downloads."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        # A single browser string is accepted for convenient manual editing.
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError("cookies_from_browsers はブラウザー名の配列にしてください。")
    result: list[str] = []
    for item in values:
        browser = normalize_cookie_browser(item)
        if browser and browser not in result:
            result.append(browser)
    return result

def looks_like_auth_error(exc: BaseException) -> bool:
    """Identify download failures that may be caused by missing X login cookies.

    yt-dlp can report "No video could be found in this tweet" when X withholds
    the video details from an unauthenticated request, including some age-gated
    posts. This is not conclusive, but warrants trying the next configured browser.
    """
    message = str(exc).lower()
    hints = (
        "age-restricted", "age restricted", "sign in", "log in", "login",
        "authentication", "cookies", "cookie", "confirm your age",
        "confirm you're not a bot", "not a bot", "members-only", "private video",
        "unable to extract cookies", "could not copy chrome cookie database",
        "could not copy firefox cookie database", "failed to decrypt",
        "no video could be found in this tweet",
    )
    return any(hint in message for hint in hints)


def browser_cookie_lock_guidance(*messages: object) -> str:
    """Return actionable guidance when Chromium-family cookie storage is locked."""
    combined = " ".join(str(message) for message in messages).casefold()
    if "could not copy chrome cookie database" in combined:
        return (
            "Brave/ChromeのCookieデータベースを読み取れませんでした。Windowsではブラウザーが起動中、"
            "またはバックグラウンドで残っていると発生します。Braveをメニューから終了し、"
            "タスクマネージャーの詳細で残った brave.exe をすべて終了してから再試行してください。"
            "Braveを開いたまま使う場合は、FirefoxにもXでログインしておき、"
            "Firefoxだけを使う設定にしてください。"
        )
    return ""



def _metadata_text(value: object, limit: int = 4000) -> str:
    """Convert optional extractor metadata into safe, plain message text."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    # Remove NUL/control characters while keeping line breaks and tabs.
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return text[:limit].strip()


DEFAULT_DISPLAY_SETTINGS: dict[str, object] = {
    "show_author_name": True,
    "show_account_name": True,
    "show_post_text": True,
    "show_original_link": True,
    "heading": "X投稿情報",
}
DISPLAY_BOOLEAN_KEYS = {
    "show_author_name",
    "show_account_name",
    "show_post_text",
    "show_original_link",
}
DISPLAY_SETTING_KEYS = DISPLAY_BOOLEAN_KEYS | {"heading"}


def _normalize_display_profile(value: object, *, base: dict[str, object], context: str) -> dict[str, object]:
    """Validate one display profile while allowing only documented options."""
    if not isinstance(value, dict):
        raise ValueError(f"display_settings の {context} はJSONオブジェクトにしてください。")
    unknown = set(value) - DISPLAY_SETTING_KEYS
    if unknown:
        raise ValueError(f"display_settings の {context} に未対応の設定があります: {', '.join(sorted(map(str, unknown)))}")
    result = dict(base)
    for key, item in value.items():
        if key in DISPLAY_BOOLEAN_KEYS:
            if not isinstance(item, bool):
                raise ValueError(f"display_settings の {context}.{key} は true または false にしてください。")
            result[key] = item
        elif key == "heading":
            if not isinstance(item, str):
                raise ValueError(f"display_settings の {context}.heading は文字列にしてください。")
            # Keep custom headings plain, short, and free of control characters.
            clean = "".join(ch for ch in item if ch in "\t" or ord(ch) >= 32).strip()
            result[key] = clean[:80]
    return result


def normalize_display_settings(value: object) -> dict[str, object]:
    """Validate display defaults, reusable groups, and channel overrides.

    Schema::
        {
          "default": {...},
          "groups": {"archive": {...}, "link_only": {...}},
          "channels": {
              "123": "archive",
              "456": {"group": "link_only", "show_post_text": true},
              "789": {"show_author_name": false}
          }
        }

    A channel may reference a named group with a string value, or use a
    ``group`` key inside an override object. Channel-specific options override
    the selected group; any remaining options inherit the global default.
    The original per-channel-object format remains supported.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("display_settings はJSONオブジェクトにしてください。")
    unknown = set(value) - {"default", "groups", "channels"}
    if unknown:
        raise ValueError(f"display_settings に未対応の項目があります: {', '.join(sorted(map(str, unknown)))}")

    default = _normalize_display_profile(
        value.get("default", {}), base=dict(DEFAULT_DISPLAY_SETTINGS), context="default"
    )

    raw_groups = value.get("groups", {})
    if not isinstance(raw_groups, dict):
        raise ValueError("display_settings.groups はグループ名をキーにしたJSONオブジェクトにしてください。")
    groups: dict[str, dict[str, object]] = {}
    for raw_name, profile in raw_groups.items():
        if not isinstance(raw_name, str):
            raise ValueError("display_settings.groups のグループ名は文字列にしてください。")
        group_name = raw_name.strip()
        if not group_name or len(group_name) > 64:
            raise ValueError(f"display_settings.groups のグループ名が空か長すぎます: {raw_name!r}")
        if group_name in groups:
            raise ValueError(f"display_settings.groups に重複するグループ名があります: {group_name}")
        groups[group_name] = _normalize_display_profile(
            profile, base=dict(default), context=f"groups.{group_name}"
        )

    raw_channels = value.get("channels", {})
    if not isinstance(raw_channels, dict):
        raise ValueError("display_settings.channels はチャンネルIDをキーにしたJSONオブジェクトにしてください。")
    # Channel entries may be a reusable group name, a group plus overrides,
    # or a channel-specific settings object. Preserve group references so
    # changing a named profile also updates every channel assigned to it.
    channels: dict[int, object] = {}
    for raw_id, raw_profile in raw_channels.items():
        try:
            channel_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"display_settings.channels のチャンネルIDが整数ではありません: {raw_id!r}") from exc
        if channel_id <= 0:
            raise ValueError(f"display_settings.channels のチャンネルIDは正の整数にしてください: {raw_id!r}")
        if channel_id in channels:
            raise ValueError(f"display_settings.channels に重複するチャンネルIDがあります: {channel_id}")

        group_name: str | None = None
        if isinstance(raw_profile, str):
            group_name = raw_profile.strip()
            if not group_name or group_name not in groups:
                raise ValueError(
                    f"display_settings.channels.{channel_id} が参照するグループがありません: {raw_profile!r}。"
                    "display_settings.groups にグループを定義してください。"
                )
            channels[channel_id] = group_name
            continue

        if not isinstance(raw_profile, dict):
            raise ValueError(
                f"display_settings.channels.{channel_id} はグループ名文字列またはJSONオブジェクトにしてください。"
            )
        profile_values = dict(raw_profile)
        if "group" in profile_values:
            raw_group_name = profile_values.pop("group")
            if not isinstance(raw_group_name, str) or not raw_group_name.strip():
                raise ValueError(f"display_settings.channels.{channel_id}.group はグループ名文字列にしてください。")
            group_name = raw_group_name.strip()
            if group_name not in groups:
                raise ValueError(
                    f"display_settings.channels.{channel_id} が参照するグループがありません: {group_name!r}。"
                    "display_settings.groups にグループを定義してください。"
                )
        base = dict(groups[group_name]) if group_name else {}
        normalized_profile = _normalize_display_profile(
            profile_values, base=base, context=f"channels.{channel_id}"
        )
        if group_name:
            # Store only the explicit overrides alongside the group reference.
            # Missing fields continue to inherit updates to that named group.
            overrides = {key: normalized_profile[key] for key in profile_values}
            channels[channel_id] = {"group": group_name, **overrides}
        else:
            channels[channel_id] = normalized_profile

    return {"default": default, "groups": groups, "channels": channels}


def resolve_display_settings(settings: object, channel_id: int | str) -> dict[str, object]:
    """Resolve display settings for one channel, including reusable groups."""
    normalized = normalize_display_settings(settings)
    default = dict(normalized["default"])  # type: ignore[arg-type]
    groups = normalized["groups"]  # type: ignore[assignment]
    channels = normalized["channels"]  # type: ignore[assignment]
    try:
        numeric_id = int(channel_id)
    except (TypeError, ValueError):
        numeric_id = -1
    channel_profile = channels.get(numeric_id, {})  # type: ignore[union-attr]
    if isinstance(channel_profile, str):
        default.update(groups.get(channel_profile, {}))  # type: ignore[union-attr]
    elif isinstance(channel_profile, dict):
        group_name = channel_profile.get("group")
        if isinstance(group_name, str):
            default.update(groups.get(group_name, {}))  # type: ignore[union-attr]
        default.update({key: value for key, value in channel_profile.items() if key != "group"})
    return default


def normalize_channel_bool_overrides(value: object, *, setting_name: str) -> dict[int, bool]:
    """Validate per-channel boolean overrides stored in config.json."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{setting_name} はチャンネルIDをキーにしたJSONオブジェクトにしてください。")
    normalized: dict[int, bool] = {}
    for raw_channel_id, raw_enabled in value.items():
        try:
            channel_id = int(raw_channel_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{setting_name} のチャンネルIDが整数ではありません: {raw_channel_id!r}") from exc
        if channel_id <= 0:
            raise ValueError(f"{setting_name} のチャンネルIDは正の整数にしてください: {raw_channel_id!r}")
        if channel_id in normalized:
            raise ValueError(f"{setting_name} に重複するチャンネルIDがあります: {channel_id}")
        if not isinstance(raw_enabled, bool):
            raise ValueError(f"{setting_name}.{channel_id} は true または false にしてください。")
        normalized[channel_id] = raw_enabled
    return normalized


def resolve_channel_bool_setting(default: bool, overrides: object, channel_id: int | str) -> bool:
    """Return a global boolean default overridden by an optional channel value."""
    normalized = normalize_channel_bool_overrides(overrides, setting_name="channel_bool_overrides")
    try:
        numeric_id = int(channel_id)
    except (TypeError, ValueError):
        numeric_id = -1
    return normalized.get(numeric_id, default)


def format_post_metadata(
    metadata: dict[str, object],
    url: str,
    max_chars: int = 1850,
    settings: dict[str, object] | None = None,
) -> str:
    """Format configurable X post details as plain text without embed previews.

    Optional fields can be hidden independently per Discord channel. Profile URL
    and post date are intentionally never included.
    """
    max_chars = max(50, min(int(max_chars), 1950))
    profile = dict(DEFAULT_DISPLAY_SETTINGS)
    if settings:
        profile.update(settings)

    uploader = _metadata_text(metadata.get("uploader"), 160)
    uploader_id = _metadata_text(metadata.get("uploader_id"), 80)
    description = _metadata_text(metadata.get("description"), 10000)
    title = _metadata_text(metadata.get("title"), 10000)
    post_text = description or title or "（投稿本文を取得できませんでした）"

    heading = _metadata_text(profile.get("heading", "X投稿情報"), 80)
    sections: list[str] = []
    if heading:
        sections.append(f"**{heading}**")

    if profile.get("show_author_name", True) and uploader:
        sections.append(f"**投稿者:** {uploader}")
    if profile.get("show_account_name", True) and uploader_id:
        handle = uploader_id if uploader_id.startswith("@") else f"@{uploader_id}"
        sections.append(f"**アカウント:** {handle}")

    body_label = "**投稿内容:**"
    show_body = bool(profile.get("show_post_text", True))
    show_link = bool(profile.get("show_original_link", True))
    footer = [f"ソース: {url}"] if show_link else []

    # Compute a budget for body text after reserving every other selected field.
    if show_body:
        fixed = "\n\n".join(sections + [body_label] + footer)
        body_budget = max(0, max_chars - len(fixed) - 4)
        if len(post_text) > body_budget:
            if body_budget >= 2:
                post_text = post_text[:body_budget - 1].rstrip() + "…"
            else:
                post_text = "" if body_budget == 0 else "…"
        sections.append(f"{body_label}\n{post_text}" if post_text else body_label)
    sections.extend(footer)

    result = "\n\n".join(section for section in sections if section)
    if len(result) > max_chars:
        result = result[:max_chars - 1].rstrip() + "…"
    return result


def normalize_x_url(candidate: str) -> tuple[str, str] | None:
    """Return (canonical_url, dedup_key) for a permitted X media URL.

    Supported links are X/Twitter status URLs (including Fixup/FxTwitter aliases)
    and direct X/Twitter Spaces URLs. Space IDs use a separate key namespace to
    avoid colliding with numeric status IDs.
    """
    candidate = candidate.strip().rstrip(_TRAILING_PUNCTUATION)
    try:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        # HTTPS only; reject deceptive hosts, credentials, and explicit ports.
        if parsed.scheme.lower() != "https":
            return None
        status_hosts = {
            "x.com", "www.x.com", "twitter.com", "www.twitter.com",
            "fixupx.com", "www.fixupx.com", "xfixup.com", "www.xfixup.com",
            "fxtwitter.com", "www.fxtwitter.com", "twittpr.com", "www.twittpr.com",
        }
        if host not in status_hosts:
            return None
        if parsed.username is not None or parsed.password is not None or parsed.port is not None:
            return None
    except ValueError:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    # yt-dlp has a TwitterSpaces extractor for recorded/public Spaces. These
    # direct links have no username/status path, and aliases are not dependable.
    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        if len(parts) == 3 and parts[0].lower() == "i" and parts[1].lower() == "spaces":
            space_id = parts[2]
            if _SPACE_ID_RE.fullmatch(space_id):
                return f"https://x.com/i/spaces/{space_id}", f"space_{space_id}"

    if len(parts) < 3 or parts[1].lower() != "status":
        return None
    username, status_id = parts[0], parts[2]
    if not _USERNAME_RE.fullmatch(username) or not _STATUS_ID_RE.fullmatch(status_id):
        return None
    # Permit only well-known media subpaths; reject arbitrary trailing path data.
    if len(parts) == 5 and parts[3].lower() in {"photo", "video"} and parts[4].isdigit():
        pass
    elif len(parts) != 3:
        return None
    return f"https://x.com/{username}/status/{status_id}", status_id


def normalize_pixiv_url(candidate: str) -> tuple[str, str] | None:
    """Validate a single Pixiv artwork or novel URL and return a canonical URL/key.

    Only individual works are accepted; profile, bookmark, search, and series
    pages are intentionally excluded to prevent unexpectedly large downloads.
    """
    candidate = candidate.strip().rstrip(_TRAILING_PUNCTUATION)
    try:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() != "https" or host not in _PIXIV_HOSTS:
            return None
        if parsed.username is not None or parsed.password is not None or parsed.port is not None:
            return None
    except ValueError:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].lower() in _PIXIV_LOCALES:
        parts = parts[1:]

    query = parse_qs(parsed.query)
    if len(parts) == 2 and parts[0].lower() == "artworks" and _PIXIV_ID_RE.fullmatch(parts[1]):
        work_id = parts[1]
        return f"https://www.pixiv.net/artworks/{work_id}", f"pixiv_artwork_{work_id}"

    # Legacy Pixiv URLs still commonly appear in older Discord posts.
    if len(parts) == 1 and parts[0].lower() == "member_illust.php":
        work_id = (query.get("illust_id") or query.get("id") or [""])[0]
        if _PIXIV_ID_RE.fullmatch(work_id):
            return f"https://www.pixiv.net/artworks/{work_id}", f"pixiv_artwork_{work_id}"

    if len(parts) == 2 and parts[0].lower() == "novel" and parts[1].lower() == "show.php":
        novel_id = (query.get("id") or [""])[0]
        if _PIXIV_ID_RE.fullmatch(novel_id):
            return f"https://www.pixiv.net/novel/show.php?id={novel_id}", f"pixiv_novel_{novel_id}"
    if len(parts) == 2 and parts[0].lower() == "novel" and _PIXIV_ID_RE.fullmatch(parts[1]):
        novel_id = parts[1]
        return f"https://www.pixiv.net/novel/show.php?id={novel_id}", f"pixiv_novel_{novel_id}"

    return None


def normalize_media_url(candidate: str) -> tuple[str, str] | None:
    """Validate a supported X/Twitter or Pixiv media URL."""
    normalized_x = normalize_x_url(candidate)
    if normalized_x is not None:
        return normalized_x
    return normalize_pixiv_url(candidate)


def is_pixiv_url(url: str) -> bool:
    """Return whether a previously validated URL belongs to Pixiv."""
    try:
        return (urlsplit(url).hostname or "").lower() in _PIXIV_HOSTS
    except ValueError:
        return False


def extract_media_urls(text: str) -> list[tuple[str, str]]:
    """Extract unique supported X/Pixiv links from message text in order."""
    found: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for match in _URL_CANDIDATE_RE.finditer(text or ""):
        normalized = normalize_media_url(match.group(0))
        if normalized is None:
            continue
        url, dedup_key = normalized
        if dedup_key not in seen_keys:
            seen_keys.add(dedup_key)
            found.append((url, dedup_key))
    return found


def message_text_for_url_detection(message: object) -> str:
    """Collect message content and embed fields, including webhook embeds.

    This intentionally accepts a duck-typed message so the embed parsing can be
    unit-tested without logging into Discord. URLs are validated later by
    ``extract_media_urls``; arbitrary embed URLs cannot trigger shell commands or
    downloads unless they match an explicitly supported site.
    """
    pieces: list[str] = []
    content = getattr(message, "content", "") or ""
    if isinstance(content, str) and content:
        pieces.append(content)

    for embed in getattr(message, "embeds", []) or []:
        for attr in ("url", "title", "description"):
            value = getattr(embed, attr, None)
            if isinstance(value, str) and value:
                pieces.append(value)
        for obj_name in ("author", "footer", "image", "thumbnail", "video", "provider"):
            obj = getattr(embed, obj_name, None)
            if obj is None:
                continue
            for attr in ("url", "text", "name"):
                value = getattr(obj, attr, None)
                if isinstance(value, str) and value:
                    pieces.append(value)
        for field in getattr(embed, "fields", []) or []:
            for attr in ("name", "value"):
                value = getattr(field, attr, None)
                if isinstance(value, str) and value:
                    pieces.append(value)
    return "\n".join(pieces)


def should_ignore_bot_message(*, author_is_bot: bool, is_webhook_message: bool, is_own_message: bool = False) -> bool:
    """Ignore self-messages and ordinary bots, but accept webhook messages."""
    if is_own_message:
        return True
    return bool(author_is_bot and not is_webhook_message)


def classify_media_paths(paths: list[str | Path]) -> str:
    """Classify a set of downloaded files as video, audio, or image.

    Images are expected to come from gallery-dl, while yt-dlp normally produces
    one video or audio file. Video takes precedence if a downloader leaves both
    merged output and intermediate audio behind.
    """
    suffixes = {Path(path).suffix.lower() for path in paths}
    if not paths or not suffixes.issubset(MEDIA_EXTENSIONS):
        raise ValueError("対応していないメディアファイルが含まれています。")
    if suffixes & VIDEO_EXTENSIONS:
        return "video"
    if suffixes & AUDIO_EXTENSIONS:
        return "audio"
    if suffixes & IMAGE_EXTENSIONS:
        return "image"
    raise ValueError("対応する動画・音声・画像ファイルがありません。")


def extract_x_urls(text: str) -> list[tuple[str, str]]:
    """Extract unique valid X status URLs from a Discord message, preserving order."""
    found: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for match in _URL_CANDIDATE_RE.finditer(text or ""):
        normalized = normalize_x_url(match.group(0))
        if normalized is None:
            continue
        url, status_id = normalized
        if status_id not in seen_ids:
            seen_ids.add(status_id)
            found.append((url, status_id))
    return found


def positive_int(value: object, default: int) -> int:
    try:
        converted = int(value)  # type: ignore[arg-type]
        return converted if converted > 0 else default
    except (TypeError, ValueError):
        return default


def mb_to_bytes(mb: float) -> int:
    return int(mb * 1024 * 1024)


def file_size_bytes(path: str | Path) -> int:
    return Path(path).stat().st_size


class DedupStore:
    """SQLite-backed post deduplication. Failed jobs may be retried; success is permanent."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=15)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                status_id TEXT PRIMARY KEY,
                source_message_id TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at REAL NOT NULL,
                detail TEXT
            )"""
        )
        # Queued/processing records cannot be resumed after a hard process exit.
        self.connection.execute(
            "UPDATE jobs SET status='failed', detail='Previous process ended before completion', updated_at=? "
            "WHERE status IN ('queued', 'processing')",
            (time.time(),),
        )
        self.connection.commit()

    def claim(self, status_id: str, message_id: int | str, *, deduplicate_success: bool = True) -> bool:
        """Atomically claim a post unless it is already queued, active, or uploaded."""
        now = time.time()
        row = self.connection.execute(
            "SELECT status FROM jobs WHERE status_id=?", (str(status_id),)
        ).fetchone()
        if row and row[0] in {"queued", "processing"}:
            return False
        if row and row[0] == "success" and deduplicate_success:
            return False
        self.connection.execute(
            """INSERT INTO jobs(status_id, source_message_id, status, updated_at, detail)
               VALUES(?, ?, 'queued', ?, NULL)
               ON CONFLICT(status_id) DO UPDATE SET
                 source_message_id=excluded.source_message_id,
                 status='queued', updated_at=excluded.updated_at, detail=NULL""",
            (str(status_id), str(message_id), now),
        )
        self.connection.commit()
        return True

    def set_status(self, status_id: str, status: str, detail: str | None = None) -> None:
        if status not in {"queued", "processing", "success", "failed"}:
            raise ValueError(f"Unsupported job status: {status}")
        self.connection.execute(
            "UPDATE jobs SET status=?, updated_at=?, detail=? WHERE status_id=?",
            (status, time.time(), (detail or "")[:1000] or None, str(status_id)),
        )
        self.connection.commit()

    def status_for(self, status_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT status FROM jobs WHERE status_id=?", (str(status_id),)
        ).fetchone()
        return str(row[0]) if row else None

    def close(self) -> None:
        self.connection.close()
