FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul

# ExifTool(RAW/영상 메타데이터 파싱) 및 시간대 데이터
RUN apt-get update \
    && apt-get install -y --no-install-recommends libimage-exiftool-perl tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/ /app/src/

# 사용자 계정(PUID/PGID)으로 실행 시 패키지 및 모듈 읽기/탐색 권한 보장
RUN chmod -R a+rX /app

# 실행 UID:GID 는 docker-compose 의 user 로 주입 (PUID/PGID)
# 서브커맨드 기본값은 daemon. oneshot 은 `docker run ... media-sorter python -m src.main oneshot --dry-run`
CMD ["python", "-m", "src.main", "daemon"]
