from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import random
import re
import smtplib
import subprocess
import time
from urllib.parse import urlparse
from email.message import EmailMessage
from pathlib import Path

import requests


ENV_FILE = Path(".env")
STATE_FILE = Path("last_state.txt")
DEFAULT_CHECK_INTERVAL_SECONDS = 300
DEFAULT_MAX_RANDOM_DELAY_SECONDS = 300
DEFAULT_QUIET_START_HOUR = 0
DEFAULT_QUIET_END_HOUR = 6
LEGACY_STATE_PREFIX = "visible-text-v1:"
STATE_PREFIX = "visible-text-v2:"
MAX_CHANGE_LINES = 40


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def normalize_page(content: str) -> str:
    content = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", content)
    content = re.sub(r"(?s)<!--.*?-->", " ", content)
    content = re.sub(r"(?s)<[^>]+>", "\n", content)
    content = html.unescape(content)
    lines = [re.sub(r"\s+", " ", line).strip() for line in content.splitlines()]
    return "\n".join(line for line in lines if line)


def hash_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def hash_page(content: str) -> str:
    return hash_text(normalize_page(content))


def read_state() -> str:
    if not STATE_FILE.exists():
        return ""
    return STATE_FILE.read_text(encoding="utf-8").strip()


def parse_state(state: str) -> dict[str, str]:
    if state.startswith(STATE_PREFIX):
        return json.loads(state.removeprefix(STATE_PREFIX))

    if state.startswith(LEGACY_STATE_PREFIX):
        return {"hash": state.removeprefix(LEGACY_STATE_PREFIX), "text": ""}

    return {"hash": state, "text": ""}


def write_state(text: str) -> None:
    state = {"hash": hash_text(text), "text": text}
    STATE_FILE.write_text(
        f"{STATE_PREFIX}{json.dumps(state, ensure_ascii=False)}\n",
        encoding="utf-8",
    )


def validate_hour(value: int, name: str) -> int:
    if not 0 <= value <= 23:
        raise SystemExit(f"{name} must be between 0 and 23.")
    return value


def validate_non_negative(value: int, name: str) -> int:
    if value < 0:
        raise SystemExit(f"{name} must be 0 or greater.")
    return value


def in_quiet_hours(hour: int, start_hour: int, end_hour: int) -> bool:
    if start_hour == end_hour:
        return False

    if start_hour < end_hour:
        return start_hour <= hour < end_hour

    return hour >= start_hour or hour < end_hour


def skip_for_quiet_hours(start_hour: int, end_hour: int) -> bool:
    current_hour = time.localtime().tm_hour
    if not in_quiet_hours(current_hour, start_hour, end_hour):
        return False

    print(
        f"Skipping check during quiet hours "
        f"({start_hour:02d}:00-{end_hour:02d}:00)."
    )
    return True


def sleep_random_delay(max_delay_seconds: int) -> None:
    if max_delay_seconds <= 0:
        return

    delay = random.randint(0, max_delay_seconds)
    if delay:
        print(f"Waiting {delay} seconds before checking.")
        time.sleep(delay)


def fetch_page(url: str) -> str:
    scrapingbee_api_key = os.environ.get("SCRAPINGBEE_API_KEY")
    if scrapingbee_api_key:
        response = requests.get(
            "https://app.scrapingbee.com/api/v1/",
            params={
                "api_key": scrapingbee_api_key,
                "url": url,
                "render_js": os.environ.get("SCRAPINGBEE_RENDER_JS", "false"),
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.text

    parsed_url = urlparse(url)
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    response = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": os.environ.get(
                "USER_AGENT",
                (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9,sv;q=0.8",
            "Cache-Control": "no-cache",
            "Referer": origin,
        },
    )
    if response.status_code == 403:
        raise RuntimeError(
            "The website returned 403 Forbidden. It may be blocking GitHub "
            "Actions runners; try again after this browser-like header change, "
            "or run the watcher from another host if it still fails."
        )
    response.raise_for_status()
    return response.text


def applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def describe_changes(old_text: str, new_text: str) -> str:
    diff_lines = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile="before",
        tofile="after",
        lineterm="",
    )
    changed_lines = [
        line
        for line in diff_lines
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]

    if not changed_lines:
        return "The visible text changed, but no concise line-level diff was found."

    limited_lines = changed_lines[:MAX_CHANGE_LINES]
    summary = "\n".join(limited_lines)

    if len(changed_lines) > MAX_CHANGE_LINES:
        summary += f"\n... and {len(changed_lines) - MAX_CHANGE_LINES} more changes"

    return summary


def send_email(url: str, change_summary: str) -> None:
    smtp_host = required_env("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")
    smtp_username = required_env("SMTP_USERNAME")
    smtp_password = required_env("SMTP_PASSWORD")
    email_from = os.environ.get("EMAIL_FROM") or smtp_username
    email_to = required_env("EMAIL_TO")

    message = EmailMessage()
    message["Subject"] = "Website changed"
    message["From"] = email_from
    message["To"] = email_to
    message.set_content(
        f"The monitored website changed:\n\n{url}\n\nWhat changed:\n\n"
        f"{change_summary}"
    )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


def send_notification(url: str, change_summary: str) -> None:
    message = f"Website changed: {url}"
    email_to = os.environ.get("EMAIL_TO")
    discord_webhook = os.environ.get("DISCORD_WEBHOOK")

    if email_to:
        send_email(url, change_summary)
        return

    if discord_webhook:
        notify_response = requests.post(
            discord_webhook,
            json={"content": f"{message}\n\nWhat changed:\n{change_summary}"},
            timeout=15,
        )
        notify_response.raise_for_status()
        return

    subprocess.run(
        [
            "osascript",
            "-e",
            f"display notification {applescript_string(message)} "
            'with title "Website Watcher"',
        ],
        check=True,
    )


def check_once(url: str) -> None:
    old_state = read_state()
    new_text = normalize_page(fetch_page(url))
    new_hash = hash_text(new_text)

    if not old_state:
        write_state(new_text)
        print("Saved initial website state.")
        return

    parsed_state = parse_state(old_state)

    if not old_state.startswith(STATE_PREFIX) or not parsed_state["text"]:
        write_state(new_text)
        print("Migrated saved website state.")
        return

    if new_hash != parsed_state["hash"]:
        change_summary = describe_changes(parsed_state["text"], new_text)
        send_notification(url, change_summary)
        write_state(new_text)
        print("Website changed. Notification sent.")
        return

    print("No change detected.")


def run_forever(
    url: str,
    interval: int,
    quiet_start_hour: int,
    quiet_end_hour: int,
    max_random_delay_seconds: int,
) -> None:
    while True:
        try:
            if not skip_for_quiet_hours(quiet_start_hour, quiet_end_hour):
                sleep_random_delay(max_random_delay_seconds)
                check_once(url)
        except Exception as error:
            print(f"Check failed: {error}")

        time.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch a website for visible text changes.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one check and exit. Use this for GitHub Actions.",
    )
    parser.add_argument(
        "--quiet-start-hour",
        type=int,
        default=int(os.environ.get("QUIET_START_HOUR", DEFAULT_QUIET_START_HOUR)),
        help="Hour to start skipping checks, in 24-hour time. Default: 0 (12 AM).",
    )
    parser.add_argument(
        "--quiet-end-hour",
        type=int,
        default=int(os.environ.get("QUIET_END_HOUR", DEFAULT_QUIET_END_HOUR)),
        help="Hour to resume checks, in 24-hour time. Default: 6 (6 AM).",
    )
    parser.add_argument(
        "--max-random-delay-seconds",
        type=int,
        default=int(
            os.environ.get(
                "MAX_RANDOM_DELAY_SECONDS",
                DEFAULT_MAX_RANDOM_DELAY_SECONDS,
            )
        ),
        help="Maximum random delay before each check. Default: 300 seconds.",
    )
    return parser.parse_args()


def main() -> None:
    load_env()
    args = parse_args()
    url = required_env("URL")
    interval = int(os.environ.get("CHECK_INTERVAL_SECONDS", DEFAULT_CHECK_INTERVAL_SECONDS))
    quiet_start_hour = validate_hour(args.quiet_start_hour, "--quiet-start-hour")
    quiet_end_hour = validate_hour(args.quiet_end_hour, "--quiet-end-hour")
    max_random_delay_seconds = validate_non_negative(
        args.max_random_delay_seconds,
        "--max-random-delay-seconds",
    )

    if args.once:
        if skip_for_quiet_hours(quiet_start_hour, quiet_end_hour):
            return
        sleep_random_delay(max_random_delay_seconds)
        check_once(url)
        return

    run_forever(
        url,
        interval,
        quiet_start_hour,
        quiet_end_hour,
        max_random_delay_seconds,
    )


if __name__ == "__main__":
    main()