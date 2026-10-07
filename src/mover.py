"""원자적 이동, 충돌 방지, 빈 폴더 정리 및 배치 처리 파이프라인."""
from __future__ import annotations

import errno
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .classifier import assign_names
from .config import EXCLUDED_DIRS, MOVE_LEVEL, Config, logger
from .exif import build_media, run_exiftool
from .models import BatchResult, MediaFile


def _fsync_dir(path: Path) -> None:
    """디렉터리 엔트리 변경을 디스크에 반영한다 (미지원 FS는 무시)."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_move(src: Path, dst: Path) -> None:
    """덮어쓰기 없이 원자적으로 파일을 이동한다.

    같은 파일시스템: ``os.link`` + ``unlink`` (dst 존재 시 FileExistsError 로
    원자적 실패 → rename 의 무음 덮어쓰기 방지).
    다른 파일시스템(EXDEV): 대상 폴더 내 임시 파일로 복사+fsync 후 link 로 공개.

    Args:
        src: 원본.
        dst: 목적지 (존재하지 않아야 함).

    Raises:
        FileExistsError: dst 가 이미 존재.
        OSError: 복사/검증 실패.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        os.unlink(src)
        _fsync_dir(dst.parent)
        return
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EMLINK):
            raise
        logger.debug("link 불가(%s) -> 복사 폴백: %s", errno.errorcode.get(exc.errno), src.name)

    tmp = dst.parent / f".{dst.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout, length=8 * 1024 * 1024)
            fout.flush()
            os.fsync(fout.fileno())
        shutil.copystat(src, tmp)
        if tmp.stat().st_size != src.stat().st_size:
            raise OSError(f"복사본 크기 불일치: {src}")
        try:
            os.link(tmp, dst)  # 원자적 공개 (dst 존재 시 실패)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise FileExistsError(dst) from exc
            if exc.errno in (errno.EPERM, errno.ENOTSUP):
                if dst.exists():
                    raise FileExistsError(dst) from exc
                os.rename(tmp, dst)  # 하드링크 미지원 FS 폴백
            else:
                raise
        _fsync_dir(dst.parent)
        os.unlink(src)  # 공개 및 검증 완료 후에만 원본 삭제
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


_DATE_DIR_RE = re.compile(r"^\d{6}$")


def cleanup_empty_dirs(cfg: Config) -> None:
    """INPUT 하위의 빈 디렉터리를 정리한다 (루트/제외 폴더/결과 날짜 폴더 유지).

    Why: 복사 직후 생성된 빈 폴더를 지우면 진행 중인 전송이 깨질 수 있으므로
    수정 시각이 SETTLE_THRESHOLD 이상 지난 폴더만 삭제한다.

    Args:
        cfg: 설정.
    """
    now = time.time()
    for dirpath, _dirnames, _ in os.walk(cfg.input_dir, topdown=False):
        p = Path(dirpath)
        if (
            p == cfg.input_dir
            or p.name in EXCLUDED_DIRS
            or _DATE_DIR_RE.match(p.name)
            or p.name in (cfg.movie_dir_name, cfg.raw_dir_name, ".raw")
        ):
            continue
        try:
            if now - p.stat().st_mtime >= cfg.settle_threshold:
                p.rmdir()  # 비어있지 않으면 OSError
                logger.info("빈 디렉터리 정리: %s", p)
        except OSError:
            pass


def process_batch(
    cfg: Config,
    files: List[Path],
    snapshot_of: Callable[[Path], Optional[Tuple[int, int]]],
    dry_run: bool = False,
    on_moved: Optional[Callable[[Path], None]] = None,
    inplace: bool = False,
) -> BatchResult:
    """파일군을 일괄 파싱하고 이동(또는 dry-run 계획 출력)한다.

    사이드카 파일(.xml, .xmp 등)도 메인 미디어와 함께 리네이밍 및 이동된다.

    Args:
        cfg: 설정.
        files: 처리 대상 파일 목록.
        snapshot_of: 파일 -> (size, mtime_ns) 스냅샷 제공자 (없으면 None 반환).
        dry_run: True 면 이동 없이 ``[DRY-RUN] src -> dst`` 만 로그로 남긴다.
        on_moved: 이동 완료 시 호출되는 콜백 (추적 상태 정리 등).
        inplace: True 면 yymmdd 폴더 생성 없이 현재 위치 기준 분류(oneshot).

    Returns:
        배치 결과 집계.
    """
    result = BatchResult()
    logger.info("배치 시작: %d개 파일%s", len(files), " (dry-run)" if dry_run else "")
    rows: Dict[str, dict] = run_exiftool(files)
    items: List[MediaFile] = []
    for p in files:
        snap = snapshot_of(p)
        if snap is None:
            result.skipped += 1
            continue
        items.append(build_media(p, rows.get(str(p), {}), snap))
    result.total = len(files)

    for m, dst, sidecar_plan in assign_names(cfg, items, inplace=inplace):
        rel_src = m.path.relative_to(cfg.input_dir)
        rel_dst = dst.relative_to(cfg.target_dir)

        # 소스와 목적지가 이미 동일한 경우 불필요한 이동 방지
        try:
            if m.path.resolve() == dst.resolve():
                logger.debug("이미 정렬된 위치 및 이름: %s", rel_src)
                result.skipped += 1
                continue
        except OSError:
            pass

        if dry_run:
            logger.info("[DRY-RUN] %s -> %s", rel_src, rel_dst)
            for ssrc, sdst in sidecar_plan:
                rel_ssrc = ssrc.relative_to(cfg.input_dir)
                rel_sdst = sdst.relative_to(cfg.target_dir)
                logger.info("[DRY-RUN] %s -> %s (사이드카)", rel_ssrc, rel_sdst)
            result.moved += 1
            result.moved_paths.append(m.path)
            continue
        try:
            st = m.path.stat()
            # 이동 직전 최종 재검증: 파싱 중 파일이 다시 변경되었으면 다음 주기로 연기
            if (st.st_size, st.st_mtime_ns) != (m.size, m.mtime_ns):
                logger.info("이동 직전 변경 감지, 다음 주기로 연기: %s", m.path)
                result.skipped += 1
                continue
            atomic_move(m.path, dst)

            # 연결된 사이드카 파일도 함께 이동
            for ssrc, sdst in sidecar_plan:
                try:
                    if ssrc.resolve() != sdst.resolve():
                        atomic_move(ssrc, sdst)
                        rel_ssrc = ssrc.relative_to(cfg.input_dir)
                        rel_sdst = sdst.relative_to(cfg.target_dir)
                        logger.log(MOVE_LEVEL, "%s -> %s (사이드카)", rel_ssrc, rel_sdst)
                except OSError as sexc:
                    logger.error("사이드카 이동 실패: %s -> %s (%s)", ssrc, sdst, sexc)

            if on_moved:
                on_moved(m.path)
            result.moved += 1
            result.moved_paths.append(m.path)
            logger.log(MOVE_LEVEL, "%s -> %s", rel_src, rel_dst)
        except FileExistsError:
            result.errors += 1
            logger.error("목적지 충돌(경쟁 상태), 다음 주기에 재시도: %s -> %s", m.path, dst)
        except OSError as exc:
            result.errors += 1
            logger.error("이동 실패 (다음 주기 재시도): %s -> %s (%s)", m.path, dst, exc)
    logger.info("배치 완료: %d/%d 이동", result.moved, result.total)
    return result
