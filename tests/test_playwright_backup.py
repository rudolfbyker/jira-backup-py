from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jira_backup._backup import Atlassian, extract_backup_rate_limit_message, main
from jira_backup._config import Config


def make_config(**overrides: object) -> Config:
    values = {
        "host_url": "refstudycentre.atlassian.net",
        "user_email": "backup@example.com",
        "api_token": "token",
        "include_attachments": False,
        "download_locally": False,
    }
    values.update(overrides)
    return Config.model_validate(values)


class Response:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class PlaywrightConfigTests(unittest.TestCase):
    def test_config_accepts_nested_playwright_fields(self) -> None:
        config = make_config(
            playwright={
                "headless": False,
                "login_timeout": 120,
                "storage_state": "state.json",
            },
        )

        self.assertFalse(config.playwright.headless)
        self.assertEqual(config.playwright.login_timeout, 120)
        self.assertEqual(config.playwright.storage_state, "state.json")

    def test_playwright_login_timeout_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            make_config(playwright={"login_timeout": 0})


class ExistingJiraBackupTests(unittest.TestCase):
    def test_get_existing_jira_backup_returns_download_url(self) -> None:
        atlas = Atlassian(make_config())
        atlas.session.get = Mock(
            side_effect=[
                Response(200, "task-123"),
                Response(200, json.dumps({"result": "export/download?fileId=abc"})),
            ]
        )

        self.assertEqual(
            atlas.get_existing_jira_backup(),
            "https://refstudycentre.atlassian.net/plugins/servlet/export/download?fileId=abc",
        )

    def test_get_existing_jira_backup_returns_none_without_result(self) -> None:
        atlas = Atlassian(make_config())
        atlas.session.get = Mock(
            side_effect=[
                Response(200, "task-123"),
                Response(200, json.dumps({"status": "RUNNING"})),
            ]
        )

        self.assertIsNone(atlas.get_existing_jira_backup())


class PlaywrightBackendTests(unittest.TestCase):
    def test_rate_limit_message_extraction(self) -> None:
        message = extract_backup_rate_limit_message(
            """
            Sorry
            Backup frequency is limited. You cannot make another backup right now.
            Approximate time until next allowed backup: 12 hours and 4 minutes
            """
        )

        self.assertIsNotNone(message)
        self.assertIn("Backup frequency is limited", message or "")

    def test_create_jira_backup_uses_playwright_to_trigger_browser_flow(self) -> None:
        atlas = Atlassian(make_config())
        playwright_context = MagicMock()
        playwright_context.__enter__.return_value = Mock()
        sync_api = Mock(sync_playwright=Mock(return_value=playwright_context))

        with patch("jira_backup._backup.import_module", return_value=sync_api):
            with patch.object(
                atlas, "_launch_jira_browser", return_value=(Mock(), Mock())
            ) as launch_browser:
                with patch.object(
                    atlas,
                    "_create_jira_backup_in_browser",
                    return_value=(
                        "https://refstudycentre.atlassian.net/plugins/servlet/export/download?fileId=abc"
                    ),
                ) as create_in_browser:
                    backup_url = atlas.create_jira_backup()

        self.assertEqual(
            backup_url,
            "https://refstudycentre.atlassian.net/plugins/servlet/export/download?fileId=abc",
        )
        launch_browser.assert_called_once()
        create_in_browser.assert_called_once()


class MainSelectionTests(unittest.TestCase):
    def test_jira_backup_uses_existing_atlassian_instance(self) -> None:
        config = make_config()

        with patch.object(sys, "argv", ["jira-backup", "-j"]):
            with patch("jira_backup._backup.read_config", return_value=config):
                with patch("jira_backup._backup.ensure_upload_extras"):
                    with patch("jira_backup._backup.Atlassian") as atlassian_class:
                        atlas = atlassian_class.return_value
                        atlas.create_jira_backup.return_value = (
                            "https://refstudycentre.atlassian.net/plugins/servlet/export/download?fileId=abc"
                        )
                        atlas.generate_filename.return_value = "backup.zip"

                        main()

        atlassian_class.assert_called_once_with(config)
        atlas.create_jira_backup.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
