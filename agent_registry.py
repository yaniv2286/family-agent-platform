from collections import defaultdict


class AgentRegistry:
    def __init__(self):
        self._handlers = defaultdict(list)

    def register(self, event_type, handler):
        if handler not in self._handlers[event_type]:
            self._handlers[event_type].append(handler)

    def handlers_for(self, event_type):
        return tuple(self._handlers.get(event_type, ()))


agent_registry = AgentRegistry()
