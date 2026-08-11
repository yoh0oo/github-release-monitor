from __future__ import annotations

import json
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import release_monitor  # noqa: E402


def make_release(
    release_id: int,
    tag: str,
    published_at: str,
    *,
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, object]:
    return {
        "id": release_id,
        "tag_name": tag,
        "name": tag,
        "published_at": published_at,
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.com/example/project/releases/tag/{tag}",
        "body": "Changes",
    }


class ConfigTests(unittest.TestCase):
    def test_loads_string_and_object_repositories(self) -> None:
        raw = {
            "notify_on_first_run": True,
            "repositories": [
                "owner/one",
                {
                    "repo": "owner/two",
                    "name": "Second",
                    "include_prereleases": True,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            config = release_monitor.load_config(path)

        self.assertTrue(config.notify_on_first_run)
        self.assertEqual(config.repositories[0].name, "owner/one")
        self.assertTrue(config.repositories[1].include_prereleases)

    def test_rejects_duplicate_repositories_case_insensitively(self) -> None:
        raw = {"repositories": ["Owner/Repo", "owner/repo"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(release_monitor.MonitorError, "重复"):
                release_monitor.load_config(path)


class GitHubClientTests(unittest.TestCase):
    @patch("release_monitor.request_json")
    def test_selects_latest_stable_non_draft_release(self, request_json_mock) -> None:
        request_json_mock.return_value = [
            make_release(4, "v4", "2026-04-01T00:00:00Z", draft=True),
            make_release(3, "v3-beta", "2026-03-01T00:00:00Z", prerelease=True),
            make_release(1, "v1", "2026-01-01T00:00:00Z"),
            make_release(2, "v2", "2026-02-01T00:00:00Z"),
        ]
        repository = release_monitor.RepositoryConfig("owner/repo", "Repo")

        result = release_monitor.GitHubClient(token="token").latest_release(repository)

        self.assertIsNotNone(result)
        self.assertEqual(result["tag_name"], "v2")
        headers = request_json_mock.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer token")

    @patch("release_monitor.request_json")
    def test_can_include_prereleases(self, request_json_mock) -> None:
        request_json_mock.return_value = [
            make_release(2, "v2-beta", "2026-02-01T00:00:00Z", prerelease=True),
            make_release(1, "v1", "2026-01-01T00:00:00Z"),
        ]
        repository = release_monitor.RepositoryConfig(
            "owner/repo", "Repo", include_prereleases=True
        )

        result = release_monitor.GitHubClient().latest_release(repository)

        self.assertIsNotNone(result)
        self.assertEqual(result["tag_name"], "v2-beta")


class ReleaseComparisonTests(unittest.TestCase):
    def test_newer_release_uses_published_time(self) -> None:
        previous = {
            "release_id": 10,
            "tag_name": "v1",
            "published_at": "2026-01-01T00:00:00Z",
        }
        newer = make_release(11, "v2", "2026-02-01T00:00:00Z")
        older = make_release(9, "v0.9", "2025-12-01T00:00:00Z")

        self.assertTrue(release_monitor.is_newer_release(newer, previous))
        self.assertFalse(release_monitor.is_newer_release(older, previous))

    def test_same_release_is_not_newer(self) -> None:
        previous = {
            "release_id": 10,
            "tag_name": "v1",
            "published_at": "2026-01-01T00:00:00Z",
        }
        current = make_release(10, "renamed-v1", "2026-02-01T00:00:00Z")

        self.assertTrue(release_monitor.same_release(current, previous))
        self.assertFalse(release_monitor.is_newer_release(current, previous))


class FakeGitHubClient:
    def __init__(self, release: dict[str, object]):
        self.release = release

    def latest_release(self, repository):
        return self.release


class MonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = release_monitor.RepositoryConfig("owner/repo", "Repo")
        self.config = release_monitor.MonitorConfig([self.repository])
        self.release = make_release(20, "v2", "2026-02-01T00:00:00Z")

    @patch("release_monitor.send_feishu_notification")
    def test_first_run_records_baseline_without_notification(self, send_mock) -> None:
        state = {"version": 1, "repositories": {}}

        changed, errors = release_monitor.monitor(
            self.config,
            state,
            github_client=FakeGitHubClient(self.release),
            webhook_url=None,
            webhook_secret=None,
        )

        self.assertTrue(changed)
        self.assertEqual(errors, [])
        self.assertEqual(state["repositories"]["owner/repo"]["tag_name"], "v2")
        send_mock.assert_not_called()

    @patch("release_monitor.send_feishu_notification")
    def test_new_release_sends_notification_and_advances_state(self, send_mock) -> None:
        state = {
            "version": 1,
            "repositories": {
                "owner/repo": {
                    "release_id": 10,
                    "tag_name": "v1",
                    "published_at": "2026-01-01T00:00:00Z",
                }
            },
        }

        changed, errors = release_monitor.monitor(
            self.config,
            state,
            github_client=FakeGitHubClient(self.release),
            webhook_url="https://example.invalid/webhook",
            webhook_secret=None,
        )

        self.assertTrue(changed)
        self.assertEqual(errors, [])
        self.assertEqual(state["repositories"]["owner/repo"]["tag_name"], "v2")
        send_mock.assert_called_once()

    @patch("release_monitor.send_feishu_notification")
    def test_failed_notification_does_not_advance_state(self, send_mock) -> None:
        send_mock.side_effect = release_monitor.MonitorError("send failed")
        state = {
            "version": 1,
            "repositories": {
                "owner/repo": {
                    "release_id": 10,
                    "tag_name": "v1",
                    "published_at": "2026-01-01T00:00:00Z",
                }
            },
        }

        changed, errors = release_monitor.monitor(
            self.config,
            state,
            github_client=FakeGitHubClient(self.release),
            webhook_url="https://example.invalid/webhook",
            webhook_secret=None,
        )

        self.assertFalse(changed)
        self.assertEqual(len(errors), 1)
        self.assertEqual(state["repositories"]["owner/repo"]["tag_name"], "v1")

    @patch("release_monitor.send_feishu_notification")
    def test_dry_run_neither_notifies_nor_changes_state(self, send_mock) -> None:
        state = {"version": 1, "repositories": {}}

        changed, errors = release_monitor.monitor(
            self.config,
            state,
            github_client=FakeGitHubClient(self.release),
            webhook_url=None,
            webhook_secret=None,
            force_notify=True,
            dry_run=True,
        )

        self.assertFalse(changed)
        self.assertEqual(errors, [])
        self.assertEqual(state["repositories"], {})
        send_mock.assert_not_called()


class FeishuCardTests(unittest.TestCase):
    def test_card_contains_release_details(self) -> None:
        repository = release_monitor.RepositoryConfig("owner/repo", "My Repo")
        release = make_release(1, "v1", "2026-01-01T00:00:00Z")

        card = release_monitor.build_feishu_card(repository, release)

        self.assertEqual(card["msg_type"], "interactive")
        self.assertIn("My Repo", card["card"]["header"]["title"]["content"])
        self.assertEqual(
            card["card"]["elements"][-1]["actions"][0]["url"], release["html_url"]
        )

    def test_release_notes_cannot_inject_feishu_mentions(self) -> None:
        notes = "Hello <at id=all></at> and <AT id=user></AT>"

        result = release_monitor.truncate_release_notes(notes)

        self.assertNotIn("<at", result.lower())
        self.assertIn("&lt;at", result.lower())

    @patch("release_monitor.request_json")
    def test_feishu_response_must_have_success_status(self, request_json_mock) -> None:
        request_json_mock.return_value = {}
        repository = release_monitor.RepositoryConfig("owner/repo", "My Repo")
        release = make_release(1, "v1", "2026-01-01T00:00:00Z")

        with self.assertRaisesRegex(release_monitor.MonitorError, "缺少状态码"):
            release_monitor.send_feishu_notification(
                "https://example.invalid/secret-webhook", repository, release
            )


class HttpRequestTests(unittest.TestCase):
    @patch("release_monitor.urlopen")
    def test_error_target_redacts_sensitive_url(self, urlopen_mock) -> None:
        sensitive_url = "https://example.invalid/webhook/secret-token"
        urlopen_mock.side_effect = HTTPError(
            sensitive_url,
            400,
            "Bad Request",
            {},
            BytesIO(b'{"code": 1}'),
        )

        with self.assertRaises(release_monitor.MonitorError) as context:
            release_monitor.request_json(
                sensitive_url,
                attempts=1,
                error_target="redacted webhook",
            )

        self.assertIn("redacted webhook", str(context.exception))
        self.assertNotIn("secret-token", str(context.exception))


if __name__ == "__main__":
    unittest.main()
