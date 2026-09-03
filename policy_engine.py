from memory_manager import PRECEDENCE


class PolicyEngine:
    def rank(self, category, priority=0):
        return PRECEDENCE.get(category, 0) + int(priority)

    def resolve(self, candidates):
        return sorted(
            candidates,
            key=lambda item: (self.rank(item.get("category", "preference"), item.get("priority", 0)),
                              item.get("confidence", 1.0)),
            reverse=True,
        )

    def allowed(self, recommendation, constraints):
        text = recommendation.lower()
        return not any(str(constraint).lower() in text for constraint in constraints if constraint)


policy_engine = PolicyEngine()
