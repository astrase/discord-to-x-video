from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

try:
    import discord
    import yt_dlp
    from dotenv import load_dotenv
except ImportError as exc:
    print(
        f"[ERROR] 必要なパッケージを読み込めません: {exc}\n"
        "setup.bat を実行して依存パッケージをインストールしてください。",
        file=sys.stderr,
    )
    raise SystemExit(1)

from bot_core import (
    AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, MEDIA_EXTENSIONS, VIDEO_EXTENSIONS,
    DedupStore, classify_media_paths, extract_media_urls, file_size_bytes,
    format_post_metadata, is_pixiv_url, mb_to_bytes, normalize_cookie_browser,
    normalize_cookie_browsers, normalize_display_settings, resolve_display_settings, should_ignore_bot_message,
    message_text_for_url_detection, positive_int, looks_like_auth_error,
    browser_cookie_lock_guidance, normalize_channel_bool_overrides,
    resolve_channel_bool_setting,
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"
STOP_REQUEST_PATH = BASE_DIR / "stop.request"
LOCK_PATH = BASE_DIR / ".bot.lock"
# Discord raised its documented standard free upload limit to 20 MB in 2026;
# discord.py's cached guild table may lag platform-wide changes.
DISCORD_STANDARD_UPLOAD_LIMIT_BYTES = 20 * 1024 * 1024


class ConfigError(Exception):
    pass


def configure_logging(level_name: str = "INFO") -> logging.Logger:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    numeric_level = getattr(logging, str(level_name).upper(), logging.INFO)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
    logger = logging.getLogger("x_video_bot")
    logger.setLevel(numeric_level)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        log_dir / "bot.log", maxBytes=4 * 1024 * 1024, backupCount=4, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False
    discord_logger = logging.getLogger("discord")
    discord_logger.handlers.clear()
    discord_logger.setLevel(numeric_level)
    discord_logger.addHandler(file_handler)
    discord_logger.addHandler(console_handler)
    discord_logger.propagate = False
    return logger


def read_config() -> dict[str, Any]:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fp:
            config = json.load(fp)
    except FileNotFoundError as exc:
        raise ConfigError(f"config.json が見つかりません: {CONFIG_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json のJSON形式が不正です (行 {exc.lineno})") from exc
    if not isinstance(config, dict):
        raise ConfigError("config.json の最上位はJSONオブジェクトにしてください。")

    channel_ids = config.get("monitored_channel_ids")
    if not isinstance(channel_ids, list):
        raise ConfigError(
            "monitored_channel_ids はDiscordチャンネルIDの配列にしてください。 "
            "空配列 [] の場合は自動変換を停止します。全チャンネル監視はしません。"
        )
    normalized_ids: list[int] = []
    for item in channel_ids:
        try:
            channel_id = int(item)
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"チャンネルIDが整数ではありません: {item!r}") from exc
        if channel_id <= 0:
            raise ConfigError(f"チャンネルIDは正の整数にしてください: {item!r}")
        if channel_id not in normalized_ids:
            normalized_ids.append(channel_id)
    config["monitored_channel_ids"] = normalized_ids

    config["max_concurrent_downloads"] = min(8, positive_int(config.get("max_concurrent_downloads"), 2))
    config["max_queue_size"] = min(500, positive_int(config.get("max_queue_size"), 20))
    config["max_download_size_mb"] = positive_int(config.get("max_download_size_mb"), 8192)
    config["max_split_parts"] = min(500, positive_int(config.get("max_split_parts"), 100))
    config["fallback_upload_limit_mb"] = positive_int(config.get("fallback_upload_limit_mb"), 20)
    config["compression_attempts"] = min(12, positive_int(config.get("compression_attempts"), 8))
    config["compression_audio_bitrate_kbps"] = positive_int(config.get("compression_audio_bitrate_kbps"), 96)
    config["compression_min_video_bitrate_kbps"] = positive_int(config.get("compression_min_video_bitrate_kbps"), 100)
    # Try Windows D3D11VA hardware decoding during FFmpeg video compression by default.
    # Only a JSON boolean true enables it; strings such as "false" do not accidentally enable it.
    config["use_gpu_decode"] = config.get("use_gpu_decode", True) is True
    config["shutdown_drain_timeout_seconds"] = positive_int(config.get("shutdown_drain_timeout_seconds"), 60)
    upload_mb = config.get("max_upload_size_mb", 0)
    try:
        config["max_upload_size_mb"] = max(0, int(upload_mb))
    except (TypeError, ValueError):
        config["max_upload_size_mb"] = 0

    try:
        config["display_settings"] = normalize_display_settings(config.get("display_settings", {}))
        config["deduplicate_posts_by_channel"] = normalize_channel_bool_overrides(
            config.get("deduplicate_posts_by_channel", {}),
            setting_name="deduplicate_posts_by_channel",
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    try:
        # Plural setting supports ordered fallback (e.g. Firefox, then Brave).
        # Older config.json files with only cookies_from_browser remain compatible.
        legacy_browser = normalize_cookie_browser(config.get("cookies_from_browser", ""))
        if "cookies_from_browsers" in config:
            config["cookies_from_browsers"] = normalize_cookie_browsers(config.get("cookies_from_browsers"))
        else:
            config["cookies_from_browsers"] = [legacy_browser] if legacy_browser else []
        config["cookies_from_browser"] = legacy_browser
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    # Optional Netscape-format cookies.txt is useful when browser databases are
    # locked or cannot be decrypted. Keep it outside source control and logs.
    cookie_file_setting = config.get("cookies_file", "")
    if not isinstance(cookie_file_setting, str):
        raise ConfigError("cookies_file は空欄または相対ファイルパスにしてください。")
    cookie_file_setting = cookie_file_setting.strip()
    if not cookie_file_setting:
        config["cookies_file"] = ""
        config["_cookies_file_path"] = None
    else:
        cookie_rel = Path(cookie_file_setting)
        if cookie_rel.is_absolute() or ".." in cookie_rel.parts:
            raise ConfigError("cookies_file はBotフォルダー内の相対パスにしてください (例: cookies.txt)。")
        cookie_resolved = (BASE_DIR / cookie_rel).resolve()
        if not cookie_resolved.is_relative_to(BASE_DIR.resolve()):
            raise ConfigError("cookies_file はBotフォルダー内に指定してください。")
        if not cookie_resolved.is_file():
            raise ConfigError(f"cookies_file が見つかりません: {cookie_file_setting}。先にCookieをエクスポートしてください。")
        config["cookies_file"] = cookie_file_setting
        config["_cookies_file_path"] = cookie_resolved

    temp_value = config.get("temp_dir", "temp")
    if not isinstance(temp_value, str) or not temp_value.strip():
        raise ConfigError("temp_dir は空でない相対フォルダー名にしてください。")
    temp_rel = Path(temp_value)
    if temp_rel.is_absolute() or ".." in temp_rel.parts:
        raise ConfigError("安全のため temp_dir はプロジェクト内の相対パスに限定しています (.. や絶対パス不可)。")
    resolved_temp = (BASE_DIR / temp_rel).resolve()
    if resolved_temp == BASE_DIR.resolve() or not resolved_temp.is_relative_to(BASE_DIR.resolve()):
        raise ConfigError("temp_dir はプロジェクトフォルダー内の専用サブフォルダーに指定してください。")
    config["_temp_path"] = resolved_temp

    level = str(config.get("log_level", "INFO")).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("log_level は DEBUG / INFO / WARNING / ERROR / CRITICAL のいずれかです。")
    config["log_level"] = level
    for key in (
        "compress_if_too_large",
        "deduplicate_posts",
        "reply_with_progress",
        "delete_source_message_on_success",
    ):
        config[key] = bool(config.get(key, True))
    return config


def find_executable(config_value: object, names: tuple[str, ...], base_dir: Path = BASE_DIR) -> str | None:
    """Resolve an optional executable path or search PATH; never invoke a shell."""
    if isinstance(config_value, str) and config_value.strip():
        candidate = Path(config_value.strip())
        if not candidate.is_absolute():
            candidate = (base_dir / candidate).resolve()
        if candidate.is_file():
            return str(candidate)
        if candidate.is_dir():
            for name in names:
                possible = candidate / name
                if possible.is_file():
                    return str(possible)
        return None
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def resolve_ffmpeg_tools(config: dict[str, Any], logger: logging.Logger) -> tuple[str | None, str | None]:
    ffmpeg = find_executable(config.get("ffmpeg_path"), ("ffmpeg.exe", "ffmpeg"))
    ffprobe_setting = config.get("ffprobe_path")
    ffprobe = find_executable(ffprobe_setting, ("ffprobe.exe", "ffprobe"))
    if not ffprobe and ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.is_file():
            ffprobe = str(sibling)
    if ffmpeg:
        logger.info("FFmpeg: %s", ffmpeg)
    else:
        logger.warning("FFmpegが見つかりません。動画の結合や動画・音声のサイズ圧縮は利用できない場合があります。")
    if ffprobe:
        logger.info("FFprobe: %s", ffprobe)
    elif config.get("compress_if_too_large", True):
        logger.warning("FFprobeが見つかりません。容量超過時の動画・音声圧縮にはFFprobeが必要です。")
    return ffmpeg, ffprobe


class YtDlpLogger:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def debug(self, message: str) -> None:
        # yt-dlp debug output can be verbose. Keep only useful messages at DEBUG.
        self.logger.debug("yt-dlp: %s", message[:1500])

    def warning(self, message: str) -> None:
        self.logger.warning("yt-dlp: %s", message[:1500])

    def error(self, message: str) -> None:
        self.logger.error("yt-dlp: %s", message[:1500])


def configured_cookie_browsers(config: dict[str, Any]) -> list[str]:
    """Return configured browser order; preserve compatibility with old configs."""
    browsers = config.get("cookies_from_browsers")
    if isinstance(browsers, list):
        return [str(value) for value in browsers if value]
    legacy = config.get("cookies_from_browser", "")
    return [str(legacy)] if legacy else []


def configured_cookie_sources(config: dict[str, Any]) -> list[tuple[str, str]]:
    """Return cookie source attempts in safe, deterministic order.

    A configured cookies.txt is tried first, then the configured browser profiles.
    The tuple kind is 'file', 'browser', or 'anonymous'.
    """
    sources: list[tuple[str, str]] = []
    cookie_path = config.get("_cookies_file_path")
    if cookie_path:
        sources.append(("file", str(cookie_path)))
    sources.extend(("browser", browser) for browser in configured_cookie_browsers(config))
    if not sources:
        sources.append(("anonymous", ""))
    return sources


def cookie_source_label(source: tuple[str, str]) -> str:
    kind, value = source
    if kind == "file":
        return "cookies.txt"
    if kind == "browser":
        return value
    return "Cookieなし"


def safe_exception_summary(exc: BaseException, limit: int = 600) -> str:
    """Return a concise download error for Discord without leaking cookie/token values."""
    message = str(exc).replace("\r", " ").replace("\n", " ")
    message = re.sub(r"\s+", " ", message).strip()
    # Defense in depth in case a third-party error includes sensitive auth values.
    message = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+", r"\1[REDACTED]", message)
    message = re.sub(r"(?i)(\b(?:ct0|auth_token|oauth_token|access_token|refresh_token|cookie|password)=)[^&\s]+", r"\1[REDACTED]", message)
    return (message or type(exc).__name__)[:limit]


def is_disk_full_error(exc: BaseException) -> bool:
    """Recognize common POSIX and Windows disk-full errors through exception chains."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "errno", None) == 28 or getattr(current, "winerror", None) == 112:
            return True
        message = str(current).lower()
        if any(phrase in message for phrase in ("no space left on device", "disk is full", "not enough space on the disk", "ディスクの空き領域が不足")):
            return True
        current = current.__cause__ or current.__context__
    return False


def _safe_metadata(extracted: object) -> dict[str, Any]:
    """Keep only harmless public post text fields from yt-dlp output."""
    info: dict[str, Any] = extracted if isinstance(extracted, dict) else {}
    entries = info.get("entries")
    if not info.get("description") and isinstance(entries, list) and entries and isinstance(entries[0], dict):
        info = entries[0]
    return {
        key: info.get(key)
        for key in ("title", "description", "uploader", "uploader_id")
        if info.get(key) is not None
    }


def _find_downloaded_media(output_dir: Path) -> list[Path]:
    """Find complete supported media files in a job's private temporary folder."""
    candidates: list[Path] = []
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name.lower().endswith((".part", ".ytdl", ".tmp")):
            continue
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        try:
            if path.stat().st_size > 0:
                candidates.append(path)
        except OSError:
            continue
    return candidates


def _choose_media_files(paths: list[Path]) -> tuple[list[Path], str]:
    """Prefer a merged video/audio file; return all images from an image post."""
    if not paths:
        raise FileNotFoundError("ダウンロード後に対応するメディアファイルが見つかりませんでした。")
    media_type = classify_media_paths(paths)
    if media_type == "video":
        choices = [path for path in paths if path.suffix.lower() in VIDEO_EXTENSIONS]
        choices.sort(key=lambda path: (path.suffix.lower() == ".mp4", path.stat().st_size), reverse=True)
        return choices[:1], media_type
    if media_type == "audio":
        choices = [path for path in paths if path.suffix.lower() in AUDIO_EXTENSIONS]
        choices.sort(key=lambda path: path.stat().st_size, reverse=True)
        return choices[:1], media_type
    choices = [path for path in paths if path.suffix.lower() in IMAGE_EXTENSIONS]
    choices.sort(key=lambda path: path.name.casefold())
    # Discord accepts at most 10 attachments in a single message. Fail safely
    # rather than posting only some images and deleting the source link.
    if len(choices) > 10:
        raise RuntimeError(f"画像が{len(choices)}枚あります。Discordの1メッセージあたりの添付上限10枚を超えるため投稿できません。")
    return choices, media_type


def _download_images_with_gallery_dl(
    url: str, output_dir: Path, config: dict[str, Any], logger: logging.Logger,
) -> list[Path]:
    """Download image-only X posts, trying cookies.txt then browser cookies."""
    sources = configured_cookie_sources(config)
    last_error = "gallery-dlからファイルが返りませんでした"
    for source_index, source in enumerate(sources):
        source_kind, source_value = source
        source_label = cookie_source_label(source)
        safe_source = "cookies-file" if source_kind == "file" else (source_value or "anonymous")
        image_dir = output_dir / "gallery_images" / safe_source
        image_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, "-m", "gallery_dl", "--config-ignore", "--quiet",
            "--no-mtime", "--no-part", "--windows-filenames",
            "--directory", str(image_dir),
            "--filesize-max", f"{config['max_download_size_mb']}M",
        ]
        if source_kind == "file":
            command.extend(["--cookies", source_value])
        elif source_kind == "browser":
            command.extend(["--cookies-from-browser", source_value])
        command.extend(["--range", "1-10", url])
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=240, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise RuntimeError("gallery-dlがインストールされていません。update.bat または setup.bat を実行してください。") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("gallery-dlによる画像取得がタイムアウトしました。") from exc
        downloaded = [
            path for path in image_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
            and not path.name.lower().endswith((".part", ".tmp"))
            and path.stat().st_size > 0
        ]
        if result.returncode == 0 and downloaded:
            if source_kind != "anonymous":
                logger.info("gallery-dlのCookie取得に成功しました (source=%s)。Cookie値は記録しません。", source_label)
            return downloaded
        last_error = result.stderr or f"gallery-dl終了コード {result.returncode}"
        logger.warning("gallery-dlで画像を取得できませんでした (source=%s, 終了コード=%s): %s", source_label, result.returncode, last_error[-800:])
        if source_index + 1 >= len(sources):
            break
    diagnostic = safe_exception_summary(RuntimeError(last_error), 450)
    raise RuntimeError(f"gallery-dlで画像を取得できませんでした: {diagnostic}")


def _read_pixiv_sidecar_metadata(output_dir: Path) -> dict[str, Any]:
    """Read the first gallery-dl metadata sidecar and map it to display fields."""
    candidates = sorted(
        (path for path in output_dir.rglob("*.json") if path.name.lower() not in {"pixiv-auth.json"}),
        key=lambda path: path.name.casefold(),
    )
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict):
            continue
        user_value = data.get("user") or data.get("user_name") or data.get("user[name]") or data.get("artist")
        # Only display a true account/handle as @name; don't mislabel numeric Pixiv IDs as handles.
        user_id_value = data.get("user_account") or data.get("user[account]") or data.get("account_name")
        if isinstance(user_value, dict):
            user_id_value = user_id_value or user_value.get("account") or user_value.get("username")
            user_value = user_value.get("name") or user_value.get("account") or user_value.get("account_name")
        metadata = {
            "platform": "Pixiv",
            "title": data.get("title") or data.get("work_title") or data.get("illust_title"),
            "description": data.get("description") or data.get("caption") or data.get("comment"),
            "uploader": user_value,
            "uploader_id": user_id_value,
        }
        # Ignore only date and profile-related values; those are never used by the formatter.
        if any(metadata.get(key) for key in ("title", "description", "uploader", "uploader_id")):
            return {key: value for key, value in metadata.items() if value is not None}
    return {"platform": "Pixiv"}


def _download_pixiv_with_gallery_dl(
    url: str,
    output_dir: Path,
    config: dict[str, Any],
    ffmpeg_path: str | None,
    logger: logging.Logger,
) -> tuple[list[Path], dict[str, Any], str]:
    """Download one authorized Pixiv artwork/novel cover with gallery-dl.

    Pixiv's supported extractor requires a gallery-dl OAuth refresh token. The
    token is read from the process environment and written only to a temporary
    per-job config inside the job's temporary directory, never to logs/config.json.
    """
    refresh_token = os.getenv("PIXIV_REFRESH_TOKEN", "").strip()
    if not refresh_token:
        raise RuntimeError(
            "Pixiv取得にはgallery-dlのOAuthトークンが必要です。pixiv_oauth.batを実行し、"
            "取得した値を.envのPIXIV_REFRESH_TOKENに設定してから再起動してください。"
        )

    media_dir = output_dir / "pixiv_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    auth_config = output_dir / "pixiv-auth.json"
    pixiv_config: dict[str, Any] = {
        "extractor": {
            "pixiv": {
                "refresh-token": refresh_token,
                "max-posts": 1,
                "metadata": True,
                "captions": True,
            },
            "pixiv-novel": {
                "refresh-token": refresh_token,
                "covers": True,
                "embeds": False,
                "max-posts": 1,
            },
        }
    }
    # Only enable the ugoira-to-MP4 converter when FFmpeg is available.
    if ffmpeg_path:
        pixiv_config["extractor"]["pixiv"]["ugoira"] = True
    else:
        # Avoid treating raw ZIP animation frames as Discord media attachments.
        pixiv_config["extractor"]["pixiv"]["ugoira"] = False
    auth_config.write_text(json.dumps(pixiv_config, ensure_ascii=False), encoding="utf-8")

    command = [
        sys.executable, "-m", "gallery_dl", "--config-ignore", "--config-json", str(auth_config),
        "--quiet", "--no-mtime", "--no-part", "--windows-filenames",
        "--directory", str(media_dir), "--filesize-max", f"{config['max_download_size_mb']}M",
        "--range", "1-10", "--write-metadata",
    ]
    cookie_browsers = configured_cookie_browsers(config)
    if cookie_browsers:
        command.extend(["--cookies-from-browser", f"{cookie_browsers[0]}/pixiv.net"])
    if ffmpeg_path:
        command.extend(["--ugoira", "mp4"])
    command.append(url)

    env = os.environ.copy()
    if ffmpeg_path:
        ffmpeg_dir = str(Path(ffmpeg_path).resolve().parent)
        env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=360, check=False, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("gallery-dlがインストールされていません。setup.bat または update.bat を実行してください。") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Pixivのメディア取得がタイムアウトしました。") from exc
    finally:
        # The TemporaryDirectory owner removes this file as a second safety net.
        try:
            auth_config.unlink(missing_ok=True)
        except OSError:
            logger.warning("Pixiv一時認証設定を削除できませんでした。tempフォルダーを確認してください。")

    media_paths = [
        path for path in media_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
        and not path.name.lower().endswith((".part", ".tmp"))
        and path.stat().st_size > 0
    ]
    if result.returncode != 0:
        # Never log the command or OAuth token; explicitly redact the token from diagnostics.
        stderr_text = (result.stderr or "").replace(refresh_token, "[REDACTED]")
        logger.warning("Pixiv gallery-dl取得失敗 (終了コード=%s): %s", result.returncode, stderr_text[-1200:])
        if not media_paths:
            diagnostic = safe_exception_summary(RuntimeError(stderr_text or "gallery-dlからファイルが返りませんでした"), 600)
            if not ffmpeg_path and "ugoira" in stderr_text.lower():
                diagnostic += "。アニメーション作品にはFFmpegが必要です。"
            raise RuntimeError(f"Pixiv取得失敗 (gallery-dl終了コード {result.returncode}): {diagnostic}")
    if not media_paths:
        raise FileNotFoundError(
            "Pixivから添付できる画像・動画を取得できませんでした。公開範囲、PIXIV_REFRESH_TOKEN、"
            "FFmpeg（うごイラの場合）、logs/bot.logを確認してください。"
        )
    metadata = _read_pixiv_sidecar_metadata(media_dir)
    selected, media_type = _choose_media_files(media_paths)
    return selected, metadata, media_type


def _read_metadata_only(url: str, logger: logging.Logger) -> dict[str, Any]:
    """Try to retrieve author and text for image-only statuses without downloading media."""
    try:
        params = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 20, "logger": YtDlpLogger(logger), "skip_download": True,
        }
        with yt_dlp.YoutubeDL(params) as ydl:
            return _safe_metadata(ydl.extract_info(url, download=False))
    except Exception as exc:
        logger.debug("メタデータのみの取得に失敗しました (%s)", type(exc).__name__)
        return {}


def download_media_sync(
    url: str,
    output_dir: Path,
    config: dict[str, Any],
    ffmpeg_path: str | None,
    logger: logging.Logger,
) -> tuple[list[Path], dict[str, Any], str]:
    """Download Pixiv or X media using the corresponding supported extractor."""
    if is_pixiv_url(url):
        return _download_pixiv_with_gallery_dl(url, output_dir, config, ffmpeg_path, logger)

    output_template = str(output_dir / "%(id)s.%(ext)s")
    params: dict[str, Any] = {
        "outtmpl": {"default": output_template},
        "paths": {"home": str(output_dir), "temp": str(output_dir)},
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "logger": YtDlpLogger(logger),
        "socket_timeout": 30,
        "retries": 3,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 1,
        "max_filesize": mb_to_bytes(config["max_download_size_mb"]),
        "windowsfilenames": True,
        "overwrites": False,
        "restrictfilenames": True,
    }
    if ffmpeg_path:
        params.update({
            "ffmpeg_location": ffmpeg_path,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
        })
    else:
        params["format"] = "best[ext=mp4]/best"

    cookie_sources = configured_cookie_sources(config)
    metadata: dict[str, Any] = {}
    yt_error: Exception | None = None
    for source_index, source in enumerate(cookie_sources):
        source_kind, source_value = source
        source_label = cookie_source_label(source)
        # Isolate each attempt so an incomplete file from a failed source cannot
        # be mistaken for a successful download from the next source.
        if source_kind == "file":
            attempt_name = "yt-dlp-cookies-file"
        elif source_kind == "browser":
            attempt_name = f"yt-dlp-{source_value}"
        else:
            attempt_name = "yt-dlp-anonymous"
        attempt_dir = output_dir / attempt_name
        attempt_dir.mkdir(parents=True, exist_ok=True)
        attempt_params = dict(params)
        attempt_params["outtmpl"] = {"default": str(attempt_dir / "%(id)s.%(ext)s")}
        attempt_params["paths"] = {"home": str(attempt_dir), "temp": str(attempt_dir)}
        attempt_params.pop("cookiesfrombrowser", None)
        attempt_params.pop("cookiefile", None)
        if source_kind == "file":
            attempt_params["cookiefile"] = source_value
            logger.info("X用Cookieファイルを試します。Cookie内容やファイルパスは記録しません。")
        elif source_kind == "browser":
            attempt_params["cookiesfrombrowser"] = (source_value, None, None, None)
            logger.info("X用Cookieを試します (browser=%s)。Cookie値は記録しません。", source_value)
        try:
            with yt_dlp.YoutubeDL(attempt_params) as ydl:
                extracted = ydl.extract_info(url, download=True)
            metadata = _safe_metadata(extracted)
            candidates = _find_downloaded_media(attempt_dir)
            if candidates:
                selected, media_type = _choose_media_files(candidates)
                if source_kind != "anonymous":
                    logger.info("X用Cookieでの取得に成功しました (source=%s)。", source_label)
                return selected, metadata, media_type
            yt_error = RuntimeError(f"{source_label}で取得しましたがメディアファイルがありません")
        except Exception as exc:
            yt_error = exc
            logger.warning("yt-dlp取得失敗 (source=%s): %s", source_label, safe_exception_summary(exc, 900))
            if source_index + 1 >= len(cookie_sources) or not looks_like_auth_error(exc):
                break
            logger.info("認証/Cookie関連エラーのため、次のCookieソースで再試行します。")

    # yt-dlp is primarily used for audio/video. gallery-dl is the fallback for
    # picture-only posts, including multi-image posts.
    try:
        images = _download_images_with_gallery_dl(url, output_dir, config, logger)
    except Exception as gallery_exc:
        cookie_guidance = browser_cookie_lock_guidance(yt_error or "", gallery_exc)
        if yt_error is not None:
            yt_detail = safe_exception_summary(yt_error, 450)
            gallery_detail = safe_exception_summary(gallery_exc, 450)
            if cookie_guidance:
                ending = cookie_guidance
            else:
                ending = "公開状態・ログイン状態を確認し、update.batでyt-dlpを更新してください。"
            raise RuntimeError(
                f"yt-dlp取得失敗: {yt_detail} / gallery-dl取得失敗: {gallery_detail}. {ending}"
            ) from gallery_exc
        if cookie_guidance:
            raise RuntimeError(f"gallery-dl取得失敗: {safe_exception_summary(gallery_exc, 500)}. {cookie_guidance}") from gallery_exc
        raise
    if not images:
        if yt_error is not None:
            yt_detail = safe_exception_summary(yt_error, 500)
            raise RuntimeError(
                f"動画・音声・画像を取得できませんでした。yt-dlpの原因: {yt_detail}。 "
                "gallery-dlも画像を返しませんでした。公開状態、ログイン状態、logs/bot.logを確認してください。"
            ) from yt_error
        raise FileNotFoundError("画像投稿としてもメディアファイルが見つかりませんでした。")
    if not metadata:
        metadata = _read_metadata_only(url, logger)
    selected, media_type = _choose_media_files(images)
    return selected, metadata, media_type


def probe_duration_sync(path: Path, ffprobe_path: str) -> float:
    result = subprocess.run(
        [ffprobe_path, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError("FFprobeでメディアの再生時間を取得できませんでした。")
    try:
        duration = float(result.stdout.strip())
    except (ValueError, TypeError) as exc:
        raise RuntimeError("メディアの再生時間が不明なため、圧縮サイズを計算できません。") from exc
    if duration <= 0:
        raise RuntimeError("メディアの再生時間が0以下のため、圧縮できません。")
    return duration


def compress_video_sync(
    source: Path,
    output_dir: Path,
    max_bytes: int,
    config: dict[str, Any],
    ffmpeg_path: str,
    ffprobe_path: str,
    logger: logging.Logger,
) -> Path:
    """Create separate H.264/AAC MP4 attempts and return the first file under the cap."""
    duration = probe_duration_sync(source, ffprobe_path)
    audio_kbps = config["compression_audio_bitrate_kbps"]
    min_video_kbps = config["compression_min_video_bitrate_kbps"]
    target_bytes = int(max_bytes * 0.90)  # leave room for container overhead and estimation error
    estimated_total_kbps = (target_bytes * 8) / duration / 1000
    start_video_kbps = max(min_video_kbps, int(estimated_total_kbps - audio_kbps - 24))
    last_output: Path | None = None

    for attempt in range(config["compression_attempts"]):
        video_kbps = max(min_video_kbps, int(start_video_kbps * (0.72 ** attempt)))
        output = output_dir / f"compressed_{attempt + 1}.mp4"
        base_command = [
            ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_kbps}k",
            "-maxrate", f"{max(video_kbps, int(video_kbps * 1.15))}k",
            "-bufsize", f"{max(256, video_kbps * 2)}k",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart", "-map_metadata", "-1", str(output),
        ]
        timeout = max(120, int(duration * 4 + 60))
        result = None
        gpu_decode_requested = config.get("use_gpu_decode", True) and os.name == "nt"

        if gpu_decode_requested:
            # D3D11VA uses supported AMD/Intel/NVIDIA hardware video decoders on Windows.
            # Keep libx264 as the encoder: this setting enables GPU *decoding*, not GPU encoding.
            # FFmpeg transfers decoded frames back to system memory for the software encoder.
            gpu_command = list(base_command)
            input_index = gpu_command.index("-i")
            gpu_command[input_index:input_index] = ["-hwaccel", "d3d11va"]
            try:
                output.unlink(missing_ok=True)
                logger.info(
                    "動画を圧縮します (試行 %s/%s、D3D11VA GPUデコード、映像約 %s kbps)",
                    attempt + 1, config["compression_attempts"], video_kbps,
                )
                result = subprocess.run(
                    gpu_command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout, check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as exc:
                result = subprocess.CompletedProcess(gpu_command, 124, "", f"タイムアウト: {exc}")

            gpu_succeeded = (
                result.returncode == 0 and output.is_file() and output.stat().st_size > 0
            )
            if not gpu_succeeded:
                gpu_error = (result.stderr or "").strip()
                logger.warning(
                    "D3D11VA GPUデコードに失敗したためCPUデコードへ切り替えます: %s",
                    gpu_error[-600:] or f"終了コード {result.returncode}",
                )
                output.unlink(missing_ok=True)
                result = None

        if result is None or result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            # Always retain a CPU decode fallback so unsupported codecs/drivers do not break uploads.
            try:
                output.unlink(missing_ok=True)
                logger.info(
                    "動画を圧縮します (試行 %s/%s、CPUデコード、映像約 %s kbps)",
                    attempt + 1, config["compression_attempts"], video_kbps,
                )
                result = subprocess.run(
                    base_command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout, check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as exc:
                result = subprocess.CompletedProcess(base_command, 124, "", f"タイムアウト: {exc}")

        if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            output.unlink(missing_ok=True)
            last_output = None
            logger.warning("FFmpeg圧縮試行 %s が失敗しました: %s", attempt + 1, (result.stderr or "")[-1200:])
            continue
        last_output = output
        if file_size_bytes(output) <= max_bytes:
            return output
        logger.warning("圧縮後も上限超過です: %.2f MiB / %.2f MiB", file_size_bytes(output) / 1048576, max_bytes / 1048576)
    if last_output is not None:
        raise RuntimeError(
            f"圧縮を試しましたが上限以下になりませんでした ({file_size_bytes(last_output) / 1048576:.2f} MiB、上限 {max_bytes / 1048576:.2f} MiB)。"
        )
    raise RuntimeError("FFmpeg圧縮に失敗しました。ログを確認してください。")



def split_video_sync(
    source: Path,
    output_dir: Path,
    max_bytes: int,
    ffmpeg_path: str,
    ffprobe_path: str,
    logger: logging.Logger,
    max_parts: int = 100,
) -> list[Path]:
    """Split a video into separate MP4 files, each under Discord's per-file cap.

    Stream-copy is used to avoid an additional quality loss. Segment duration is
    reduced and retried if keyframe spacing creates any part that is still too big.
    """
    if max_bytes <= 0:
        raise RuntimeError("動画分割のサイズ上限が不正です。")
    duration = probe_duration_sync(source, ffprobe_path)
    source_bytes = file_size_bytes(source)
    target_bytes = max(1, int(max_bytes * 0.82))
    if source_bytes <= max_bytes:
        return [source]
    segment_seconds = max(0.5, duration * target_bytes / max(1, source_bytes))
    pattern = output_dir / "split_part_%03d.mp4"
    timeout = max(120, int(duration * 2 + 60))

    for attempt in range(12):
        for old_part in output_dir.glob("split_part_*.mp4"):
            try:
                old_part.unlink(missing_ok=True)
            except OSError:
                pass
        command = [
            ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
            "-f", "segment", "-segment_time", f"{segment_seconds:.3f}",
            "-reset_timestamps", "1", "-segment_format", "mp4", str(pattern),
        ]
        logger.info(
            "動画を分割します (試行 %s/12、目標約 %.2f 秒/パート、上限 %.2f MiB)",
            attempt + 1, segment_seconds, max_bytes / 1048576,
        )
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(command, 124, "", f"タイムアウト: {exc}")

        parts = sorted(
            (path for path in output_dir.glob("split_part_*.mp4") if path.is_file() and path.stat().st_size > 0),
            key=lambda path: path.name,
        )
        if result.returncode != 0 or len(parts) < 2:
            logger.warning("動画分割に失敗しました: %s", (result.stderr or "出力パートが作成されませんでした")[-1000:])
            for part in parts:
                part.unlink(missing_ok=True)
            segment_seconds *= 0.65
            continue
        if len(parts) > max_parts:
            raise RuntimeError(
                f"動画を分割すると{len(parts)}パート必要ですが、設定上限は{max_parts}パートです。"
                " config.json の max_split_parts を増やすか、動画を短くしてください。"
            )
        oversize = [part for part in parts if file_size_bytes(part) > max_bytes]
        if not oversize:
            logger.info(
                "動画分割成功: %s パート、合計 %.2f MiB",
                len(parts), sum(file_size_bytes(part) for part in parts) / 1048576,
            )
            return parts
        largest = max(file_size_bytes(part) for part in oversize) / 1048576
        logger.warning(
            "分割後も上限を超えるパートがあります (最大 %.2f MiB / 上限 %.2f MiB)。分割時間を短縮します。",
            largest, max_bytes / 1048576,
        )
        for part in parts:
            part.unlink(missing_ok=True)
        segment_seconds = max(0.25, segment_seconds * 0.65)

    raise RuntimeError(
        "動画をDiscordのファイル上限以下に分割できませんでした。FFmpegのログ、動画形式、"
        "実際のアップロード上限を確認してください。"
    )


def compress_audio_sync(
    source: Path,
    output_dir: Path,
    max_bytes: int,
    config: dict[str, Any],
    ffmpeg_path: str,
    ffprobe_path: str,
    logger: logging.Logger,
) -> Path:
    """Transcode oversized audio to a separate AAC/M4A file under the upload cap."""
    duration = probe_duration_sync(source, ffprobe_path)
    target_bytes = int(max_bytes * 0.90)
    estimated_kbps = int((target_bytes * 8) / duration / 1000)
    starting_kbps = max(32, min(config["compression_audio_bitrate_kbps"], estimated_kbps))
    last_output: Path | None = None
    for attempt in range(config["compression_attempts"]):
        bitrate = max(24, int(starting_kbps * (0.72 ** attempt)))
        output = output_dir / f"compressed_audio_{attempt + 1}.m4a"
        command = [
            ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "aac",
            "-b:a", f"{bitrate}k", "-movflags", "+faststart", "-map_metadata", "-1", str(output),
        ]
        logger.info("音声を圧縮します (試行 %s/%s、約 %s kbps)", attempt + 1, config["compression_attempts"], bitrate)
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(120, int(duration * 4 + 60)), check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            last_output = None
            logger.warning("音声圧縮試行 %s が失敗しました: %s", attempt + 1, result.stderr[-1200:])
            continue
        last_output = output
        if file_size_bytes(output) <= max_bytes:
            return output
        logger.warning("音声圧縮後も上限超過です: %.2f MiB / %.2f MiB", file_size_bytes(output) / 1048576, max_bytes / 1048576)
    if last_output is not None:
        raise RuntimeError(
            f"音声を圧縮しましたがDiscordの上限以下になりませんでした ({file_size_bytes(last_output) / 1048576:.2f} MiB、上限 {max_bytes / 1048576:.2f} MiB)。"
        )
    raise RuntimeError("音声のFFmpeg圧縮に失敗しました。ログを確認してください。")


@dataclass(slots=True)
class Job:
    url: str
    status_id: str
    message: discord.Message


class SingleInstanceLock:
    """OS file lock; stale lock files are harmless because the lock is kernel-managed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None
        self._backend = None

    def acquire(self) -> bool:
        self._file = self.path.open("a+", encoding="utf-8")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(" ")
            self._file.flush()
        self._file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                self._backend = "msvcrt"
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._backend = "fcntl"
        except (OSError, BlockingIOError):
            self._file.close()
            self._file = None
            return False
        self._file.seek(0)
        self._file.truncate()
        self._file.write(str(os.getpid()))
        self._file.flush()
        return True

    def release(self) -> None:
        if not self._file:
            return
        try:
            self._file.seek(0)
            if self._backend == "msvcrt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            elif self._backend == "fcntl":
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._file.close()
        self._file = None


class XVideoBot(discord.Client):
    def __init__(
        self,
        *,
        config: dict[str, Any],
        logger: logging.Logger,
        ffmpeg_path: str | None,
        ffprobe_path: str | None,
        dedup_store: DedupStore,
    ) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.logger = logger
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.dedup_store = dedup_store
        self.monitored_ids = set(config["monitored_channel_ids"])
        self.job_queue: asyncio.Queue[Job | None] = asyncio.Queue(maxsize=config["max_queue_size"])
        self.worker_tasks: list[asyncio.Task[None]] = []
        self.stop_task: asyncio.Task[None] | None = None
        self._closing_requested = False
        # Tracks all X links found in each source message. Delete the source only
        # after every link from that message was queued and uploaded successfully.
        self._source_batches: dict[int, dict[str, Any]] = {}
        self.tree = discord.app_commands.CommandTree(self)
        self._application_commands_synced = False
        self._register_channel_commands()
        self._register_display_commands()
        self._register_duplicate_commands()

    def _register_channel_commands(self) -> None:
        """Register administrator-only slash commands for runtime channel management."""
        async def add_channel(
            interaction: discord.Interaction,
            channel: discord.TextChannel | None = None,
        ) -> None:
            if not await self._authorize_channel_command(interaction):
                return
            guild = interaction.guild
            target = channel or interaction.channel
            if guild is None or not isinstance(target, discord.TextChannel) or target.guild.id != guild.id:
                await interaction.response.send_message(
                    "対象はこのサーバー内のテキストチャンネルにしてください。コマンドをチャンネル内で実行すれば、そのチャンネルを指定できます。",
                    ephemeral=True,
                )
                return
            if target.id in self.monitored_ids:
                await interaction.response.send_message(
                    f"{target.mention} はすでに自動変換の対象です。", ephemeral=True
                )
                return

            bot_member = guild.me
            if bot_member is not None and not target.permissions_for(bot_member).view_channel:
                await interaction.response.send_message(
                    f"Botに {target.mention} の「チャンネルを見る」権限がありません。先にチャンネル権限を確認してください。",
                    ephemeral=True,
                )
                return

            previous_ids = set(self.monitored_ids)
            self.monitored_ids.add(target.id)
            try:
                self._persist_monitored_channel_ids()
            except OSError as exc:
                self.monitored_ids = previous_ids
                self.config["monitored_channel_ids"] = sorted(previous_ids)
                self.logger.exception("監視チャンネル設定の保存に失敗しました")
                await interaction.response.send_message(
                    f"設定を保存できませんでした: `{type(exc).__name__}`。Botフォルダーの書き込み権限を確認してください。",
                    ephemeral=True,
                )
                return

            warning = ""
            if bot_member is not None:
                perms = target.permissions_for(bot_member)
                missing = []
                if not perms.read_message_history:
                    missing.append("メッセージ履歴を読む")
                if not perms.send_messages:
                    missing.append("メッセージを送信")
                if not perms.attach_files:
                    missing.append("ファイルを添付")
                if self.config.get("delete_source_message_on_success", True) and not perms.manage_messages:
                    missing.append("メッセージを管理（元投稿の削除用）")
                if missing:
                    warning = "\n\n⚠️ Botに不足している可能性がある権限: " + "、".join(missing)
            await interaction.response.send_message(
                f"✅ {target.mention} を自動変換チャンネルに追加しました。再起動は不要です。{warning}",
                ephemeral=True,
            )
            self.logger.info("スラッシュコマンドで監視チャンネルを追加しました (guild_id=%s, channel_id=%s, user_id=%s)", guild.id, target.id, interaction.user.id)

        async def remove_channel(
            interaction: discord.Interaction,
            channel: discord.TextChannel | None = None,
        ) -> None:
            if not await self._authorize_channel_command(interaction):
                return
            guild = interaction.guild
            target = channel or interaction.channel
            if guild is None or not isinstance(target, discord.TextChannel) or target.guild.id != guild.id:
                await interaction.response.send_message(
                    "対象はこのサーバー内のテキストチャンネルにしてください。コマンドをチャンネル内で実行すれば、そのチャンネルを指定できます。",
                    ephemeral=True,
                )
                return
            if target.id not in self.monitored_ids:
                await interaction.response.send_message(
                    f"{target.mention} は自動変換の対象に設定されていません。", ephemeral=True
                )
                return

            previous_ids = set(self.monitored_ids)
            self.monitored_ids.discard(target.id)
            try:
                self._persist_monitored_channel_ids()
            except OSError as exc:
                self.monitored_ids = previous_ids
                self.config["monitored_channel_ids"] = sorted(previous_ids)
                self.logger.exception("監視チャンネル設定の保存に失敗しました")
                await interaction.response.send_message(
                    f"設定を保存できませんでした: `{type(exc).__name__}`。Botフォルダーの書き込み権限を確認してください。",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"✅ {target.mention} を自動変換チャンネルから削除しました。再起動は不要です。",
                ephemeral=True,
            )
            self.logger.info("スラッシュコマンドで監視チャンネルを削除しました (guild_id=%s, channel_id=%s, user_id=%s)", guild.id, target.id, interaction.user.id)

        async def list_channels(interaction: discord.Interaction) -> None:
            if not await self._authorize_channel_command(interaction):
                return
            guild = interaction.guild
            if guild is None:
                await interaction.response.send_message("このコマンドはサーバー内で使用してください。", ephemeral=True)
                return
            lines = [
                f"• {channel.mention} (`{channel.id}`)"
                for channel in guild.text_channels
                if channel.id in self.monitored_ids
            ]
            if lines:
                content = "**このサーバーの自動変換チャンネル**\n" + "\n".join(lines)
            else:
                content = "このサーバーには自動変換チャンネルがありません。`/convert_channel_add` で追加できます。"
            await interaction.response.send_message(content, ephemeral=True)

        add_channel = discord.app_commands.guild_only()(add_channel)
        add_channel = discord.app_commands.default_permissions(manage_guild=True)(add_channel)
        add_channel = discord.app_commands.describe(
            channel="追加するチャンネル。省略するとコマンドを実行したチャンネルです。"
        )(add_channel)
        remove_channel = discord.app_commands.guild_only()(remove_channel)
        remove_channel = discord.app_commands.default_permissions(manage_guild=True)(remove_channel)
        remove_channel = discord.app_commands.describe(
            channel="削除するチャンネル。省略するとコマンドを実行したチャンネルです。"
        )(remove_channel)
        list_channels = discord.app_commands.guild_only()(list_channels)
        list_channels = discord.app_commands.default_permissions(manage_guild=True)(list_channels)

        self.tree.add_command(discord.app_commands.Command(
            name="convert_channel_add",
            description="指定したテキストチャンネルを自動変換の対象に追加します",
            callback=add_channel,
        ))
        self.tree.add_command(discord.app_commands.Command(
            name="convert_channel_remove",
            description="指定したテキストチャンネルを自動変換の対象から削除します",
            callback=remove_channel,
        ))
        self.tree.add_command(discord.app_commands.Command(
            name="convert_channel_list",
            description="このサーバーの自動変換チャンネルを一覧表示します",
            callback=list_channels,
        ))

    def _register_display_commands(self) -> None:
        """Register administrator-only commands for channel display profiles."""

        def get_target_channel(
            interaction: discord.Interaction,
            channel: discord.TextChannel | None,
        ) -> discord.TextChannel | None:
            guild = interaction.guild
            target = channel or interaction.channel
            if guild is None or not isinstance(target, discord.TextChannel) or target.guild.id != guild.id:
                return None
            return target

        def display_flags(profile: dict[str, object]) -> str:
            labels = (
                ("show_author_name", "投稿者"),
                ("show_account_name", "アカウント名"),
                ("show_post_text", "投稿本文"),
                ("show_original_link", "元リンク"),
            )
            values = [f"{label}: {'表示' if profile.get(key) else '非表示'}" for key, label in labels]
            heading = str(profile.get("heading", ""))
            values.append(f"見出し: {heading if heading else 'なし'}")
            return " / ".join(values)

        async def list_profiles(
            interaction: discord.Interaction,
            channel: discord.TextChannel | None = None,
        ) -> None:
            if not await self._authorize_channel_command(interaction):
                return
            target = get_target_channel(interaction, channel)
            if target is None:
                await interaction.response.send_message(
                    "対象はこのサーバー内のテキストチャンネルにしてください。", ephemeral=True
                )
                return

            settings = self.config.get("display_settings", {})
            groups = settings.get("groups", {}) if isinstance(settings, dict) else {}
            channels = settings.get("channels", {}) if isinstance(settings, dict) else {}
            current_assignment = channels.get(target.id, channels.get(str(target.id))) if isinstance(channels, dict) else None
            current_group = None
            if isinstance(current_assignment, str):
                current_group = current_assignment
            elif isinstance(current_assignment, dict) and isinstance(current_assignment.get("group"), str):
                current_group = current_assignment["group"]

            current = resolve_display_settings(settings, target.id)
            lines = [f"**{target.mention} の表示設定**"]
            lines.append(f"現在のプロファイル: `{current_group or 'チャンネル個別設定 / default'}`")
            lines.append(display_flags(current))
            lines.append("\n**利用できるプロファイル**")
            if isinstance(groups, dict) and groups:
                for name, profile in sorted(groups.items(), key=lambda item: str(item[0]).lower()):
                    if isinstance(profile, dict):
                        lines.append(f"• `{name}` — {display_flags(profile)}")
            else:
                lines.append("登録済みプロファイルはありません。`/display_profile_create` で作成してください。")
            await interaction.response.send_message("\n".join(lines)[:1950], ephemeral=True)

        async def create_profile(
            interaction: discord.Interaction,
            profile_name: str,
            show_author_name: bool,
            show_account_name: bool,
            show_post_text: bool,
            show_original_link: bool,
            heading: str | None = None,
        ) -> None:
            if not await self._authorize_channel_command(interaction):
                return
            name = profile_name.strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name) or name == "default":
                await interaction.response.send_message(
                    "プロファイル名は英小文字・数字・`_`・`-` の32文字以内にしてください（`default` は使用できません）。",
                    ephemeral=True,
                )
                return

            settings = self.config.setdefault("display_settings", {"default": {}, "groups": {}, "channels": {}})
            if not isinstance(settings, dict):
                await interaction.response.send_message("表示設定の形式が不正です。config.json を確認してください。", ephemeral=True)
                return
            groups = settings.setdefault("groups", {})
            if not isinstance(groups, dict):
                await interaction.response.send_message("表示プロファイルの保存領域が不正です。config.json を確認してください。", ephemeral=True)
                return
            previous = groups.get(name)
            profile = {
                "show_author_name": show_author_name,
                "show_account_name": show_account_name,
                "show_post_text": show_post_text,
                "show_original_link": show_original_link,
                "heading": (heading or "").strip()[:80],
            }
            groups[name] = profile
            try:
                self._persist_config()
            except OSError as exc:
                if previous is None:
                    groups.pop(name, None)
                else:
                    groups[name] = previous
                self.logger.exception("表示プロファイルの保存に失敗しました")
                await interaction.response.send_message(
                    f"設定を保存できませんでした: `{type(exc).__name__}`。Botフォルダーの書き込み権限を確認してください。",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"✅ 表示プロファイル `{name}` を保存しました。\n{display_flags(profile)}\n"
                "適用するには `/display_profile_set` を実行してください。既にこのプロファイルを使うチャンネルにも変更が反映されます。",
                ephemeral=True,
            )

        async def set_profile(
            interaction: discord.Interaction,
            profile_name: str,
            channel: discord.TextChannel | None = None,
        ) -> None:
            if not await self._authorize_channel_command(interaction):
                return
            target = get_target_channel(interaction, channel)
            if target is None:
                await interaction.response.send_message(
                    "対象はこのサーバー内のテキストチャンネルにしてください。", ephemeral=True
                )
                return
            name = profile_name.strip().lower()
            settings = self.config.get("display_settings", {})
            groups = settings.get("groups", {}) if isinstance(settings, dict) else {}
            if not isinstance(groups, dict) or name not in groups:
                available = ", ".join(f"`{key}`" for key in sorted(groups)) if isinstance(groups, dict) else "なし"
                await interaction.response.send_message(
                    f"プロファイル `{name}` がありません。利用可能: {available}。`/display_profile_create` で作成できます。",
                    ephemeral=True,
                )
                return
            channels = settings.setdefault("channels", {})
            previous = channels.get(target.id)
            channels[target.id] = name
            try:
                self._persist_config()
            except OSError as exc:
                if previous is None:
                    channels.pop(target.id, None)
                else:
                    channels[target.id] = previous
                self.logger.exception("チャンネル表示プロファイルの保存に失敗しました")
                await interaction.response.send_message(
                    f"設定を保存できませんでした: `{type(exc).__name__}`。Botフォルダーの書き込み権限を確認してください。",
                    ephemeral=True,
                )
                return
            profile = resolve_display_settings(settings, target.id)
            await interaction.response.send_message(
                f"✅ {target.mention} に表示プロファイル `{name}` を設定しました。再起動は不要です。\n{display_flags(profile)}",
                ephemeral=True,
            )

        async def set_items(
            interaction: discord.Interaction,
            show_author_name: bool | None = None,
            show_account_name: bool | None = None,
            show_post_text: bool | None = None,
            show_original_link: bool | None = None,
            heading: str | None = None,
            channel: discord.TextChannel | None = None,
        ) -> None:
            if not await self._authorize_channel_command(interaction):
                return
            target = get_target_channel(interaction, channel)
            if target is None:
                await interaction.response.send_message(
                    "対象はこのサーバー内のテキストチャンネルにしてください。", ephemeral=True
                )
                return
            changes = {
                "show_author_name": show_author_name,
                "show_account_name": show_account_name,
                "show_post_text": show_post_text,
                "show_original_link": show_original_link,
            }
            changes = {key: value for key, value in changes.items() if value is not None}
            if heading is not None:
                changes["heading"] = heading.strip()[:80]
            if not changes:
                await interaction.response.send_message(
                    "変更する項目を1つ以上指定してください。true は表示、false は非表示です。",
                    ephemeral=True,
                )
                return

            settings = self.config.setdefault("display_settings", {"default": {}, "groups": {}, "channels": {}})
            channels = settings.setdefault("channels", {})
            previous = channels.get(target.id)
            updated = resolve_display_settings(settings, target.id)
            updated.update(changes)
            channels[target.id] = updated
            try:
                self._persist_config()
            except OSError as exc:
                if previous is None:
                    channels.pop(target.id, None)
                else:
                    channels[target.id] = previous
                self.logger.exception("チャンネル表示項目の保存に失敗しました")
                await interaction.response.send_message(
                    f"設定を保存できませんでした: `{type(exc).__name__}`。Botフォルダーの書き込み権限を確認してください。",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"✅ {target.mention} の表示項目を更新しました。再起動は不要です。\n{display_flags(updated)}",
                ephemeral=True,
            )

        list_profiles = discord.app_commands.guild_only()(list_profiles)
        list_profiles = discord.app_commands.default_permissions(manage_guild=True)(list_profiles)
        list_profiles = discord.app_commands.describe(
            channel="確認するチャンネル。省略するとコマンドを実行したチャンネルです。"
        )(list_profiles)
        create_profile = discord.app_commands.guild_only()(create_profile)
        create_profile = discord.app_commands.default_permissions(manage_guild=True)(create_profile)
        create_profile = discord.app_commands.describe(
            profile_name="英小文字・数字・_・- で指定するプロファイル名",
            show_author_name="投稿者の表示名を表示するか",
            show_account_name="@アカウント名を表示するか",
            show_post_text="投稿本文を表示するか",
            show_original_link="元投稿リンクを表示するか",
            heading="任意の見出し。空欄なら見出しなし",
        )(create_profile)
        set_profile = discord.app_commands.guild_only()(set_profile)
        set_profile = discord.app_commands.default_permissions(manage_guild=True)(set_profile)
        set_profile = discord.app_commands.describe(
            profile_name="適用するプロファイル名（例: archive / links_only）",
            channel="設定するチャンネル。省略するとコマンドを実行したチャンネルです。",
        )(set_profile)
        set_items = discord.app_commands.guild_only()(set_items)
        set_items = discord.app_commands.default_permissions(manage_guild=True)(set_items)
        set_items = discord.app_commands.describe(
            show_author_name="投稿者の表示名（true=表示 / false=非表示）",
            show_account_name="@アカウント名（true=表示 / false=非表示）",
            show_post_text="投稿本文（true=表示 / false=非表示）",
            show_original_link="元投稿リンク（true=表示 / false=非表示）",
            heading="見出し。空欄にすると見出しを消します。",
            channel="設定するチャンネル。省略するとコマンドを実行したチャンネルです。",
        )(set_items)

        commands = (
            ("display_profile_list", "利用可能な表示プロファイルと現在の設定を表示します", list_profiles),
            ("display_profile_create", "表示項目を選んで独自プロファイルを作成または更新します", create_profile),
            ("display_profile_set", "このチャンネルに表示プロファイルを適用します", set_profile),
            ("display_items_set", "このチャンネルで表示する項目を個別に設定します", set_items),
        )
        for name, description, callback in commands:
            self.tree.add_command(discord.app_commands.Command(name=name, description=description, callback=callback))

    def _register_duplicate_commands(self) -> None:
        """Register administrator-only per-channel duplicate-skip controls."""
        async def set_duplicate_skip(
            interaction: discord.Interaction,
            enabled: bool,
            channel: discord.TextChannel | None = None,
        ) -> None:
            if not await self._authorize_channel_command(interaction):
                return
            guild = interaction.guild
            target = channel or interaction.channel
            if guild is None or not isinstance(target, discord.TextChannel) or target.guild.id != guild.id:
                await interaction.response.send_message(
                    "対象はこのサーバー内のテキストチャンネルにしてください。", ephemeral=True
                )
                return

            overrides = self.config.setdefault("deduplicate_posts_by_channel", {})
            previous = overrides.get(target.id)
            had_previous = target.id in overrides
            overrides[target.id] = enabled
            try:
                self._persist_config()
            except OSError as exc:
                if had_previous:
                    overrides[target.id] = previous
                else:
                    overrides.pop(target.id, None)
                self.logger.exception("チャンネル重複スキップ設定の保存に失敗しました")
                await interaction.response.send_message(
                    f"設定を保存できませんでした: `{type(exc).__name__}`。Botフォルダーの書き込み権限を確認してください。",
                    ephemeral=True,
                )
                return

            if enabled:
                description = "有効：過去に正常変換した同じ投稿IDはスキップします。"
            else:
                description = (
                    "無効：正常変換済みの同じ投稿も再変換します。"
                    "ただし、同じ投稿がキュー内または処理中の場合は二重処理を防ぐためスキップします。"
                )
            await interaction.response.send_message(
                f"✅ {target.mention} の重複投稿スキップを **{'有効' if enabled else '無効'}** にしました。\n{description}",
                ephemeral=True,
            )
            self.logger.info(
                "チャンネル重複スキップ設定を変更しました (guild_id=%s, channel_id=%s, enabled=%s, user_id=%s)",
                guild.id, target.id, enabled, interaction.user.id,
            )

        async def show_duplicate_skip_status(
            interaction: discord.Interaction,
            channel: discord.TextChannel | None = None,
        ) -> None:
            if not await self._authorize_channel_command(interaction):
                return
            guild = interaction.guild
            target = channel or interaction.channel
            if guild is None or not isinstance(target, discord.TextChannel) or target.guild.id != guild.id:
                await interaction.response.send_message(
                    "対象はこのサーバー内のテキストチャンネルにしてください。", ephemeral=True
                )
                return
            overrides = self.config.get("deduplicate_posts_by_channel", {})
            is_custom = target.id in overrides
            enabled = resolve_channel_bool_setting(
                self.config.get("deduplicate_posts", True), overrides, target.id
            )
            source = "チャンネル個別設定" if is_custom else "全体の既定値を継承"
            detail = (
                "成功済みの同一投稿をスキップします。" if enabled
                else "成功済みの同一投稿も再変換します（処理中・キュー内の二重処理は防止します）。"
            )
            await interaction.response.send_message(
                f"**{target.mention} の重複投稿スキップ設定**\n"
                f"状態: **{'有効' if enabled else '無効'}**\n"
                f"適用元: {source}\n{detail}",
                ephemeral=True,
            )

        async def reset_duplicate_skip(
            interaction: discord.Interaction,
            channel: discord.TextChannel | None = None,
        ) -> None:
            if not await self._authorize_channel_command(interaction):
                return
            guild = interaction.guild
            target = channel or interaction.channel
            if guild is None or not isinstance(target, discord.TextChannel) or target.guild.id != guild.id:
                await interaction.response.send_message(
                    "対象はこのサーバー内のテキストチャンネルにしてください。", ephemeral=True
                )
                return
            overrides = self.config.setdefault("deduplicate_posts_by_channel", {})
            if target.id not in overrides:
                await interaction.response.send_message(
                    f"{target.mention} はすでに全体の既定値を使用しています。", ephemeral=True
                )
                return
            previous = overrides.pop(target.id)
            try:
                self._persist_config()
            except OSError as exc:
                overrides[target.id] = previous
                self.logger.exception("チャンネル重複スキップ設定のリセットに失敗しました")
                await interaction.response.send_message(
                    f"設定を保存できませんでした: `{type(exc).__name__}`。Botフォルダーの書き込み権限を確認してください。",
                    ephemeral=True,
                )
                return
            enabled = bool(self.config.get("deduplicate_posts", True))
            await interaction.response.send_message(
                f"✅ {target.mention} の個別設定を削除し、全体の既定値に戻しました（{'有効' if enabled else '無効'}）。",
                ephemeral=True,
            )

        set_duplicate_skip = discord.app_commands.guild_only()(set_duplicate_skip)
        set_duplicate_skip = discord.app_commands.default_permissions(manage_guild=True)(set_duplicate_skip)
        set_duplicate_skip = discord.app_commands.describe(
            enabled="true=成功済みの重複をスキップ / false=成功済みでも再変換",
            channel="設定するチャンネル。省略するとコマンドを実行したチャンネルです。",
        )(set_duplicate_skip)
        show_duplicate_skip_status = discord.app_commands.guild_only()(show_duplicate_skip_status)
        show_duplicate_skip_status = discord.app_commands.default_permissions(manage_guild=True)(show_duplicate_skip_status)
        show_duplicate_skip_status = discord.app_commands.describe(
            channel="確認するチャンネル。省略するとコマンドを実行したチャンネルです。",
        )(show_duplicate_skip_status)
        reset_duplicate_skip = discord.app_commands.guild_only()(reset_duplicate_skip)
        reset_duplicate_skip = discord.app_commands.default_permissions(manage_guild=True)(reset_duplicate_skip)
        reset_duplicate_skip = discord.app_commands.describe(
            channel="既定値に戻すチャンネル。省略するとコマンドを実行したチャンネルです。",
        )(reset_duplicate_skip)

        commands = (
            ("duplicate_skip_set", "指定チャンネルの重複投稿スキップを有効／無効にします", set_duplicate_skip),
            ("duplicate_skip_status", "指定チャンネルの重複投稿スキップ設定を確認します", show_duplicate_skip_status),
            ("duplicate_skip_reset", "指定チャンネルの重複投稿設定を全体の既定値に戻します", reset_duplicate_skip),
        )
        for name, description, callback in commands:
            self.tree.add_command(discord.app_commands.Command(name=name, description=description, callback=callback))

    async def _authorize_channel_command(self, interaction: discord.Interaction) -> bool:
        """Only server administrators or members with Manage Server can change channels."""
        if interaction.guild is None:
            await interaction.response.send_message("このコマンドはサーバー内で使用してください。", ephemeral=True)
            return False
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is None or not (permissions.administrator or permissions.manage_guild):
            await interaction.response.send_message(
                "この操作には「サーバー管理」または「管理者」権限が必要です。", ephemeral=True
            )
            return False
        return True

    def _persist_config(self) -> None:
        """Atomically save the serializable runtime configuration to config.json."""
        serializable = {key: value for key, value in self.config.items() if not key.startswith("_")}
        temporary_path = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        try:
            temporary_path.write_text(
                json.dumps(serializable, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, CONFIG_PATH)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _persist_monitored_channel_ids(self) -> None:
        """Save monitored channel IDs and the full runtime configuration."""
        self.config["monitored_channel_ids"] = sorted(self.monitored_ids)
        self._persist_config()

    async def _sync_application_commands(self) -> None:
        """Sync globally and copy commands into joined guilds for immediate availability."""
        if self._application_commands_synced:
            return
        try:
            global_commands = await self.tree.sync()
            self.logger.info("グローバルスラッシュコマンドを同期しました (%s件)", len(global_commands))
            for guild in self.guilds:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            self._application_commands_synced = True
        except Exception:
            self.logger.exception("スラッシュコマンドの同期に失敗しました。Discord側の権限・接続とapplications.commandsスコープを確認してください。")

    async def setup_hook(self) -> None:
        self.worker_tasks = [
            asyncio.create_task(self._worker(index + 1), name=f"download-worker-{index + 1}")
            for index in range(self.config["max_concurrent_downloads"])
        ]
        self.stop_task = asyncio.create_task(self._watch_stop_request(), name="stop-request-watcher")

    async def on_ready(self) -> None:
        self.logger.info("Discordに接続しました: %s (ID: %s)", self.user, self.user.id if self.user else "?")
        await self._sync_application_commands()
        self.logger.info("監視対象チャンネルID: %s", sorted(self.monitored_ids))
        self.logger.info(
            "元リンク自動削除: %s",
            "有効" if self.config.get("delete_source_message_on_success", True) else "無効",
        )
        for channel_id in sorted(self.monitored_ids):
            channel = self.get_channel(channel_id)
            if channel is None:
                self.logger.warning("監視チャンネルがキャッシュにありません (ID: %s)。Bot権限・IDを確認してください。", channel_id)
                continue
            # Deleting a source message from a user or webhook requires Manage Messages.
            # Warn at startup instead of silently leaving the original link behind.
            guild = getattr(channel, "guild", None)
            bot_member = getattr(guild, "me", None) if guild is not None else None
            if bot_member is not None:
                try:
                    permissions = channel.permissions_for(bot_member)
                    if self.config.get("delete_source_message_on_success", True) and not permissions.manage_messages:
                        self.logger.warning(
                            "このチャンネルでは元リンクを削除できません: Botに「メッセージを管理」権限がありません (channel_id=%s)。",
                            channel_id,
                        )
                except Exception:
                    self.logger.debug("チャンネル権限の確認に失敗しました (channel_id=%s)", channel_id, exc_info=True)

    async def on_message(self, message: discord.Message) -> None:
        if self._closing_requested:
            return
        is_own_message = bool(self.user and message.author.id == self.user.id)
        is_webhook_message = getattr(message, "webhook_id", None) is not None
        if should_ignore_bot_message(
            author_is_bot=bool(getattr(message.author, "bot", False)),
            is_webhook_message=is_webhook_message,
            is_own_message=is_own_message,
        ):
            return
        if message.channel.id not in self.monitored_ids:
            return

        source_text = message_text_for_url_detection(message)
        found = extract_media_urls(source_text)
        if not found:
            return

        # A source message can contain multiple X links. Keep it if any link is
        # skipped, cannot be queued, or fails; this prevents losing the only copy
        # of a link before the corresponding video is safely posted.
        batch: dict[str, Any] = {
            "message": message,
            "expected": len(found),
            "queued": 0,
            "completed": 0,
            "successes": 0,
            "can_delete": True,
            "finalized": False,
        }
        self._source_batches[message.id] = batch

        for url, status_id in found:
            try:
                claimed = self.dedup_store.claim(
                    status_id,
                    message.id,
                    deduplicate_success=resolve_channel_bool_setting(
                        self.config["deduplicate_posts"],
                        self.config.get("deduplicate_posts_by_channel", {}),
                        message.channel.id,
                    ),
                )
            except Exception:
                batch["can_delete"] = False
                self.logger.exception("重複管理DBの処理に失敗しました (投稿ID=%s)", status_id)
                await self._reply_safely(message, "⚠️ 重複管理DBでエラーが発生したため、このリンクは処理しませんでした。")
                continue
            if not claimed:
                batch["can_delete"] = False
                self.logger.info("重複または処理中の投稿をスキップしました (投稿ID=%s)", status_id)
                continue
            try:
                self.job_queue.put_nowait(Job(url=url, status_id=status_id, message=message))
                batch["queued"] += 1
                self.logger.info("ジョブをキューに追加しました (投稿ID=%s、キュー=%s)", status_id, self.job_queue.qsize())
            except asyncio.QueueFull:
                batch["can_delete"] = False
                self.dedup_store.set_status(status_id, "failed", "Queue full")
                await self._reply_safely(message, "⚠️ Botの処理キューが満杯です。時間をおいてリンクを再投稿してください。")

        batch["finalized"] = True
        if batch["queued"] != batch["expected"]:
            batch["can_delete"] = False
        await self._maybe_finalize_source_batch(message.id)

    async def _finish_source_job(self, job: Job, success: bool) -> None:
        batch = self._source_batches.get(job.message.id)
        if batch is None:
            return
        batch["completed"] += 1
        if success:
            batch["successes"] += 1
        await self._maybe_finalize_source_batch(job.message.id)

    async def _maybe_finalize_source_batch(self, message_id: int) -> None:
        batch = self._source_batches.get(message_id)
        if batch is None or not batch["finalized"]:
            return
        if batch["completed"] < batch["queued"]:
            return

        # Remove the tracker before making Discord API calls, avoiding duplicate
        # deletion attempts if several workers finish at nearly the same time.
        self._source_batches.pop(message_id, None)
        if not self.config["delete_source_message_on_success"]:
            self.logger.info("設定で元リンクの自動削除が無効です (message_id=%s)", message_id)
            return
        if not batch["can_delete"] or batch["queued"] != batch["expected"]:
            self.logger.info(
                "元リンクは残しました。すべてのURLを処理できていません (message_id=%s, queued=%s, expected=%s, can_delete=%s)",
                message_id, batch["queued"], batch["expected"], batch["can_delete"],
            )
            return
        if batch["successes"] != batch["expected"]:
            self.logger.info(
                "元リンクは残しました。メディア投稿に失敗したURLがあります (message_id=%s, successes=%s, expected=%s)",
                message_id, batch["successes"], batch["expected"],
            )
            return

        source_message: discord.Message = batch["message"]
        is_webhook = getattr(source_message, "webhook_id", None) is not None
        self.logger.info(
            "元リンクメッセージの削除を試みます (message_id=%s, channel_id=%s, webhook=%s)",
            message_id, getattr(source_message.channel, "id", "?"), is_webhook,
        )
        try:
            await source_message.delete()
            self.logger.info("メディア投稿の成功後、元のリンクメッセージを削除しました (message_id=%s)", message_id)
        except discord.NotFound:
            # It was already removed by a user or a moderator.
            self.logger.info("元のリンクメッセージは既に削除されています (message_id=%s)", message_id)
        except discord.Forbidden:
            self.logger.warning(
                "元のリンクメッセージを削除できません。監視チャンネルでBotに「メッセージを管理」権限を許可してください (message_id=%s, webhook=%s)",
                message_id, is_webhook,
            )
            await self._reply_safely(
                source_message,
                "⚠️ メディアの投稿は成功しましたが、元リンクを削除できませんでした。Botにこのチャンネルの「メッセージを管理」権限を付与してください。",
            )
        except discord.HTTPException as exc:
            self.logger.warning(
                "元のリンクメッセージ削除に失敗しました (%s、HTTP %s、message_id=%s)",
                type(exc).__name__, getattr(exc, "status", "?"), message_id,
            )
            await self._reply_safely(
                source_message,
                "⚠️ メディアの投稿は成功しましたが、Discord APIのエラーにより元リンクを削除できませんでした。logs/bot.log を確認してください。",
            )

    async def _watch_stop_request(self) -> None:
        while not self.is_closed():
            await asyncio.sleep(1)
            if STOP_REQUEST_PATH.exists():
                try:
                    STOP_REQUEST_PATH.unlink(missing_ok=True)
                except OSError:
                    pass
                self._closing_requested = True
                self.logger.info("stop.request を検出しました。キューの処理終了を待ちます。")
                try:
                    await asyncio.wait_for(
                        self.job_queue.join(),
                        timeout=self.config["shutdown_drain_timeout_seconds"],
                    )
                except asyncio.TimeoutError:
                    self.logger.warning("停止待ち時間を超えました。ファイルの破損や一時ファイル残留を避けるため、実行中ジョブが終了するまで待機を継続します。")
                    await self.job_queue.join()
                await self.close()
                return

    async def _worker(self, worker_no: int) -> None:
        self.logger.info("ワーカー %s 起動", worker_no)
        while True:
            job = await self.job_queue.get()
            if job is None:
                self.job_queue.task_done()
                return
            try:
                self.dedup_store.set_status(job.status_id, "processing")
                await self._process_job(job)
            except asyncio.CancelledError:
                self.logger.warning("ワーカー %s が停止されました (投稿ID=%s)", worker_no, job.status_id)
                try:
                    self.dedup_store.set_status(job.status_id, "failed", "Bot shutdown interrupted processing")
                except Exception:
                    pass
                raise
            except Exception as exc:
                self.logger.exception("ジョブ処理で予期しないエラー (投稿ID=%s)", job.status_id)
                try:
                    self.dedup_store.set_status(job.status_id, "failed", type(exc).__name__)
                except Exception:
                    self.logger.exception("ジョブ失敗状態をDBに記録できません (投稿ID=%s)", job.status_id)
                if is_disk_full_error(exc):
                    error_text = "保存先ディスクの空き容量が不足しています。tempフォルダーとドライブの空き容量を確認してください。"
                else:
                    error_text = f"メディア処理中に予期しないエラーが発生しました（{type(exc).__name__}）。logs/bot.log を確認してください。"
                await self._reply_safely(job.message, f"❌ {error_text}")
            finally:
                self.job_queue.task_done()

    async def _process_job(self, job: Job) -> None:
        status_message: discord.Message | None = None
        if self.config["reply_with_progress"]:
            status_message = await self._reply_safely(job.message, f"⏳ メディアを取得中です（ID: {job.status_id}）。")

        temp_root: Path = self.config["_temp_path"]
        temp_root.mkdir(parents=True, exist_ok=True)
        final_success = False
        try:
            # TemporaryDirectory is dedicated to this job; its context removes all files afterward.
            with tempfile.TemporaryDirectory(prefix=f"x_{job.status_id}_", dir=temp_root, ignore_cleanup_errors=True) as temp_name:
                job_dir = Path(temp_name)
                try:
                    media_paths, post_metadata, media_type = await asyncio.to_thread(
                        download_media_sync, job.url, job_dir, self.config, self.ffmpeg_path, self.logger
                    )
                except Exception as exc:
                    self.logger.exception("Xメディアの取得に失敗しました (投稿ID=%s)", job.status_id)
                    detail = safe_exception_summary(exc, 800)
                    raise RuntimeError(
                        f"メディアを取得できませんでした: {detail}. "
                        "X側の仕様変更・一時制限の場合はupdate.batを実行してください。"
                    ) from exc

                if not media_paths:
                    raise FileNotFoundError("ダウンロードされたメディアファイルがありません。")
                if media_type == "image" and len(media_paths) > 10:
                    raise RuntimeError(f"画像が{len(media_paths)}枚あります。Discordの1メッセージ上限10個を超えています。")

                limit = self._upload_limit_bytes(job.message)
                upload_paths: list[Path] = []
                for media_path in media_paths:
                    size = file_size_bytes(media_path)
                    if size > mb_to_bytes(self.config["max_download_size_mb"]):
                        raise RuntimeError(
                            f"ダウンロードしたファイルが設定上限を超えています ({media_path.name}: {size / 1048576:.1f} MiB)。"
                        )
                    if size <= limit:
                        upload_paths.append(media_path)
                        continue

                    self.logger.info(
                        "アップロード上限超過 (投稿ID=%s、種類=%s): %.2f MiB > %.2f MiB",
                        job.status_id, media_type, size / 1048576, limit / 1048576,
                    )
                    if not self.config["compress_if_too_large"]:
                        raise RuntimeError(
                            f"{self._media_type_label(media_type)}ファイルがDiscordのアップロード上限を超えています ({size / 1048576:.1f} MiB / 上限 {limit / 1048576:.1f} MiB)。圧縮は設定で無効です。"
                        )
                    if media_type == "image":
                        raise RuntimeError(
                            f"画像ファイルがDiscordのアップロード上限を超えています ({size / 1048576:.1f} MiB / 上限 {limit / 1048576:.1f} MiB)。画像の自動圧縮は未対応です。"
                        )
                    if not self.ffmpeg_path or not self.ffprobe_path:
                        raise RuntimeError(
                            f"{self._media_type_label(media_type)}ファイルが上限超過 ({size / 1048576:.1f} MiB / 上限 {limit / 1048576:.1f} MiB) で、FFmpegまたはFFprobeが見つからないため圧縮できません。"
                        )
                    if media_type == "video":
                        try:
                            upload_path = await asyncio.to_thread(
                                compress_video_sync,
                                media_path, job_dir, limit, self.config, self.ffmpeg_path, self.ffprobe_path, self.logger,
                            )
                            if file_size_bytes(upload_path) > limit:
                                raise RuntimeError("圧縮後もDiscordの上限を超えています。")
                            upload_paths.append(upload_path)
                        except Exception as compression_exc:
                            # If a complete video cannot fit as one attachment, split it into playable parts.
                            # Prefer the last successful compressed attempt, else split the source stream-copy.
                            self.logger.warning(
                                "動画を1ファイルに圧縮できませんでした。複数パートへの分割を試します: %s",
                                str(compression_exc)[:500],
                            )
                            candidates = sorted(
                                (p for p in job_dir.glob("compressed_*.mp4") if p.is_file() and p.stat().st_size > 0),
                                key=lambda p: p.stat().st_mtime,
                            )
                            split_source = candidates[-1] if candidates else media_path
                            split_dir = job_dir / f"split_source_{len(upload_paths) + 1:03d}"
                            split_dir.mkdir(parents=True, exist_ok=True)
                            split_paths = await asyncio.to_thread(
                                split_video_sync,
                                split_source, split_dir, limit, self.ffmpeg_path, self.ffprobe_path,
                                self.logger, self.config["max_split_parts"],
                            )
                            for part in split_paths:
                                if file_size_bytes(part) > limit:
                                    raise RuntimeError("分割後の動画パートがDiscordのアップロード上限を超えています。")
                            upload_paths.extend(split_paths)
                    elif media_type == "audio":
                        upload_path = await asyncio.to_thread(
                            compress_audio_sync,
                            media_path, job_dir, limit, self.config, self.ffmpeg_path, self.ffprobe_path, self.logger,
                        )
                        size = file_size_bytes(upload_path)
                        if size > limit:
                            raise RuntimeError(f"圧縮後の{self._media_type_label(media_type)}ファイルがDiscordのアップロード上限を超えています。")
                        upload_paths.append(upload_path)
                    else:
                        raise RuntimeError(f"未対応のメディア種別です: {media_type}")

                base_names: list[str] = []
                platform_prefix = "pixiv" if is_pixiv_url(job.url) else "x"
                for index, path in enumerate(upload_paths, start=1):
                    suffix = path.suffix.lower()
                    if media_type == "image":
                        base_names.append(f"{platform_prefix}_image_{job.status_id}_{index:02d}{suffix}")
                    elif media_type == "video" and len(upload_paths) > 1:
                        base_names.append(f"{platform_prefix}_video_{job.status_id}_part_{index:03d}{suffix}")
                    else:
                        base_names.append(f"{platform_prefix}_{media_type}_{job.status_id}{suffix}")

                await self._upload_with_retry(job, upload_paths, base_names, post_metadata)
                final_success = True
                self.dedup_store.set_status(job.status_id, "success")
                total_size = sum(file_size_bytes(path) for path in upload_paths)
                self.logger.info(
                    "%sアップロード成功 (投稿ID=%s、ファイル数=%s、合計%.2f MiB)",
                    self._media_type_label(media_type), job.status_id, len(upload_paths), total_size / 1048576,
                )
                # Leave one clean result message by deleting the temporary progress reply.
                await self._delete_status_safely(status_message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.dedup_store.set_status(job.status_id, "failed", f"{type(exc).__name__}: {exc}")
            message_text = str(exc).replace("\n", " ")[:900]
            self.logger.error("メディア処理失敗 (投稿ID=%s): %s", job.status_id, message_text)
            await self._edit_status(status_message, f"❌ 処理に失敗しました: {message_text}\n詳細はPCの logs/bot.log を確認してください。")
            if status_message is None:
                await self._reply_safely(job.message, f"❌ メディアの処理に失敗しました: {message_text}")
        finally:
            if not final_success:
                self.logger.info("ジョブが成功扱いにならず終了しました (投稿ID=%s)", job.status_id)
            await self._finish_source_job(job, final_success)

    @staticmethod
    def _media_type_label(media_type: str) -> str:
        return {"video": "動画", "audio": "音声", "image": "画像"}.get(media_type, "メディア")

    def _upload_limit_bytes(self, message: discord.Message) -> int:
        fallback = mb_to_bytes(self.config["fallback_upload_limit_mb"])
        guild_limit = None
        guild = getattr(message.channel, "guild", None)
        try:
            if guild is not None:
                candidate = int(guild.filesize_limit)
                if candidate > 0:
                    guild_limit = candidate
        except (AttributeError, TypeError, ValueError):
            guild_limit = None
        # Floor stale/unboosted guild metadata at Discord's current standard 20 MiB cap.
        # Boosted guild limits remain higher and are preserved from guild.filesize_limit.
        limit = max(DISCORD_STANDARD_UPLOAD_LIMIT_BYTES, guild_limit or fallback)
        configured_mb = self.config["max_upload_size_mb"]
        if configured_mb > 0:
            limit = min(limit, mb_to_bytes(configured_mb))
        # Never use zero or negative limits from incomplete Discord metadata.
        return max(1, int(limit))

    async def _upload_with_retry(
        self, job: Job, paths: list[Path], filenames: list[str], metadata: dict[str, Any] | None = None
    ) -> None:
        if not paths or len(paths) != len(filenames):
            raise RuntimeError("アップロード対象ファイルの指定が不正です。")
        last_error: Exception | None = None
        channel_display = resolve_display_settings(self.config.get("display_settings", {}), job.message.channel.id)
        display_metadata = dict(metadata or {})
        if is_pixiv_url(job.url):
            display_metadata.setdefault("platform", "Pixiv")
            if channel_display.get("heading") == "X投稿情報":
                channel_display["heading"] = "Pixiv投稿情報"
        content = format_post_metadata(display_metadata, job.url, max_chars=1850, settings=channel_display)
        batch_count = (len(paths) + 9) // 10
        for batch_index, start in enumerate(range(0, len(paths), 10)):
            batch_paths = paths[start:start + 10]
            batch_filenames = filenames[start:start + 10]
            if batch_index == 0:
                batch_content = content
                if batch_count > 1:
                    batch_content = f"🎞️ 分割動画（1/{len(paths)}パート）\n{content}"[:1900]
            else:
                batch_content = f"🎞️ 分割動画の続き（メッセージ {batch_index + 1}/{batch_count}）\nソース: {job.url}"[:1900]
            last_error = None
            for attempt in range(3):
                try:
                    # Discord allows at most 10 attachments per message; large videos are sent in batches.
                    files: list[discord.File] = []
                    try:
                        for path, filename in zip(batch_paths, batch_filenames):
                            files.append(discord.File(str(path), filename=filename))
                        await job.message.channel.send(
                            content=batch_content,
                            files=files,
                            allowed_mentions=discord.AllowedMentions.none(),
                            suppress_embeds=True,
                        )
                    finally:
                        for file_obj in files:
                            try:
                                file_obj.close()
                            except Exception:
                                self.logger.debug("Discord添付ファイルのハンドルを閉じられませんでした", exc_info=True)
                    last_error = None
                    break
                except discord.HTTPException as exc:
                    last_error = exc
                    if exc.status < 500 or attempt >= 2:
                        break
                    await asyncio.sleep(2 ** (attempt + 1))
                except (asyncio.TimeoutError, OSError) as exc:
                    last_error = exc
                    if attempt >= 2:
                        break
                    await asyncio.sleep(2 ** (attempt + 1))
            if last_error is not None:
                break
        if last_error is None:
            return
        if last_error is not None:
            if isinstance(last_error, discord.Forbidden):
                raise RuntimeError("Discordへの送信権限がありません。チャンネルの送信・ファイル添付権限を確認してください。") from last_error
            if isinstance(last_error, discord.HTTPException) and last_error.status in {413, 400}:
                raise RuntimeError("Discordがファイルを受け付けませんでした。実際のアップロード上限・ファイル形式を確認してください。") from last_error
            if isinstance(last_error, discord.HTTPException) and last_error.status == 429:
                raise RuntimeError("Discordのレート制限が続いたためアップロードを中止しました。時間をおいて再試行してください。") from last_error
            raise RuntimeError(f"Discordへのアップロードに失敗しました ({type(last_error).__name__})") from last_error
        raise RuntimeError("Discordへのアップロードに失敗しました。")

    async def _reply_safely(self, message: discord.Message, content: str) -> discord.Message | None:
        try:
            return await message.reply(
                content[:1900],
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            self.logger.warning("Discordへの状況通知に失敗しました (%s)", type(exc).__name__)
            return None

    async def _delete_status_safely(self, message: discord.Message | None) -> None:
        if message is None:
            return
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            self.logger.warning("進捗メッセージを削除できません (message_id=%s)", getattr(message, "id", "?"))
        except Exception as exc:
            self.logger.warning("進捗メッセージの削除に失敗しました (%s)", type(exc).__name__)

    async def _edit_status(self, message: discord.Message | None, content: str) -> None:
        if message is None:
            return
        try:
            await message.edit(content=content[:1900], allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            self.logger.warning("状況メッセージを更新できません (%s)", type(exc).__name__)


def main() -> int:
    # Load .env without logging or displaying the token value.
    load_dotenv(ENV_PATH, override=False)
    try:
        config = read_config()
    except ConfigError as exc:
        logger = configure_logging()
        logger.error("設定エラー: %s", exc)
        print("\nconfig.json を修正してから再起動してください。", file=sys.stderr)
        return 2

    logger = configure_logging(config["log_level"])
    if "--check" in sys.argv:
        ffmpeg, ffprobe = resolve_ffmpeg_tools(config, logger)
        token_value = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        token_present = bool(token_value and token_value != "PASTE_YOUR_DISCORD_BOT_TOKEN_HERE")
        logger.info("設定ファイル: OK")
        logger.info("監視チャンネルID: %s", config["monitored_channel_ids"])
        logger.info("Discord Bot token: %s", "設定済み" if token_present else "未設定 (.env を編集してください)")
        logger.info("FFmpeg: %s / FFprobe: %s", "あり" if ffmpeg else "なし", "あり" if ffprobe else "なし")
        return 0 if token_present else 1

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token or token == "PASTE_YOUR_DISCORD_BOT_TOKEN_HERE":
        logger.error("Discord Botトークンが未設定です。.env の DISCORD_BOT_TOKEN を設定してください。")
        return 2

    lock = SingleInstanceLock(LOCK_PATH)
    if not lock.acquire():
        logger.error("Botはすでに起動しているようです。多重起動を防ぐため終了します。")
        return 3

    STOP_REQUEST_PATH.unlink(missing_ok=True)
    ffmpeg_path, ffprobe_path = resolve_ffmpeg_tools(config, logger)
    (BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
    (BASE_DIR / config["_temp_path"].relative_to(BASE_DIR)).mkdir(parents=True, exist_ok=True)
    dedup_store = DedupStore(BASE_DIR / "state.sqlite3")
    client = XVideoBot(
        config=config, logger=logger, ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path, dedup_store=dedup_store,
    )
    logger.info("Botを起動します。Ctrl+C または stop.bat で安全に停止できます。")
    exit_code = 0
    previous_sigint_handler = None
    try:
        # On Ctrl+C, request the same graceful queue-drain path used by stop.bat.
        previous_sigint_handler = signal.getsignal(signal.SIGINT)
        def request_graceful_stop(signum: int, frame: Any) -> None:
            try:
                STOP_REQUEST_PATH.write_text("stop\n", encoding="utf-8")
            except OSError:
                logger.warning("Ctrl+C停止要求ファイルを作成できませんでした。")
        signal.signal(signal.SIGINT, request_graceful_stop)
        client.run(token, log_handler=None)
    except KeyboardInterrupt:
        logger.info("Ctrl+Cを受け取りました。停止します。")
    except discord.LoginFailure:
        logger.error("Discordログインに失敗しました。Botトークンが正しいか確認してください。")
        exit_code = 4
    except Exception:
        logger.exception("Botが予期しない理由で終了しました。")
        exit_code = 5
    finally:
        if previous_sigint_handler is not None:
            try:
                signal.signal(signal.SIGINT, previous_sigint_handler)
            except (ValueError, OSError):
                pass
        try:
            dedup_store.close()
        except Exception:
            pass
        lock.release()
    logger.info("Botを終了しました。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
