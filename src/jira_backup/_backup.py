import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from importlib import import_module
from pathlib import Path
from typing import Dict, Any, Literal, Optional

import requests
import urllib3

from ._config import read_config, Config


_JIRA_EXPORT_LINK_SELECTOR = 'a[href*="/plugins/servlet/export/"]'
_JIRA_EXPORT_TIMEOUT_MS = 600_000
_QUICK_TIMEOUT_MS = 3_000


class OptionalExtraMissingError(RuntimeError):
    pass


def import_optional_extra(module_name: str, extra_name: str, purpose: str) -> Any:
    try:
        return import_module(module_name)
    except ImportError as e:
        raise OptionalExtraMissingError(
            f"{purpose} requires the optional '{extra_name}' extra. "
            f"Install it with: pip install 'jira_backup[{extra_name}]'"
        ) from e


def ensure_upload_extras(config: Config) -> None:
    if config.upload_to_s3 and config.upload_to_s3.s3_bucket:
        import_optional_extra("boto3", "s3", "S3 uploads")

    if config.upload_to_gcp and config.upload_to_gcp.gcs_bucket:
        import_optional_extra("google.cloud.storage", "gcp", "GCS uploads")

    if config.upload_to_azure and config.upload_to_azure.azure_container:
        import_optional_extra(
            "azure.storage.blob", "azure", "Azure Blob Storage uploads"
        )


def extract_backup_rate_limit_message(page_text: str) -> Optional[str]:
    rate_limit_keywords = [
        "sorry",
        "backup frequency is limited",
        "you can not make another backup",
        "you cannot make another backup",
        "approximate time till next allowed backup",
        "approximate time until next allowed backup",
    ]
    page_text_lower = page_text.lower()

    if not any(keyword in page_text_lower for keyword in rate_limit_keywords[1:]):
        return None

    message_lines = []
    for line in page_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        line_lower = stripped.lower()
        if any(keyword in line_lower for keyword in rate_limit_keywords):
            message_lines.append(stripped)

    return "\n".join(message_lines) if message_lines else "Backup frequency is limited."


class Atlassian:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.auth = (config.user_email, config.api_token)
        self.session.headers.update(
            {"Content-Type": "application/json", "Accept": "application/json"}
        )
        self.payload = {
            "cbAttachments": self.config.include_attachments,
            "exportToCloud": "true",
        }
        self.start_confluence_backup = "https://{}/wiki/rest/obm/1.0/runbackup".format(
            self.config.host_url
        )
        self.start_jira_backup = "https://{}/rest/backup/1/export/runbackup".format(
            self.config.host_url
        )
        self.get_last_jira_backup = "https://{}/rest/backup/1/export/lastTaskId".format(
            self.config.host_url
        )
        self.backup_status: Dict[str, Any] = {}
        self.wait = 10

    def generate_filename(self, backup_url: str, backup_type: str = "jira") -> str:
        """
        Generate filename based on config or default pattern.
        Supports placeholders:
        - {timestamp} - Current timestamp in format DDMMYYYY_HHMM
        - {date} - Current date in format YYYY-MM-DD
        - {time} - Current time in format HHMM
        - {uuid} - UUID from backup URL
        - {type} - Backup type (jira or confluence)
        """
        uuid = backup_url.split("/")[-1].replace("?fileId=", "")
        timestamp = time.strftime("%d%m%Y_%H%M")

        custom_pattern = self.config.custom_filename

        if custom_pattern is None:
            pattern = ""
        elif backup_type == "confluence":
            pattern = custom_pattern.confluence
        else:
            pattern = custom_pattern.jira

        if pattern:
            filename = pattern.format(
                timestamp=timestamp,
                date=time.strftime("%Y-%m-%d"),
                time=time.strftime("%H%M"),
                uuid=uuid,
                type=backup_type,
            )
            if not filename.endswith(".zip"):
                filename += ".zip"
            return filename
        else:
            return f"{timestamp}_{uuid}_{backup_type}.zip"

    def create_confluence_backup(self) -> str:
        backup = self.session.post(
            self.start_confluence_backup, data=json.dumps(self.payload)
        )

        if backup.status_code not in (200, 406):
            raise Exception(backup, backup.text)

        print("-> Backup process successfully started")
        confluence_backup_status = "https://{}/wiki/rest/obm/1.0/getprogress".format(
            self.config.host_url
        )
        time.sleep(self.wait)
        while "fileName" not in self.backup_status.keys():
            self.backup_status = json.loads(
                self.session.get(confluence_backup_status).text
            )
            print(
                "Current status: {progress}; {description}".format(
                    progress=self.backup_status["alternativePercentage"],
                    description=self.backup_status["currentStatus"],
                )
            )
            time.sleep(self.wait)
        return "https://{url}/wiki/download/{file_name}".format(
            url=self.config.host_url, file_name=self.backup_status["fileName"]
        )

    def create_jira_backup(self) -> str:
        sync_api = import_module("playwright.sync_api")

        with sync_api.sync_playwright() as playwright:
            browser, context = self._launch_jira_browser(playwright)
            page = context.new_page()
            try:
                backup_url = self._create_jira_backup_in_browser(page)
                self._save_playwright_storage_state(context)
                return backup_url
            finally:
                browser.close()

    def get_existing_jira_backup(self) -> Optional[str]:
        """Return the latest Jira backup download URL, if Jira exposes one."""
        try:
            backup = self.session.get(self.get_last_jira_backup)
            if backup.status_code != 200:
                return None

            task_id = backup.text.strip().strip('"')
            if not task_id:
                return None

            jira_backup_status = (
                "https://{jira_host}/rest/backup/1/export/getProgress?taskId={task_id}"
            ).format(jira_host=self.config.host_url, task_id=task_id)
            status_response = self.session.get(jira_backup_status)
            if status_response.status_code != 200:
                return None

            status = json.loads(status_response.text)
            result_id = status.get("result")
            if not result_id:
                return None
            result_id = str(result_id).lstrip("/")

            return "{prefix}/{result_id}".format(
                prefix="https://" + self.config.host_url + "/plugins/servlet",
                result_id=result_id,
            )
        except (KeyError, TypeError, ValueError, requests.RequestException):
            return None

    def _launch_jira_browser(self, playwright: Any) -> tuple[Any, Any]:
        storage_state_path = self._playwright_storage_state_path()
        storage_state_exists = storage_state_path.exists()
        headless = self.config.playwright.headless if storage_state_exists else False

        if not storage_state_exists:
            print(
                "-> No Playwright storage state found; launching a headed browser "
                "for one-time Atlassian login."
            )

        browser = playwright.chromium.launch(headless=headless)
        context_options: dict[str, str] = {}
        if storage_state_exists:
            context_options["storage_state"] = str(storage_state_path)
            print(f"-> Loaded Playwright storage state from {storage_state_path}")

        return browser, browser.new_context(**context_options)

    def _create_jira_backup_in_browser(self, page: Any) -> str:
        backup_page = f"https://{self.config.host_url}/secure/admin/CloudExport.jspa"
        print(f"-> Navigating to Jira Cloud Export page: {backup_page}")
        self._goto_jira_backup_page(page, backup_page)
        self._ensure_jira_browser_authenticated(page, backup_page)

        print("-> Waiting for Jira export page to render existing backup state")
        self._wait_for_page_timeout(page, 10_000)

        pre_click_href = self._read_first_href(page, _JIRA_EXPORT_LINK_SELECTOR)
        if pre_click_href:
            print(f"-> Existing backup link found on page: {pre_click_href}")

        try:
            self._check_backup_rate_limit(page, wait_ms=0)
        except RuntimeError:
            fallback_url = self._existing_jira_backup_url(pre_click_href)
            if fallback_url:
                print(f"-> Using existing Jira backup: {fallback_url}")
                return fallback_url
            raise

        self._set_jira_include_attachments(page)
        self._click_jira_backup_button(page)

        try:
            self._check_backup_rate_limit(page)
        except RuntimeError:
            fallback_url = self._existing_jira_backup_url(pre_click_href)
            if fallback_url:
                print(f"-> Using existing Jira backup: {fallback_url}")
                return fallback_url
            raise

        print("-> Backup process started, waiting for download link")
        link = page.locator(_JIRA_EXPORT_LINK_SELECTOR).first
        link.wait_for(state="visible", timeout=_JIRA_EXPORT_TIMEOUT_MS)
        href = link.get_attribute("href")
        if not href:
            raise RuntimeError("Jira backup link appeared without an href attribute.")

        href = self._absolute_jira_url(href)
        self._validate_jira_backup_url(href)
        print(f"-> Backup ready: {href}")
        return href

    def _goto_jira_backup_page(self, page: Any, backup_page: str) -> None:
        try:
            page.goto(
                backup_page,
                wait_until="load",
                timeout=self.config.playwright.login_timeout * 1_000,
            )
        except Exception as exc:
            if exc.__class__.__name__ == "TimeoutError":
                print("-> Warning: backup page timed out waiting for load; continuing")
                return
            raise

    def _ensure_jira_browser_authenticated(self, page: Any, backup_page: str) -> None:
        if not self._is_auth_redirect(page.url):
            return

        if (
            self.config.playwright.headless
            and self._playwright_storage_state_path().exists()
        ):
            raise RuntimeError(
                "Playwright storage state is expired, and Jira redirected to "
                "Atlassian login while running headless. Run once with "
                "playwright.headless: false, complete login/MFA in the browser, "
                "then rerun the backup."
            )

        if not sys.stdin.isatty():
            raise RuntimeError(
                "Atlassian login is required but no interactive terminal is "
                "available. Run once from a terminal to create Playwright storage "
                "state before using scheduled/headless backups."
            )

        print(
            "-> Atlassian login is required. Complete login/MFA in the browser, "
            "then return here."
        )
        while self._is_auth_redirect(page.url):
            input("-> Press Enter after the browser login has completed: ")
            self._goto_jira_backup_page(page, backup_page)

    def _set_jira_include_attachments(self, page: Any) -> None:
        try:
            checkbox = page.get_by_label("Include attachments", exact=False)
            if checkbox.is_visible(timeout=_QUICK_TIMEOUT_MS):
                checked = checkbox.is_checked()
                if checked != self.config.include_attachments:
                    checkbox.click()
        except Exception:
            return

    def _click_jira_backup_button(self, page: Any) -> None:
        for button_name in ("Backup", "Start backup", "Export", "Submit"):
            try:
                button = page.get_by_role("button", name=button_name)
                button.click(timeout=_QUICK_TIMEOUT_MS)
                return
            except Exception:
                continue

        page.locator('input[type="submit"], button[type="submit"]').first.click(
            timeout=15_000
        )

    def _check_backup_rate_limit(self, page: Any, wait_ms: int = 3_000) -> None:
        if wait_ms:
            self._wait_for_page_timeout(page, wait_ms)

        try:
            page_text = page.locator("body").inner_text(timeout=5_000)
        except Exception:
            return

        message = extract_backup_rate_limit_message(page_text)
        if message:
            print(f"-> Rate limit message from site:\n{message}")
            raise RuntimeError(message)

    def _existing_jira_backup_url(self, href: str) -> Optional[str]:
        if href:
            return self._absolute_jira_url(href)

        backup_url = self.get_existing_jira_backup()
        if backup_url:
            print(f"-> Found existing Jira backup via REST API: {backup_url}")
        return backup_url

    def _read_first_href(self, page: Any, selector: str) -> str:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=_QUICK_TIMEOUT_MS):
                return locator.get_attribute("href") or ""
        except Exception:
            return ""
        return ""

    def _absolute_jira_url(self, href: str) -> str:
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if not href.startswith("/"):
            href = "/" + href
        return f"https://{self.config.host_url}{href}"

    def _validate_jira_backup_url(self, href: str) -> None:
        if "/plugins/servlet/export/" not in href:
            raise RuntimeError(
                "Unexpected Jira backup URL detected "
                f"(possible page-layout mismatch): {href}"
            )

    def _save_playwright_storage_state(self, context: Any) -> None:
        storage_state_path = self._playwright_storage_state_path()
        try:
            storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(storage_state_path))
            print(f"-> Playwright storage state saved to {storage_state_path}")
        except Exception as exc:
            print(f"-> Warning: could not save Playwright storage state ({exc})")

    def _playwright_storage_state_path(self) -> Path:
        path = Path(self.config.playwright.storage_state).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path

    def _wait_for_page_timeout(self, page: Any, wait_ms: int) -> None:
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:
            time.sleep(wait_ms / 1_000)

    def _is_auth_redirect(self, url: str) -> bool:
        url_lower = url.lower()
        return any(
            indicator in url_lower
            for indicator in (
                "id.atlassian.com",
                "atlassian.com/login",
                "/login",
            )
        )

    def download_file(self, url: str, local_filename: str, max_retries: int = 5) -> str:
        print("-> Downloading file from URL: {}".format(url))
        file_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "backups", local_filename
        )

        # check if alredy downloaded partially
        downloaded_bytes = 0
        if os.path.exists(file_path):
            downloaded_bytes = os.path.getsize(file_path)
            print("-> Resuming download from byte {}".format(downloaded_bytes))

        for attempt in range(max_retries):
            try:
                headers = {}
                if downloaded_bytes > 0:
                    headers["Range"] = f"bytes={downloaded_bytes}-"

                r = self.session.get(url, stream=True, headers=headers, timeout=60)

                # get complete size
                if "content-range" in r.headers:
                    total_size = int(r.headers["content-range"].split("/")[-1])
                elif "content-length" in r.headers:
                    total_size = int(r.headers["content-length"]) + downloaded_bytes
                else:
                    total_size = 0

                mode = "ab" if downloaded_bytes > 0 else "wb"

                with open(file_path, mode) as file_:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                        if chunk:
                            file_.write(chunk)
                            downloaded_bytes += len(chunk)

                            # show progress
                            if total_size > 0:
                                percent = (downloaded_bytes / total_size) * 100
                                downloaded_gb = downloaded_bytes / (1024**3)
                                total_gb = total_size / (1024**3)
                                print(
                                    f"\r-> Progress: {percent:.1f}% ({downloaded_gb:.2f} GB / {total_gb:.2f} GB)",
                                    end="",
                                    flush=True,
                                )

                print("\n-> Download completed: {}".format(file_path))
                return file_path

            except (
                requests.exceptions.RequestException,
                urllib3.exceptions.ProtocolError,
            ) as e:
                print(f"\n-> Download interrupted: {e}")
                print(f"-> Retry {attempt + 1}/{max_retries} in 10 seconds...")
                time.sleep(10)

                # refresh downloaded_bytes for resume
                if os.path.exists(file_path):
                    downloaded_bytes = os.path.getsize(file_path)

        raise Exception(f"Download failed after {max_retries} retries")

    def stream_to_s3(self, url: str, remote_filename: str) -> None:
        print("-> Streaming to S3")
        boto3 = import_optional_extra("boto3", "s3", "S3 uploads")
        upload_config = self.config.upload_to_s3

        if upload_config is None:
            raise ValueError(
                "S3 upload was requested but upload_to_s3 is not configured"
            )

        if upload_config.aws_access_key == "":
            s3_client = boto3.client("s3")
        else:
            s3_client = boto3.client(
                "s3",
                aws_access_key_id=upload_config.aws_access_key,
                aws_secret_access_key=upload_config.aws_secret_key,
                region_name=upload_config.aws_region or None,
                endpoint_url=upload_config.aws_endpoint_url or None,
                use_ssl=upload_config.aws_is_secure,
            )

        bucket_name = upload_config.s3_bucket
        r = self.session.get(url, stream=True)
        if r.status_code == 200:
            key = "{s3_bucket}{s3_filename}".format(
                s3_bucket=upload_config.s3_dir,
                s3_filename=remote_filename,
            )

            s3_client.upload_fileobj(
                r.raw,
                Bucket=bucket_name,
                Key=key,
                ExtraArgs={"ContentType": r.headers["content-type"]},
            )

    def stream_to_gcs(self, url: str, remote_filename: str) -> None:
        print("-> Streaming to GCS")
        storage = import_optional_extra("google.cloud.storage", "gcp", "GCS uploads")
        upload_config = self.config.upload_to_gcp

        if upload_config is None:
            raise ValueError(
                "GCS upload was requested but upload_to_gcp is not configured"
            )

        if upload_config.gcp_service_account_key:
            client = storage.Client.from_service_account_json(
                upload_config.gcp_service_account_key,
                project=upload_config.gcp_project_id,
            )
        else:
            client = storage.Client(project=upload_config.gcp_project_id)

        bucket_name = upload_config.gcs_bucket
        bucket = client.bucket(bucket_name)

        r = self.session.get(url, stream=True)
        if r.status_code == 200:
            blob_name = "{gcs_dir}{filename}".format(
                gcs_dir=upload_config.gcs_dir,
                filename=remote_filename,
            )

            blob = bucket.blob(blob_name)
            blob.content_type = r.headers.get("content-type", "application/zip")

            blob.upload_from_file(r.raw, content_type=blob.content_type)

    def stream_to_azure(self, url: str, remote_filename: str) -> None:
        print("-> Streaming to Azure Blob Storage")
        blob_module = import_optional_extra(
            "azure.storage.blob", "azure", "Azure Blob Storage uploads"
        )
        blob_service_client_class = blob_module.BlobServiceClient
        upload_config = self.config.upload_to_azure

        if upload_config is None:
            raise ValueError(
                "Azure upload was requested but upload_to_azure is not configured"
            )

        if upload_config.azure_connection_string:
            blob_service_client = blob_service_client_class.from_connection_string(
                upload_config.azure_connection_string
            )
        else:
            account_url = (
                f"https://{upload_config.azure_account_name}.blob.core.windows.net"
            )
            blob_service_client = blob_service_client_class(
                account_url=account_url,
                credential=upload_config.azure_account_key,
            )

        container_name = upload_config.azure_container

        r = self.session.get(url, stream=True)
        if r.status_code == 200:
            blob_name = "{azure_dir}{filename}".format(
                azure_dir=upload_config.azure_dir,
                filename=remote_filename,
            )

            blob_client = blob_service_client.get_blob_client(
                container=container_name, blob=blob_name
            )

            blob_client.upload_blob(
                r.raw,
                content_type=r.headers.get("content-type", "application/zip"),
                overwrite=True,
            )


def setup_scheduled_task(
    *,
    frequency_days: int = 4,
    time_hour: int = 10,
    time_minute: int = 0,
    service_type: Literal["jira", "confluence"] = "jira",
    config_path: Path,
) -> bool:
    system = platform.system().lower()

    if system in ["linux", "darwin"]:
        return setup_cron_task(
            frequency_days=frequency_days,
            time_hour=time_hour,
            time_minute=time_minute,
            service_type=service_type,
            config_path=config_path,
        )
    elif system == "windows":
        return setup_windows_task(
            frequency_days=frequency_days,
            time_hour=time_hour,
            time_minute=time_minute,
            service_type=service_type,
            config_path=config_path,
        )
    else:
        raise Exception(f"Unsupported operating system: {system}")


def setup_cron_task(
    *,
    frequency_days: int,
    time_hour: int,
    time_minute: int,
    service_type: Literal["jira", "confluence"],
    config_path: Path,
) -> bool:
    service_flag = "-j" if service_type == "jira" else "-c"
    backup_command = shlex.join(
        [
            sys.executable,
            "-m",
            "jira_backup",
            service_flag,
            "-C",
            config_path.as_posix(),
        ]
    )
    cron_command = f"{time_minute} {time_hour} */{frequency_days} * * {backup_command}"

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing_cron = result.stdout if result.returncode == 0 else ""

        # Remove only the cron entry for the same service type
        lines = existing_cron.strip().split("\n") if existing_cron.strip() else []
        updated_lines = []
        skip_next = False

        for i, line in enumerate(lines):
            if skip_next:
                skip_next = False
                continue

            # Check if this is a comment line for jira-backup-py
            if (
                "jira-backup-py automated backup" in line
                and f"({service_type})" in line
            ):
                # Check if the next line contains the cron command for this service
                if i + 1 < len(lines) and service_flag in lines[i + 1]:
                    skip_next = True  # Skip both the comment and the command
                    print(f"-> Updating existing {service_type} backup schedule...")
                    continue

            updated_lines.append(line)

        existing_cron = "\n".join(updated_lines) + "\n" if updated_lines else ""
        new_cron = (
            existing_cron
            + f"# jira-backup-py automated backup ({service_type})\n{cron_command}\n"
        )

        process = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE, text=True)
        process.communicate(input=new_cron)

        if process.returncode == 0:
            print(
                f"-> Successfully scheduled {service_type} backup to run every {frequency_days} days at {time_hour:02d}:{time_minute:02d}"
            )
            return True
        else:
            print("-> Failed to create cron job")
            return False

    except Exception as e:
        print(f"-> Error setting up cron job: {e}")
        return False


def setup_windows_task(
    *,
    frequency_days: int,
    time_hour: int,
    time_minute: int,
    service_type: Literal["jira", "confluence"],
    config_path: Path,
) -> bool:
    task_name = f"jira-backup-py-{service_type}"
    service_flag = "-j" if service_type == "jira" else "-c"
    backup_command = subprocess.list2cmdline(
        [sys.executable, "-m", "jira_backup", service_flag, "-C", config_path]
    )
    cmd = [
        "schtasks",
        "/create",
        "/tn",
        task_name,
        "/sc",
        "DAILY",
        "/mo",
        str(frequency_days),
        "/tr",
        backup_command,
        "/st",
        f"{time_hour:02d}:{time_minute:02d}",
        "/f",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(
                f"-> Successfully scheduled {service_type} backup to run every {frequency_days} days at {time_hour:02d}:{time_minute:02d}"
            )
            return True
        else:
            print(f"-> Failed to create scheduled task: {result.stderr}")
            return False
    except Exception as e:
        print(f"-> Error setting up scheduled task: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-C",
        "--config",
        type=str,
        dest="config_file",
        default="config.yaml",
        help="path to config file",
    )
    parser.add_argument(
        "-w", action="store_true", dest="wizard", help="activate config wizard"
    )
    parser.add_argument(
        "-c", action="store_true", dest="confluence", help="activate confluence backup"
    )
    parser.add_argument(
        "-j", action="store_true", dest="jira", help="activate jira backup"
    )
    parser.add_argument(
        "-s",
        "--schedule",
        action="store_true",
        dest="schedule",
        help="setup automated scheduled backup",
    )
    parser.add_argument(
        "--schedule-days",
        type=int,
        default=4,
        help="frequency in days for scheduled backup (default: 4)",
    )
    parser.add_argument(
        "--schedule-time",
        type=str,
        default="10:00",
        help="time for scheduled backup in HH:MM format (default: 10:00)",
    )
    parser.add_argument(
        "--schedule-service",
        type=str,
        choices=["jira", "confluence"],
        default="jira",
        help="service type for scheduled backup (default: jira)",
    )
    args = parser.parse_args()
    config_path = Path(args.config_file)

    if args.wizard:
        from ._wizard import create_config

        create_config(config_path=config_path)

    if args.schedule:
        try:
            time_parts = args.schedule_time.split(":")
            hour = int(time_parts[0])
            minute = int(time_parts[1]) if len(time_parts) > 1 else 0

            if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                raise ValueError("Invalid time format")

            if not config_path.exists():
                print("-> Error: Can't schedule script without a config file.")
                exit(1)

            setup_scheduled_task(
                frequency_days=args.schedule_days,
                time_hour=hour,
                time_minute=minute,
                service_type=args.schedule_service,
                config_path=config_path.resolve(),
            )
            print("-> Scheduled task setup completed")
            exit(0)
        except ValueError as e:
            print(f"-> Error: Invalid time format. Use HH:MM format (e.g., 10:30)")
            exit(1)
        except Exception as e:
            print(f"-> Error setting up scheduled task: {e}")
            exit(1)

    try:
        config = read_config(config_path=config_path)
    except Exception as e:
        print(f"-> Error: {e}", file=sys.stderr)
        exit(1)

    if config.host_url == "something.atlassian.net":
        raise ValueError(
            'You forgot to edit config.yaml or to run the backup script with "-w" flag'
        )

    try:
        ensure_upload_extras(config)
    except OptionalExtraMissingError as e:
        print(f"-> Error: {e}", file=sys.stderr)
        exit(1)

    backup_type = "confluence" if args.confluence else "jira"
    print(
        "-> Starting {} backup; include attachments: {}".format(
            backup_type, config.include_attachments
        )
    )

    atlass = Atlassian(config)

    try:
        if args.confluence:
            backup_url = atlass.create_confluence_backup()
        else:
            backup_url = atlass.create_jira_backup()
    except OptionalExtraMissingError as e:
        print(f"-> Error: {e}", file=sys.stderr)
        exit(1)
    except (RuntimeError, TimeoutError) as e:
        print(f"-> Backup failed: {e}", file=sys.stderr)
        exit(1)

    print("-> Backup URL: {}".format(backup_url))
    file_name = atlass.generate_filename(backup_url, backup_type)
    print("-> Generated filename: {}".format(file_name))

    if config.download_locally:
        atlass.download_file(backup_url, file_name)

    if config.upload_to_s3 and config.upload_to_s3.s3_bucket:
        atlass.stream_to_s3(backup_url, file_name)

    if config.upload_to_gcp and config.upload_to_gcp.gcs_bucket:
        atlass.stream_to_gcs(backup_url, file_name)

    if config.upload_to_azure and config.upload_to_azure.azure_container:
        atlass.stream_to_azure(backup_url, file_name)
