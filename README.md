# media-sorter

Synology DSM(Btrfs)용 미디어 자동 분류/리네이밍 데몬. 수신 폴더의 복사 완료 파일을
EXIF 일시 기준으로 `{TARGET}/{yymmdd}/` 에 `{yymmdd}-{hhmmss}-{고유번호}.{ext}` 로 이동합니다.

## 실행 모드

코어 로직 모듈(`config`, `exif`, `classifier`, `mover`, `settle`)을 기반으로 두 가지 서브커맨드를 지원합니다:

| 서브커맨드 | 주요 특징 및 대상 | 실행 방식 |
|---|---|---|
| `daemon` | 수신 폴더(`INPUT_DIR`) 상시 감시 → Settle Check 통과 시 대상 폴더(`TARGET_DIR`)로 이동 | `docker compose up -d` (기본값) |
| `oneshot` | 지정 폴더(`INPUT_DIR`) 1회 스캔 → 내부 제자리(in-place) 날짜별 분류 및 리네이밍 (TARGET_DIR 불필요) | `docker run` 단독 실행 |

---

### 1. 상시 감시 데몬 실행 (`docker compose`)

수신 전용 Inbox 폴더를 감시하여 완료된 파일을 최종 Photos 라이브러리로 지속 자동 이동합니다.

```bash
# 백그라운드 데몬 시작 (docker-compose.yml 기본 명령어가 daemon)
sudo docker compose up -d --build

# 실시간 로그 확인
sudo docker logs -f media-sorter

# 중지
sudo docker compose down
```

---

### 2. 일회성(oneshot) 제자리 정리 실행 (`docker run`)

특정 사진 폴더 내부에서 파일들을 날짜별 서브폴더(`{yymmdd}/`)로 **제자리(in-place) 분류 및 리네이밍**합니다.  
`TARGET_DIR`을 따로 지정할 필요 없이 대상 폴더를 `/input` 하나만 마운트하여 실행합니다.

- **기본 탐색 범위**: 루트 바로 아래의 파일만 **1단계**로 탐색 (기존 하위 날짜 폴더 제외).
- **`-r, --recursive`**: 하위 디렉터리까지 재귀 탐색 (단, 이미 생성된 `yymmdd` 날짜 폴더는 중복 방지를 위해 자동 제외).
- **`--dry-run`**: 파일을 실제로 이동하지 않고 Settle Check 없이 `[DRY-RUN] src -> dst` 계획과 통계 요약만 즉시 출력.

```bash
# [추천] 1단계 탐색 Dry-run (이동 없이 계획 및 요약만 확인)
sudo docker run --rm --env-file .env \
  -v /volume1/photo_inbox:/input \
  media-sorter python -m src.main oneshot --dry-run

# [실제 실행] 1단계 탐색 제자리 이동
sudo docker run --rm --env-file .env \
  -v /volume1/photo_inbox:/input \
  media-sorter python -m src.main oneshot

# [재귀 탐색 Dry-run] 하위 폴더까지 포함하여 시뮬레이션
sudo docker run --rm --env-file .env \
  -v /volume1/photo_inbox:/input \
  media-sorter python -m src.main oneshot --dry-run -r

# [재귀 탐색 실제 실행] 하위 폴더까지 포함하여 제자리 이동
sudo docker run --rm --env-file .env \
  -v /volume1/photo_inbox:/input \
  media-sorter python -m src.main oneshot -r
```

## 분류 규칙

| 종류 | 확장자 | 위치 |
|---|---|---|
| 사진 | JPG, JPEG, HEIC, PNG | `{yymmdd}/` |
| 영상 | MP4, MOV, M4V, AVI | `{yymmdd}/movie/` |
| RAW | CR2, CR3, NEF, ARW, DNG, RAF, RW2, ORF | `{yymmdd}/.raw/` |
| 기타(미지원) | 그 외 | `{yymmdd}/` (원본 파일명 유지) |

- **일시**: 사진/RAW `DateTimeOriginal`(→`CreateDate`), 영상 `CreateDate`→`MediaCreateDate`, 없으면 `st_mtime`.
  영상의 QuickTime UTC 시각은 `TZ` 기준 로컬 시각으로 변환됩니다.
- **고유번호**(사진/RAW): `FileIndex` → `ImageNumber` → `ShutterCount` → 파일명 마지막 연속 숫자.
  마지막 4자리를 사용하며 4자리 미만이면 zero-pad (`IMG_12.CR3` → `0012`, `IMG_0012345` → `2345`).
  없거나 영상이면 동일 초 내 `001`, `002`… 시퀀스.
- **충돌 방지**: 대상에 같은 이름이 있으면 ① `-{카메라모델}` 추가 시도 → ② `_1`, `_2` 서픽스. 덮어쓰기는 절대 하지 않습니다.
- **무시 대상**: `@eaDir`, `.raw`, `#recycle`, 숨김(`.`/`~` 시작), `.part/.tmp/.filepart/.crdownload`, `.DS_Store`, `Thumbs.db`.
- 이동 후 비게 된 INPUT 하위 폴더는 정리합니다(INPUT 루트 유지).

## 복사 완료 검증

15초마다 폴링 → 파일의 size/mtime 이 **2회 연속 동일** + **30초 이상 무변경**(단조 시계 및 mtime 경과 모두)일 때만
안정화로 판정. 안정화된 파일군을 배치로 ExifTool 파싱 후 이동하며, **이동 직전 size/mtime 을 한 번 더 확인**합니다.

## 원자적 이동

1. 같은 파일시스템: `os.link` + `unlink` (대상 존재 시 원자적으로 실패 → 무음 덮어쓰기 방지)
2. 다른 파일시스템(컨테이너의 서로 다른 바인드 마운트는 대부분 해당): 대상 폴더에 `.tmp` 로 복사 → `fsync` → 크기 검증 → `link` 로 공개 → 원본 삭제.
   복사 중간 상태 파일은 숨김 `.tmp` 라서 Photos 에 불완전 파일이 노출되지 않습니다.

> 두 바인드 마운트는 같은 볼륨이어도 컨테이너에서 서로 다른 장치로 보여 즉시 rename 이 불가능할 수 있습니다.
> 대용량 영상은 복사 시간이 소요되니 참고하세요.

## UID/GID 확인

DSM 제어판 → 터미널 및 SNMP → SSH 활성화 후:

```bash
ssh 사용자명@NAS_IP
id
# uid=1026(username) gid=100(users) groups=100(users),101(administrators)
```

`uid` → `PUID`, `gid` → `PGID`.

## 배포 (Container Manager / SSH)

```bash
# 1) 프로젝트 폴더를 NAS 에 업로드 (예: /volume1/docker/media-sorter)
cd /volume1/docker/media-sorter
cp .env.example .env && vi .env      # PUID/PGID/경로 수정

# 2) 빌드 및 실행
sudo docker compose up -d --build

# 3) 로그 확인 ([INFO] / [MOVE] / [ERROR])
sudo docker logs -f media-sorter
```

Container Manager UI: 프로젝트 → 생성 → 경로에 위 폴더 선택 → `docker-compose.yml` 사용.

### 권한 체크리스트
- `PUID` 계정이 INPUT/TARGET 공유폴더에 **읽기/쓰기** 권한 보유(INPUT은 파일 삭제 권한 필요).
- 경로(`HOST_*`)는 실제 존재해야 하며 없으면 컨테이너가 오류 로그 후 종료됩니다.
- Multi-platform 빌드 예: `docker buildx build --platform linux/amd64,linux/arm64 -f docker/Dockerfile -t media-sorter .`

## 문제 해결
- 파일이 안 움직임: `docker logs` 확인. 전송 중이거나 mtime 이 최근이면 대기합니다.
- 상세 로그: `.env` 에 `LOG_LEVEL=DEBUG`.
