"""Map Vision LLM components to L2 Function structures."""

import re
from typing import List, Optional

from memplex.models.memory import Function
from memplex.models.misc import FieldValue


class VisionMapper:
    """Converts Vision LLM output to L2 Function entities."""

    COMPONENT_TYPE_MAP = {
        "button": "button",
        "nav": "navigation",
        "navbar": "navigation",
        "input": "input_field",
        "textfield": "input_field",
        "card": "card",
        "kpi": "metric_card",
        "chart": "chart",
        "graph": "chart",
        "table": "table",
        "form": "form",
        "modal": "modal",
        "dialog": "modal",
        "sidebar": "sidebar",
        "header": "header",
        "footer": "footer",
        "label": "label",
        "text": "text",
        "icon": "icon",
        "image": "image",
        "link": "link",
        "menu": "menu",
        "dropdown": "dropdown",
        "checkbox": "checkbox",
        "radio": "radio",
        "switch": "switch",
        "slider": "slider",
        "tab": "tab",
    }

    def vision_to_functions(self, vision_result: dict, source_id: str = "vision") -> List[Function]:
        """Convert Vision LLM components to L2 Function objects."""
        components = vision_result.get("components", [])
        functions = []

        for i, comp in enumerate(components):
            func = self._component_to_function(comp, i, source_id, vision_result)
            if func:
                functions.append(func)

        return functions

    def _component_to_function(
        self, component: dict, index: int, source_id: str, vision_result: dict
    ) -> Optional[Function]:
        """Convert a single Vision component to Function."""
        comp_type = component.get("type", "unknown")
        label = component.get("label", "")
        function_name = component.get("function")
        data = component.get("data", {})

        if not label and not function_name:
            return None

        if function_name:
            name = label if label else function_name
            normalized = self._normalize_name(function_name)
            trigger_desc = (
                f"点击 {label} 按钮" if comp_type in ("button", "nav") else f"与 {label} 交互"
            )
        else:
            name = label
            normalized = self._normalize_name(label)
            trigger_desc = f"查看 {label}"

        action_desc = self._build_action(component)

        func = Function(
            id=f"vision_{source_id}_{index:03d}",
            name=name,
            name_normalized=normalized,
            source_paragraphs=[source_id],
            trigger=[FieldValue(desc=trigger_desc)],
            condition=[],
            action=[FieldValue(desc=action_desc)],
            benefit=[],
            confidence=0.85,
            attributes={
                "component_type": self.COMPONENT_TYPE_MAP.get(comp_type, comp_type),
                "vision_data": data,
                "layout": vision_result.get("layout"),
                "page_type": vision_result.get("page_type"),
            },
        )

        return func

    def _normalize_name(self, name: str) -> str:
        """Normalize name to snake_case."""
        normalized = re.sub(r"[^a-zA-Z0-9一-鿿]", "_", name)
        normalized = normalized.strip("_").lower()
        normalized = re.sub(r"_+", "_", normalized)
        return normalized

    def _build_action(self, component: dict) -> str:
        """Build action description from component."""
        comp_type = component.get("type", "")
        label = component.get("label", "")

        action_map = {
            "button": f"点击 {label}",
            "nav": f"导航到 {label}",
            "navbar": f"导航到 {label}",
            "input": f"输入 {label}",
            "textfield": f"输入 {label}",
            "form": f"提交 {label} 表单",
            "link": f"跳转 {label}",
            "menu": f"打开 {label} 菜单",
            "dropdown": f"选择 {label}",
            "modal": f"打开 {label} 弹窗",
        }

        return action_map.get(comp_type, f"与 {label} 交互")
