
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

PENDING_MARKERS = (
    "공지 준비중입니다",
    "현재 공지 내용을 준비중에 있습니다",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class ContentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.heading_level = 0
        self.heading_parts = []
        self.headings = []
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1

        if tag in ("h1", "h2", "h3"):
            self.heading_level += 1
            self.heading_parts = []

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3"):
            if self.heading_level:
                self.headings.append(
                    " ".join(self.heading_parts)
                )
                self.heading_level -= 1

        if tag in ("script", "style", "noscript"):
            if self.skip:
                self.skip -= 1

    def handle_data(self, data):
        if self.skip:
            return

        text = " ".join(data.split())

        if text:
            self.text_parts.append(text)

            if self.heading_level:
                self.heading_parts.append(text)


def classify(html, number):
    doc = ContentParser()
    doc.feed(html)

    text = " ".join(doc.text_parts)

    if any(marker in text for marker in PENDING_MARKERS):
        return "pending"

    target = re.compile(
        r"(?<!\d)" + re.escape(number) + r"(?!\d)"
    )

    for heading in doc.headings:
        if "코믹월드" in heading and target.search(heading):
            return "open"

    return "unknown"


def request_page(number):
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
            raise RuntimeError("Unexpected content type")

        body = response.read(2_000_001)

        if len(body) > 2_000_000:
            raise RuntimeError("Response too large")

        charset = (
            response.headers.get_content_charset()
            or "utf-8"
        )

        return body.decode(charset, errors="replace")


def load_numbers():
    raw = os.environ.get("CONVENTIONS", "")

    numbers = [
        number.strip()
        for number in raw.split(",")
        if number.strip()
    ]

    if not numbers or len(numbers) > 30:
        raise ValueError("Invalid convention configuration")

    for number in numbers:
        if not re.fullmatch(r"[1-9][0-9]{0,5}", number):
            raise ValueError("Invalid convention number")

    return list(dict.fromkeys(numbers))


def state_token(number, key):
    message = f"comicworld-announced:v1:{number}"

    return hmac.new(
        key.encode(),
        message.encode(),
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
                    "A convention announcement "
                    "appears to be published.\n\n"
                    "Click the title to view the page."
                ),
                "color": 0x36B37E,
            }
        ],
    }

    data = json.dumps(payload).encode("utf-8")

    request = Request(
        webhook,
        data=data,
        headers={
            **HEADERS,
            "Content-Type": "application/json",
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
                retry = exc.headers.get(
                    "Retry-After", "2"
                )

                try:
                    delay = float(retry)
                except ValueError:
                    delay = 2

                time.sleep(max(1, min(15, delay)))
                continue

            raise RuntimeError(
                f"Discord HTTP {exc.code}"
            ) from None

        except (URLError, TimeoutError):
            if attempt < 2:
                time.sleep(2)
                continue

            raise RuntimeError(
                "Discord network error or timeout"
            ) from None


def main():
    key = os.environ.get("STATE_HMAC_KEY", "")
    webhook = os.environ.get(
        "DISCORD_WEBHOOK_URL", ""
    ).strip()

    if len(key) < 32:
        raise ValueError("Missing STATE_HMAC_KEY")

    if not webhook.startswith(
        (
            "https://discord.com/api/webhooks/",
            "https://discordapp.com/api/webhooks/",
        )
    ):
        raise ValueError("Invalid Discord webhook")

    numbers = load_numbers()

    if STATE_PATH.exists():
        notified = set(
            STATE_PATH.read_text().splitlines()
        )
    else:
        notified = set()

    changed = False
    errors = False

    for index, number in enumerate(numbers, 1):
        token = state_token(number, key)

        if token in notified:
            print(
                f"Page check {index}: "
                "Previously notified."
            )
            continue

        try:
            html = request_page(number)
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
                "Website connection failed.",
                file=sys.stderr,
            )
            errors = True
            continue

        except Exception:
            print(
                f"Page check {index}: "
                "Website response error.",
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

        if status == "open":
            try:
                send_discord(webhook, number)

            except RuntimeError as exc:
                print(
                    f"Page check {index}: {exc}",
                    file=sys.stderr,
                )
                errors = True
                continue

            print(
                f"Page check {index}: "
                "Discord notification sent."
            )

            notified.add(token)
            changed = True

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
