"""미디어 종류/날짜/raw·movie 폴더 분류 및 리네이밍 계획 수립."""
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


def dest_dir_for(cfg: Config, m: MediaFile, inplace: bool = False) -> Path:
    """분류 규칙에 따른 대상 디렉터리를 반환한다.

    - inplace (oneshot): 현재 파일이 위치한 디렉터리 직하에 raw/movie 생성 (yymmdd 생략).
    - non-inplace (daemon): TARGET_DIR/{yymmdd}[/raw|movie] 구조로 생성.

    Args:
        cfg: 설정.
        m: 파싱된 미디어.
        inplace: True 면 yymmdd 폴더 생성 없이 현재 위치 기준 분류.

    Returns:
        대상 디렉터리 경로.
    """
    if inplace:
        base = m.path.parent
        if m.kind == "video":
            return base / cfg.movie_dir_name
        if m.kind == "raw":
            return base / cfg.raw_dir_name
        return base

    base = cfg.target_dir / m.stamp.strftime("%y%m%d")
    if m.kind == "video":
        return base / cfg.movie_dir_name
    if m.kind == "raw":
        return base / cfg.raw_dir_name
    return base


def assign_names(
    cfg: Config, items: List[MediaFile], inplace: bool = False,
) -> List[Tuple[MediaFile, Path, List[Tuple[Path, Path]]]]:
    """파일별 최종 목적지 경로 및 사이드카 매핑을 계산한다 (시퀀스/충돌 처리 포함).

    - 영상(video): 초단위 일치 가능성이 적으므로 시퀀스 번호 생략 ({yymmdd}-{hhmmss}.{EXT}).
    - 사진/RAW: 고유번호 우선, 부재 시 동일 초 그룹 내 시퀀스(001, 002...) 부여.
    - 기타(미지원 확장자): 원본 파일명 유지.
    - 사이드카(.xml, .xmp 등): 메인 미디어와 동일한 새 이름으로 변경되어 메인 미디어 폴더로 함께 이동.
    - 충돌 방지: 모델명 토큰 시도 -> ``_1``, ``_2`` 서픽스.

    Args:
        cfg: 설정.
        items: 파싱된 미디어 목록.
        inplace: 일회성(oneshot) 제자리 정렬 여부.

    Returns:
        ``(MediaFile, main_dst, [(sidecar_src, sidecar_dst), ...])`` 리스트.
    """
    ordered = sorted(items, key=lambda x: (x.stamp, str(x.path)))
    seq_counter: Dict[str, int] = defaultdict(int)
    reserved: Set[Path] = set()
    plan: List[Tuple[MediaFile, Path, List[Tuple[Path, Path]]]] = []

    for m in ordered:
        ddir = dest_dir_for(cfg, m, inplace=inplace)
        stamp = m.stamp.strftime("%y%m%d-%H%M%S")

        if m.kind == "other":
            stem, ext = m.path.stem, m.path.suffix  # 원본명 유지
        elif m.kind == "video":
            # 영상 파일은 시퀀스 문자 없이 일시만 사용
            stem, ext = stamp, "." + m.ext.upper()
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

        # 사이드카 파일 대상 경로 계산
        sidecar_plan: List[Tuple[Path, Path]] = []
        for s in m.sidecars:
            if s.name == f"{m.path.stem}{s.suffix}":
                sdst = final.parent / f"{final.stem}{s.suffix}"
            elif s.name == f"{m.path.name}{s.suffix}":
                sdst = final.parent / f"{final.name}{s.suffix}"
            else:
                sdst = final.parent / f"{final.stem}{s.suffix}"
            reserved.add(sdst)
            sidecar_plan.append((s, sdst))

        plan.append((m, final, sidecar_plan))
    return plan
