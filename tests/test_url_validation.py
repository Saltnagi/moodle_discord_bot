import re
import pytest
import moodle
from config import MOODLE_ALLOWED_HOSTS

_URL_RE = re.compile(r"https?://[^\s'\")>]+")
_REQ_URL_RE = re.compile(r"(url: )\S+")
_TOKEN_RE = re.compile(r"(authtoken=)[^&\s'\")]+", re.IGNORECASE)

def redact(text: str) -> str:
    text = _URL_RE.sub("[URL]", text)
    text = _REQ_URL_RE.sub(r"\1[URL]", text)
    return _TOKEN_RE.sub(r"\1[REDACTED]", text)

def test_check_url_valid():
    host = next(iter(MOODLE_ALLOWED_HOSTS))
    valid_url = f"https://{host}/calendar/export_execute.php?userid=123&authtoken=abc"
    assert moodle.check_url(valid_url) is None

def test_check_url_not_https():
    host = next(iter(MOODLE_ALLOWED_HOSTS))
    http_url = f"http://{host}/calendar/export_execute.php?userid=123&authtoken=abc"
    err = moodle.check_url(http_url)
    assert err is not None
    assert "https" in err

def test_check_url_disallowed_domain():
    url = "https://evil.example.com/calendar/export_execute.php?userid=123&authtoken=abc"
    err = moodle.check_url(url)
    assert err is not None

def test_check_url_invalid_format():
    err = moodle.check_url("not_a_url")
    assert err is not None

def test_redact_url():
    url = "https://moodle.example.ac.jp/export.php?authtoken=secrettoken12345&user=10"
    redacted = redact(url)
    assert "secrettoken12345" not in redacted
    assert "[URL]" in redacted
