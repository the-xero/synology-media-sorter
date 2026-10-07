"""카메라 모델명을 짧은 기기식별코드로 변환하는 모듈.

카메라 기종별 EXIF Model 문자열(예: 'Canon EOS R6 Mark II', 'ILCE-7M4')을
짧고 직관적인 식별자('R6M2', 'A7M4')로 축약한다.
사용자 정의 'camera_map.json' 파일을 최우선 적용하며,
미등록 기기는 제조사별 정규식 및 규칙을 기반으로 자동 축약한다.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("sorter")

# 기본 camera_map.json 경로 (프로젝트 루트 기준)
DEFAULT_CAMERA_MAP_PATH = Path("camera_map.json")

# 로마 숫자 Mark 표기 변환 매핑
_MARK_REPLACEMENTS = [
    (re.compile(r"\bMark\s*IV\b", re.IGNORECASE), "M4"),
    (re.compile(r"\bMark\s*III\b", re.IGNORECASE), "M3"),
    (re.compile(r"\bMark\s*II\b", re.IGNORECASE), "M2"),
    (re.compile(r"\bMark\s*I\b", re.IGNORECASE), "M1"),
    (re.compile(r"\bMark\s*([0-9]+)\b", re.IGNORECASE), r"M\1"),
]


def load_camera_map(map_path: Optional[Path] = None) -> Dict[str, str]:
    """사용자 정의 카메라 매핑 JSON 파일을 로드한다.

    Args:
        map_path: JSON 파일 경로. 미지정 시 기본 'camera_map.json' 조회.

    Returns:
        {원본 모델명: 짧은 식별코드} 딕셔너리. 파일 부재 시 빈 딕셔너리.
    """
    path = map_path or DEFAULT_CAMERA_MAP_PATH
    if not path.is_file():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as fin:
            data = json.load(fin)
            if isinstance(data, dict):
                # 대소문자 무관 검색을 위해 strip 처리
                return {str(k).strip(): str(v).strip() for k, v in data.items()}
    except Exception as exc:
        logger.warning("camera_map.json 로드 실패 (%s): %s", path, exc)
    return {}


def shorten_model_name(raw_model: Optional[str], custom_map: Optional[Dict[str, str]] = None) -> Optional[str]:
    """카메라 모델명을 짧은 식별코드로 변환한다.

    1) 사용자 정의 매핑(custom_map) 일치 여부 확인
    2) 제조사별 정규식 자동 축약:
       - Canon: 'Canon EOS ' 제거, 'Mark II' -> 'M2' 등
       - Sony: 'ILCE-' -> 'A', 'ZV-' -> 'ZV' 등
       - Nikon: 'NIKON ' 제거, '_2'/' II' -> 'M2' 등
       - 기타: 공백 및 특수문자 제거 정규화

    Args:
        raw_model: EXIF에서 추출된 원본 Model 문자열.
        custom_map: 사용자 정의 매핑 딕셔너리 (선택).

    Returns:
        짧은 식별코드 문자열. 모델 정보가 없으면 None.
    """
    if not raw_model:
        return None

    model = raw_model.strip()
    if not model:
        return None

    # 1. 사용자 정의 매핑 최우선 확인
    if custom_map and model in custom_map:
        return custom_map[model]

    # 2. Mark N 축약 공통 적용 (예: Mark II -> M2)
    s = model
    for pattern, repl in _MARK_REPLACEMENTS:
        s = pattern.sub(repl, s)

    # 3. 제조사별 특화 규칙
    # Canon: 'Canon EOS R6 Mark II' -> 'R6M2', 'Canon EOS 5D Mark IV' -> '5DM4'
    if re.search(r"\bCanon\b", s, re.IGNORECASE):
        s = re.sub(r"\bCanon\b", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\bEOS\b", "", s, flags=re.IGNORECASE)
        # 공백 및 하이픈 제거
        cleaned = re.sub(r"[^A-Za-z0-9]", "", s)
        return cleaned or None

    # Sony: 'ILCE-7M4' -> 'A7M4', 'ILCE-7RM5' -> 'A7RM5', 'ILCE-1' -> 'A1'
    if re.search(r"\bILCE-?", s, re.IGNORECASE):
        s = re.sub(r"\bILCE-?", "A", s, flags=re.IGNORECASE)
        cleaned = re.sub(r"[^A-Za-z0-9]", "", s)
        return cleaned or None

    # Nikon: 'NIKON Z 8' -> 'Z8', 'NIKON Z 6_2' / 'NIKON Z 6 II' -> 'Z6M2'
    if re.search(r"\bNIKON\b", s, re.IGNORECASE):
        s = re.sub(r"\bNIKON\b", "", s, flags=re.IGNORECASE)
        s = re.sub(r"_([0-9]+)", r"M\1", s)
        s = re.sub(r"\s+IV\b", "M4", s, flags=re.IGNORECASE)
        s = re.sub(r"\s+III\b", "M3", s, flags=re.IGNORECASE)
        s = re.sub(r"\s+II\b", "M2", s, flags=re.IGNORECASE)
        cleaned = re.sub(r"[^A-Za-z0-9]", "", s)
        return cleaned or None

    # Apple iPhone: 'iPhone 15 Pro' -> 'iPhone15Pro'
    if re.search(r"\biPhone\b", s, re.IGNORECASE):
        cleaned = re.sub(r"[^A-Za-z0-9]", "", s)
        return cleaned or None

    # 기타: 공백 및 특수문자 제거
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", s.replace(" ", ""))
    return cleaned or None
