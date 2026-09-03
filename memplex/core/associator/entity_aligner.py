"""Entity alignment using fuzzy matching and semantic similarity."""

import re
from difflib import SequenceMatcher
from typing import Any, ClassVar


class EntityAligner:
    """Aligns and merges entities from multiple sources."""

    TERM_EQUIVALENCES: ClassVar[dict[str, list[str]]] = {
        "login": ["登录", "登入", "认证", "authenticate"],
        "logout": ["登出", "退出", "signout"],
        "register": ["注册", "登记", "signup"],
        "user": ["用户", "user", "users", "member", "会员"],
        "password": ["密码", "password", "pwd"],
        "order": ["订单", "order", "订购"],
        "payment": ["支付", "payment", "pay", "付款"],
    }

    def __init__(self) -> None:
        """Build reverse mapping from Chinese/alt terms to English canonical forms."""
        self._chinese_to_english = {}
        for english, chinese_list in self.TERM_EQUIVALENCES.items():
            for ch in chinese_list:
                self._chinese_to_english[ch] = english

    def normalize(self, text: str) -> str:
        """Normalize text for comparison, translating Chinese terms to English."""
        normalized = text.lower()
        normalized = re.sub(r"[^a-z0-9一-鿿]", "", normalized)

        for chinese, english in self._chinese_to_english.items():
            if chinese in normalized:
                normalized = normalized.replace(chinese, english)

        return normalized

    def calculate_similarity(self, str1: str, str2: str) -> float:
        """Calculate similarity between two strings (0-1)."""
        norm1 = self.normalize(str1)
        norm2 = self.normalize(str2)

        if norm1 == norm2:
            return 1.0

        for base, equivalents in self.TERM_EQUIVALENCES.items():
            if norm1 in equivalents or norm1 == base and norm2 in equivalents or norm2 == base:
                return 0.85

        return SequenceMatcher(None, norm1, norm2).ratio()

    def find_similar(
        self, target: str, entities: list, threshold: float = 0.6
    ) -> list[tuple[Any, float]]:
        """Find entities similar to target."""
        results = []

        for entity in entities:
            entity_name = getattr(entity, "name", "") or ""
            score = self.calculate_similarity(target, entity_name)
            if score >= threshold:
                results.append((entity, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def find_merge_candidates(
        self, entities: list[dict], threshold: float = 0.9
    ) -> list[list[dict]]:
        """
        Find groups of entities that should be merged.

        Uses blocking strategy for O(n) performance instead of O(n^2).
        """
        groups = []
        used = set()

        blocks: dict[str, list[dict]] = {}
        for entity in entities:
            normalized = self.normalize(entity["name"])
            first_char = normalized[0] if normalized else "#"
            if first_char not in blocks:
                blocks[first_char] = []
            blocks[first_char].append(entity)

        for block_entities in blocks.values():
            for i, entity in enumerate(block_entities):
                if entity["id"] in used:
                    continue

                group = [entity]
                used.add(entity["id"])

                for other in block_entities[i + 1 :]:
                    if other["id"] in used:
                        continue

                    score = self.calculate_similarity(entity["name"], other["name"])
                    if score >= threshold:
                        group.append(other)
                        used.add(other["id"])

                if len(group) > 1:
                    groups.append(group)

        return groups

    def suggest_merged_name(self, entities: list[dict]) -> str:
        """Suggest a merged name from multiple entities."""
        if not entities:
            return ""

        if len(entities) == 1:
            return entities[0]["name"]

        for entity in entities:
            if re.search(r"[一-鿿]", entity["name"]):
                return entity["name"]

        return max(entities, key=lambda x: len(x["name"]))["name"]
