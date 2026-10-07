"""Synology DSM용 미디어 자동 분류/리네이밍 데몬.

INPUT_DIR 을 주기적으로 폴링하여 복사가 완료(안정화)된 파일군을 묶어
ExifTool 로 일시/고유번호를 파싱한 뒤, TARGET_DIR/{yymmdd}/ 구조로
``{yymmdd}-{hhmmss}-{고유번호}.{ext}`` 이름으로 원자적 이동한다.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
PHOTO_EXTS: Set[str] = {"jpg", "jpeg", "heic", "png"}
VIDEO_EXTS: Set[str] = {"mp4", "mov", "m4v", "avi"}
RAW_EXTS: Set[str] = {"cr2", "cr3", "nef", "arw", "dng", "raf", "rw2", "orf"}

# 탐색에서 완전히 제외할 디렉터리 (Synology 메타/휴지통 및 분류 결과 폴더)
EXCLUDED_DIRS: Set[str] = {"@eaDir", ".raw", "#recycle", "@tmp", "#snapshot"}
# 전송 중/임시/시스템 파일은 무시 (원본 그대로 유지)
IGNORED_NAMES: Set[str] = {".ds_store", "thumbs.db", "desktop.ini"}
IGNORED_SUFFIXES: Tuple[str, ...] = (
    ".part", ".tmp", ".filepart", ".crdownload", ".!qb", ".swp", ".temp",
)

# 고유번호 태그 우선순위 (ExifTool JSON 키)
UNIQUE_TAGS: Tuple[str, ...] = ("FileIndex", "ImageNumber", "ShutterCount")
PHOTO_DATE_TAGS: Tuple[str, ...] = ("DateTimeOriginal", "CreateDate")
VIDEO_DATE_TAGS: Tuple[str, ...] = ("CreateDate", "MediaCreateDate")

EXIFTOOL_CHUNK = 200  # exiftool 1회 호출당 파일 수
MOVE_LEVEL = 25  # INFO(20)와 WARNING(30) 사이 커스텀 [MOVE] 레벨

logging.addLevelName(MOVE_LEVEL, "MOVE")
logger = logging.getLogger("sorter")


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    """환경 변수 기반 설정값.

    Attributes:
        input_dir: 수신 디렉터리.
        target_dir: 대상(Photos) 디렉터리.
        check_interval: 폴링 주기(초).
        settle_threshold: 변경 없이 유지되어야 하는 최소 시간(초).
        stable_rounds: 동일 상태가 연속 유지되어야 하는 폴링 횟수.
    """

    input_dir: Path
    target_dir: Path
    check_interval: float
    settle_threshold: float
    stable_rounds: int

    @classmethod
    def from_env(cls) -> "Config":
        """환경 변수에서 설정을 로드한다.

        Returns:
            Config 인스턴스.

        Raises:
            ValueError: 숫자 변환 실패 또는 값 범위 오류.
        """
        try:
            cfg = cls(
                input_dir=Path(os.environ.get("INPUT_DIR", "/input")),
                target_dir=Path(os.environ.get("TARGET_DIR", "/photos")),
                check_interval=float(os.environ.get("CHECK_INTERVAL", "15")),
                settle_threshold=float(os.environ.get("SETTLE_THRESHOLD", "30")),
                stable_rounds=int(os.environ.get("STABLE_ROUNDS", "2")),
            )
        except ValueError as exc:
            raise ValueError(f"환경 변수 숫자 형식 오류: {exc}") from exc
        if cfg.check_interval <= 0 or cfg.stable_rounds < 1 or cfg.settle_threshold < 0:
            raise ValueError("CHECK_INTERVAL>0, STABLE_ROUNDS>=1, SETTLE_THRESHOLD>=0 이어야 합니다.")
        return cfg


# ---------------------------------------------------------------------------
# 데이터 모델
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 탐색 / 안정화 검증
# ---------------------------------------------------------------------------
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


def iter_input_files(root: Path) -> Iterator[Path]:
    """INPUT 트리를 순회한다. @eaDir, .raw 등은 진입 자체를 차단한다.

    Args:
        root: 수신 디렉터리.

    Yields:
        처리 후보 파일 경로.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        # in-place 수정으로 하위 탐색 트리에서 제외 (숨김 디렉터리도 제외)
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")]
        for fn in filenames:
            if not is_ignored_file(fn):
                yield Path(dirpath) / fn


class SettleTracker:
    """복사 완료 이중 검증: (1) N회 연속 동일 size/mtime, (2) 임계 시간 경과."""

    def __init__(self, cfg: Config) -> None:
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


# ---------------------------------------------------------------------------
# 메타데이터 파싱
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def parse_exif_date(value: object) -> Optional[datetime]:
    """ExifTool 날짜 문자열을 datetime 으로 변환한다.

    Args:
        value: ``YYYY:MM:DD HH:MM:SS[.sss][+TZ]`` 형태의 값.

    Returns:
        naive datetime. 형식 오류/영(0000) 날짜면 None.
    """
    if not isinstance(value, str):
        return None
    m = _DATE_RE.match(value.strip())
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()))
    except ValueError:  # 0000:00:00 등
        return None


def normalize_number(raw: object) -> Optional[str]:
    """고유번호를 숫자만 추려 마지막 4자리, zero-pad 로 정규화한다.

    Args:
        raw: 태그 값 또는 숫자 문자열.

    Returns:
        4자리 문자열. 숫자가 없거나 0이면 None.
    """
    digits = re.sub(r"\D", "", str(raw)) if raw is not None else ""
    if not digits or int(digits) == 0:
        return None
    return digits[-4:].zfill(4)


def number_from_filename(stem: str) -> Optional[str]:
    """원본 파일명 마지막 연속 숫자에서 고유번호를 추출한다.

    Args:
        stem: 확장자 제외 파일명 (예: ``IMG_1234``).

    Returns:
        정규화된 4자리 번호 또는 None.
    """
    m = re.search(r"(\d+)(?!.*\d)", stem)
    return normalize_number(m.group(1)) if m else None


def run_exiftool(paths: List[Path]) -> Dict[str, dict]:
    """ExifTool 을 배치 호출하여 메타데이터를 JSON 으로 수집한다.

    Args:
        paths: 대상 파일 목록.

    Returns:
        ``SourceFile`` 문자열 -> 태그 dict 매핑. 실패한 청크는 비어 있다.
    """
    result: Dict[str, dict] = {}
    tags = [*PHOTO_DATE_TAGS, *VIDEO_DATE_TAGS, *UNIQUE_TAGS, "Model"]
    for i in range(0, len(paths), EXIFTOOL_CHUNK):
        chunk = paths[i : i + EXIFTOOL_CHUNK]
        # 파일 경로는 stdin(-@) 으로 전달하여 인자 길이 제한 및 특수문자 문제 회피
        cmd = [
            "exiftool", "-json", "-q", "-m", "-api", "QuickTimeUTC=1",
            "-charset", "filename=utf8", *[f"-{t}" for t in tags], "-@", "-",
        ]
        try:
            proc = subprocess.run(
                cmd, input="\n".join(str(p) for p in chunk),
                capture_output=True, text=True, encoding="utf-8", timeout=600,
            )
            for row in json.loads(proc.stdout or "[]"):
                result[row["SourceFile"]] = row
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
            logger.error("ExifTool 실행/파싱 실패 (%d개 파일, mtime 폴백): %s", len(chunk), exc)
    return result


def classify(ext: str) -> str:
    """확장자로 미디어 종류를 판별한다."""
    if ext in RAW_EXTS:
        return "raw"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in PHOTO_EXTS:
        return "photo"
    return "other"


def build_media(path: Path, row: dict, snap: Tuple[int, int]) -> MediaFile:
    """ExifTool 결과로 MediaFile 을 구성한다 (일시 폴백 포함).

    Args:
        path: 원본 경로.
        row: ExifTool JSON row (없으면 빈 dict).
        snap: 안정화 시점의 (size, mtime_ns).

    Returns:
        MediaFile.
    """
    ext = path.suffix.lstrip(".").lower()
    kind = classify(ext)
    date_tags = VIDEO_DATE_TAGS if kind == "video" else PHOTO_DATE_TAGS
    stamp: Optional[datetime] = None
    for tag in date_tags:
        stamp = parse_exif_date(row.get(tag))
        if stamp:
            break
    if stamp is None:
        # 메타데이터 누락 시 파일 수정일시 폴백 (TZ 환경변수 기준 로컬 시각)
        stamp = datetime.fromtimestamp(snap[1] / 1e9)
        logger.info("메타데이터 일시 없음 -> mtime 폴백: %s", path.name)

    unique: Optional[str] = None
    if kind in ("photo", "raw"):  # 영상은 시퀀스 번호 사용
        for tag in UNIQUE_TAGS:
            unique = normalize_number(row.get(tag))
            if unique:
                break
        if unique is None:
            unique = number_from_filename(path.stem)
    model = row.get("Model")
    return MediaFile(path, ext, kind, stamp, unique, str(model) if model else None, snap[0], snap[1])


# ---------------------------------------------------------------------------
# 목적지 계산
# ---------------------------------------------------------------------------
def safe_token(text: str) -> str:
    """파일명에 안전한 토큰으로 변환한다 (공백->_, 특수문자 제거)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "", text.strip().replace(" ", "_"))


def dest_dir_for(cfg: Config, m: MediaFile) -> Path:
    """분류 규칙에 따른 대상 디렉터리를 반환한다."""
    base = cfg.target_dir / m.stamp.strftime("%y%m%d")
    if m.kind == "video":
        return base / "movie"
    if m.kind == "raw":
        return base / ".raw"
    return base


def assign_names(cfg: Config, items: List[MediaFile]) -> List[Tuple[MediaFile, Path]]:
    """파일별 최종 목적지 경로를 계산한다 (시퀀스/충돌 처리 포함).

    - 고유번호 없는 미디어: 동일 초 그룹 내 정렬 순서대로 001, 002 ... 부여.
    - 기타(미지원 확장자): 원본 파일명 유지.
    - 충돌: 카메라 모델 토큰을 먼저 시도, 이후 ``_1``, ``_2`` 서픽스.

    Args:
        cfg: 설정.
        items: 파싱된 파일들.

    Returns:
        (MediaFile, 목적지 경로) 리스트.
    """
    # 시퀀스 번호는 결정론적이도록 (일시, 경로) 정렬 후 그룹별 카운트
    ordered = sorted(items, key=lambda x: (x.stamp, str(x.path)))
    seq_counter: Dict[str, int] = defaultdict(int)
    reserved: Set[Path] = set()
    plan: List[Tuple[MediaFile, Path]] = []

    for m in ordered:
        ddir = dest_dir_for(cfg, m)
        stamp = m.stamp.strftime("%y%m%d-%H%M%S")
        if m.kind == "other":
            stem, ext = m.path.stem, m.path.suffix  # 원본명 유지
        else:
            if m.unique is None:
                seq_counter[stamp] += 1
                m.unique = f"{seq_counter[stamp]:03d}"
            stem, ext = f"{stamp}-{m.unique}", "." + m.ext.upper()

        candidates = [stem]
        if m.model and m.kind != "other":
            candidates.append(f"{stem}-{safe_token(m.model)}")
        final: Optional[Path] = None
        for cand in candidates:
            p = ddir / f"{cand}{ext}"
            if p not in reserved and not p.exists():
                final = p
                break
        n = 1
        while final is None:  # 마지막 수단: _N 서픽스 (덮어쓰기 절대 금지)
            p = ddir / f"{candidates[-1]}_{n}{ext}"
            if p not in reserved and not p.exists():
                final = p
            n += 1
        reserved.add(final)
        plan.append((m, final))
    return plan


# ---------------------------------------------------------------------------
# 원자적 이동
# ---------------------------------------------------------------------------
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


def cleanup_empty_dirs(cfg: Config) -> None:
    """INPUT 하위의 빈 디렉터리를 정리한다 (루트/제외 폴더 유지).

    Why: 복사 직후 생성된 빈 폴더를 지우면 진행 중인 전송이 깨질 수 있으므로
    수정 시각이 SETTLE_THRESHOLD 이상 지난 폴더만 삭제한다.
    """
    now = time.time()
    for dirpath, dirnames, _ in os.walk(cfg.input_dir, topdown=False):
        p = Path(dirpath)
        if p == cfg.input_dir or p.name in EXCLUDED_DIRS:
            continue
        try:
            if now - p.stat().st_mtime >= cfg.settle_threshold:
                p.rmdir()  # 비어있지 않으면 OSError
                logger.info("빈 디렉터리 정리: %s", p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 메인 루프
# ---------------------------------------------------------------------------
def process_batch(cfg: Config, tracker: SettleTracker, stable: List[Path]) -> int:
    """안정화된 파일군을 일괄 파싱하고 이동한다.

    Args:
        cfg: 설정.
        tracker: 안정화 추적기.
        stable: 안정화 완료 파일 목록.

    Returns:
        성공적으로 이동한 파일 수.
    """
    logger.info("배치 시작: %d개 파일", len(stable))
    rows = run_exiftool(stable)
    items: List[MediaFile] = []
    for p in stable:
        snap = tracker.snapshot(p)
        if snap is None:
            continue
        items.append(build_media(p, rows.get(str(p), {}), snap))

    moved = 0
    for m, dst in assign_names(cfg, items):
        try:
            st = m.path.stat()
            # 이동 직전 최종 재검증: 파싱 중 파일이 다시 변경되었으면 다음 주기로 연기
            if (st.st_size, st.st_mtime_ns) != (m.size, m.mtime_ns):
                logger.info("이동 직전 변경 감지, 다음 주기로 연기: %s", m.path)
                continue
            atomic_move(m.path, dst)
            tracker.forget(m.path)
            moved += 1
            logger.log(MOVE_LEVEL, "%s -> %s", m.path.relative_to(cfg.input_dir), dst.relative_to(cfg.target_dir))
        except FileExistsError:
            logger.error("목적지 충돌(경쟁 상태), 다음 주기에 재시도: %s -> %s", m.path, dst)
        except OSError as exc:
            logger.error("이동 실패 (다음 주기 재시도): %s -> %s (%s)", m.path, dst, exc)
    logger.info("배치 완료: %d/%d 이동", moved, len(items))
    return moved


def setup_logging() -> None:
    """``[LEVEL] 메시지`` 형식의 콘솔 로깅을 구성한다."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), handlers=[handler])


def main() -> int:
    """데몬 진입점. SIGTERM/SIGINT 로 정상 종료한다.

    Returns:
        프로세스 종료 코드.
    """
    setup_logging()
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        logger.error("설정 오류: %s", exc)
        return 2
    if shutil.which("exiftool") is None:
        logger.error("exiftool 바이너리를 찾을 수 없습니다.")
        return 2
    for d in (cfg.input_dir, cfg.target_dir):
        if not d.is_dir():
            logger.error("디렉터리가 존재하지 않습니다(마운트 확인): %s", d)
            return 2

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    logger.info(
        "시작: input=%s target=%s interval=%ss settle=%ss rounds=%d",
        cfg.input_dir, cfg.target_dir, cfg.check_interval, cfg.settle_threshold, cfg.stable_rounds,
    )
    tracker = SettleTracker(cfg)
    while not stop.is_set():
        try:
            files = list(iter_input_files(cfg.input_dir))
            stable = tracker.poll(files)
            if stable:
                if process_batch(cfg, tracker, stable):
                    cleanup_empty_dirs(cfg)
        except Exception:  # 데몬 보호: 예기치 못한 오류에도 루프 지속
            logger.exception("주기 처리 중 예외 발생")
        stop.wait(cfg.check_interval)
    logger.info("종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
