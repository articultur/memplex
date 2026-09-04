"""CoreEngine -- pure computation layer for SourceDocument -> ExtractedData.

Zero-dependency on Agent platforms.  Input and output are data structures only.
All I/O (storage, network) is handled by callers (MemplexService).

Usage::

    from memplex.core import CoreEngine

    engine = CoreEngine()
    extracted = engine.extract(source)
    for func in extracted.functions:
        print(func.name)
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from memplex.core.associator.domain_classifier import DomainClassifier
from memplex.core.associator.entity_aligner import EntityAligner
from memplex.core.associator.ref_linker import RefLinker
from memplex.core.associator.term_mapper import TermMapper
from memplex.core.extractors.docx import DOCXExtractor
from memplex.core.extractors.image import ImageExtractor
from memplex.core.extractors.markdown import MarkdownExtractor
from memplex.core.extractors.pdf import PDFExtractor
from memplex.core.extractors.vision_mapper import VisionMapper
from memplex.core.handlers.clipboard import ClipboardHandler
from memplex.core.handlers.file_handler import FileHandler
from memplex.core.handlers.url_handler import URLHandler
from memplex.intent import detect_memory_type
from memplex.models import (
    ExtractedData,
    Fact,
    Function,
    GraphData,
    Preference,
    SourceDocument,
)
from memplex.models.paragraph import Paragraph, ParagraphCollection
from memplex.processing.function_builder import (
    build_functions_from_paragraphs as _build_functions_from_paragraphs,
)
from memplex.processing.function_builder import normalize_name
from memplex.processing.graph_builder import GraphBuilder, build_edges_rule_based
from memplex.processing.merger.confidence_calculator import ConfidenceCalculator
from memplex.processing.merger.conflict_resolver import ConflictResolver

if TYPE_CHECKING:
    from memplex.storage.base import MemoryStore

logger = logging.getLogger(__name__)

_normalize_name = normalize_name


# ── Helpers ──────────────────────────────────────────────────────────
# ``_detect_memory_type`` previously lived here but had zero callers; the
# live classifier is ``memplex.intent.detect_memory_type``.
# ``_normalize_name`` and ``_build_functions_from_paragraphs`` are now
# imported from ``memplex.processing.function_builder`` (see imports above)
# and re-bound here under underscored names so existing callers
# (``tests/test_core_engine.py`` imports ``_normalize_name``) keep working.


# ── CoreEngine ───────────────────────────────────────────────────────


class CoreEngine:
    """Pure computation engine: ``SourceDocument`` -> ``ExtractedData``.

    Orchestrates the full extraction pipeline:

    1. **Handler** -- acquire raw content from source type
    2. **Extractor** -- content -> L1 Paragraphs
    3. **Vision/image** -- map pre-extracted vision data to Functions
    4. **Paragraph -> Function** -- with multi-value ``FieldValue`` fields
    5. **DomainClassifier** -- assign domain
    6. **RefLinker** -- resolve cross-references
    7. **ConflictResolver** -- detect conflicts (before dedup so that
       same-name Functions with differing conditions are flagged with
       ``needs_review`` instead of being silently merged)
    8. **EntityAligner** -- deduplicate/merge (conflicted Functions are
       kept separate)
    9. **ConfidenceCalculator** -- compute confidence
    10. **GraphBuilder** -- build relationship edges

    Parameters
    ----------
    store:
        Optional :class:`MemoryStore`.  Required only when
        :meth:`extract` needs to look up existing Functions for
        graph-edge detection.  Pass ``None`` for stateless usage
        (graph edges will be built from the batch only).
    """

    def __init__(self, store: MemoryStore | None = None) -> None:
        # ── Extractors ──────────────────────────────────────────────
        self.markdown_extractor = MarkdownExtractor()
        self.image_extractor = ImageExtractor()
        self.pdf_extractor = PDFExtractor()
        self.docx_extractor = DOCXExtractor()
        self.vision_mapper = VisionMapper()

        # ── Handlers ────────────────────────────────────────────────
        self.file_handler = FileHandler()
        self.url_handler = URLHandler()
        self.clipboard_handler = ClipboardHandler()

        # ── Associators ─────────────────────────────────────────────
        self.term_mapper = TermMapper()
        self.ref_linker = RefLinker()
        self.entity_aligner = EntityAligner()
        self.domain_classifier = DomainClassifier()

        # ── Merge layer ─────────────────────────────────────────────
        self.conflict_resolver = ConflictResolver()
        self.confidence_calculator = ConfidenceCalculator()

        # ── Graph builder (optional store) ──────────────────────────
        self._store = store

    # ════════════════════════════════════════════════════════════════
    #  Public API
    # ════════════════════════════════════════════════════════════════

    def extract(self, source: SourceDocument) -> ExtractedData:
        """Main extraction pipeline.

        Parameters
        ----------
        source:
            The source document to process.

        Returns
        -------
        ExtractedData
            Extracted Functions and graph edges.
        """
        # Step 1: Acquire content via handler
        text, extracted_images, source_hint = self._acquire_content(source)

        # Step 2: Extract L1 Paragraphs
        paragraphs = self._extract_paragraphs(text, source_hint)

        # Step 3: Handle vision / image extracted data
        vision_functions = []
        if source.vision:
            vision_functions = self.vision_mapper.vision_to_functions(
                source.vision,
                source_id=source.type,
            )

        # Also process extracted images from PDF
        image_functions = []
        for img_info in extracted_images:
            img_path = img_info.get("path")
            if not img_path:
                continue
            full = self.image_extractor.extract_full(img_path)
            if full and full.get("vision"):
                img_funcs = self.vision_mapper.vision_to_functions(
                    full["vision"],
                    source_id=f"pdf_img_{img_info.get('page', 0)}_{img_info.get('index', 0)}",
                )
                image_functions.extend(img_funcs)
            # Cleanup temp file
            if img_info.get("_tmp"):
                try:
                    import os

                    os.unlink(img_path)
                except OSError:
                    pass

        # Step 4: Paragraphs -> memory nodes.  Paragraphs whose intent
        # classifies as fact / preference (memplex.intent.detect) produce
        # Fact / Preference nodes; everything else produces Functions
        # (previous behaviour, including observation-intent text).
        facts: list[Fact] = []
        preferences: list[Preference] = []
        func_paragraphs = ParagraphCollection()
        for para in paragraphs.paragraphs:
            intent = detect_memory_type(para.raw_text or "")
            if intent == "fact":
                facts.append(self._build_fact_from_paragraph(para, source))
            elif intent == "preference":
                preferences.append(self._build_preference_from_paragraph(para, source))
            else:
                func_paragraphs.add(para)
        functions = _build_functions_from_paragraphs(func_paragraphs, source)

        # Merge vision/image functions
        functions.extend(vision_functions)
        functions.extend(image_functions)

        if not functions and not facts and not preferences:
            return ExtractedData(
                functions=[],
                graph=GraphData(nodes=[], edges=[]),
                delta=False,
            )

        # Step 5: DomainClassifier
        for func in functions:
            func.domain = self.domain_classifier.classify(func)

        # Step 6: RefLinker -- extract cross-references from raw text
        if text:
            refs = self.ref_linker.extract_references(text)
            for func in functions:
                # Each Function gets its own copy -- assigning the same list
                # object would make later per-function mutations leak across
                # all Functions.
                func.cross_references = list(refs)

        # Step 7: ConflictResolver -- detect conflicts BEFORE dedup so that
        # same-name Functions with differing conditions are flagged instead
        # of being silently merged by the EntityAligner (v3.2 §1.6: all
        # conflicting values are preserved and marked needs_review).
        conflicted_ids = self._flag_conflicts(functions)

        # Step 8: EntityAligner -- deduplicate/merge (conflicted Functions
        # are excluded from merging so both values survive for review).
        functions = self._deduplicate_functions(functions, skip_ids=conflicted_ids)

        # Step 9: ConfidenceCalculator -- compute confidence for each function
        for func in functions:
            if func.confidence == 1.0:
                func.confidence = self._calculate_function_confidence(func, paragraphs, source_hint)

        # Step 10: GraphBuilder -- build edges
        graph = self._build_graph(functions)

        return ExtractedData(
            functions=functions,
            graph=graph,
            delta=False,
            facts=facts,
            preferences=preferences,
        )

    def extract_batch(self, sources: list[SourceDocument]) -> ExtractedData:
        """Batch extraction: process multiple sources and merge results.

        Parameters
        ----------
        sources:
            List of source documents to process.

        Returns
        -------
        ExtractedData
            Merged extraction results from all sources.
        """
        all_functions: list[Function] = []
        all_facts: list[Fact] = []
        all_preferences: list[Preference] = []

        for source in sources:
            extracted = self.extract(source)
            all_functions.extend(extracted.functions)
            all_facts.extend(extracted.facts)
            all_preferences.extend(extracted.preferences)

        # Detect cross-source conflicts before the batch-level dedup,
        # otherwise same-name Functions from different sources would be
        # silently merged (same reasoning as in ``extract``).
        conflicted_ids = self._flag_conflicts(all_functions)

        # Deduplicate across batch
        all_functions = self._deduplicate_functions(all_functions, skip_ids=conflicted_ids)

        # Rebuild graph with deduped functions
        graph = self._build_graph(all_functions)

        return ExtractedData(
            functions=all_functions,
            graph=graph,
            delta=False,
            facts=all_facts,
            preferences=all_preferences,
        )

    # ════════════════════════════════════════════════════════════════
    #  Internal: content acquisition
    # ════════════════════════════════════════════════════════════════

    def _acquire_content(self, source: SourceDocument) -> tuple[str, list[Any], str]:
        """Route source to the correct handler and return normalized content.

        Returns
        -------
        tuple of (text, extracted_images, source_hint)
            text: str -- the textual content to process
            extracted_images: list -- image dicts extracted from PDF etc.
            source_hint: str -- hint for confidence calculation
        """
        source_type = source.type
        extracted_images: list[Any] = []

        # Text / clipboard content
        if source_type in ("text", "clipboard"):
            text = source.content or ""
            # Use ClipboardHandler to detect subtype
            if source_type == "clipboard":
                parsed = self.clipboard_handler.parse(text)
                if parsed:
                    source_hint = parsed[0][0]  # "markdown" or "text"
                else:
                    source_hint = "text"
            else:
                source_hint = "text"
            return text, extracted_images, source_hint

        # File content
        if source_type == "file" and source.source_path:
            result = self.file_handler.read(source.source_path)
            if result is None:
                return "", extracted_images, "text"

            content_type, content = result

            if content_type == "image":
                # Process image: OCR + vision
                full = self.image_extractor.extract_full(content)
                combined_text = full.get("combined_text", "")
                return combined_text, extracted_images, "image"

            if content_type == "pdf":
                full = self.pdf_extractor.extract_full(content)
                if full is None:
                    return "", extracted_images, "pdf"
                text = "\n\n".join(full.get("pages", []))
                for page_images in full.get("images", []):
                    for img in page_images:
                        if img.get("path"):
                            img["_tmp"] = True
                            extracted_images.append(img)
                return text, extracted_images, "pdf"

            if content_type == "docx":
                docx_text = self.docx_extractor.extract(content)
                return docx_text or "", extracted_images, "docx"

            # markdown / text
            return content, extracted_images, content_type

        # URL content
        if source_type == "url" and source.url:
            result = self.url_handler.fetch(source.url)
            if result is None:
                return "", extracted_images, "url"

            content_type, content = result

            if content_type == "image":
                full = self.image_extractor.extract_full(content)
                combined_text = full.get("combined_text", "")
                # Cleanup temp file
                self.url_handler.cleanup_temp_file(content)
                return combined_text, extracted_images, "image"

            if content_type == "pdf":
                full = self.pdf_extractor.extract_full(content)
                if full is None:
                    self.url_handler.cleanup_temp_file(content)
                    return "", extracted_images, "pdf"
                text = "\n\n".join(full.get("pages", []))
                for page_images in full.get("images", []):
                    for img in page_images:
                        if img.get("path"):
                            img["_tmp"] = True
                            extracted_images.append(img)
                self.url_handler.cleanup_temp_file(content)
                return text, extracted_images, "pdf"

            # text / markdown / html
            return content, extracted_images, content_type

        # Fallback: use source.content directly
        return source.content or "", extracted_images, "text"

    # ════════════════════════════════════════════════════════════════
    #  Internal: extraction pipeline steps
    # ════════════════════════════════════════════════════════════════

    def _extract_paragraphs(self, text: str, source_hint: str) -> ParagraphCollection:
        """Route content to the correct extractor and return L1 Paragraphs."""
        if not text or not text.strip():
            return ParagraphCollection()

        # All text goes through MarkdownExtractor (handles plain text too)
        return self.markdown_extractor.extract(text, source=source_hint)

    # ── Fact / Preference builders (intent-routed paragraphs) ──────

    @staticmethod
    def _build_fact_from_paragraph(para: Paragraph, source: SourceDocument) -> Fact:
        """Build a Fact node from a fact-intent paragraph.

        Subject/predicate/object are split on the first copula
        (``" is "`` / ``" are "`` / ``"是"`` etc.).  When no copula is
        present the paragraph text becomes the object under the generic
        predicate ``"states"``.
        """
        raw = (para.raw_text or "").strip()
        first_sentence = re.split(r"[。.!?！？\n]", raw, maxsplit=1)[0].strip() or raw
        subject, predicate, obj = "", "states", first_sentence
        for copula in (" is ", " are ", " means ", "是", "指的是", "定义为", "意味着"):
            if copula in first_sentence:
                head, _, tail = first_sentence.partition(copula)
                subject, predicate, obj = head.strip(), copula.strip(), tail.strip()
                break
        content_hash = hashlib.sha256(raw.encode()).hexdigest()
        name = para.section if para.section else raw[:50]
        now = datetime.now(UTC).isoformat()
        return Fact(
            id=f"fact_{content_hash[:16]}",
            name=name,
            subject=subject or name,
            predicate=predicate,
            object_=obj,
            source_paragraphs=[para.id],
            source_type=source.source_type,
            content_hash=content_hash,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _build_preference_from_paragraph(para: Paragraph, source: SourceDocument) -> Preference:
        """Build a Preference node from a preference-intent paragraph."""
        raw = (para.raw_text or "").strip()
        content_hash = hashlib.sha256(raw.encode()).hexdigest()
        name = para.section if para.section else raw[:50]
        now = datetime.now(UTC).isoformat()
        return Preference(
            id=f"pref_{content_hash[:16]}",
            name=name,
            aspect=name,
            preference=raw,
            source_paragraphs=[para.id],
            source_type=source.source_type,
            content_hash=content_hash,
            created_at=now,
            updated_at=now,
        )

    def _flag_conflicts(self, functions: list[Function]) -> set:
        """Detect conflicts and mark involved Functions ``needs_review``.

        Returns the set of Function ids involved in a human-reviewable
        conflict so the dedup step can keep them separate.
        """
        conflicted_ids: set = set()
        conflicts = self.conflict_resolver.detect_conflicts(functions)
        for conflict in conflicts:
            if conflict.needs_human:
                for val in conflict.values:
                    target_id = val.get("source", "")
                    for func in functions:
                        if (
                            func.id == target_id
                            or func.source_paragraphs
                            and target_id in func.source_paragraphs
                        ):
                            func.needs_review = True
                            conflicted_ids.add(func.id)
        return conflicted_ids

    def _deduplicate_functions(
        self, functions: list[Function], skip_ids: set | None = None
    ) -> list[Function]:
        """Use EntityAligner to merge duplicate Functions.

        Functions whose id is in ``skip_ids`` (e.g. involved in a detected
        conflict) are passed through unchanged and never merged.
        """
        skip_ids = skip_ids or set()
        candidates = [f for f in functions if f.id not in skip_ids]
        if len(candidates) <= 1:
            return functions

        # Build entity dicts for EntityAligner
        entity_dicts = [
            {"id": f.id, "name": f.name, "name_normalized": f.name_normalized}
            for f in candidates
        ]

        merge_groups = self.entity_aligner.find_merge_candidates(entity_dicts, threshold=0.9)

        if not merge_groups:
            return functions

        # Build merge map
        merge_map: dict = {}  # canonical_id -> list of Function
        func_by_id: dict = {f.id: f for f in candidates}
        merged_ids: set = set()

        for group in merge_groups:
            # Use first entity as canonical
            canonical_id = group[0]["id"]
            for member in group:
                member_id = member["id"]
                merge_map.setdefault(canonical_id, []).append(func_by_id[member_id])
                if member_id != canonical_id:
                    merged_ids.add(member_id)

        # Merge fields for grouped functions
        result: list[Function] = []
        for func in functions:
            if func.id in merged_ids:
                continue
            if func.id in merge_map:
                merged = self._merge_function_fields(merge_map[func.id])
                result.append(merged)
            else:
                result.append(func)

        return result

    def _merge_function_fields(self, functions: list[Function]) -> Function:
        """Merge FieldValues from multiple Functions into one."""
        if not functions:
            raise ValueError("Cannot merge empty function list")
        canonical = functions[0]

        for other in functions[1:]:
            # Merge each role field
            for role in ("trigger", "condition", "action", "benefit"):
                existing_descs = {fv.desc for fv in getattr(canonical, role)}
                for fv in getattr(other, role):
                    if fv.desc not in existing_descs:
                        getattr(canonical, role).append(fv)
                        existing_descs.add(fv.desc)

            # Merge source_paragraphs
            for sp in other.source_paragraphs:
                if sp not in canonical.source_paragraphs:
                    canonical.source_paragraphs.append(sp)

            # A merged Function inherits the review flag from any member.
            canonical.needs_review = canonical.needs_review or other.needs_review

        return canonical

    def _calculate_function_confidence(
        self,
        func: Function,
        paragraphs: ParagraphCollection,
        source_hint: str,
    ) -> float:
        """Calculate confidence for a Function using ConfidenceCalculator."""
        # Find matching paragraph
        matching_para = None
        for para in paragraphs.paragraphs:
            if para.id in func.source_paragraphs:
                matching_para = para
                break

        if matching_para:
            return self.confidence_calculator.calculate_paragraph_confidence(
                matching_para, source_hint
            )

        # Fallback: use source-based base confidence
        return self.confidence_calculator._get_base_confidence(source_hint)

    def _build_graph(self, functions: list[Function]) -> GraphData:
        """Build relationship edges between Functions using GraphBuilder."""
        edges = []

        if self._store is not None:
            # Use store-aware GraphBuilder
            try:
                builder = GraphBuilder(store=self._store)
                edges = builder.build_from_batch(functions)
            except Exception as exc:  # noqa: BLE001 - logged degradation path
                logger.warning("GraphBuilder failed: %s", exc)
                edges = build_edges_rule_based(functions)
        else:
            # Stateless: use rule-based edge detection
            edges = build_edges_rule_based(functions)

        return GraphData(nodes=functions, edges=edges)
