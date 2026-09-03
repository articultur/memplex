"""Extract content from Markdown/Text documents."""

import re

from memplex.models.paragraph import (
    Paragraph,
    ParagraphCollection,
    Sentence,
    SentenceRelation,
)


class MarkdownExtractor:
    """Extracts L1 paragraphs and L2 structured data from markdown."""

    def extract(self, content: str, source: str = "document.md") -> ParagraphCollection:
        """
        Extract paragraphs from markdown content.

        Args:
            content: Markdown text content
            source: Source identifier

        Returns:
            ParagraphCollection with extracted paragraphs
        """
        paragraphs = ParagraphCollection()

        # Split by double newlines (paragraph separation)
        blocks = re.split(r"\n\s*\n", content)

        for i, block in enumerate(blocks):
            block = block.strip()
            if not block:
                continue

            para_id = f"para_{i + 1:03d}"

            # Detect section header
            header_match = re.match(r"^(#{1,6})\s+(.+)$", block, re.MULTILINE)
            section = ""
            if header_match:
                section = header_match.group(2).strip()

            # Extract sentences and roles
            sentences = self._extract_sentences(block, para_id)
            relations = self._extract_relations(sentences)

            paragraph = Paragraph(
                id=para_id,
                source=f"{source}#{para_id}",
                section=section,
                raw_text=block,
                semantic_unit=True,
                sentences=sentences,
                sentence_relations=relations,
            )

            paragraphs.add(paragraph)

        return paragraphs

    def _extract_sentences(self, text: str, para_id: str) -> list[Sentence]:
        """Extract sentences and their roles from text."""
        # Split on sentence-ending punctuation followed by whitespace and uppercase/Chinese
        # Also split on newlines (paragraph breaks)
        # Avoids splitting on decimals (1.5) or common abbreviations (e.g., i.e.)
        sentence_texts = re.split(r"(?<=[.!?。！？])\s+(?=[A-Z一-鿿])|[\n]+", text)
        sentences = []

        for i, sent in enumerate(sentence_texts):
            sent = sent.strip()
            if not sent:
                continue

            role = self._infer_role(sent)
            sentences.append(Sentence(id=f"{para_id}_s{i + 1}", text=sent, role=role))

        return sentences

    def _infer_role(self, sentence: str) -> str:
        """Infer the role of a sentence from its content."""
        sentence_lower = sentence.lower()

        # Trigger indicators — user interaction or event start
        # NOTE: avoid overly generic words like "登录" or "用户" that appear in all sentence types
        trigger_patterns = [
            "点击",
            "选择",
            "提交",
            "按下",
            "输入",
            "勾选",
            "当",
            "when",
            "after",
            "before",
            "登录时",
            "登出时",
            "注册时",
        ]
        for p in trigger_patterns:
            if p in sentence_lower:
                return "trigger"

        # Condition indicators — prerequisite or conditional logic
        cond_patterns = [
            "如果",
            "满足",
            "条件是",
            "前提是",
            "when",
            "if",
            "unless",
            "条件",
            "required",
            "前提",
        ]
        for p in cond_patterns:
            if p in sentence_lower:
                return "condition"

        # Action indicators — system or user performs an operation
        action_patterns = [
            "自动",
            "发送",
            "创建",
            "更新",
            "删除",
            "跳转",
            "打开",
            "关闭",
            "显示",
            "隐藏",
            "提交",
            "保存",
            "取消",
            "action",
            "do",
            "perform",
            "execute",
        ]
        for p in action_patterns:
            if p in sentence_lower:
                return "action"

        # Result indicators — outcome or benefit
        result_patterns = [
            "享受",
            "获得",
            "收到",
            "看到",
            "返回",
            "跳转到",
            "result",
            "then",
            "因此",
            "所以",
            "进入",
        ]
        for p in result_patterns:
            if p in sentence_lower:
                return "result"

        return "statement"

    def _extract_relations(self, sentences: list[Sentence]) -> list[SentenceRelation]:
        """Extract relations between sentences."""
        relations = []

        for i, sent in enumerate(sentences):
            if sent.role == "condition" and i + 1 < len(sentences):
                next_sent = sentences[i + 1]
                if next_sent.role == "action":
                    relations.append(
                        SentenceRelation(from_id=sent.id, to_id=next_sent.id, type="if_then")
                    )

        return relations
