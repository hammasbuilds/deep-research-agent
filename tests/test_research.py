"""Deep research agent tests.

Search and fetch are injected, so the whole agent runs deterministically with no
network and no model. Every property worth asserting — that copies do not corroborate,
that disagreement survives, that fabricated quotes are dropped — is a property of the
logic rather than of the model behind it.
"""

from __future__ import annotations

import pytest

from research.agent import (
    Budget,
    BudgetExhausted,
    ResearchAgent,
    plan,
    relevance,
    verify_quote,
)
from research.claims import corroborate, extract
from research.sources import (
    Source,
    deduplicate,
    domain_authority,
    independence,
    jaccard,
    shingles,
)

WIRE = (
    "Inflation in Pakistan was 8.2 percent in the year to June, the statistics bureau "
    "said. Cotton output was 5.5 million bales this season."
)

DOCS = {
    "https://reuters.com/a": WIRE,
    "https://dawn.com/b": WIRE,  # republished verbatim
    "https://x.blogspot.com/c": WIRE,  # republished verbatim
    "https://worldbank.org/d": (
        "Inflation in Pakistan was 12.4 percent last year. "
        "Cotton output was 5.4 million bales this season."
    ),
    "https://finance.gov.pk/e": (
        "Cotton output was 5.6 million bales this season. "
        "The cotton crop is vulnerable to leaf curl virus."
    ),
}


def agent(**kw) -> ResearchAgent:
    kw.setdefault("search", lambda q, n: list(DOCS)[:n])
    kw.setdefault("fetch", lambda u: Source(url=u, text=DOCS[u]))
    return ResearchAgent(**kw)


class TestAuthority:
    @pytest.mark.parametrize(
        ("url", "expected_class"),
        [
            ("https://arxiv.org/abs/1", "primary_research"),
            ("https://finance.gov.pk/x", "official"),
            ("https://reuters.com/x", "established_press"),
            ("https://en.wikipedia.org/x", "reference"),
            ("https://someone.blogspot.com/x", "user_generated"),
        ],
    )
    def test_domains_are_classified(self, url, expected_class):
        assert domain_authority(url)[1] == expected_class

    def test_research_outranks_a_blog(self):
        assert (
            domain_authority("https://nature.com/x")[0]
            > domain_authority("https://x.wordpress.com/y")[0]
        )

    def test_an_unknown_domain_is_not_trusted_by_default(self):
        assert domain_authority("https://whatever.xyz/a")[0] <= 0.3


class TestDeduplication:
    def test_republished_copies_collapse_to_one_source(self):
        """A research agent that counts them reports 'corroborated by 12 sources'
        about a claim with exactly one origin."""
        groups = deduplicate([Source(url=u, text=t) for u, t in DOCS.items()])
        assert len(groups) == 3

    def test_the_highest_authority_copy_represents_the_group(self):
        """The order results arrive in is not evidence about anything."""
        groups = deduplicate(
            [
                Source(url="https://x.blogspot.com/c", text=WIRE),
                Source(url="https://reuters.com/a", text=WIRE),
            ]
        )
        assert groups[0].representative.domain == "reuters.com"
        assert groups[0].copies == 2

    def test_genuinely_different_documents_stay_apart(self):
        groups = deduplicate(
            [
                Source(url="https://a.com", text="Cotton output rose sharply this year."),
                Source(url="https://b.com", text="Wheat prices fell across the province."),
            ]
        )
        assert len(groups) == 2

    def test_independence_ratio_makes_the_difference_visible(self):
        groups = deduplicate([Source(url=u, text=t) for u, t in DOCS.items()])
        stats = independence(groups)
        assert stats["documents"] == 5
        assert stats["independent_sources"] == 3
        assert stats["independence_ratio"] == 0.6

    def test_shingles_of_identical_text_match_completely(self):
        assert jaccard(shingles(WIRE), shingles(WIRE)) == 1.0

    def test_shingles_of_unrelated_text_do_not(self):
        assert jaccard(shingles(WIRE), shingles("Entirely unrelated prose here.")) < 0.1

    def test_short_text_does_not_crash_shingling(self):
        assert shingles("two words") != set()


class TestClaimExtraction:
    def test_a_numeric_claim_is_extracted(self):
        claims = extract("Inflation in Pakistan was 8.2 percent last year.")
        assert claims[0].value == 8.2
        assert claims[0].unit == "%"

    def test_units_are_normalised_so_values_compare(self):
        """2 billion and 2000 million are the same number."""
        a = extract("Revenue was 2 billion.")[0]
        b = extract("Revenue was 2000 million.")[0]
        assert a.comparable_value == b.comparable_value

    def test_commas_in_numbers_are_handled(self):
        assert extract("The population was 240,485,000 people.")[0].value == 240485000.0

    def test_negation_is_captured_as_a_stance(self):
        claim = extract("Vaccination is not associated with autism.")[0]
        assert claim.negated

    def test_the_quote_is_the_whole_sentence(self):
        """A fragment cannot be verified against the source, and a citation that
        cannot be checked is not a citation."""
        claim = extract("Inflation in Pakistan was 8.2 percent last year.")[0]
        assert claim.quote.endswith(".")

    def test_stopwords_do_not_split_a_subject(self):
        a = extract("The inflation rate was 8.2 percent.")[0]
        b = extract("Inflation rate was 8.2 percent.")[0]
        assert a.subject == b.subject


class TestCorroboration:
    def test_agreeing_sources_corroborate(self):
        claims = (
            extract("Cotton output was 5.5 million bales.", source_url="a")
            + extract("Cotton output was 5.4 million bales.", source_url="b")
            + extract("Cotton output was 5.6 million bales.", source_url="c")
        )
        finding = corroborate(claims)[0]
        assert not finding.contradiction
        assert finding.confidence() == "well-corroborated"

    def test_disagreeing_sources_are_reported_as_disputed(self):
        """Most agents summarise the conflict away, and the conflict was the finding."""
        claims = extract("Inflation in Pakistan was 8.2 percent.", source_url="a") + extract(
            "Inflation in Pakistan was 12.4 percent.", source_url="b"
        )
        finding = corroborate(claims)[0]
        assert finding.contradiction
        assert finding.confidence() == "disputed"
        assert finding.spread > 0.10

    def test_ordinary_measurement_variation_is_not_a_contradiction(self):
        """8.2% and 8.4% are corroborating each other."""
        claims = extract("Inflation in Pakistan was 8.2 percent.", source_url="a") + extract(
            "Inflation in Pakistan was 8.4 percent.", source_url="b"
        )
        assert not corroborate(claims)[0].contradiction

    def test_the_consensus_is_the_median_not_the_mean(self):
        """One wildly wrong figure should not drag the consensus toward itself — and
        on an even count, taking the upper middle value would silently prefer the
        higher of two disagreeing sources."""
        claims = extract("Output was 8 million.", source_url="a") + extract(
            "Output was 12 million.", source_url="b"
        )
        assert corroborate(claims)[0].consensus_value == 10_000_000.0

    def test_opposing_stances_contradict(self):
        claims = extract("Vaccination is not associated with autism.", source_url="a") + extract(
            "Vaccination is associated with autism.", source_url="b"
        )
        assert corroborate(claims)[0].contradiction

    def test_one_source_is_never_well_corroborated(self):
        assert (
            corroborate(extract("Cotton output was 5.5 million bales.", source_url="a"))[
                0
            ].confidence()
            == "single-source"
        )

    def test_contradictions_sort_first(self):
        claims = (
            extract("Inflation in Pakistan was 8.2 percent.", source_url="a")
            + extract("Inflation in Pakistan was 12.4 percent.", source_url="b")
            + extract("Cotton output was 5.5 million bales.", source_url="a")
            + extract("Cotton output was 5.5 million bales.", source_url="b")
        )
        assert corroborate(claims)[0].contradiction


class TestPlanning:
    def test_a_contrary_query_is_always_included(self):
        """An agent that only searches for confirmation finds it."""
        assert any("contradicting" in q for q in plan("Is X effective"))

    def test_a_quantitative_question_gets_data_queries(self):
        assert any("statistics" in q for q in plan("How many people live in Lahore"))

    def test_a_comparative_question_gets_drawback_queries(self):
        assert any("drawbacks" in q for q in plan("Is Postgres better than MySQL"))

    def test_queries_are_deduplicated_and_capped(self):
        queries = plan("How many, why, and is it best?", max_queries=3)
        assert len(queries) == 3 == len(set(queries))


class TestQuoteVerification:
    def test_an_exact_quote_verifies(self):
        assert verify_quote("Cotton output was 5.5 million bales", WIRE)

    def test_whitespace_differences_are_tolerated(self):
        assert verify_quote("Cotton  output   was 5.5 million bales", WIRE)

    def test_a_fabricated_quote_is_rejected(self):
        """A citation the agent wrote from memory is worse than no citation, because
        it looks like evidence."""
        assert not verify_quote("Cotton output was 9.9 million bales", WIRE)

    def test_a_near_miss_is_a_miss(self):
        """A fuzzy threshold is exactly where fabricated quotes slip through."""
        assert not verify_quote("Cotton output was 5.5 billion bales", WIRE)


class TestAgent:
    def test_a_full_run_separates_agreement_from_dispute(self):
        report = agent().run("What is inflation in Pakistan?")
        assert report.disputed
        assert report.disputed[0]["values"] == [8.2, 12.4]
        assert any(f["confidence"] == "well-corroborated" for f in report.findings)

    def test_copies_do_not_manufacture_corroboration(self):
        """Extracting from every copy would let one wire story contribute the same
        claim a dozen times."""
        report = agent().run("What is inflation in Pakistan?")
        assert report.independence["documents"] == 5
        assert report.independence["independent_sources"] == 3

    def test_the_query_budget_is_enforced(self):
        report = agent(budget_factory=lambda: Budget(max_queries=1)).run(
            "How many people live in Lahore and why"
        )
        assert len(report.queries_run) == 1

    def test_the_fetch_budget_is_enforced(self):
        report = agent(budget_factory=lambda: Budget(max_fetches=2)).run("anything")
        assert report.independence["documents"] <= 2
        assert "budget exhausted" in report.stopped_because

    def test_the_agent_stops_when_it_has_enough(self):
        """Research that cannot stop when it has enough is as broken as research that
        cannot stop at all."""
        report = agent(
            budget_factory=lambda: Budget(sufficient_findings=1, sufficient_independent_sources=2)
        ).run("What is inflation in Pakistan?")
        assert report.stopped_because == "found enough corroborated evidence"

    def test_a_dead_link_costs_its_fetch_and_is_skipped(self):
        def flaky(url):
            if "dawn" in url:
                raise ConnectionError("404")
            return Source(url=url, text=DOCS[url])

        report = agent(fetch=flaky).run("What is inflation in Pakistan?")
        assert report.independence["documents"] == 4

    def test_low_authority_sources_can_be_excluded(self):
        report = agent(min_authority=0.7).run("What is inflation in Pakistan?")
        assert all(s["authority"] >= 0.7 for s in report.sources)

    def test_sources_are_ranked_by_authority(self):
        report = agent().run("What is inflation in Pakistan?")
        authorities = [s["authority"] for s in report.sources]
        assert authorities == sorted(authorities, reverse=True)

    def test_copies_found_is_reported_per_source(self):
        report = agent().run("What is inflation in Pakistan?")
        assert any(s["copies_found"] == 3 for s in report.sources)

    def test_no_results_produces_an_empty_report_not_a_crash(self):
        empty = ResearchAgent(search=lambda q, n: [], fetch=lambda u: Source(url=u))
        report = empty.run("anything")
        assert report.findings == [] and report.disputed == []

    def test_the_summary_reports_why_it_stopped(self):
        assert agent().run("anything").summary()["stopped_because"]


class TestBudget:
    def test_exhaustion_names_the_ceiling(self):
        budget = Budget(max_queries=1)
        budget.queries = 1
        with pytest.raises(BudgetExhausted) as exc:
            budget.check()
        assert exc.value.kind == "queries"

    def test_remaining_is_reported(self):
        assert Budget(max_fetches=20).remaining()["fetches"] == 20


class TestRealPageText:
    """Fetched pages are not tidy prose: headings without full stops, pronoun subjects,
    currency before the number, and many claims that are not about the question."""

    def test_a_line_break_ends_a_sentence(self):
        claims = extract("Population\nThe population was 241.49 million.")
        assert [c.subject for c in claims] == ["population"]
        assert claims[0].quote == "The population was 241.49 million."

    def test_a_pronoun_is_not_a_subject(self):
        """Two 'there were N' sentences are about different things; matched on
        'there', they would be reported as a dispute."""
        a = extract("There were 33.7 million in 1951.", source_url="a")
        b = extract("There were 207.7 million in 2017.", source_url="b")
        assert a == [] and b == []
        assert not any(f.contradiction for f in corroborate(a + b))

    def test_a_leading_currency_is_read(self):
        claim = extract("Economic output was estimated at $410 billion in 2025.")[0]
        assert (claim.value, claim.unit) == (410.0, "billion")
        assert claim.subject == "economic output"

    def test_past_tense_measurement_verbs_are_read(self):
        assert extract("Exports stood at 31.2 billion.")[0].value == 31.2
        assert extract("Inflation averaged 9.5 percent.")[0].unit == "%"

    def test_relevance_is_the_share_of_question_words_present(self):
        assert relevance("What is inflation in Pakistan?", "Inflation in Pakistan was 8%") == 1.0
        assert relevance("What is inflation in Pakistan?", "Cotton output fell") == 0.0
        assert relevance("What is inflation in Pakistan?", "Pakistan's cotton") == 0.5
        assert relevance("What is it?", "anything") == 0.0

    def test_findings_about_the_question_come_first(self):
        report = agent().run("How large was the cotton output?")
        assert report.findings[0]["subject"].lower().startswith("cotton output")
        scores = [f["relevance"] for f in report.findings]
        assert scores == sorted(scores, reverse=True)

    def test_a_long_sentence_copied_onto_another_page_counts_once(self):
        """Two pages too different for document deduplication can still share a
        paragraph; that shared sentence is one source's evidence, not two."""
        shared = (
            "Pakistan is the world's fifth most populous country and has the second "
            "largest Muslim population as of 2023."
        )
        docs = {
            "https://en.example.org/pakistan": shared + " Its capital is Islamabad. " * 20,
            "https://en.example.org/demographics": shared + " Urdu is spoken widely. " * 20,
        }
        report = ResearchAgent(
            search=lambda q, n: list(docs)[:n],
            fetch=lambda u: Source(url=u, text=docs[u]),
        ).run("What is the population of Pakistan?")
        finding = next(f for f in report.findings if "populous" in f["citations"][0]["quote"])
        assert finding["independent_sources"] == 1
        assert finding["confidence"] == "single-source"
        assert finding["copied_sentences"] == 1

    def test_short_identical_sentences_stay_independent(self):
        report = agent().run("What is cotton output?")
        assert report.independence["independent_sources"] == 3
        cotton = next(
            f for f in report.findings if f["subject"].lower().startswith("cotton output")
        )
        assert cotton["copied_sentences"] == 0

    def test_power_units_are_kept(self):
        claim = extract("Global installed solar capacity reached 2100 GW in 2024.")[0]
        assert (claim.value, claim.unit) == (2100.0, "gw")
