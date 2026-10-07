"""daemon 모드: INPUT_DIR 을 상시 폴링하며 Settle Check 통과 파일을 처리한다."""
from __future__ import annotations

import threading

from ..config import Config, logger
from ..mover import cleanup_empty_dirs, process_batch
from ..settle import SettleTracker, iter_input_files


def run_daemon(cfg: Config, stop: threading.Event) -> int:
    """상시 감시 루프를 실행한다.

    Args:
        cfg: 설정.
        stop: 설정되면 루프를 정상 종료하는 이벤트.

    Returns:
        프로세스 종료 코드.
    """
    logger.info(
        "daemon 시작: input=%s target=%s interval=%ss settle=%ss rounds=%d",
        cfg.input_dir, cfg.target_dir, cfg.check_interval, cfg.settle_threshold, cfg.stable_rounds,
    )
    tracker = SettleTracker(cfg)
    extra_excluded = {cfg.raw_dir_name, cfg.movie_dir_name, "raw", "movie", ".raw"}
    while not stop.is_set():
        try:
            files = list(iter_input_files(cfg.input_dir, extra_excluded_dirs=extra_excluded))
            stable = tracker.poll(files)
            if stable:
                result = process_batch(cfg, stable, tracker.snapshot, on_moved=tracker.forget)
                if result.moved:
                    cleanup_empty_dirs(cfg)
        except Exception:  # 데몬 보호: 예기치 못한 오류에도 루프 지속
            logger.exception("주기 처리 중 예외 발생")
        stop.wait(cfg.check_interval)
    logger.info("종료")
    return 0
