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
from datetime import datetime
from typing import List

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
from memplex.models import (
    ExtractedData,
    FieldValue,
    Function,
    GraphData,
    SourceDocument,
)
from memplex.models.paragraph import ParagraphCollection
from memplex.processing.graph_builder import GraphBuilder
from memplex.processing.merger.confidence_calculator import ConfidenceCalculator
from memplex.processing.merger.conflict_resolver import ConflictResolver

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────


def _detect_memory_type(text: str) -> str:
    """Heuristic: classify text into a memory type.

    Returns one of ``"function"`` | ``"fact"`` | ``"preference"`` |
    ``"observation"``.
    """
    text_lower = text.lower()

    obs_keywords = [
        "observe",
        "observed",
        "noticed",
        "happened",
        "occurred",
        "事件",
        "观察",
        "发生",
        "记录",
    ]
    if any(k in text_lower for k in obs_keywords):
        return "observation"

    pref_keywords = [
        "prefer",
        "like",
        "dislike",
        "want",
        "always",
        "never",
        "喜欢",
        "偏好",
        "讨厌",
        "倾向",
        "总是",
        "从不",
    ]
    if any(k in text_lower for k in pref_keywords):
        return "preference"

    fact_keywords = [
        "is",
        "are",
        "means",
        "defined as",
        "refers to",
        "是",
        "意味着",
        "定义为",
        "指的是",
        "事实",
    ]
    if any(k in text_lower for k in fact_keywords):
        return "fact"

    return "function"


def _normalize_name(name: str) -> str:
    """Generate name_normalized from a display name.

    Rules (per spec SS1.3):
    1. lowercase
    2. strip whitespace
    3. collapse consecutive whitespace to single space
    4. remove punctuation (keep letters, digits, CJK, spaces, underscores, hyphens)
    """
    normalized = name.lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"[^a-z0-9一-鿿 _-]", "", normalized)
    return normalized


# ── CoreEngine ───────────────────────────────────────────────────────


class CoreEngine:
    """Pure computation engine: ``SourceDocument`` -> ``ExtractedData``.

    Orchestrates the full extraction pipeline:

    1. **Handler** -- acquire raw content from source type
    2. **Extractor** -- content -> L1 Paragraphs
    3. **Paragraph -> Function** -- with multi-value ``FieldValue`` fields
    4. **DomainClassifier** -- assign domain
    5. **RefLinker** -- resolve cross-references
    6. **EntityAligner** -- deduplicate/merge
    7. **ConflictResolver** -- detect conflicts
    8. **ConfidenceCalculator** -- compute confidence
    9. **GraphBuilder** -- build relationship edges

    Parameters
    ----------
    store:
        Optional :class:`MemoryStore`.  Required only when
        :meth:`extract` needs to look up existing Functions for
        graph-edge detection.  Pass ``None`` for stateless usage
        (graph edges will be built from the batch only).
    """

    def __init__(self, store=None) -> None:
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

        # Step 4: Paragraphs -> Functions
        functions = self._paragraphs_to_functions(paragraphs, source)

        # Merge vision/image functions
        functions.extend(vision_functions)
        functions.extend(image_functions)

        if not functions:
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
                func.cross_references = refs

        # Step 7: EntityAligner -- deduplicate/merge
        functions = self._deduplicate_functions(functions)

        # Step 8: ConflictResolver -- detect conflicts
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

        # Step 9: ConfidenceCalculator -- compute confidence for each function
        for func in functions:
            if func.confidence == 1.0:
                func.confidence = self._calculate_function_confidence(
                    func, paragraphs, source_hint
                )

        # Step 10: GraphBuilder -- build edges
        graph = self._build_graph(functions)

        return ExtractedData(
            functions=functions,
            graph=graph,
            delta=False,
        )

    def extract_batch(self, sources: List[SourceDocument]) -> ExtractedData:
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
        all_functions: List[Function] = []
        all_edges: list = []

        for source in sources:
            extracted = self.extract(source)
            all_functions.extend(extracted.functions)
            all_edges.extend(extracted.graph.edges)

        # Deduplicate across batch
        all_functions = self._deduplicate_functions(all_functions)

        # Rebuild graph with deduped functions
        graph = self._build_graph(all_functions)

        return ExtractedData(
            functions=all_functions,
            graph=graph,
            delta=False,
        )

    # ════════════════════════════════════════════════════════════════
    #  Internal: content acquisition
    # ════════════════════════════════════════════════════════════════

    def _acquire_content(self, source: SourceDocument):
        """Route source to the correct handler and return normalized content.

        Returns
        -------
        tuple of (text, extracted_images, source_hint)
            text: str -- the textual content to process
            extracted_images: list -- image dicts extracted from PDF etc.
            source_hint: str -- hint for confidence calculation
        """
        source_type = source.type
        extracted_images = []

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
                extracted_images = []
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

    def _paragraphs_to_functions(
        self,
        paragraphs: ParagraphCollection,
        source: SourceDocument,
    ) -> List[Function]:
        """Convert L1 Paragraphs to L2 Functions with multi-value fields."""
        functions: List[Function] = []
        source_id = source.type

        for para in paragraphs.paragraphs:
            if not para.raw_text or not para.raw_text.strip():
                continue

            # Generate stable ID from content hash
            content_hash = hashlib.sha256(para.raw_text.encode()).hexdigest()[:16]
            func_id = f"func_{content_hash}"

            name = para.section if para.section else para.raw_text[:50]
            name_normalized = _normalize_name(
                para.section if para.section else para.raw_text[:50]
            )

            # Classify FieldValues from sentences
            triggers: List[FieldValue] = []
            conditions: List[FieldValue] = []
            actions: List[FieldValue] = []
            benefits: List[FieldValue] = []

            for sent in para.sentences:
                fv = FieldValue(
                    desc=sent.text,
                    sources=[f"{source_id}:{para.id}"],
                    source_method="rule_based",
                    weight=0.7,
                )
                if sent.role == "trigger":
                    triggers.append(fv)
                elif sent.role == "condition":
                    conditions.append(fv)
                elif sent.role in ("action",):
                    actions.append(fv)
                elif sent.role == "result":
                    benefits.append(fv)
                else:
                    # "statement" -> put as action by default
                    if not actions:
                        actions.append(fv)
                    else:
                        actions.append(fv)

            # If no structured sentences, use raw text as trigger/action
            if not triggers and not actions and para.raw_text:
                sentences_text = [
                    s.strip()
                    for s in re.split(r"[。.!?！？]", para.raw_text)
                    if s.strip()
                ]
                if sentences_text:
                    triggers.append(
                        FieldValue(
                            desc=sentences_text[0],
                            sources=[f"{source_id}:{para.id}"],
                            source_method="rule_based",
                            weight=0.7,
                        )
                    )
                    for s in sentences_text[1:]:
                        actions.append(
                            FieldValue(
                                desc=s,
                                sources=[f"{source_id}:{para.id}"],
                                source_method="rule_based",
                                weight=0.7,
                            )
                        )
                else:
                    triggers.append(
                        FieldValue(
                            desc=para.raw_text,
                            sources=[f"{source_id}:{para.id}"],
                            source_method="rule_based",
                            weight=0.7,
                        )
                    )

            func = Function(
                id=func_id,
                name=name,
                name_normalized=name_normalized,
                trigger=triggers,
                condition=conditions,
                action=actions,
                benefit=benefits,
                source_paragraphs=[para.id],
                source_type=source.source_type,
                content_hash=hashlib.sha256(para.raw_text.encode()).hexdigest(),
            )
            functions.append(func)

        return functions

    def _deduplicate_functions(self, functions: List[Function]) -> List[Function]:
        """Use EntityAligner to merge duplicate Functions."""
        if len(functions) <= 1:
            return functions

        # Build entity dicts for EntityAligner
        entity_dicts = [
            {"id": f.id, "name": f.name, "name_normalized": f.name_normalized}
            for f in functions
        ]

        merge_groups = self.entity_aligner.find_merge_candidates(
            entity_dicts, threshold=0.9
        )

        if not merge_groups:
            return functions

        # Build merge map
        merge_map: dict = {}  # canonical_id -> list of Function
        func_by_id: dict = {f.id: f for f in functions}
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
        result: List[Function] = []
        for func in functions:
            if func.id in merged_ids:
                continue
            if func.id in merge_map:
                merged = self._merge_function_fields(merge_map[func.id])
                result.append(merged)
            else:
                result.append(func)

        return result

    def _merge_function_fields(self, functions: List[Function]) -> Function:
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

    def _build_graph(self, functions: List[Function]) -> GraphData:
        """Build relationship edges between Functions using GraphBuilder."""
        edges = []

        if self._store is not None:
            # Use store-aware GraphBuilder
            try:
                builder = GraphBuilder(store=self._store)
                edges = builder.build_from_batch(functions)
            except Exception as exc:
                logger.warning("GraphBuilder failed: %s", exc)
                edges = self._build_edges_rule_based(functions)
        else:
            # Stateless: use rule-based edge detection
            edges = self._build_edges_rule_based(functions)

        return GraphData(nodes=functions, edges=edges)

    def _build_edges_rule_based(self, functions: List[Function]) -> list:
        """Simple rule-based edge detection when no store is available."""
        from memplex.models import GraphEdge

        edges: List[GraphEdge] = []
        seen: set = set()

        for func in functions:
            # REFERENCES from cross-references
            for ref in func.cross_references:
                target = ref.get("target", "")
                if not target:
                    continue
                # Find matching function by name
                for other in functions:
                    if other.id == func.id:
                        continue
                    if (
                        target.lower() in other.name.lower()
                        or target.lower() in other.name_normalized
                    ):
                        key = (func.id, other.id, "REFERENCES")
                        if key not in seen:
                            seen.add(key)
                            edges.append(
                                GraphEdge(
                                    source=func.id,
                                    target=other.id,
                                    edge_type="REFERENCES",
                                    weight=1.0,
                                    evidence=[
                                        f"cross-reference: {func.name} -> {other.name}"
                                    ],
                                    created_at=datetime.now(),
                                )
                            )

            # ASSOCIATED_WITH: shared domain
            if func.domain:
                for other in functions:
                    if other.id == func.id:
                        continue
                    if other.domain == func.domain:
                        key = (func.id, other.id, "ASSOCIATED_WITH")
                        rev_key = (other.id, func.id, "ASSOCIATED_WITH")
                        if key not in seen and rev_key not in seen:
                            seen.add(key)
                            edges.append(
                                GraphEdge(
                                    source=func.id,
                                    target=other.id,
                                    edge_type="ASSOCIATED_WITH",
                                    weight=0.5,
                                    evidence=[f"shared domain: {func.domain}"],
                                    created_at=datetime.now(),
                                )
                            )

        return edges
