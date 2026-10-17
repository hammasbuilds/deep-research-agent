"""Claims: extraction, corroboration, and contradiction.

The part research agents usually skip. A typical agent retrieves several documents,
feeds them all to a model, and asks for a summary. When the documents disagree, the
model silently picks one — and the disagreement, which is the single most useful thing
the research turned up, disappears.

Here, claims are extracted per source, matched across sources, and **disagreement is an
output rather than something to resolve**. A report that says "sources disagree: 8.2%
(World Bank, 2025) vs 12.4% (national statistics office, 2024)" is more useful than one
that confidently states either figure.

Extraction is pattern-based rather than model-based. That is a real limitation — it
catches numeric and definitional claims and little else — but it is deterministic,
inspectable, and testable, and it means the disagreement logic can be verified without
an API key. A model-based extractor fits behind the same `Claim` interface.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

# "inflation was 8.2%", "GDP grew by 3.4 percent", "the population is 240 million"
_NUMERIC = re.compile(
    r"(?P<subject>[A-Za-z][\w\s\-']{2,60}?)\s+"
    r"(?:(?:is|was|were|are)(?:\s+estimated\s+at)?|reached|rose to|fell to|grew by|"
    r"stands at|stood at|averaged|peaked at|climbed to|dropped to|declined to|"
    r"increased to|decreased to|amounted to|hit)\s+"
    r"(?:about\s+|around\s+|roughly\s+|approximately\s+)?"
    r"(?P<currency>\$|US\$|USD\s?|PKR\s?|Rs\.?\s?)?"
    r"(?P<value>-?\d[\d,]*\.?\d*)\s*"
    r"(?P<unit>%|percent|per cent|million|billion|trillion|thousand|"
    r"kg|km|tonnes?|tons?|years?|days?|TWh|GWh|GW|MW|kW|USD|PKR|\$)?",
    re.I,
)

# "X does not cause Y", "X is not associated with Y"
_NEGATION = re.compile(
    r"(?P<subject>[A-Za-z][\w\s\-']{2,60}?)\s+"
    r"(?:is|are|was|were|does|do|did|has|have)\s+not\s+"
    r"(?P<predicate>[\w\s\-']{3,60})",
    re.I,
)

_AFFIRMATION = re.compile(
    r"(?P<subject>[A-Za-z][\w\s\-']{2,60}?)\s+"
    r"(?:is|are|was|were|does|do|did|has|have)\s+"
    r"(?!not\b)(?P<predicate>[\w\s\-']{3,60})",
    re.I,
)

_UNIT_SCALE = {
    "thousand": 1e3,
    "million": 1e6,
    "billion": 1e9,
    "trillion": 1e12,
}

_STOPWORDS = {
    "the",
    "a",
    "an",
    "this",
    "that",
    "these",
    "those",
    "its",
    "their",
    "his",
    "her",
    "it",
    "they",
    # Pronouns and question words carry no subject. "There were 33.7 million" and "there
    # were 207.7 million" are about different things, and matching them on "there" would
    # report a dispute between two unrelated sentences.
    "there",
    "here",
    "which",
    "what",
    "who",
    "whom",
    "whose",
    "where",
    "when",
    "we",
    "you",
    "he",
    "she",
    "i",
    # A conjunction left over from a clause split ("..., and is home to 10.5%") is not a
    # subject either.
    "and",
    "but",
    "or",
}


def _normalise_subject(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOPWORDS]
    return " ".join(words)


@dataclass
class Claim:
    subject: str  # normalised, for matching
    raw_subject: str
    kind: str  # "numeric" | "assertion"
    value: float | None = None
    unit: str = ""
    predicate: str = ""
    negated: bool = False
    quote: str = ""  # the sentence it came from, verbatim
    source_url: str = ""

    @property
    def key(self) -> str:
        """What two claims must share to be *about* the same thing."""
        if self.kind == "numeric":
            return f"numeric::{self.subject}::{self.unit}"
        return f"assertion::{self.subject}::{_normalise_subject(self.predicate)}"

    @property
    def comparable_value(self) -> float | None:
        """Value scaled to base units, so 2 billion and 2000 million compare equal."""
        if self.value is None:
            return None
        return self.value * _UNIT_SCALE.get(self.unit.lower(), 1.0)


def _sentences(text: str) -> list[str]:
    # A line break ends a sentence too: headings, list items and table cells on fetched
    # pages carry no full stop, and without this a heading becomes the subject of the
    # first claim in the paragraph below it.
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\s*\n\s*", text) if s.strip()]


def extract(text: str, *, source_url: str = "") -> list[Claim]:
    """Pull numeric and assertional claims out of prose."""
    claims: list[Claim] = []

    for sentence in _sentences(text):
        for match in _NUMERIC.finditer(sentence):
            subject = _normalise_subject(match.group("subject"))
            if not subject:
                continue
            unit = (match.group("unit") or "").lower()
            if not unit and match.group("currency"):
                # "$410" and "410 USD" are the same claim; "$410 billion" keeps the
                # scale as its unit so it compares with "410,000 million".
                unit = match.group("currency").strip().rstrip(".").lower()
                unit = {"us$": "$", "usd": "$", "rs": "pkr"}.get(unit, unit)
            unit = {"percent": "%", "per cent": "%"}.get(unit, unit)
            try:
                value = float(match.group("value").replace(",", ""))
            except ValueError:
                continue
            claims.append(
                Claim(
                    subject=subject,
                    raw_subject=match.group("subject").strip(),
                    kind="numeric",
                    value=value,
                    unit=unit,
                    quote=sentence,
                    source_url=source_url,
                )
            )

        negated = _NEGATION.search(sentence)
        if negated:
            subject = _normalise_subject(negated.group("subject"))
            if subject:
                claims.append(
                    Claim(
                        subject=subject,
                        raw_subject=negated.group("subject").strip(),
                        kind="assertion",
                        predicate=negated.group("predicate").strip(),
                        negated=True,
                        quote=sentence,
                        source_url=source_url,
                    )
                )
        elif not _NUMERIC.search(sentence):
            affirmed = _AFFIRMATION.search(sentence)
            if affirmed:
                subject = _normalise_subject(affirmed.group("subject"))
                if subject:
                    claims.append(
                        Claim(
                            subject=subject,
                            raw_subject=affirmed.group("subject").strip(),
                            kind="assertion",
                            predicate=affirmed.group("predicate").strip(),
                            negated=False,
                            quote=sentence,
                            source_url=source_url,
                        )
                    )

    return claims


@dataclass
class Finding:
    """One thing the research established, with everything that bears on it."""

    key: str
    subject: str
    kind: str
    claims: list[Claim] = field(default_factory=list)
    supporting_sources: set[str] = field(default_factory=set)
    contradiction: bool = False
    spread: float | None = None  # numeric disagreement, as a fraction
    consensus_value: float | None = None
    unit: str = ""

    @property
    def independent_support(self) -> int:
        return len(self.supporting_sources)

    def confidence(self) -> str:
        """Confidence from corroboration, not from tone.

        A contradiction caps confidence regardless of how many sources are on each
        side — the useful output there is the disagreement, not a winner.
        """
        if self.contradiction:
            return "disputed"
        if self.independent_support >= 3:
            return "well-corroborated"
        if self.independent_support == 2:
            return "corroborated"
        return "single-source"

    def summary(self) -> dict:
        out = {
            "subject": self.subject,
            "confidence": self.confidence(),
            "independent_sources": self.independent_support,
            "contradiction": self.contradiction,
        }
        if self.kind == "numeric":
            out["values"] = sorted({c.value for c in self.claims if c.value is not None})
            out["unit"] = self.unit
            out["consensus"] = self.consensus_value
            out["spread"] = self.spread
        else:
            out["predicate"] = self.claims[0].predicate if self.claims else ""
            out["negated"] = sorted({c.negated for c in self.claims})
        out["citations"] = [
            {"source": c.source_url, "quote": c.quote[:200]} for c in self.claims[:5]
        ]
        return out


def corroborate(claims: Sequence[Claim], *, numeric_tolerance: float = 0.10) -> list[Finding]:
    """Group claims that are about the same thing, and flag where they disagree.

    `numeric_tolerance` is fractional. Two sources reporting 8.2% and 8.4% are
    corroborating each other with ordinary measurement variation; 8.2% and 12.4% are
    disagreeing, and rounding that away would destroy the finding.
    """
    grouped: dict[str, Finding] = {}

    for claim in claims:
        finding = grouped.setdefault(
            claim.key,
            Finding(key=claim.key, subject=claim.raw_subject, kind=claim.kind, unit=claim.unit),
        )
        finding.claims.append(claim)
        if claim.source_url:
            finding.supporting_sources.add(claim.source_url)

    for finding in grouped.values():
        if finding.kind == "numeric":
            values = [c.comparable_value for c in finding.claims if c.comparable_value is not None]
            if not values:
                continue
            low, high = min(values), max(values)
            midpoint = (low + high) / 2
            finding.spread = round((high - low) / abs(midpoint), 6) if midpoint else 0.0
            finding.contradiction = finding.spread > numeric_tolerance
            # The median, not the mean: one wildly wrong figure should not drag the
            # consensus toward itself. statistics.median averages the middle pair on an
            # even count - taking ordered[n // 2] would silently prefer the higher of
            # two disagreeing sources, which is a bias, not a tie-break.
            finding.consensus_value = round(statistics.median(values), 6)
        else:
            stances = {c.negated for c in finding.claims}
            finding.contradiction = len(stances) > 1

    return sorted(
        grouped.values(),
        key=lambda f: (not f.contradiction, -f.independent_support),
    )
