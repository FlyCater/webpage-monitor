#!/usr/bin/env python3
"""Monitor Comic World announcement pages without disclosing configuration in logs.

State file holds only HMAC identifiers, NOT convention numbers or webhook URLs.
"""
import hashlib
import hmac
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STATE_PATH = Path('.state/notified.txt')
PENDING_MARKERS = ('공지 준비중입니다', '현재 공지 내용을 준비중에 있습니다')


class ContentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_heading = 0
        self.heading_parts = []
        self.headings = []
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.skip += 1
        if tag in ('h1', 'h2', 'h3'):
            self.in_heading += 1
            self.heading_parts = []

    def handle_endtag(self, tag):
        if tag in ('h1', 'h2', 'h3'):
            if self.in_heading:
                self.headings.append(' '.join(self.heading_parts))
                self.in_heading -= 1
        if tag in ('script', 'style', 'noscript') and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            stripped = ' '.join(data.split())
            if stripped:
                self.text_parts.append(stripped)
                if self.in_heading:
                    self.heading_parts.append(stripped)


def classify(html, number):
    doc = ContentParser()
    doc.feed(html)
    whole = ' '.join(doc.text_parts)
    if any(marker in whole for marker in PENDING_MARKERS):
        return 'pending'
    # Page-specific convention title: do not mistake global navigation for a published announcement.
    target = re.compile(r'(?<!\d)' + re.escape(number) + r'(?!\d)')
    if any('코믹월드' in heading and target.search(heading) for heading in doc.headings):
        return 'open'
    return 'unknown'


def request_page(number):
    url = f'https://comicw.net/e/{number}'
    request = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; ComicWorldStatusMonitor/1.0)',
        'Accept': 'text/html,application/xhtml+xml',
    })
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError('Unexpected HTTP response')
        content_type = response.headers.get('Content-Type', '')
        if 'html' not in content_type.lower():
            raise RuntimeError('Unexpected response content type')
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise RuntimeError('Response too large')
        charset = response.headers.get_content_charset() or 'utf-8'
        return body.decode(charset, errors='replace')


def load_numbers():
    raw = os.environ.get('CONVENTIONS', '')
    values = [entry.strip() for entry in raw.split(',') if entry.strip()]
    if not values or len(values) > 30 or any(not re.fullmatch(r'[1-9][0-9]{0,5}', v) for v in values):
        raise ValueError('CONVENTIONS secret must contain 1–30 comma-separated positive convention numbers')
    return list(dict.fromkeys(values))


def state_token(number, key):
    return hmac.new(key.encode(), ('comicworld-announced:v1:' + number).encode(), hashlib.sha256).hexdigest()


def send_discord(webhook, number):
    payload = {
        'username': 'Comic World Monitor',
        'allowed_mentions': {'parse': []},
        'embeds': [{
            'title': f'Comic World #{number} announcement is open',
            'url': f'https://comicw.net/e/{number}',
            'description': 'The announcement page now has a convention-specific heading. Please confirm details on the site.',
            'color': 0x36b37e,
        }],
    }
    data = json.dumps(payload).encode()
    req = Request(webhook, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    # Discord may rate limit; retry a few times without printing secret-containing errors.
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as response:
                if response.status not in (200, 204):
                    raise RuntimeError('Discord notification unsuccessful')
            return
        except HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                # Honor short delays without trusting arbitrary-long external values.
                delay = max(1, min(15, int(float(exc.headers.get('Retry-After', '2')))))
                time.sleep(delay)
                continue
            raise RuntimeError(f'Discord HTTP {exc.code}') from None
        except (URLError, TimeoutError):
            if attempt < 2:
                time.sleep(2)
                continue
            raise RuntimeError('Discord notification unsuccessful') from None


def main():
    # Avoid printing numbers, URL, HTML, or configuration to public action logs.
    key = os.environ.get('STATE_HMAC_KEY', '')
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '')
    if len(key) < 32 or not webhook.startswith('https://discord.com/api/webhooks/'):
        raise ValueError('Required repository secrets are missing or invalid')
    numbers = load_numbers()
    notified = set(STATE_PATH.read_text().splitlines()) if STATE_PATH.exists() else set()
    changed = False
    errors = False
    for index, number in enumerate(numbers, 1):
        token = state_token(number, key)
        if token in notified:
            continue
        try:
            classification = classify(request_page(number), number)
        except HTTPError as exc:
            print(f'Page check {index}: website HTTP {exc.code}.', file=sys.stderr)
            errors = True
            continue
        except (URLError, TimeoutError):
            print(f'Page check {index}: website network error or timeout.', file=sys.stderr)
            errors = True
            continue
        except (UnicodeError, ValueError, RuntimeError, OSError):
            print(f'Page check {index}: website response could not be processed.', file=sys.stderr)
            errors = True
            continue
        if classification == 'open':
            try:
                send_discord(webhook, number)
            except RuntimeError as exc:
    print(f'Page check {index}: {exc}', file=sys.stderr)
    errors = True
    continue
except (HTTPError, URLError, TimeoutError, UnicodeError, ValueError, OSError):
    print(f'Page check {index}: Discord network or delivery error.', file=sys.stderr)
    errors = True
    continue
                errors = True
                continue
            notified.add(token)
            changed = True
            print('A newly published page was detected and a private Discord alert was sent.')
        elif classification == 'pending':
            print('A monitored page is still pending.')
        else:
            print('Unable to confirm a published page; no notification sent.')
    if changed:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text('\n'.join(sorted(notified)) + '\n')
    if errors:
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError):
        print('Configuration or state error; check repository secrets.', file=sys.stderr)
        sys.exit(1)
