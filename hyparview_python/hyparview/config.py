from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    active_size: int = 5
    passive_size: int = 30
    arwl: int = 6 # Active Random Walk Length
    prwl: int = 3 # Passive Random Walk Length
    shuffle_ttl: int = 6 # Time To Live
    k_a: int = 3 # shuffle_active
    k_p: int = 4 # shuffle_passive

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.active_size < 1 or self.prwl > self.arwl:
            raise ValueError("Require active_size >= 1 and prwl <= arwl")

