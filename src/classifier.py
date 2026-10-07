"""미디어 종류/날짜/.raw·movie 폴더 분류 및 리네이밍 계획 수립."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .config import PHOTO_EXTS, RAW_EXTS, VIDEO_EXTS, Config
from .models import MediaFile


def classify(ext: str) -> str:
    """확장자로 미디어 종류를 판별한다.

    Args:
        ext: 소문자 확장자(점 제외).

    Returns:
        ``raw`` | ``video`` | ``photo`` | ``other``.
    """
    if ext in RAW_EXTS:
        return "raw"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in PHOTO_EXTS:
        return "photo"
    return "other"


def safe_token(text: str) -> str:
    """파일명에 안전한 토큰으로 변환한다 (공백->_, 특수문자 제거)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "", text.strip().replace(" ", "_"))


def dest_dir_for(cfg: Config, m: MediaFile) -> Path:
    """분류 규칙에 따른 대상 디렉터리를 반환한다.

    Args:
        cfg: 설정.
        m: 파싱된 미디어.

    Returns:
        ``{target}/{yymmdd}[/movie|/.raw]`` 경로.
    """
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
