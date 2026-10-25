
#!/usr/bin/env python3

import hashlib
import hmac
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STATE_PATH = Path(".state/notified.txt")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

PENDING_MARKERS = (
    "공지 준비중입니다",
    "현재 공지 내용을 준비중에 있습니다",
)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.heading_tag = None
        self.heading_parts = []
        self.headings = []
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
            return

        if self.skip:
            return

        if tag in ("h1", "h2", "h3"):
            self.heading_tag = tag
            self.heading_parts = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
            return

        if self.skip:
            return

        if tag == self.heading_tag:
            heading = " ".join(self.heading_parts).strip()
            if heading:
                self.headings.append(heading)

            self.heading_tag = None
            self.heading_parts = []

    def handle_data(self, data):
        if self.skip:
            return

        value = " ".join(data.split())
        if not value:
            return

        self.text_parts.append(value)

        if self.heading_tag:
            self.heading_parts.append(value)


def classify(html, number):
    parser = PageParser()
    parser.feed(html)

    page_text = " ".join(parser.text_parts)

    # Verify the convention number in a published heading.
    number_pattern = re.compile(
        r"(?<!\d)" + re.escape(number) + r"(?!\d)"
    )

    for heading in parser.headings:
        if (
            "코믹월드" in heading
            and number_pattern.search(heading)
        ):
            return "open"

    # Only check the pending message if no valid
    # announcement heading was found.
    if any(
        marker in page_text
        for marker in PENDING_MARKERS
    ):
        return "pending"

    # Do not send a notification for unknown pages.
    return "unknown"


def fetch_page(number):
    url = f"https://comicw.net/e/{number}"

    request = Request(
        url,
        headers={
            **HEADERS,
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    with urlopen(request, timeout=20) as response:
        content_type = response.headers.get(
            "Content-Type", ""
        )

        if "html" not in content_type.lower():
            raise ValueError("Unexpected content type")

        body = response.read(2_000_001)

        if len(body) > 2_000_000:
            raise ValueError("Page too large")

        charset = (
            response.headers.get_content_charset()
            or "utf-8"
        )

        return body.decode(charset, errors="replace")


def get_conventions():
    raw = os.environ.get("CONVENTIONS", "")

    numbers = [
        value.strip()
        for value in raw.split(",")
        if value.strip()
    ]

    if not numbers or len(numbers) > 30:
        raise ValueError("Invalid CONVENTIONS")

    for number in numbers:
        if not re.fullmatch(r"[1-9]\d{0,5}", number):
            raise ValueError("Invalid convention number")

    return list(dict.fromkeys(numbers))


def make_token(number, key):
    message = f"comicworld-announced:v1:{number}"

    return hmac.new(
        key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def send_discord(webhook, number):
    payload = {
        "username": "Comic World Monitor",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": (
                    f"Comic World #{number} "
                    "announcement is OPEN!"
                ),
                "url": f"https://comicw.net/e/{number}",
                "description": (
                    "The convention announcement "
                    "has been published.\n\n"
                    "Open the page to view details."
                ),
                "color": 3584894,
            }
        ],
    }

    data = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    request = Request(
        webhook,
        data=data,
        headers={
            **HEADERS,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                if response.status in (200, 204):
                    return

                raise RuntimeError(
                    f"Discord HTTP {response.status}"
                )

        except HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                retry = exc.headers.get("Retry-After", "2")

                try:
                    delay = float(retry)
                except ValueError:
                    delay = 2.0

                time.sleep(max(1, min(delay, 15)))
                continue

            raise RuntimeError(
                f"Discord HTTP {exc.code}"
            ) from None

        except (URLError, TimeoutError):
            if attempt < 2:
                time.sleep(2)
                continue

            raise RuntimeError(
                "Discord network error"
            ) from None


def main():
    key = os.environ.get("STATE_HMAC_KEY", "")
    webhook = os.environ.get(
        "DISCORD_WEBHOOK_URL", ""
    ).strip()

    if len(key) < 32:
        raise ValueError("Invalid HMAC key")

    if not webhook.startswith(
        (
            "https://discord.com/api/webhooks/",
            "https://discordapp.com/api/webhooks/",
        )
    ):
        raise ValueError("Invalid Discord webhook")

    conventions = get_conventions()

    if STATE_PATH.exists():
        notified = set(
            STATE_PATH.read_text().splitlines()
        )
    else:
        notified = set()

    changed = False
    errors = False

    for index, number in enumerate(conventions, 1):
        token = make_token(number, key)

        if token in notified:
            print(
                f"Page check {index}: "
                "Already notified."
            )
            continue

        try:
            html = fetch_page(number)
            status = classify(html, number)

        except HTTPError as exc:
            print(
                f"Page check {index}: "
                f"Website HTTP {exc.code}",
                file=sys.stderr,
            )
            errors = True
            continue

        except (URLError, TimeoutError):
            print(
                f"Page check {index}: "
                "Website connection error.",
                file=sys.stderr,
            )
            errors = True
            continue

        except Exception:
            print(
                f"Page check {index}: "
                "Website processing error.",
                file=sys.stderr,
            )
            errors = True
            continue

        if status == "pending":
            print(
                f"Page check {index}: "
                "Announcement pending."
            )
            continue

        if status == "unknown":
            print(
                f"Page check {index}: "
                "Publication not confirmed."
            )
            continue

        print(
            f"Page check {index}: "
            "Published announcement detected."
        )

        try:
            send_discord(webhook, number)

        except RuntimeError as exc:
            print(
                f"Page check {index}: {exc}",
                file=sys.stderr,
            )
            errors = True
            continue

        notified.add(token)
        changed = True

        print(
            f"Page check {index}: "
            "Discord notification sent."
        )

    if changed:
        STATE_PATH.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        STATE_PATH.write_text(
            "\n".join(sorted(notified)) + "\n"
        )

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError):
        print(
            "Configuration or state error.",
            file=sys.stderr,
        )
        sys.exit(1)
