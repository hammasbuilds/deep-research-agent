"""The web page's server: request checking, the search/fetch adapters, and the HTTP layer.

No network: live fetching is replaced by a stub fetcher, and the HTTP tests run a real
server on a free local port.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from research.sources import Source
from research.web import pages, server
from research.web.server import BadRequest, parse_request, run_research

WIRE = "Global installed solar capacity reached 2100 GW in 2024. Analysts expect growth."

SOLAR = {
    "question": "How much solar capacity was installed globally by 2024?",
    "sources": [
        {
            "url": "https://wire.example.com/a",
            "text": WIRE,
        },
        {
            "url": "https://aggregator.example.net/a",
            "text": WIRE,
        },
        {
            "url": "https://agency.example.org/b",
            "text": "Global installed solar capacity reached 1600 GW in 2024. Growth was slower.",
        },
        {"url": "notes", "text": "Cotton output was 5.5 million bales this season."},
    ],
}


def run(payload, fetcher=None):
    question, sources, options = parse_request(payload)
    if fetcher is None:
        return run_research(question, sources, options)
    return run_research(question, sources, options, fetcher)


class TestParseRequest:
    def test_a_question_is_required(self):
        with pytest.raises(BadRequest, match="question"):
            parse_request({"question": " ", "sources": SOLAR["sources"]})

    def test_at_least_one_source_is_required(self):
        with pytest.raises(BadRequest, match="at least one source"):
            parse_request({"question": "q", "sources": [{"url": "", "text": "  "}]})

    def test_a_name_without_text_cannot_be_fetched(self):
        with pytest.raises(BadRequest, match="not a web address"):
            parse_request({"question": "q", "sources": [{"url": "my notes", "text": ""}]})

    def test_a_name_becomes_a_pasted_address(self):
        _, sources, _ = parse_request(
            {"question": "q", "sources": [{"url": "My notes", "text": "x"}]}
        )
        assert sources[0]["url"] == "pasted://my-notes"

    def test_the_same_source_twice_is_refused(self):
        twice = [{"url": "https://a.example.com/x", "text": "t"}] * 2
        with pytest.raises(BadRequest, match="twice"):
            parse_request({"question": "q", "sources": twice})

    def test_settings_are_range_checked(self):
        with pytest.raises(BadRequest, match="numeric_tolerance"):
            parse_request({**SOLAR, "settings": {"numeric_tolerance": 5}})
        with pytest.raises(BadRequest, match="min_authority"):
            parse_request({**SOLAR, "settings": {"min_authority": "high"}})

    def test_pasted_html_is_reduced_to_text(self):
        html = (
            "<html><head><title>T</title><script>x=1</script></head>"
            "<body><p>Hello.</p></body></html>"
        )
        _, sources, _ = parse_request({"question": "q", "sources": [{"url": "a", "text": html}]})
        assert sources[0]["text"] == "Hello."
        assert sources[0]["title"] == "T"


class TestRunResearch:
    def test_the_report_carries_the_dispute_and_both_citations(self):
        out = run(SOLAR)
        assert len(out["disputed"]) == 1
        dispute = out["disputed"][0]
        assert dispute["values"] == [1600.0, 2100.0]
        assert {c["source"] for c in dispute["citations"]} == {
            "https://wire.example.com/a",
            "https://agency.example.org/b",
        }

    def test_merged_copies_are_named(self):
        out = run(SOLAR)
        wire = next(s for s in out["sources"] if s["url"] == "https://wire.example.com/a")
        assert wire["copies"] == ["https://aggregator.example.net/a"]
        status = {i["url"]: i["status"] for i in out["inputs"]}
        assert status["https://aggregator.example.net/a"] == "merged as a copy"

    def test_a_source_no_query_matches_is_not_read(self):
        status = {i["url"]: i["status"] for i in run(SOLAR)["inputs"]}
        assert status["pasted://notes"] == "no query matched it"

    def test_an_address_without_text_is_fetched(self):
        calls = []

        def fetcher(url):
            calls.append(url)
            return Source(
                url=url,
                title="Live",
                text="Global installed solar capacity reached 2090 GW in 2024.",
            )

        payload = {
            "question": SOLAR["question"],
            "sources": [*SOLAR["sources"], {"url": "https://live.example.org/c", "text": ""}],
        }
        out = run(payload, fetcher)
        assert calls == ["https://live.example.org/c"]
        live = next(i for i in out["inputs"] if i["url"] == "https://live.example.org/c")
        assert (live["status"], live["title"]) == ("fetched", "Live")

    def test_a_failed_fetch_is_reported_and_skipped(self):
        def fetcher(url):
            raise pages.FetchError("HTTP 404")

        payload = {
            "question": SOLAR["question"],
            "sources": [{"url": "https://gone.example.com/x"}],
        }
        out = run(payload, fetcher)
        assert out["inputs"][0]["status"] == "fetch failed"
        assert out["inputs"][0]["error"] == "HTTP 404"
        assert out["summary"]["documents_fetched"] == 0

    def test_the_authority_floor_is_applied(self):
        out = run({**SOLAR, "settings": {"min_authority": 0.5}})
        status = {i["url"]: i["status"] for i in out["inputs"]}
        assert status["https://wire.example.com/a"] == "below the authority floor"
        assert status["https://agency.example.org/b"] == "read"

    def test_the_fetch_budget_is_the_setting(self):
        out = run({**SOLAR, "settings": {"max_fetches": 1}})
        assert out["summary"]["documents_fetched"] == 1
        assert "fetches budget exhausted" in out["summary"]["stopped_because"]


class TestPages:
    def test_html_to_text_drops_scripts_and_keeps_blocks_apart(self):
        text, title = pages.html_to_text(
            "<title> A  page </title><nav>menu</nav><h2>Heading</h2>"
            "<p>One.</p><p>Two&nbsp;three.</p>"
        )
        assert title == "A page"
        assert text == "Heading\nOne.\nTwo three."

    def test_only_web_addresses_are_fetched(self):
        with pytest.raises(pages.FetchError, match="http"):
            pages.fetch_page("file:///etc/passwd")

    def test_labels(self):
        assert pages.label_to_url("https://x.org/a", 0) == "https://x.org/a"
        assert pages.label_to_url("report.txt", 0) == "pasted://report.txt"
        assert pages.label_to_url("", 2) == "pasted://source-3"


@pytest.fixture
def live_server():
    class Stubbed(server.Handler):
        fetcher = staticmethod(lambda url: Source(url=url, text="Stub text was 1 percent."))

    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), Stubbed)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers.get_content_type(), r.read()


def _post(url, body: bytes):
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestHttp:
    def test_the_page_and_its_assets_are_served(self, live_server):
        status, kind, body = _get(live_server + "/")
        assert (status, kind) == (200, "text/html")
        assert b"Deep Research Agent" in body
        for asset in ("/static/app.js", "/static/style.css"):
            assert _get(live_server + asset)[0] == 200

    def test_a_path_outside_static_is_not_served(self, live_server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(live_server + "/static/../server.py")
        assert exc.value.code == 404

    def test_samples_are_valid_requests(self, live_server):
        samples = json.loads(_get(live_server + "/api/samples")[2])["samples"]
        assert {s["id"] for s in samples} >= {"solar", "live"}
        for sample in samples:
            parse_request({"question": sample["question"], "sources": sample["sources"]})

    def test_research_round_trip(self, live_server):
        status, out = _post(live_server + "/api/research", json.dumps(SOLAR).encode())
        assert status == 200
        assert out["summary"]["disputed"] == 1

    def test_bad_input_is_a_400_with_a_reason(self, live_server):
        status, out = _post(live_server + "/api/research", b"{not json")
        assert status == 400 and "JSON" in out["error"]
        status, out = _post(live_server + "/api/research", json.dumps({"question": ""}).encode())
        assert status == 400 and "question" in out["error"]

    def test_the_live_fetcher_is_used_for_bare_addresses(self, live_server):
        payload = {
            "question": "What was stub text?",
            "sources": [{"url": "https://s.example.com/"}],
        }
        status, out = _post(live_server + "/api/research", json.dumps(payload).encode())
        assert status == 200
        assert out["inputs"][0]["status"] == "fetched"
        assert out["findings"][0]["values"] == [1.0]
