"""Term-based association using dictionary lookup."""

import logging
from typing import List, Optional, Set, Tuple

from memplex.core.dictionaries import TermDictionary
from memplex.models.memory import Function

logger = logging.getLogger(__name__)


class TermMapper:
    """Maps terms between documents using dictionary lookup."""

    def __init__(self, dictionary: TermDictionary = None):
        self.dictionary = dictionary or TermDictionary()

    def embed_text(self, text: str) -> Optional[List[float]]:
        """Generate embedding vector using sentence-transformers when available."""
        try:
            from sentence_transformers import SentenceTransformer

            if not hasattr(self, "_embedding_model"):
                self._embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
            embedding = self._embedding_model.encode([text])[0]
            return embedding.tolist()
        except ImportError:
            logger.debug("sentence-transformers not installed; skipping term embedding")
            return None
        except Exception as exc:
            logger.debug("Term embedding unavailable: %s", exc)
            return None

    def extract_terms(self, text: str) -> Set[str]:
        """Extract all matching terms from text."""
        return self.dictionary.find_matching_terms(text)

    def find_associations(
        self, source_terms: Set[str], target_candidates: List[Function]
    ) -> List[Tuple[Function, float]]:
        """
        Find associations based on term overlap.

        Returns:
            List of (function, confidence) tuples
        """
        associations = []

        for func in target_candidates:
            score = self._calculate_term_overlap(source_terms, func)
            if score > 0:
                associations.append((func, score))

        associations.sort(key=lambda x: x[1], reverse=True)
        return associations

    def _calculate_term_overlap(self, source_terms: Set[str], func: Function) -> float:
        """Calculate term overlap score between source and function."""
        if not source_terms:
            return 0.0

        func_terms = self.extract_terms(func.name)
        func_terms.update(self.extract_terms(func.name_normalized))

        # Get terms from multi-value fields (List[FieldValue])
        for fv_list in [func.trigger, func.condition, func.action, func.benefit]:
            for fv in fv_list:
                func_terms.update(self.extract_terms(fv.desc))

        if not func_terms:
            return 0.0

        intersection = source_terms & func_terms
        union = source_terms | func_terms

        return len(intersection) / len(union) if union else 0.0

    def build_term_normalized(self, text: str) -> str:
        """Build normalized term from text using dictionary."""
        terms = self.extract_terms(text)
        if terms:
            return "_".join(sorted(terms))
        return text.lower().replace(" ", "_")
