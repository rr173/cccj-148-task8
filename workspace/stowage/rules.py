"""Dangerous-goods segregation rules (simplified IMDG-style, configurable).

Rule kinds:
  min_distance : 3D distance between the two slots must be >= value (metres)
  not_same_stack: the two boxes may not share a stack
  not_same_hold : the two boxes may not share a hold

Matching:
  EXACT_RULES  – keyed by a specific unordered class pair ("5.1" vs "1.4S")
  MAJOR_RULES  – keyed by major class number ("4" vs "5" matches 4.1/5.1 etc.)
"""
from typing import Optional, Tuple

EXACT_RULES = {
    frozenset({"5.1", "1.4S"}): ("min_distance", 12.0),
}

MAJOR_RULES = {
    frozenset({"3", "5"}): ("min_distance", 6.0),
    frozenset({"4", "5"}): ("min_distance", 6.0),
    frozenset({"3", "1"}): ("min_distance", 12.0),
    frozenset({"2", "5"}): ("min_distance", 3.0),
    frozenset({"7", "8"}): ("not_same_hold", None),
    frozenset({"6", "8"}): ("not_same_stack", None),
}

# Any two dangerous boxes (different classes) keep at least this apart.
DEFAULT_DG_MIN_DISTANCE = 3.0


def major(hazard_class: str) -> str:
    return hazard_class.split(".")[0].strip()


def rule_for(class_a: Optional[str], class_b: Optional[str]) -> Optional[Tuple[str, Optional[float]]]:
    """Return the segregation rule between two hazard classes, or None."""
    if not class_a or not class_b:
        return None
    if class_a == class_b:
        return None  # same class: no segregation requirement
    pair = frozenset({class_a, class_b})
    if pair in EXACT_RULES:
        return EXACT_RULES[pair]
    major_pair = frozenset({major(class_a), major(class_b)})
    if major_pair in MAJOR_RULES:
        return MAJOR_RULES[major_pair]
    return ("min_distance", DEFAULT_DG_MIN_DISTANCE)
