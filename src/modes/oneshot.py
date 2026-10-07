"""oneshot 모드: INPUT_DIR 전체를 1회 처리하고 종료한다.

- dry-run: Settle Check 없이 즉시 계획만 출력(이동/폴더 정리 없음) 후 요약.
- 실제 실행: Settle Check 를 통과한 파일만 이동. 불안정 파일은 최대 대기 시간 후 스킵.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from ..config import Config, logger
from ..models import BatchResult
from ..mover import cleanup_empty_dirs, process_batch
from ..settle import SettleTracker, iter_input_files

DEFAULT_MAX_WAIT = 600.0  # 실제 실행 시 안정화 대기 상한(초)


def _stat_snapshot(p: Path) -> Optional[Tuple[int, int]]:
    """dry-run 용 즉시 (size, mtime_ns) 스냅샷을 반환한다."""
    try:
        st = os.stat(p)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _log_summary(res: BatchResult, dry_run: bool) -> None:
    """처리 요약을 로그로 남긴다."""
    label = "이동예정" if dry_run else "이동"
    logger.info(
        "요약%s: 총 %d / %s %d / 스킵 %d / 오류 %d",
        " [DRY-RUN]" if dry_run else "", res.total, label, res.moved, res.skipped, res.errors,
    )


def run_oneshot(
    cfg: Config,
    dry_run: bool,
    stop: threading.Event,
    max_wait: float = DEFAULT_MAX_WAIT,
    recursive: bool = False,
) -> int:
    """일회성 처리를 실행한다.

    oneshot 은 INPUT_DIR 하나만 사용하여 대상 디렉터리도 INPUT_DIR 로 처리한다(in-place 정렬).

    Args:
        cfg: 설정 (target_dir 은 input_dir 과 동일하게 설정됨).
        dry_run: True 면 이동 없이 계획만 출력.
        stop: 설정되면 대기 루프를 중단하는 이벤트.
        max_wait: 실제 실행 시 안정화 대기 상한(초).
        recursive: True 면 하위 디렉터리까지 재귀 탐색, False 면 루트 직하만 1단계 탐색.

    Returns:
        종료 코드 (오류 발생 시 1, 그 외 0).
    """
    logger.info(
        "oneshot 시작: input=%s (in-place) dry_run=%s recursive=%s",
        cfg.input_dir, dry_run, recursive,
    )
    files = list(iter_input_files(cfg.input_dir, recursive=recursive, skip_date_dirs=True))
    total = BatchResult()

    if dry_run:
        # Why: dry-run 은 파일을 건드리지 않으므로 안정화 대기 없이 즉시 계획을 보여준다.
        if files:
            total.merge(process_batch(cfg, files, _stat_snapshot, dry_run=True))
        _log_summary(total, dry_run=True)
        return 0

    tracker = SettleTracker(cfg)
    deadline = time.monotonic() + max_wait
    pending = files
    while pending and not stop.is_set():
        stable = tracker.poll(pending)
        if stable:
            total.merge(process_batch(cfg, stable, tracker.snapshot, on_moved=tracker.forget))
        # 이동된 파일은 제외하고 다시 탐색 (이동 중 새로 도착한 파일도 포함)
        pending = list(iter_input_files(cfg.input_dir, recursive=recursive, skip_date_dirs=True))
        if not pending:
            break
        if time.monotonic() >= deadline:
            logger.warning("안정화 대기 시간 초과(%ss): 미처리 %d개 스킵", max_wait, len(pending))
            total.skipped += len(pending)
            total.total += len(pending)
            break
        stop.wait(cfg.check_interval)

    if total.moved:
        cleanup_empty_dirs(cfg)
    _log_summary(total, dry_run=False)
    return 1 if total.errors else 0
