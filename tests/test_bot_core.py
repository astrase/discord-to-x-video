import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from bot_core import (
    DedupStore, classify_media_paths, extract_x_urls, file_size_bytes,
    extract_media_urls, format_post_metadata, mb_to_bytes, normalize_cookie_browser,
    normalize_cookie_browsers, normalize_x_url, normalize_pixiv_url, normalize_media_url,
    normalize_display_settings, resolve_display_settings, should_ignore_bot_message,
    message_text_for_url_detection, looks_like_auth_error,
    browser_cookie_lock_guidance,
)


class UrlValidationTests(unittest.TestCase):
    def test_standard_x_and_twitter_urls(self):
        self.assertEqual(
            normalize_x_url("https://x.com/example/status/123456"),
            ("https://x.com/example/status/123456", "123456"),
        )
        self.assertEqual(
            normalize_x_url("https://twitter.com/example/status/123456?s=20"),
            ("https://x.com/example/status/123456", "123456"),
        )

    def test_fixup_and_fxembed_aliases_normalize_to_x(self):
        supported = [
            "https://fixupx.com/example/status/123456",
            "https://xfixup.com/example/status/123456",
            "https://fxtwitter.com/example/status/123456",
            "https://twittpr.com/example/status/123456",
        ]
        for url in supported:
            with self.subTest(url=url):
                self.assertEqual(
                    normalize_x_url(url),
                    ("https://x.com/example/status/123456", "123456"),
                )

    def test_direct_space_link_is_supported_and_has_distinct_dedup_key(self):
        self.assertEqual(
            normalize_x_url("https://x.com/i/spaces/1jMKgLQqDLAxL"),
            ("https://x.com/i/spaces/1jMKgLQqDLAxL", "space_1jMKgLQqDLAxL"),
        )
        self.assertEqual(
            extract_x_urls("音声 https://twitter.com/i/spaces/1jMKgLQqDLAxL?s=20"),
            [("https://x.com/i/spaces/1jMKgLQqDLAxL", "space_1jMKgLQqDLAxL")],
        )

    def test_fixup_link_is_extracted_from_text_and_deduplicated(self):
        self.assertEqual(
            extract_x_urls(
                "見て https://fixupx.com/alice/status/123456 "
                "https://x.com/alice/status/123456"
            ),
            [("https://x.com/alice/status/123456", "123456")],
        )

    def test_text_and_punctuation(self):
        text = "見てください！ https://x.com/alice/status/123456。 https://twitter.com/bob/status/789012)"
        self.assertEqual(
            extract_x_urls(text),
            [
                ("https://x.com/alice/status/123456", "123456"),
                ("https://x.com/bob/status/789012", "789012"),
            ],
        )

    def test_duplicate_same_status_url_is_removed(self):
        text = "https://x.com/alice/status/123456 https://twitter.com/bob/status/123456?s=10"
        self.assertEqual(len(extract_x_urls(text)), 1)

    def test_rejects_other_hosts_and_deceptive_hosts(self):
        self.assertIsNone(normalize_x_url("https://example.com/alice/status/123456"))
        self.assertIsNone(normalize_x_url("https://x.com.example.org/alice/status/123456"))
        self.assertIsNone(normalize_x_url("https://x.com@evil.example/alice/status/123456"))
        self.assertIsNone(normalize_x_url("https://fixupx.com.evil.example/alice/status/123456"))

    def test_rejects_http_and_invalid_paths(self):
        self.assertIsNone(normalize_x_url("http://x.com/alice/status/123456"))
        self.assertIsNone(normalize_x_url("https://x.com/alice/status/not-a-number"))
        self.assertIsNone(normalize_x_url("https://x.com/alice/123456"))
        self.assertIsNone(normalize_x_url("https://x.com/alice/status/123456/anything/with-invalid-id"))

    def test_rejects_credentials_and_ports(self):
        self.assertIsNone(normalize_x_url("https://alice:secret@x.com/alice/status/123456"))
        self.assertIsNone(normalize_x_url("https://x.com:443/alice/status/123456"))


class PixivUrlTests(unittest.TestCase):
    def test_artwork_url_and_locale_prefix(self):
        expected = ("https://www.pixiv.net/artworks/12345678", "pixiv_artwork_12345678")
        self.assertEqual(normalize_pixiv_url("https://www.pixiv.net/artworks/12345678"), expected)
        self.assertEqual(normalize_pixiv_url("https://www.pixiv.net/en/artworks/12345678?utm_source=discord"), expected)

    def test_legacy_illustration_url(self):
        self.assertEqual(
            normalize_pixiv_url("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=987654"),
            ("https://www.pixiv.net/artworks/987654", "pixiv_artwork_987654"),
        )

    def test_novel_links_are_normalized_as_distinct_media_keys(self):
        self.assertEqual(
            normalize_pixiv_url("https://www.pixiv.net/novel/show.php?id=7654321"),
            ("https://www.pixiv.net/novel/show.php?id=7654321", "pixiv_novel_7654321"),
        )
        self.assertEqual(
            normalize_pixiv_url("https://www.pixiv.net/novel/7654321"),
            ("https://www.pixiv.net/novel/show.php?id=7654321", "pixiv_novel_7654321"),
        )

    def test_pixiv_urls_reject_deceptive_and_bulk_urls(self):
        self.assertIsNone(normalize_pixiv_url("https://pixiv.net.example.com/artworks/123"))
        self.assertIsNone(normalize_pixiv_url("https://www.pixiv.net:443/artworks/123"))
        self.assertIsNone(normalize_pixiv_url("http://www.pixiv.net/artworks/123"))
        self.assertIsNone(normalize_pixiv_url("https://www.pixiv.net/users/123/artworks"))
        self.assertIsNone(normalize_pixiv_url("https://www.pixiv.net/artworks/not-a-number"))

    def test_extraction_mixes_x_and_pixiv_and_deduplicates(self):
        self.assertEqual(
            extract_media_urls(
                "X https://x.com/a/status/12345 Pixiv https://www.pixiv.net/artworks/12345678 "
                "same https://pixiv.net/en/artworks/12345678"
            ),
            [
                ("https://x.com/a/status/12345", "12345"),
                ("https://www.pixiv.net/artworks/12345678", "pixiv_artwork_12345678"),
            ],
        )


class WebhookEmbedExtractionTests(unittest.TestCase):
    def test_extracts_supported_links_from_webhook_content_and_embeds(self):
        message = SimpleNamespace(
            content="Webhook message with https://x.com/alice/status/12345",
            embeds=[
                SimpleNamespace(
                    url="https://www.pixiv.net/artworks/987654",
                    title="Pixiv artwork",
                    description="See https://x.com/bob/status/789012",
                    author=SimpleNamespace(url=None, name="Source"),
                    footer=SimpleNamespace(text="extra https://pixiv.net/artworks/543210"),
                    image=SimpleNamespace(url="https://cdn.example.com/image.jpg"),
                    thumbnail=None,
                    video=None,
                    provider=None,
                    fields=[SimpleNamespace(name="link", value="https://www.pixiv.net/novel/show.php?id=7654321")],
                )
            ],
        )
        found = extract_media_urls(message_text_for_url_detection(message))
        self.assertEqual(
            found,
            [
                ("https://x.com/alice/status/12345", "12345"),
                ("https://www.pixiv.net/artworks/987654", "pixiv_artwork_987654"),
                ("https://x.com/bob/status/789012", "789012"),
                ("https://www.pixiv.net/artworks/543210", "pixiv_artwork_543210"),
                ("https://www.pixiv.net/novel/show.php?id=7654321", "pixiv_novel_7654321"),
            ],
        )


class WebhookMessagePolicyTests(unittest.TestCase):
    def test_webhook_messages_are_allowed_even_if_author_is_bot(self):
        self.assertFalse(should_ignore_bot_message(author_is_bot=True, is_webhook_message=True))

    def test_ordinary_bot_messages_and_own_messages_are_ignored(self):
        self.assertTrue(should_ignore_bot_message(author_is_bot=True, is_webhook_message=False))
        self.assertTrue(should_ignore_bot_message(author_is_bot=False, is_webhook_message=True, is_own_message=True))
        self.assertFalse(should_ignore_bot_message(author_is_bot=False, is_webhook_message=False))


class AuthFallbackTests(unittest.TestCase):
    def test_no_video_found_message_can_trigger_browser_fallback(self):
        self.assertTrue(looks_like_auth_error(RuntimeError("No video could be found in this tweet")))

    def test_known_auth_errors_trigger_browser_fallback(self):
        for message in ("Sign in to confirm your age", "age-restricted", "failed to decrypt"):
            with self.subTest(message=message):
                self.assertTrue(looks_like_auth_error(RuntimeError(message)))

    def test_unrelated_errors_do_not_trigger_browser_fallback(self):
        self.assertFalse(looks_like_auth_error(RuntimeError("HTTP Error 404: Not Found")))


class BrowserCookieLockGuidanceTests(unittest.TestCase):
    def test_chromium_cookie_copy_error_returns_actionable_guidance(self):
        guidance = browser_cookie_lock_guidance(
            "ERROR: Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271"
        )
        self.assertIn("Brave/Chrome", guidance)
        self.assertIn("brave.exe", guidance)

    def test_unrelated_error_does_not_return_cookie_lock_guidance(self):
        self.assertEqual(browser_cookie_lock_guidance("HTTP Error 404: Not Found"), "")


class CookieBrowserConfigTests(unittest.TestCase):
    def test_blank_cookie_browser_disables_browser_cookies(self):
        self.assertEqual(normalize_cookie_browser(""), "")
        self.assertEqual(normalize_cookie_browser(None), "")

    def test_supported_browser_is_normalized(self):
        self.assertEqual(normalize_cookie_browser(" Edge "), "edge")
        self.assertEqual(normalize_cookie_browser("CHROME"), "chrome")

    def test_arbitrary_cookie_browser_values_are_rejected(self):
        with self.assertRaises(ValueError):
            normalize_cookie_browser("edge & whoami")

    def test_multiple_cookie_browsers_keep_order_and_remove_duplicates(self):
        self.assertEqual(normalize_cookie_browsers([" Firefox ", "BRAVE", "firefox"]), ["firefox", "brave"])

    def test_cookie_browser_list_can_be_disabled(self):
        self.assertEqual(normalize_cookie_browsers([]), [])
        self.assertEqual(normalize_cookie_browsers(""), [])

    def test_invalid_cookie_browser_in_list_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_cookie_browsers(["firefox", "edge & whoami"])



class DedupStoreTests(unittest.TestCase):
    def test_claims_once_until_failed_or_success(self):
        with tempfile.TemporaryDirectory() as root:
            store = DedupStore(Path(root) / "state.sqlite3")
            self.assertTrue(store.claim("123456", "10"))
            self.assertFalse(store.claim("123456", "11"))  # active job duplicate
            store.set_status("123456", "failed", "temporary error")
            self.assertTrue(store.claim("123456", "11"))  # failed may retry
            store.set_status("123456", "success")
            self.assertFalse(store.claim("123456", "12"))  # success deduplicated
            self.assertTrue(store.claim("123456", "12", deduplicate_success=False))
            store.close()

    def test_incomplete_previous_run_can_be_retried(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.sqlite3"
            store = DedupStore(path)
            self.assertTrue(store.claim("654321", "1"))
            store.close()
            store = DedupStore(path)
            self.assertEqual(store.status_for("654321"), "failed")
            self.assertTrue(store.claim("654321", "2"))
            store.close()


class FileSizeTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(mb_to_bytes(1), 1024 * 1024)
        with tempfile.TemporaryDirectory() as root:
            sample = Path(root) / "sample.bin"
            sample.write_bytes(b"abc")
            self.assertEqual(file_size_bytes(sample), 3)


class MetadataFormattingTests(unittest.TestCase):
    def test_formats_author_post_text_and_original_link_without_profile_or_date(self):
        text = format_post_metadata(
            {
                "uploader": "Example User",
                "uploader_id": "example_user",
                "description": "新しい動画です！\n見てください。",
            },
            "https://x.com/example_user/status/12345",
        )
        self.assertIn("Example User", text)
        self.assertIn("@example_user", text)
        self.assertIn("新しい動画です！", text)
        self.assertNotIn("プロフィール", text)
        self.assertNotIn("投稿日", text)
        self.assertNotIn("2026-10-22", text)
        self.assertIn("https://x.com/example_user/status/12345", text)
        self.assertLessEqual(len(text), 1850)

    def test_uses_title_when_description_is_missing(self):
        text = format_post_metadata(
            {"title": "投稿タイトル", "uploader": "Example"},
            "https://x.com/example/status/12345",
        )
        self.assertIn("投稿タイトル", text)
        self.assertIn("投稿者:", text)

    def test_long_post_text_is_truncated_to_message_limit(self):
        text = format_post_metadata(
            {"description": "あ" * 10000, "uploader": "A"},
            "https://x.com/example/status/12345",
            max_chars=800,
        )
        self.assertLessEqual(len(text), 800)
        self.assertIn("…", text)
        self.assertIn("ソース", text)


class PerChannelDisplaySettingsTests(unittest.TestCase):
    def test_default_display_preserves_existing_fields(self):
        settings = resolve_display_settings({}, 123)
        self.assertTrue(settings["show_author_name"])
        self.assertTrue(settings["show_account_name"])
        self.assertTrue(settings["show_post_text"])
        self.assertTrue(settings["show_original_link"])
        self.assertEqual(settings["heading"], "X投稿情報")

    def test_channel_overrides_are_isolated_and_inherit_defaults(self):
        raw = {
            "default": {"show_author_name": True, "show_post_text": True},
            "channels": {
                "123": {"show_author_name": False, "show_original_link": False},
                "456": {"heading": "動画保管"},
            },
        }
        first = resolve_display_settings(raw, 123)
        second = resolve_display_settings(raw, 456)
        self.assertFalse(first["show_author_name"])
        self.assertFalse(first["show_original_link"])
        self.assertTrue(first["show_account_name"])
        self.assertTrue(second["show_author_name"])
        self.assertEqual(second["heading"], "動画保管")
        self.assertEqual(resolve_display_settings(raw, 789)["heading"], "X投稿情報")

    def test_named_groups_can_be_shared_by_multiple_channels(self):
        raw = {
            "default": {
                "show_author_name": True,
                "show_account_name": True,
                "show_post_text": True,
                "show_original_link": True,
                "heading": "共通",
            },
            "groups": {
                "archive": {"heading": "動画アーカイブ", "show_post_text": True},
                "links_only": {
                    "show_author_name": False,
                    "show_account_name": False,
                    "show_post_text": False,
                    "show_original_link": True,
                    "heading": "リンク一覧",
                },
            },
            "channels": {
                "123": "archive",
                "456": "archive",
                "789": "links_only",
            },
        }
        first = resolve_display_settings(raw, 123)
        second = resolve_display_settings(raw, 456)
        links = resolve_display_settings(raw, 789)
        self.assertEqual(first, second)
        self.assertEqual(first["heading"], "動画アーカイブ")
        self.assertTrue(first["show_post_text"])
        self.assertEqual(links["heading"], "リンク一覧")
        self.assertFalse(links["show_author_name"])
        self.assertFalse(links["show_post_text"])
        self.assertTrue(links["show_original_link"])

    def test_channel_can_reference_group_and_override_one_setting(self):
        raw = {
            "default": {"heading": "既定", "show_post_text": True},
            "groups": {"links": {"heading": "リンク", "show_post_text": False}},
            "channels": {"123": {"group": "links", "show_post_text": True}},
        }
        resolved = resolve_display_settings(raw, 123)
        self.assertEqual(resolved["heading"], "リンク")
        self.assertTrue(resolved["show_post_text"])
        self.assertTrue(resolved["show_original_link"])

    def test_unknown_group_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_display_settings({"channels": {"123": "missing_group"}})
        with self.assertRaises(ValueError):
            normalize_display_settings({"groups": {"known": {}}, "channels": {"123": {"group": "missing"}}})

    def test_formatter_hides_selected_fields_only_for_that_profile(self):
        metadata = {
            "uploader": "Example User",
            "uploader_id": "example_user",
            "description": "投稿本文です",
        }
        all_fields = format_post_metadata(metadata, "https://x.com/example/status/123", settings={})
        compact = format_post_metadata(
            metadata,
            "https://x.com/example/status/123",
            settings={
                "show_author_name": False,
                "show_account_name": False,
                "show_post_text": False,
                "show_original_link": True,
                "heading": "リンク保管",
            },
        )
        self.assertIn("Example User", all_fields)
        self.assertIn("投稿本文です", all_fields)
        self.assertIn("@example_user", all_fields)
        self.assertNotIn("Example User", compact)
        self.assertNotIn("投稿本文です", compact)
        self.assertNotIn("@example_user", compact)
        self.assertIn("リンク保管", compact)
        self.assertIn("ソース", compact)

    def test_empty_heading_and_all_hidden_fields_produce_empty_content(self):
        result = format_post_metadata(
            {"uploader": "A", "uploader_id": "a", "description": "text"},
            "https://x.com/a/status/123",
            settings={
                "show_author_name": False,
                "show_account_name": False,
                "show_post_text": False,
                "show_original_link": False,
                "heading": "",
            },
        )
        self.assertEqual(result, "")

    def test_bad_settings_fail_validation(self):
        with self.assertRaises(ValueError):
            normalize_display_settings({"channels": {"not-an-id": {"show_post_text": False}}})
        with self.assertRaises(ValueError):
            normalize_display_settings({"default": {"show_post_text": "false"}})
        with self.assertRaises(ValueError):
            normalize_display_settings({"default": {"unexpected": True}})


class MediaTypeTests(unittest.TestCase):
    def test_classifies_common_video_audio_image_files(self):
        self.assertEqual(classify_media_paths([Path("tweet.mp4")]), "video")
        self.assertEqual(classify_media_paths([Path("space.m4a")]), "audio")
        self.assertEqual(classify_media_paths([Path("image_1.jpg"), Path("image_2.png")]), "image")

    def test_video_takes_precedence_over_intermediate_audio(self):
        self.assertEqual(classify_media_paths([Path("tweet.mp4"), Path("tweet.f140.m4a")]), "video")

    def test_rejects_unknown_file_types(self):
        with self.assertRaises(ValueError):
            classify_media_paths([Path("unknown.json")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
