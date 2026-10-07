"""입력 폴더 탐색 및 복사 완료(Settle) 검증."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from .config import (
    EXCLUDED_DIRS, IGNORED_NAMES, IGNORED_SUFFIXES, SIDECAR_EXTS, Config, logger,
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


def is_sidecar_file(name: str) -> bool:
    """사이드카 파일(.xml, .xmp, .aae 등) 여부를 판단한다.

    Args:
        name: 파일명.

    Returns:
        사이드카 확장자이면 True.
    """
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in SIDECAR_EXTS


_DIR_FILES_CACHE: Dict[Path, Set[str]] = {}


def get_dir_files(parent: Path) -> Set[str]:
    """디렉터리 내 파일명 집합을 캐싱하여 반환한다 (디스크 I/O 최소화)."""
    if parent not in _DIR_FILES_CACHE:
        try:
            with os.scandir(parent) as it:
                _DIR_FILES_CACHE[parent] = {entry.name for entry in it if entry.is_file()}
        except OSError:
            _DIR_FILES_CACHE[parent] = set()
    return _DIR_FILES_CACHE[parent]


def clear_dir_files_cache() -> None:
    """디렉터리 파일 캐시를 비운다."""
    _DIR_FILES_CACHE.clear()


def find_sidecars(media_path: Path, dir_files: Optional[Set[str]] = None) -> List[Path]:
    """미디어 파일에 연결된 사이드카 파일 목록을 반환한다 (메모리 O(1) 검색).

    예: ``IMG_0001.CR3`` -> ``IMG_0001.xmp``, ``IMG_0001.xml``, ``IMG_0001.CR3.xmp`` 등.

    Args:
        media_path: 메인 미디어 파일 경로.
        dir_files: 해당 디렉터리의 파일명 집합 (생략 시 캐시 자동 조회).

    Returns:
        존재하는 사이드카 파일 경로 리스트.
    """
    sidecars: List[Path] = []
    parent = media_path.parent
    files_in_dir = dir_files if dir_files is not None else get_dir_files(parent)
    stem = media_path.stem
    full_name = media_path.name

    for ext in SIDECAR_EXTS:
        # 패턴 1: {stem}.{ext} (대소문자 확장자 대응)
        for cand_name in (f"{stem}.{ext}", f"{stem}.{ext.upper()}"):
            if cand_name in files_in_dir:
                p = parent / cand_name
                if p not in sidecars:
                    sidecars.append(p)
        # 패턴 2: {full_name}.{ext} (예: IMG_0001.CR3.xmp)
        for cand_name in (f"{full_name}.{ext}", f"{full_name}.{ext.upper()}"):
            if cand_name in files_in_dir:
                p = parent / cand_name
                if p not in sidecars:
                    sidecars.append(p)
    return sidecars


_DATE_DIR_RE = re.compile(r"^\d{4}$|^\d{4}-\d{2}-\d{2}$|^\d{6}$")


def iter_input_files(
    root: Path,
    recursive: bool = True,
    skip_date_dirs: bool = False,
    extra_excluded_dirs: Optional[Set[str]] = None,
) -> Iterator[Path]:
    """INPUT 트리를 순회한다. @eaDir, raw, movie 등은 진입 자체를 차단한다.

    사이드카 파일(.xml, .xmp 등)은 메인 미디어 파일에 귀속되므로 직접 반환하지 않는다.

    Args:
        root: 수신 디렉터리.
        recursive: True 면 하위 디렉터리 재귀 탐색, False 면 루트 직하 파일만 탐색.
        skip_date_dirs: True 면 6자리 날짜 폴더(yymmdd) 탐색 제외(in-place 정렬 시 중복 방지).
        extra_excluded_dirs: 추가로 제외할 디렉터리 이름 집합 (예: raw, movie).

    Yields:
        처리 후보 메인 미디어 파일 경로.
    """
    excluded = set(EXCLUDED_DIRS)
    if extra_excluded_dirs:
        excluded.update(extra_excluded_dirs)

    if not recursive:
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if (
                        entry.is_file()
                        and not is_ignored_file(entry.name)
                        and not is_sidecar_file(entry.name)
                    ):
                        yield Path(entry.path)
        except OSError as exc:
            logger.error("디렉터리 스캔 실패: %s (%s)", root, exc)
        return

    for dirpath, dirnames, filenames in os.walk(root):
        filtered: List[str] = []
        for d in dirnames:
            if d in excluded or d.startswith("."):
                continue
            if skip_date_dirs and _DATE_DIR_RE.match(d):
                continue
            filtered.append(d)
        dirnames[:] = filtered
        for fn in filenames:
            if not is_ignored_file(fn) and not is_sidecar_file(fn):
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

    def poll(self, files: List[Path], allow_past_mtime_instant: bool = True) -> List[Path]:
        """현재 파일 목록을 관측하여 안정화된 파일을 반환한다.

        Args:
            files: 이번 주기에 발견된 파일들.
            allow_past_mtime_instant: 파일 mtime 이 이미 settle_threshold 이전이면 1회차 즉시 통과.

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

            # 최적화: 파일 수정 시각(mtime)이 이미 충분히 과거라면(진행 중인 복사가 아님) 즉시 완료 처리
            quiet_mtime = wall - st.st_mtime >= self.cfg.settle_threshold
            if allow_past_mtime_instant and quiet_mtime:
                self._state[p] = Tracked(st.st_size, st.st_mtime_ns, self.cfg.stable_rounds, now)
                stable.append(p)
                continue

            prev = self._state.get(p)
            if prev and prev.size == st.st_size and prev.mtime_ns == st.st_mtime_ns:
                prev.rounds += 1
            else:
                self._state[p] = prev = Tracked(st.st_size, st.st_mtime_ns, 1, now)
                continue
            quiet_monotonic = now - prev.changed_at >= self.cfg.settle_threshold
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
