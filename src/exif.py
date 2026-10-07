"""ExifTool 파싱, 일시 및 고유번호 추출."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .classifier import classify
from .config import (
    EXIFTOOL_CHUNK, PHOTO_DATE_TAGS, UNIQUE_TAGS, VIDEO_DATE_TAGS, logger,
)
from .models import MediaFile

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
