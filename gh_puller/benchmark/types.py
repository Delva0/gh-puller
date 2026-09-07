"""Define data exchanged through the benchmark endpoint contract."""

from dataclasses import dataclass


@dataclass
class Answer:
    text: str
