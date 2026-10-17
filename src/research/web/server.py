"""A small web page over the research agent: ask a question, give it sources, read the report.

Standard library only (`http.server`), like the rest of the package. The agent is the real
one: the page supplies `search` (ranking the sources the user gave) and `fetch` (pasted
text, an uploaded file, or the live page at a URL), and everything after that is
`ResearchAgent.run`.

    python -m research.web --port 8845
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..agent import Budget, ResearchAgent, relevance
from ..sources import Source, deduplicate, domain_authority
from .pages import FetchError, fetch_page, html_to_text, is_web_url, label_to_url, looks_like_html

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).parent / "samples.json"

MAX_SOURCES = 30
MAX_TEXT_CHARS = 400_000
MAX_QUESTION_CHARS = 500
MAX_BODY_BYTES = 16 * MAX_TEXT_CHARS


class BadRequest(ValueError):
    pass


def _number(settings: dict, key: str, default: float, low: float, high: float) -> float:
    value = settings.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{key} must be a number") from exc
    if not low <= value <= high:
        raise BadRequest(f"{key} must be between {low:g} and {high:g}")
    return value


def parse_request(payload: dict) -> tuple[str, list[dict], dict]:
    if not isinstance(payload, dict):
        raise BadRequest("the request body must be a JSON object")
    question = str(payload.get("question") or "").strip()
    if not question:
        raise BadRequest("type a question first")
    if len(question) > MAX_QUESTION_CHARS:
        raise BadRequest(f"the question is longer than {MAX_QUESTION_CHARS} characters")

    raw_sources = payload.get("sources") or []
    if not isinstance(raw_sources, list):
        raise BadRequest("sources must be a list")
    sources: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            raise BadRequest("each source must be an object with a url and text")
        label = str(item.get("url") or "").strip()
        text = str(item.get("text") or "")
        if not label and not text.strip():
            continue  # an empty card
        if len(text) > MAX_TEXT_CHARS:
            raise BadRequest(f"source {index + 1} is longer than {MAX_TEXT_CHARS:,} characters")
        if not text.strip() and not is_web_url(label):
            raise BadRequest(
                f"source {index + 1} has no text and {label!r} is not a web address to fetch"
            )
        url = label_to_url(label, index)
        if url in seen:
            raise BadRequest(f"{url} is listed twice")
        seen.add(url)
        title = ""
        if text.strip() and looks_like_html(text):
            text, title = html_to_text(text)
        sources.append({"url": url, "text": text.strip(), "title": title})
    if not sources:
        raise BadRequest("add at least one source: paste text, upload a file, or give a URL")
    if len(sources) > MAX_SOURCES:
        raise BadRequest(f"at most {MAX_SOURCES} sources at a time")

    settings = payload.get("settings") or {}
    if not isinstance(settings, dict):
        raise BadRequest("settings must be an object")
    options = {
        "min_authority": _number(settings, "min_authority", 0.0, 0.0, 1.0),
        "numeric_tolerance": _number(settings, "numeric_tolerance", 0.10, 0.0, 1.0),
        "max_fetches": int(_number(settings, "max_fetches", 20, 1, MAX_SOURCES)),
    }
    return question, sources, options


def run_research(question: str, sources: list[dict], options: dict, fetcher=fetch_page) -> dict:
    """Run the real agent over the given sources and describe every step for the page."""
    by_url = {s["url"]: s for s in sources}
    status = {
        s["url"]: {
            "url": s["url"],
            "given": "text" if s["text"] else "address",
            "status": "not searched",
            "chars": len(s["text"]),
            "title": s["title"],
            "error": "",
        }
        for s in sources
    }
    fetched: list[Source] = []

    def search(query: str, limit: int) -> list[str]:
        """Rank the user's sources against one query, like a search engine over them.

        A source given only as an address has no text until it is fetched, so it is
        always returned; one with text is returned only when it shares a word with the
        query.
        """
        ranked = []
        for order, s in enumerate(sources):
            if s["text"]:
                score = relevance(query, s["text"][:20_000] + " " + s["title"])
                if score <= 0:
                    if status[s["url"]]["status"] == "not searched":
                        status[s["url"]]["status"] = "no query matched it"
                    continue
            else:
                score = 0.5
            ranked.append((-score, order, s["url"]))
        return [url for _, _, url in sorted(ranked)][:limit]

    def fetch(url: str) -> Source:
        given = by_url[url]
        entry = status[url]
        if given["text"]:
            source = Source(url=url, title=given["title"], text=given["text"])
            entry["status"] = "read"
        else:
            started = time.monotonic()
            try:
                source = fetcher(url)
            except FetchError as exc:
                entry["status"] = "fetch failed"
                entry["error"] = str(exc)
                raise
            entry["status"] = "fetched"
            entry["seconds"] = round(time.monotonic() - started, 2)
            entry["title"] = source.title
            if not source.text.strip():
                entry["status"] = "fetched, no text"
        entry["chars"] = len(source.text)
        fetched.append(source)
        return source

    for url, entry in status.items():
        if domain_authority(url)[0] < options["min_authority"]:
            entry["status"] = "below the authority floor"

    def budget() -> Budget:
        return Budget(max_fetches=options["max_fetches"], max_seconds=180.0)

    agent = ResearchAgent(
        search=search,
        fetch=fetch,
        budget_factory=budget,
        numeric_tolerance=options["numeric_tolerance"],
        min_authority=options["min_authority"],
    )
    started = time.monotonic()
    report = agent.run(question)

    # The same grouping the agent made (same inputs, same threshold, same order), kept
    # here so the page can say which addresses were merged as copies of one story.
    groups = deduplicate(
        [s for s in fetched if s.text.strip()], threshold=agent.duplicate_threshold
    )
    merged = {g.representative.url: [d.url for d in g.duplicates] for g in groups if g.duplicates}
    for row in report.sources:
        row["copies"] = merged.get(row["url"], [])
        row["title"] = status.get(row["url"], {}).get("title", "")
    for copies in merged.values():
        for url in copies:
            if url in status:
                status[url]["status"] = "merged as a copy"

    return {
        "question": question,
        "summary": report.summary(),
        "findings": report.findings,
        "disputed": report.disputed,
        "sources": report.sources,
        "inputs": list(status.values()),
        "queries_run": report.queries_run,
        "settings": options,
        "seconds": round(time.monotonic() - started, 2),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "deep-research-agent"
    fetcher = staticmethod(fetch_page)

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            path = "/static/index.html"
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/samples":
            self._json(HTTPStatus.OK, json.loads(SAMPLES.read_text(encoding="utf-8")))
            return
        if path.startswith("/static/"):
            name = path[len("/static/") :]
            target = (STATIC / name).resolve()
            if STATIC.resolve() in target.parents and target.is_file():
                kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if kind.startswith("text/") or kind == "application/javascript":
                    kind += "; charset=utf-8"
                self._send(HTTPStatus.OK, target.read_bytes(), kind)
                return
        if path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": f"no page at {path}"})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/research":
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "the request is empty or too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            question, sources, options = parse_request(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "the request body is not valid JSON"})
            return
        except BadRequest as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(HTTPStatus.OK, run_research(question, sources, options, self.fetcher))


def make_server(host: str = "127.0.0.1", port: int = 8845) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Web page for the deep research agent.")
    parser.add_argument("--port", type=int, default=8845, help="port to listen on (8845)")
    parser.add_argument("--host", default="127.0.0.1", help="address to bind (127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"deep-research-agent page at {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
