"""Turning what a user hands the web page into `Source` objects.

Three kinds of input reach the agent: text pasted with a URL or a label, an uploaded file,
and a bare URL that is fetched live. All three end as plain text, because claim extraction
and quote verification both work on the text a reader would see, not on markup.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlparse

from ..sources import Source

MAX_FETCH_BYTES = 3_000_000
FETCH_TIMEOUT = 15.0
USER_AGENT = "deep-research-agent/0.1 (+https://github.com/hammasbuilds/deep-research-agent)"

_SKIP = {"script", "style", "noscript", "template", "svg", "nav", "footer", "header", "sup"}
_BLOCK = {
    "p",
    "div",
    "br",
    "li",
    "tr",
    "td",
    "th",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
    "blockquote",
    "dd",
    "dt",
    "figcaption",
    "caption",
    "title",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP:
            self.skipping += 1
        elif tag == "title":
            self._in_title = True
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self.skipping:
            self.skipping -= 1
        elif tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if not self.skipping:
            self.parts.append(data)


def looks_like_html(text: str) -> bool:
    head = text[:2000].lower()
    return "<html" in head or "<!doctype html" in head or ("<p>" in head and "</p>" in head)


def html_to_text(html: str) -> tuple[str, str]:
    """Visible text and the page title. One line per block element, blank lines collapsed."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n[ \n]*", "\n", text)
    return text.strip(), " ".join(parser.title.split())


class FetchError(RuntimeError):
    pass


def is_web_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def fetch_page(url: str, *, timeout: float = FETCH_TIMEOUT) -> Source:
    """Download one page and keep its readable text. Raises FetchError with a short reason."""
    if not is_web_url(url):
        raise FetchError("only http:// and https:// addresses can be fetched")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            kind = response.headers.get_content_type()
            if kind not in ("text/html", "text/plain", "application/xhtml+xml"):
                raise FetchError(f"not a text page ({kind})")
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(MAX_FETCH_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise FetchError(f"could not connect: {reason}") from exc
    body = raw[:MAX_FETCH_BYTES].decode(charset, errors="replace")
    if kind == "text/plain":
        return Source(url=url, text=body.strip())
    text, title = html_to_text(body)
    return Source(url=url, title=title, text=text)


def label_to_url(label: str, index: int) -> str:
    """A web address stays as it is; anything else becomes `pasted://<label>`.

    The agent works on URLs (domain authority, citations), so a source given only a name
    still needs one. The `pasted://` scheme keeps it from being mistaken for a real site.
    """
    label = (label or "").strip()
    if is_web_url(label):
        return label
    if label.startswith("pasted://"):
        return label
    slugged = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-").lower()
    return f"pasted://{slugged or f'source-{index + 1}'}"
