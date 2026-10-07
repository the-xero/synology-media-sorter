# media-sorter

Synology DSM(Btrfs)용 고속 미디어 자동 분류/리네이밍 데몬. 수신 폴더의 복사 완료 파일을
EXIF 일시 기준으로 `{TARGET}/{yyyy}/{yyyy-mm-dd}/{분류폴더}/` 에 `{yymmdd}-{hhmmss}-{고유번호}.{ext}` 로 초고속 이동합니다.

> **단일 마운트 기반 초고속 이동**: 소스와 목적지를 개별 볼륨으로 분리 마운트하면 동일 물리 드라이브라도 컨테이너 OS 상에서 다른 장치(EXDEV)로 인식되어 전체 파일 바이트 복사가 발생합니다. 상위 공통 볼륨을 `/media`로 단일 마운트하여 컨테이너 내부 `os.rename`/`os.link`를 통해 기가바이트급 영상/RAW 파일도 수 밀리초 내에 즉시 이동합니다.

---

## 실행 모드

코어 로직 모듈(`config`, `exif`, `classifier`, `mover`, `settle`)을 기반으로 두 가지 서브커맨드를 지원합니다:

| 서브커맨드 | 주요 특징 및 대상 | 마운트 및 경로 방식 | 실행 방식 |
|---|---|---|---|
| `daemon` | 수신 폴더(`INPUT_DIR`) 상시 감시 → Settle Check 통과 시 대상 폴더(`TARGET_DIR`)로 이동 | 상위 볼륨 단일 마운트 (`/media`) 및 환경 변수 경로 설정 | `docker compose up -d` (기본값) |
| `oneshot` | 지정 폴더 1회 스캔 → 연도/날짜 폴더 없이 직하 제자리(in-place) 분류 및 리네이밍 | 단일 대상 폴더 마운트 강제 (`-v {대상}:/input`) | `docker run` 단독 실행 |

---

### 1. 상시 감시 데몬 실행 (`docker compose`)

수신 전용 Inbox 폴더를 감시하여 완료된 파일을 최종 라이브러리(`{yyyy}/{yyyy-mm-dd}/...`)로 지속 자동 이동합니다.

1. `.env` 파일 설정:
   ```env
   # 호스트 기본 볼륨 (공통 상위 디렉터리 마운트)
   HOST_BASE_DIR=/volume1

   # 컨테이너 내부 경로 (/media 기준)
   INPUT_DIR=/media/photo_inbox
   TARGET_DIR=/media/photo

   # 하위 폴더명 및 옵션
   RAW_DIR_NAME=RAW
   JPG_DIR_NAME=JPG
   VIDEO_DIR_NAME=Video
   EXPORT_DIR_NAME=Export
   CREATE_EXPORT_DIR=false
   ```

2. 실행 명령어:
   ```bash
   # 백그라운드 데몬 시작
   sudo docker compose up -d --build

   # 실시간 로그 확인
   sudo docker logs -f media-sorter

   # 중지
   sudo docker compose down
   ```

---

### 2. 일회성(oneshot) 제자리 정리 실행 (`docker run`)

특정 미디어 폴더 내부에서 파일들을 **해당 폴더 직하에서 바로 제자리(in-place) 분류 및 리네이밍**합니다.  
`yyyy/yyyy-mm-dd` 서브폴더를 새로 생성하지 않고, 직하에 `RAW/`, `JPG/`, `Video/` 폴더를 생성하여 분류합니다.  
컨테이너 실행 시 대상 폴더를 **`/input`으로 단일 마운트 강제**합니다 (`-v {원본 폴더}:/input`).

- **기본 탐색 범위**: 루트 바로 아래의 파일만 **1단계**로 탐색 (기존 하위 폴더 제외).
- **`-r, --recursive`**: 하위 디렉터리까지 재귀 탐색 (단, 이미 생성된 `RAW`, `JPG`, `Video`, `Export` 및 날짜 폴더는 자동 제외).
- **`--no-settle`**: 전송이 이미 끝난 로컬 디렉터리의 경우 복사 완료 대기(기본 30초)를 건너뛰고 **초고속 즉시 실행**.
- **`--dry-run`**: 파일을 실제로 이동하지 않고 `[DRY-RUN] src -> dst` 계획과 통계 요약만 즉시 출력.

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

---

## 디렉터리 구조 및 분류 규칙

### 디렉터리 구조 예시 (데몬 모드)
```text
{TARGET_DIR}/
└── 2026/
    └── 2026-10-07/
        ├── RAW/       # RAW 파일 및 사이드카 (.xmp, .xml 등)
        ├── JPG/       # 일반 사진 (JPG, HEIC, PNG 등)
        ├── Video/     # 동영상 (MP4, MOV 등)
        └── Export/    # (선택) CREATE_EXPORT_DIR=true 일 때 생성
```

### 분류 규칙 테이블

하위 폴더 이름(`RAW`, `JPG`, `Video`, `Export`)은 환경 변수로 변경할 수 있습니다.

| 종류 | 확장자 | 데몬 위치 (TARGET 기준) | 일회성(oneshot) 위치 (현재 폴더 기준) |
|---|---|---|---|
| 사진 | JPG, JPEG, HEIC, PNG | `{yyyy}/{yyyy-mm-dd}/{JPG_DIR_NAME}/` | `{현재폴더}/{JPG_DIR_NAME}/` |
| 영상 | MP4, MOV, M4V, AVI | `{yyyy}/{yyyy-mm-dd}/{VIDEO_DIR_NAME}/` | `{현재폴더}/{VIDEO_DIR_NAME}/` |
| RAW | CR2, CR3, NEF, ARW, DNG, RAF, RW2, ORF | `{yyyy}/{yyyy-mm-dd}/{RAW_DIR_NAME}/` | `{현재폴더}/{RAW_DIR_NAME}/` |
| 사이드카 | XMP, XML, AAE, ON1 | 메인 미디어와 동일 폴더 (RAW 파일의 경우 `RAW/`) | 메인 미디어와 동일 폴더 |
| Export | - | `CREATE_EXPORT_DIR=true` 시 날짜 폴더 내 빈 폴더 생성 | `CREATE_EXPORT_DIR=true` 시 현재 폴더 내 빈 폴더 생성 |
| 기타(미지원) | 그 외 | `{yyyy}/{yyyy-mm-dd}/` (원본 파일명 유지) | `{현재폴더}/` (원본 파일명 유지) |

- **일시**: 사진/RAW `DateTimeOriginal`(→`CreateDate`), 영상 `CreateDate`→`MediaCreateDate`, 없으면 `st_mtime`.
  영상의 QuickTime UTC 시각은 `TZ` 기준 로컬 시각으로 변환됩니다.
- **파일명 형식**:
  - **사진/RAW**:
    - 기기식별코드 존재 시: `{yymmdd}-{hhmmss}-{기기코드}-{고유번호/시퀀스}.{ext}` (예: `261007-143000-R6M2-1234.CR3`)
    - 기기식별코드 부재 시: `{yymmdd}-{hhmmss}-{고유번호/시퀀스}.{ext}` (예: `261007-143000-1234.JPG`)
    - 고유번호: `FileIndex` → `ImageNumber` → `ShutterCount` → 파일명 마지막 연속 숫자 (4자리 정규화, 없으면 동일 초 내 `001`, `002`… 시퀀스).
  - **영상**:
    - 기기식별코드 존재 시: `{yymmdd}-{hhmmss}-{기기코드}.{ext}` (예: `261007-143000-A7M4.MP4`)
    - 기기식별코드 부재 시: `{yymmdd}-{hhmmss}.{ext}` (시퀀스 번호 제외, 초 단위 일치 가능성이 희박함).
  - **기기식별코드 축약 규칙**:
    - 기본 내장 룰셋 및 `config/camera_map.json` 사전 등록 기기:
      - Canon: `Canon EOS ` 제거, `Mark IV`→`M4`, `Mark III`→`M3`, `Mark II`→`M2` (예: `Canon EOS R6 Mark II` → `R6M2`, `Canon EOS R5` → `R5`, 300D부터 전 라인업 지원)
      - Sony: `ILCE-` → `A` (예: `ILCE-7M4` → `A7M4`, `ILCE-7RM5` → `A7RM5`, `ILCE-1` → `A1`), `ZV-E1` → `ZVE1`, `Xperia arc`/`XZ` 시리즈
      - Nikon: `NIKON ` 제거, `_2`/` II` → `M2` (예: `NIKON Z 8` → `Z8`, `NIKON Z 6 II` → `Z6M2`)
      - DJI 드론/액션캠: `DJI Mini 5 Pro` → `Mini5Pro`, `Mini 4 Pro`/`FC8482` → `Mini4Pro`, `Air 3` → `Air3`, `Mavic 3 Pro` → `Mavic3Pro`, `Pocket 3` → `Pocket3`, `Action 5 Pro` → `Action5Pro`
      - Samsung Galaxy: `SM-S928N`/`Galaxy S24 Ultra` → `S24Ultra`, `Galaxy S23` → `S23`, `Z Flip 2~6`(`F711N`/`F712N` 포함) → `ZFlip3`/`ZFlip4`, `Z Fold 2~6` → `ZFold3`/`ZFold4`
      - Sony Xperia: `XQ-EC72`/`Xperia 1 VI` → `Xperia1VI`, `Xperia 5 V` → `Xperia5V`, `Xperia arc` → `XperiaArc`, `XZ1/XZ2/XZ3`
      - Apple iPhone: `iPhone 4`부터 `iPhone 16 Pro Max`까지 전 라인업 (`IP4` ~ `IP16PM`)
      - GoPro / 기타: `HERO13 Black` → `Hero13`, `Insta360 X4` → `InstaX4`
    - 사용자 정의 매핑: 호스트의 `./config/camera_map.json` 파일에 `{ "원본모델명": "원하는코드" }` 형태로 직접 지정 가능 (컨테이너의 `/app/config` 로 마운트 시 외부 수정 즉시 반영, 미마운트 시 내장 룰셋으로 안전 자동 폴백).
  - **사이드카 파일**: `.xml`, `.xmp`, `.aae`, `.on1` 등의 부속 파일도 메인 미디어와 동일한 새 이름으로 변경되어 해당 폴더로 함께 이동됩니다 (RAW 사이드카는 `RAW/` 폴더로 함께 이동).
- **충돌 방지**: 대상에 같은 이름이 있으면 `_1`, `_2` 서픽스를 순차 부여합니다 (덮어쓰기는 절대 하지 않습니다).
- **무시 대상**: `@eaDir`, `.raw`, `RAW`, `JPG`, `Video`, `Export`, `#recycle`, 숨김(`.`/`~` 시작), `.part/.tmp/.filepart/.crdownload`, `.DS_Store`, `Thumbs.db`.
- 이동 후 비게 된 INPUT 하위 폴더는 정리합니다 (INPUT 루트 및 결과 분류 폴더 유지).

---

## 복사 완료 검증 (Settle Check)

폴링 주기마다 파일의 size/mtime 이 **2회 연속 동일** + **안정화 시간(기본 30초) 이상 무변경**일 때만 안정화로 판정합니다.  
안정화된 파일군을 배치 단위로 ExifTool을 통해 병렬 파싱 후 이동하며, 이동 직전 size/mtime을 재검증합니다.

---

## 배포 및 설정 (Docker / Container Manager)

```bash
# 1) 프로젝트 폴더 준비
cd /volume1/docker/media-sorter
cp .env.example .env && vi .env      # PUID/PGID 및 HOST_BASE_DIR 설정

# 2) 빌드 및 실행
sudo docker compose up -d --build

# 3) 실시간 로그 확인
sudo docker logs -f media-sorter
```

### 권한 및 계정 확인
DSM SSH에서 `id` 명령어로 `uid`, `gid`를 확인하여 `.env`의 `PUID`, `PGID`에 지정합니다.  
해당 사용자는 `INPUT_DIR`과 `TARGET_DIR`에 대한 **읽기/쓰기/삭제** 권한을 보유해야 합니다.
