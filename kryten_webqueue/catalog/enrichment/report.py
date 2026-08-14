from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StepResult:
    processed: int = 0
    changed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def record_error(self, msg: str) -> None:
        self.failed += 1
        if len(self.errors) < 20:
            self.errors.append(msg)


@dataclass
class EnrichmentReport:
    steps_run: list[str]
    total_items: int
    by_step: dict[str, StepResult]
    elapsed_sec: float
    dry_run: bool

    def to_dict(self) -> dict:
        return {
            "steps_run": self.steps_run,
            "total_items": self.total_items,
            "elapsed_sec": round(self.elapsed_sec, 1),
            "dry_run": self.dry_run,
            "steps": {
                name: {
                    "processed": r.processed,
                    "changed": r.changed,
                    "skipped": r.skipped,
                    "failed": r.failed,
                    "errors": r.errors[:5],
                }
                for name, r in self.by_step.items()
            },
        }
