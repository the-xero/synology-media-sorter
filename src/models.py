"""모듈 간 공유 데이터 모델."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional


@dataclass
class Tracked:
    """폴링 간 파일 상태 추적 정보."""

    size: int
    mtime_ns: int
    rounds: int = 1  # 동일 상태가 관측된 연속 횟수
    changed_at: float = field(default_factory=time.monotonic)


@dataclass
class MediaFile:
    """분류 대상 파일 한 건의 파싱 결과."""

    path: Path
    ext: str
    kind: str  # photo | video | raw | other
    stamp: datetime
    unique: Optional[str]
    model: Optional[str]
    size: int
    mtime_ns: int


@dataclass
class BatchResult:
    """배치 처리 결과 집계.

    Attributes:
        total: 처리 대상 총 건수.
        moved: 이동(dry-run 시 이동 예정) 건수.
        skipped: 이동 직전 변경 감지 등으로 연기/스킵한 건수.
        errors: 오류 건수.
        moved_paths: 이동 완료(예정) 원본 경로 목록.
    """

    total: int = 0
    moved: int = 0
    skipped: int = 0
    errors: int = 0
    moved_paths: List[Path] = field(default_factory=list)

    def merge(self, other: "BatchResult") -> None:
        """다른 결과를 누적한다."""
        self.total += other.total
        self.moved += other.moved
        self.skipped += other.skipped
        self.errors += other.errors
        self.moved_paths.extend(other.moved_paths)
