"""설정값, 공통 상수 및 로깅 구성.

환경 변수에서 설정을 읽고, 모든 모듈이 공유하는 확장자/태그 상수를 정의한다.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Set, Tuple

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
PHOTO_EXTS: Set[str] = {"jpg", "jpeg", "heic", "png"}
VIDEO_EXTS: Set[str] = {"mp4", "mov", "m4v", "avi"}
RAW_EXTS: Set[str] = {"cr2", "cr3", "nef", "arw", "dng", "raf", "rw2", "orf"}
SIDECAR_EXTS: Set[str] = {"xmp", "xml", "aae", "on1"}

# 탐색에서 완전히 제외할 디렉터리 (Synology 메타/휴지통 및 결과 분류 폴더)
EXCLUDED_DIRS: Set[str] = {
    "@eaDir", ".raw", "RAW", "JPG", "Video", "Export", "raw", "movie",
    "#recycle", "@tmp", "#snapshot",
}
# 전송 중/임시/시스템 파일은 무시 (원본 그대로 유지)
IGNORED_NAMES: Set[str] = {".ds_store", "thumbs.db", "desktop.ini"}
IGNORED_SUFFIXES: Tuple[str, ...] = (
    ".part", ".tmp", ".filepart", ".crdownload", ".!qb", ".swp", ".temp",
)

# 고유번호/일시 태그 우선순위 (ExifTool JSON 키)
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
        raw_dir_name: RAW 파일 폴더명 (기본 RAW).
        jpg_dir_name: 사진 파일 폴더명 (기본 JPG).
        video_dir_name: 영상 파일 폴더명 (기본 Video).
        export_dir_name: 내보내기 폴더명 (기본 Export).
        create_export_dir: Export 폴더 생성 여부 (기본 False).
    """

    input_dir: Path
    target_dir: Path
    check_interval: float
    settle_threshold: float
    stable_rounds: int
    raw_dir_name: str = "RAW"
    jpg_dir_name: str = "JPG"
    video_dir_name: str = "Video"
    export_dir_name: str = "Export"
    create_export_dir: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        """환경 변수에서 설정을 로드한다.

        Returns:
            Config 인스턴스.

        Raises:
            ValueError: 숫자 변환 실패 또는 값 범위 오류.
        """
        raw_dir = os.environ.get("RAW_DIR_NAME", "RAW").strip() or "RAW"
        jpg_dir = os.environ.get("JPG_DIR_NAME", "JPG").strip() or "JPG"
        video_dir = (
            os.environ.get("VIDEO_DIR_NAME") or os.environ.get("MOVIE_DIR_NAME", "Video")
        ).strip() or "Video"
        export_dir = os.environ.get("EXPORT_DIR_NAME", "Export").strip() or "Export"
        create_export = os.environ.get("CREATE_EXPORT_DIR", "false").strip().lower() in (
            "true", "1", "yes",
        )

        try:
            cfg = cls(
                input_dir=Path(os.environ.get("INPUT_DIR", "/input")),
                target_dir=Path(os.environ.get("TARGET_DIR", "/photos")),
                check_interval=float(os.environ.get("CHECK_INTERVAL", "15")),
                settle_threshold=float(os.environ.get("SETTLE_THRESHOLD", "30")),
                stable_rounds=int(os.environ.get("STABLE_ROUNDS", "2")),
                raw_dir_name=raw_dir,
                jpg_dir_name=jpg_dir,
                video_dir_name=video_dir,
                export_dir_name=export_dir,
                create_export_dir=create_export,
            )
        except ValueError as exc:
            raise ValueError(f"환경 변수 숫자 형식 오류: {exc}") from exc
        if cfg.check_interval <= 0 or cfg.stable_rounds < 1 or cfg.settle_threshold < 0:
            raise ValueError("CHECK_INTERVAL>0, STABLE_ROUNDS>=1, SETTLE_THRESHOLD>=0 이어야 합니다.")
        return cfg


def setup_logging() -> None:
    """``[LEVEL] 메시지`` 형식의 콘솔 로깅을 구성한다."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), handlers=[handler])
