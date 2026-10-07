"""입력 폴더 탐색 및 복사 완료(Settle) 검증."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from .config import (
    EXCLUDED_DIRS, IGNORED_NAMES, IGNORED_SUFFIXES, Config, logger,
)
from .models import Tracked


def is_ignored_file(name: str) -> bool:
    """전송 중/임시/숨김 파일 여부를 판단한다.

    Args:
        name: 파일명.

    Returns:
        무시 대상이면 True.
    """
    lower = name.lower()
    return (
        name.startswith((".", "~"))
        or lower in IGNORED_NAMES
        or lower.endswith(IGNORED_SUFFIXES)
    )


_DATE_DIR_RE = re.compile(r"^\d{6}$")


def iter_input_files(
    root: Path, recursive: bool = True, skip_date_dirs: bool = False,
) -> Iterator[Path]:
    """INPUT 트리를 순회한다. @eaDir, .raw 등은 진입 자체를 차단한다.

    Args:
        root: 수신 디렉터리.
        recursive: True 면 하위 디렉터리 재귀 탐색, False 면 루트 직하 파일만 탐색.
        skip_date_dirs: True 면 6자리 날짜 폴더(yymmdd) 탐색 제외(in-place 정렬 시 중복 방지).

    Yields:
        처리 후보 파일 경로.
    """
    if not recursive:
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if entry.is_file() and not is_ignored_file(entry.name):
                        yield Path(entry.path)
        except OSError as exc:
            logger.error("디렉터리 스캔 실패: %s (%s)", root, exc)
        return

    for dirpath, dirnames, filenames in os.walk(root):
        filtered: List[str] = []
        for d in dirnames:
            if d in EXCLUDED_DIRS or d.startswith("."):
                continue
            if skip_date_dirs and _DATE_DIR_RE.match(d):
                continue
            filtered.append(d)
        dirnames[:] = filtered
        for fn in filenames:
            if not is_ignored_file(fn):
                yield Path(dirpath) / fn


class SettleTracker:
    """복사 완료 이중 검증: (1) N회 연속 동일 size/mtime, (2) 임계 시간 경과."""

    def __init__(self, cfg: Config) -> None:
        """추적기를 초기화한다.

        Args:
            cfg: 설정.
        """
        self.cfg = cfg
        self._state: Dict[Path, Tracked] = {}

    def poll(self, files: List[Path]) -> List[Path]:
        """현재 파일 목록을 관측하여 안정화된 파일을 반환한다.

        Args:
            files: 이번 주기에 발견된 파일들.

        Returns:
            안정화 조건을 모두 충족한 파일 목록.
        """
        now = time.monotonic()
        wall = time.time()
        seen: Set[Path] = set()
        stable: List[Path] = []
        for p in files:
            try:
                st = p.stat()
            except OSError as exc:
                logger.debug("stat 실패(삭제/이동됨 추정): %s (%s)", p, exc)
                continue
            seen.add(p)
            prev = self._state.get(p)
            if prev and prev.size == st.st_size and prev.mtime_ns == st.st_mtime_ns:
                prev.rounds += 1
            else:
                self._state[p] = prev = Tracked(st.st_size, st.st_mtime_ns, 1, now)
                continue
            quiet_monotonic = now - prev.changed_at >= self.cfg.settle_threshold
            # 클라이언트가 mtime 을 과거로 보존하는 경우가 있으므로 mtime 이 미래/현재에
            # 가까우면(복사 진행 중) 추가로 대기한다.
            quiet_mtime = wall - st.st_mtime >= self.cfg.settle_threshold
            if prev.rounds >= self.cfg.stable_rounds and quiet_monotonic and quiet_mtime:
                stable.append(p)
        # 사라진 파일의 상태 제거 (메모리 누수 방지)
        for gone in set(self._state) - seen:
            del self._state[gone]
        return stable

    def snapshot(self, p: Path) -> Optional[Tuple[int, int]]:
        """안정화 확정 시점의 (size, mtime_ns) 를 반환한다."""
        t = self._state.get(p)
        return (t.size, t.mtime_ns) if t else None

    def forget(self, p: Path) -> None:
        """이동 완료된 파일의 추적 상태를 제거한다."""
        self._state.pop(p, None)
