"""Known CPU/RAM inputs and a local, bounded degree-budget heuristic.

These are synthetic configuration inputs, not resource measurements or measured
message-processing rates. RAM is the memory input; it is not counted twice.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .node import Node


def _positive_number(name: str, value: float):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a finite positive number")


@dataclass(frozen=True)
class ResourceProfile:
    """Externally supplied comparable CPU units and RAM in GiB.

    CPU units can be core equivalents on equally capable cores. For different
    processor generations the experiment must supply comparable units itself.
    """

    cpu: float
    ram_gib: float

    def __post_init__(self):
        _positive_number("cpu", self.cpu)
        _positive_number("ram_gib", self.ram_gib)


@dataclass(frozen=True)
class CapacityPolicy:
    """A modeling choice, not a formula from HyParView or a calibrated model.

    c = min(CPU / cpu_reference, RAM / ram_reference_gib)
    k = clip(round_half_up(reference_degree + degree_per_doubling * log2(c)))

    Defaults map (2 CPU, 4 GiB), (4, 8), (8, 16) to limits 3, 5, 7.
    The logarithm limits concentration; degree is not proportional to capacity.
    The fixed references are local configuration, never a live network average.
    """

    cpu_reference: float = 2
    ram_reference_gib: float = 4
    reference_degree: int = 3
    degree_per_doubling: float = 2
    min_degree: int = 3
    max_degree: int = 9

    def __post_init__(self):
        for name in ("cpu_reference", "ram_reference_gib", "degree_per_doubling"):
            _positive_number(name, getattr(self, name))
        for name in ("reference_degree", "min_degree", "max_degree"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.min_degree > self.max_degree:
            raise ValueError("min_degree must be <= max_degree")

    def score(self, profile: ResourceProfile) -> float:
        return min(profile.cpu / self.cpu_reference,
                   profile.ram_gib / self.ram_reference_gib)

    def active_limit(self, profile: ResourceProfile) -> int:
        # Subtract logs instead of logging a potentially underflowed ratio.
        log_capacity = min(math.log2(profile.cpu) - math.log2(self.cpu_reference),
                           math.log2(profile.ram_gib) - math.log2(self.ram_reference_gib))
        raw = self.reference_degree + self.degree_per_doubling * log_capacity
        bounded = min(self.max_degree, max(self.min_degree, raw))
        return math.floor(bounded + 0.5)


def apply_capacity(node: Node, profile: ResourceProfile,
                   policy: CapacityPolicy | None = None) -> int:
    """Update this node's limit; its existing local repair rules do the work.

    Shrinking disconnects excess neighbors. Growing queues one bounded passive
    candidate sweep. Success is not guaranteed and no global peers are supplied.
    Call Simulator.drain after applying a batch of independent resource events.
    """
    limit = (policy or CapacityPolicy()).active_limit(profile)
    if limit != node.config.active_size:
        node.set_active_size(limit)
    return limit
