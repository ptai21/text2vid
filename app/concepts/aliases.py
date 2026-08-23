"""Query to ConceptKey — SPEC.md §6.4.

Rule-based, deterministic, free. No LLM router (CLAUDE.md §4): a router would
add a second source of non-determinism at the front door and charge for the
privilege, and a clear rejection beats a confident wrong guess.

The trade is stated in SPEC.md §16: paraphrases outside the alias sets are
rejected rather than guessed at.
"""

from __future__ import annotations

import re

from app.concepts.registry import ConceptContract, all_concepts, supported_keys
from app.domain.job import FailureCode

MIN_QUERY_CHARS = 3
MAX_QUERY_CHARS = 500

_NON_ALNUM = re.compile(r"[^a-z0-9+]+")


class ResolutionError(Exception):
    """A named refusal, carrying the FailureCode the API envelope reports."""

    def __init__(self, code: FailureCode, message: str,
                 supported_concepts: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.supported_concepts = supported_concepts or []


def normalise(query: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    `+` survives so that "H+" stays distinguishable from "H".
    """
    return _NON_ALNUM.sub(" ", query.lower()).strip()


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """Whole-phrase match, so "ph" does not match inside "graph"."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", haystack) is not None


def _matches(concept: ConceptContract, normalised: str) -> bool:
    if any(_contains_phrase(normalised, term)
           for term in concept.excludes_if_present):
        return False
    return any(
        all(_contains_phrase(normalised, term) for term in alias)
        for alias in concept.aliases
    )


def resolve(query: str) -> str:
    """Return the ConceptKey, or raise `ResolutionError`.

    Length is checked first: a 600-character essay about the pH scale is a
    malformed request, and reporting `unsupported_concept` would send the
    client looking for a concept problem that does not exist.
    """
    stripped = query.strip()
    if not MIN_QUERY_CHARS <= len(stripped) <= MAX_QUERY_CHARS:
        raise ResolutionError(
            "invalid_request",
            f"Query must be between {MIN_QUERY_CHARS} and {MAX_QUERY_CHARS} "
            f"characters; got {len(stripped)}.",
        )

    normalised = normalise(stripped)
    matched = [c.key for c in all_concepts() if _matches(c, normalised)]

    if not matched:
        raise ResolutionError(
            "unsupported_concept",
            "This service currently covers three chemistry concepts.",
            supported_keys(),
        )

    if len(matched) > 1:
        raise ResolutionError(
            "ambiguous_query",
            "That question matches more than one supported concept. "
            f"Please ask about one of {matched} at a time.",
            supported_keys(),
        )

    return matched[0]
