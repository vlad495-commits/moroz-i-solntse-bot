"""Tiny in-process metrics registry."""

from __future__ import annotations

from collections import defaultdict


ALLOWED_LABELS = frozenset({"channel", "provider", "queue", "status", "kind"})


class MetricsRegistry:
    def __init__(self) -> None:
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)

    def increment(
        self,
        name: str,
        value: float = 1,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._values[self._key(name, labels)] += float(value)

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._values[self._key(name, labels)] = float(value)

    def snapshot(self) -> dict[str, float]:
        return {self._format_name(name, labels): value for (name, labels), value in self._values.items()}

    def to_prometheus(self) -> str:
        lines = [
            f"{self._format_name(name, labels)} {value}"
            for (name, labels), value in sorted(self._values.items())
        ]
        return "\n".join(lines) + ("\n" if lines else "")

    def _key(
        self,
        name: str,
        labels: dict[str, str] | None,
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        if not name.replace("_", "").isalnum():
            raise ValueError("metric name must contain only letters, digits and underscores")
        clean_labels = tuple(sorted((labels or {}).items()))
        for label, value in clean_labels:
            if label not in ALLOWED_LABELS:
                raise ValueError("label is not allowlisted")
            if '"' in value or "\n" in value:
                raise ValueError("label value must be simple text")
        return name, clean_labels

    @staticmethod
    def _format_name(name: str, labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return name
        rendered = ",".join(f'{key}="{value}"' for key, value in labels)
        return f"{name}{{{rendered}}}"


registry = MetricsRegistry()
