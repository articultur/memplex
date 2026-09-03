"""
Detect and resolve conflicts between extracted data.

Design principle (v3.2 §1.6): Field multi-value coexistence (non-authority arbitration).
When conflicts occur, ALL values are preserved and needs_review is set to True.
Only when user manually resolves does one value become the "final" value.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class Conflict:
    id: str
    type: str  # field_value, missing_field, etc.
    severity: str  # high, medium, low
    field: str
    values: list[dict]  # [{"source": ..., "content": ..., "authority": ...}]
    resolved: bool = False
    final_value: str | None = None
    needs_human: bool = True


class ConflictResolver:
    """
    Detects and resolves conflicts in extracted data.

    Follows v3.2 §1.6 design: multi-value coexistence (no authority arbitration).
    All conflicting values are preserved, needs_human=True by default.
    """

    def detect_conflicts(self, functions: list) -> list[Conflict]:
        """Detect conflicts between functions."""
        conflicts = []
        conflict_id = 1

        func_map: dict[str, list] = {}
        for func in functions:
            key = func.name_normalized
            if key not in func_map:
                func_map[key] = []
            func_map[key].append(func)

        for key, funcs in func_map.items():
            if len(funcs) < 2:
                continue

            for i in range(len(funcs)):
                for j in range(i + 1, len(funcs)):
                    conflict = self._compare_functions(funcs[i], funcs[j], conflict_id)
                    if conflict:
                        conflicts.append(conflict)
                        conflict_id += 1

        return conflicts

    def _compare_functions(self, func1: Any, func2: Any, conflict_id: int) -> Conflict | None:
        """Compare two functions for conflicts."""
        # Compare conditions (adapted for List[FieldValue])
        cond1_descs = [fv.desc for fv in func1.condition] if func1.condition else []
        cond2_descs = [fv.desc for fv in func2.condition] if func2.condition else []

        if cond1_descs and cond2_descs and cond1_descs != cond2_descs:
            auth1 = func1.source_authority or "unknown"
            auth2 = func2.source_authority or "unknown"
            return Conflict(
                id=f"conflict_{conflict_id:03d}",
                type="field_value",
                severity="medium",
                field="condition",
                values=[
                    {
                        "source": func1.source_paragraphs[0]
                        if func1.source_paragraphs
                        else "unknown",
                        "content": ", ".join(cond1_descs),
                        "authority": auth1,
                    },
                    {
                        "source": func2.source_paragraphs[0]
                        if func2.source_paragraphs
                        else "unknown",
                        "content": ", ".join(cond2_descs),
                        "authority": auth2,
                    },
                ],
                needs_human=True,
            )
        return None

    def get_all_values(self, conflict: Conflict) -> list[str]:
        """Get all conflicting values."""
        if not conflict.values:
            return []
        return [v["content"] for v in conflict.values]

    def mark_for_human_review(self, conflict: Conflict, suggestion: str | None = None) -> None:
        """Mark conflict for human review."""
        conflict.needs_human = True
        conflict.resolved = False

    def apply_resolution(self, conflict: Conflict, value: str) -> None:
        """Apply human resolution."""
        if value not in self.get_all_values(conflict):
            raise ValueError(f"Resolution value '{value}' not in conflict values")
        conflict.final_value = value
        conflict.resolved = True
        conflict.needs_human = False

    def resolve_conflicts(self, conflicts: list[Conflict]) -> tuple:
        """Process conflicts, marking all for human review."""
        unresolved = []
        resolved = []

        for conflict in conflicts:
            if conflict.resolved and conflict.final_value:
                resolved.append(conflict)
            else:
                conflict.needs_human = True
                unresolved.append(conflict)

        return unresolved, resolved
