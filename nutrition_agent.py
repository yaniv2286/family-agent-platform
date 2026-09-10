"""Backward-compatible shim for the full Nutrition Agent.

The real implementation now lives in agents.nutrition_agent and is loaded
as a first-class agent on the agent mesh. Importing this module still
registers the event subscriber for existing fitness integrations.
"""
from agents.nutrition_agent import handle_workout_completed, router

__all__ = ["handle_workout_completed", "router"]
