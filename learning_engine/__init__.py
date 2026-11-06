"""Standalone, evidence-grounded judgment, concept and procedure learning."""
from .engine import Engine
__version__ = "0.4.0"
from .work_session import WorkSession
__all__ = ["Engine", "WorkSession"]
