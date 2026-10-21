"""Delivering one alert: an ntfy topic, a JSON webhook, or a program.

Three forms, because they cover everything else without btlog holding secrets:
a plain http(s) URL is an ntfy topic (the phone shows the message, so it gets
the text alone); `webhook:URL` gets the whole alert as JSON for Home Assistant
and friends; `command:PROGRAM ARGS` gets the JSON on stdin, which is how email
works (`command:/usr/bin/mail -s btlog me@example.org`) with no mail password
in btlog's config.

Every failure is an OSError with a one-line message, so the caller can log it
and try again next run without knowing which form failed.
"""

import base64
import http.client
import json
import shlex
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit

FORMS = "https://host/topic (ntfy), webhook:https://..., or command:PROGRAM ARGS"
_TAGS = {"open": "warning", "closed": "white_check_mark"}


def check(target):
    """Raise ValueError if target is not a form send() understands.

    Run by the config loader, so a typo shows up when btlog starts rather than
    when the freezer has already been silent for six hours.
    """
    _parse(target)


def send(target, alert, timeout=10):
    """Deliver one alert to target; OSError on failure, ValueError for an unknown form."""
    kind, where = _parse(target)
    if kind == "command":
        return _run(where, alert, timeout)
    if kind == "webhook":
        body, headers = json.dumps(alert, ensure_ascii=False), {"Content-Type": "application/json"}
    else:
        # ntfy's publish API: the body is the message, the rest goes in headers.
        rule = alert.get("rule")
        body, headers = alert["message"], {"Title": f"btlog {rule}" if rule else "btlog"}
        if alert.get("state") in _TAGS:
            headers["Tags"] = _TAGS[alert["state"]]
    headers = {k: _header(v) for k, v in headers.items()}
    req = urllib.request.Request(where, data=body.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except urllib.error.HTTPError as e:
        # The body says why (ntfy answers {"error": "unauthorized", ...}); close
        # it, since an HTTPError holds the connection open until it is collected.
        with e:
            detail = _first_line(e.read(300).decode(errors="replace"))
        raise OSError(f"{where}: HTTP {e.code} {e.reason}" + (f": {detail}" if detail else "")) from None
    except urllib.error.URLError as e:
        raise OSError(f"{where}: {_first_line(str(e.reason))}") from None
    except http.client.HTTPException as e:
        # A garbled reply (BadStatusLine and friends) is not an OSError by itself.
        raise OSError(f"{where}: {type(e).__name__}: {_first_line(str(e))}") from None


def _parse(target):
    """('command', argv) or ('webhook' | 'ntfy', url); ValueError otherwise."""
    if target.startswith("command:"):
        # POSIX splitting even on Windows: quotes work the same everywhere, and
        # btlog runs on Linux, where a backslash is not a path separator.
        try:
            argv = shlex.split(target.removeprefix("command:"))
        except ValueError as e:
            raise ValueError(f"notify target {target!r}: {e}") from None
        if argv:
            return "command", argv
    else:
        kind = "webhook" if target.startswith("webhook:") else "ntfy"
        url = target.removeprefix("webhook:")
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.hostname:
            return kind, url
    raise ValueError(f"notify target {target!r} not understood: use {FORMS}")


def _run(argv, alert, timeout):
    """Run the program with the alert on stdin; a non-zero exit is a failure."""
    try:
        subprocess.run(argv, input=json.dumps(alert, ensure_ascii=False).encode(),
                       capture_output=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as e:
        detail = _first_line(e.stderr.decode(errors="replace"))
        raise OSError(f"{argv[0]}: exit status {e.returncode}" + (f": {detail}" if detail else "")) from None
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"{argv[0]}: no exit after {timeout:g} s, killed") from None


def _header(value):
    """A header value that cannot inject another header, and that http.client can encode.

    Alert fields may carry device names from strangers' advertisements, so all
    whitespace runs (CR/LF included) become one space. Non-ASCII goes as RFC 2047,
    which ntfy decodes; http.client would otherwise fail on it as Latin-1.
    """
    value = " ".join(str(value).split())
    if value.isascii():
        return value
    return f"=?UTF-8?B?{base64.b64encode(value.encode()).decode()}?="


def _first_line(text):
    lines = text.strip().splitlines()
    return lines[0].strip()[:200] if lines else ""

