"""통합 진입점: ``python -m src.main {oneshot|daemon}``."""
from __future__ import annotations

import argparse
import dataclasses
import shutil
import signal
import sys
import threading
from pathlib import Path
from typing import List, Optional

from .config import Config, logger, setup_logging
from .modes.daemon import run_daemon
from .modes.oneshot import DEFAULT_MAX_WAIT, run_oneshot


def build_parser() -> argparse.ArgumentParser:
    """argparse 파서(서브커맨드 oneshot/daemon)를 구성한다.

    Returns:
        구성된 ArgumentParser.
    """
    parser = argparse.ArgumentParser(prog="media-sorter", description="Synology 미디어 자동 분류/리네이밍")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("oneshot", help="INPUT_DIR 을 1회 처리하고 종료 (in-place 정렬)")
    one.add_argument("--dry-run", action="store_true",
                     help="이동 없이 계획만 출력 (Settle Check 생략)")
    one.add_argument("--max-wait", type=float, default=DEFAULT_MAX_WAIT,
                     help="안정화 대기 상한(초), 기본 %(default)s")
    one.add_argument("-r", "--recursive", action="store_true",
                     help="하위 폴더까지 재귀 탐색 (기본값: 루트 직하 1단계만 탐색)")
    one.add_argument("--no-settle", action="store_true",
                     help="Settle Check(안정화 대기)를 건너뛰고 즉시 처리")

    sub.add_parser("daemon", help="INPUT_DIR 상시 감시 (Settle Check 기반)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 진입점. SIGTERM/SIGINT 로 정상 종료한다.

    Args:
        argv: 인자 목록 (None 이면 sys.argv).

    Returns:
        프로세스 종료 코드.
    """
    args = build_parser().parse_args(argv)
    setup_logging()
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        logger.error("설정 오류: %s", exc)
        return 2
    if shutil.which("exiftool") is None:
        logger.error("exiftool 바이너리를 찾을 수 없습니다.")
        return 2

    # oneshot 모드는 -v {대상폴더}:/input 단일 마운트를 기준으로 in-place 정렬 강제
    if args.command == "oneshot":
        container_input = Path("/input")
        target_path = container_input if container_input.is_dir() else cfg.input_dir
        cfg = dataclasses.replace(cfg, input_dir=target_path, target_dir=target_path)
        if not cfg.input_dir.is_dir():
            logger.error(
                "디렉터리가 존재하지 않습니다. Docker 실행 시 '-v {대상폴더}:/input' 마운트를 확인해주세요: %s",
                cfg.input_dir,
            )
            return 2
    else:
        for d in (cfg.input_dir, cfg.target_dir):
            if not d.is_dir():
                logger.error("디렉터리가 존재하지 않습니다(마운트 확인): %s", d)
                return 2

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    if args.command == "daemon":
        return run_daemon(cfg, stop)
    return run_oneshot(
        cfg, args.dry_run, stop, args.max_wait, recursive=args.recursive, no_settle=args.no_settle,
    )


if __name__ == "__main__":
    sys.exit(main())
