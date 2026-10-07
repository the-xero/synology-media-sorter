# media-sorter

Synology DSM(Btrfs)용 미디어 자동 분류/리네이밍 데몬. 수신 폴더의 복사 완료 파일을
EXIF 일시 기준으로 `{TARGET}/{yymmdd}/` 에 `{yymmdd}-{hhmmss}-{고유번호}.{ext}` 로 이동합니다.

## 실행 모드

`python -m src.main <subcommand>` (공통 코어 모듈: `config`, `exif`, `classifier`, `mover`, `settle`).

| 서브커맨드 | 동작 |
|---|---|
| `daemon` | INPUT_DIR 상시 폴링, Settle Check 통과 파일 처리 (compose 기본) |
| `oneshot` | INPUT_DIR 전체 1회 처리 후 종료. 실제 실행 시 Settle Check 적용(`--max-wait` 초과 파일은 스킵) |
| `oneshot --dry-run` | Settle Check 없이 `[DRY-RUN] src -> dst` 계획과 요약(총/이동예정/스킵/오류)만 출력. 이동·빈 폴더 정리 없음 |

```bash
# 일회성 실행 예 (dry-run)
sudo docker run --rm --env-file .env -v /volume1/photo_inbox:/input -v /volume1/homes/username/Photos:/photos \
  media-sorter python -m src.main oneshot --dry-run
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
