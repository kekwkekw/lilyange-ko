# lilyange-ko

리리안제(`jp.co.fanzagames.lilyange`) 안드로이드 한글 패치(`LilyMod`)용 번역 데이터 CDN 저장소입니다.

## 원칙 (Zero Game Assets)
- 게임 원본 APK, 바이너리, 텍스처, 사운드, AssetBundle 등 원본/파생 게임 에셋을 **일절 포함하지 않습니다.**
- 시나리오 파일(`translations/ko/scenarios/**/*.json`)은 원문 일본어 평문(`source_jp`) 없이 `PlaybackSpot` 키(`lineIndex:inlineIndex`), `lineHash`, `source_guard_sha256`, 한국어 번역문(`t`)만 저장하며, 런타임 해시 검증 불일치 시 Fail-Closed(일본어 원문 유지)로 동작합니다.
- 폰트(`fonts/NotoSansKR-Regular.ttf`)는 SIL Open Font License 1.1(OFL-1.1) 오픈소스 폰트만 포함합니다.

## 디렉터리 구조
- `manifest.json` — 코어 파일 및 폰트 SHA-256 매니페스트
- `fonts/NotoSansKR-Regular.ttf` — 오픈소스 한글 폰트 (OFL-1.1)
- `translations/ko/`
  - `ui.json` — UI 정본 번역 사전
  - `ui_scoped.json` — 다중 번역 문맥 분기 사전
  - `names.json` — 캐릭터/화자명 사전
  - `api_dict.json` — API 응답 JSON 경로별 번역 사전
  - `api_templates.json` — API 동적 정규식 템플릿
  - `scenario_index.json` — 시나리오 스크립트별 온디맨드 인덱스 (SHA-256)
  - `scenarios/{main,chara,event,other}/*.json` — 온디맨드 시나리오 번역 샤드
  - `images.json` — 자작 한글 이미지 교체 카탈로그 (확장용)
- `tools/build_lilytrans_cdn.py` — CDN 생성 및 무결성/무에셋 자동 검증 스크립트
