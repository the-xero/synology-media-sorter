FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul

# ExifTool(RAW/영상 메타데이터 파싱) 및 시간대 데이터
RUN apt-get update \
    && apt-get install -y --no-install-recommends libimage-exiftool-perl tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/sorter.py /app/sorter.py

# 실행 UID:GID 는 docker-compose 의 user 로 주입 (PUID/PGID)
CMD ["python", "/app/sorter.py"]
