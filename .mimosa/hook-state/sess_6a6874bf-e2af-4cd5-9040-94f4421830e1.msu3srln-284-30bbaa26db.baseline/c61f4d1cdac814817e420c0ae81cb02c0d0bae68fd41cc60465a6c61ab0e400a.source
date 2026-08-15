"""DomainClassifier - classifies functions into domain categories."""

from typing import Dict, List

from memplex.models.memory import Function


class DomainClassifier:
    """Classifies functions into domain categories based on keyword matching."""

    DOMAIN_KEYWORDS: Dict[str, List[str]] = {
        "认证模块": [
            "登录",
            "登出",
            "注册",
            "密码",
            "验证码",
            "认证",
            "OAuth",
            "login",
            "logout",
            "register",
            "password",
            "2FA",
        ],
        "账户模块": [
            "账户",
            "账号",
            "资料",
            "设置",
            "偏好",
            "profile",
            "account",
            "settings",
        ],
        "首页模块": [
            "首页",
            "仪表盘",
            "概览",
            "指标",
            "展示",
            "home",
            "dashboard",
            "metrics",
            "KPI",
        ],
        "订单模块": ["订单", "下单", "购物车", "order", "cart", "purchase"],
        "支付模块": [
            "支付",
            "付款",
            "银行卡",
            "账单",
            "发票",
            "payment",
            "billing",
            "invoice",
        ],
        "通知模块": [
            "通知",
            "邮件",
            "短信",
            "消息",
            "提醒",
            "notification",
            "email",
            "SMS",
        ],
        "报表模块": ["报表", "统计", "导出", "分析", "report", "analytics", "export"],
        "搜索模块": [
            "搜索",
            "查询",
            "过滤",
            "筛选",
            "推荐",
            "search",
            "filter",
            "recommendation",
        ],
        "安全模块": [
            "安全",
            "权限",
            "访问",
            "角色",
            "加密",
            "security",
            "permission",
            "access",
            "role",
        ],
        "配置模块": [
            "配置",
            "设置项",
            "参数",
            "开关",
            "config",
            "settings",
            "feature flag",
        ],
    }

    def classify(self, func: Function) -> str:
        """
        Classify a function into a domain category.

        Args:
            func: Function to classify

        Returns:
            Domain name (e.g., "认证模块", "支付模块", or "通用")
        """
        text_parts = []
        if func.name:
            text_parts.append(("name", func.name))

        # Collect text from multi-value fields (List[FieldValue])
        for fv in func.trigger:
            text_parts.append(("trigger", fv.desc))
        for fv in func.condition:
            text_parts.append(("condition", fv.desc))
        for fv in func.action:
            text_parts.append(("action", fv.desc))
        for fv in func.benefit:
            text_parts.append(("benefit", fv.desc))

        scores: Dict[str, float] = {}
        for domain, keywords in self.DOMAIN_KEYWORDS.items():
            score = 0.0
            for priority_label, text in text_parts:
                weight = 2.0 if priority_label == "name" else 1.0
                text_lower = text.lower()
                for keyword in keywords:
                    if keyword.lower() in text_lower:
                        length_bonus = len(keyword) / 10.0
                        score += weight + length_bonus
            scores[domain] = score

        if scores:
            best_domain = max(scores.items(), key=lambda x: x[1])
            if best_domain[1] > 0:
                return best_domain[0]

        return "通用"

    def classify_with_llm_fallback(self, func: Function) -> str:
        """
        Classify a function with LLM fallback for ambiguous cases.

        Currently returns "通用" as LLM fallback placeholder.
        """
        result = self.classify(func)
        if result == "通用":
            return "通用"
        return result
