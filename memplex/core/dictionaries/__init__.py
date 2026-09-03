"""Term dictionary for association mapping."""

from pathlib import Path

import yaml


class TermDictionary:
    """Term dictionary for association mapping."""

    def __init__(self, base_path: str | None = None):
        if base_path is None:
            self.base_path = Path(__file__).parent / "base_terms.yaml"
        else:
            self.base_path = Path(base_path)
        self.terms: dict[str, list[str]] = {}
        self.reverse_map: dict[str, str] = {}  # synonym -> canonical
        self._load()

    def _load(self) -> None:
        """Load dictionary from YAML."""
        if not self.base_path.exists():
            return

        with open(self.base_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        for canonical, synonyms in data.items():
            self.terms[canonical] = synonyms
            for syn in synonyms:
                self.reverse_map[syn.lower()] = canonical

    def get_canonical(self, term: str) -> str:
        """Get canonical form of a term."""
        return self.reverse_map.get(term.lower(), term.lower())

    def get_synonyms(self, term: str) -> list[str]:
        """Get all synonyms for a term."""
        canonical = self.get_canonical(term)
        return self.terms.get(canonical, [term])

    def find_matching_terms(self, text: str) -> set[str]:
        """Find all matching terms in text."""
        text_lower = text.lower()
        matches = set()
        for term, synonyms in self.terms.items():
            for syn in synonyms:
                if syn.lower() in text_lower:
                    matches.add(term)
                    break
        return matches
