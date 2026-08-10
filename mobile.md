# 도미맨 모바일 앱 (Android) — 인터페이스·구조·빌드 총정리

테일즈러너 낚시 매크로(PC판 `domiman.py`)를 **휴대폰에서 원격 제어**하는
안드로이드 앱. 같은 ntfy 채널의 PC를 조종하는 "제어(dB)" 역할만 한다(자기가
조종당하지는 않음). 통신 로직·데이터 규칙은 `domiman_m.py`(Python)를 Chaquopy로
앱에 내장해 그대로 쓰고, 화면·위젯·알림·서비스는 Kotlin/Compose로 구현했다.

> 이 문서 하나로 다음 세션에서 앱을 그대로 이어갈 수 있게 정리한 것.
> **UI는 아래 스펙대로가 최종이며, 변경 요청이 없으면 새로 설계하지 말 것.**
> (PC판/프로토콜 세부는 같은 폴더 `CLAUDE.md` 참고. 이 파일은 모바일 전용.)

---

## 1. 위치 — 프로젝트·도구·산출물이 각각 어디 있나

| 항목 | 경로 |
|---|---|
| **안드로이드 프로젝트** | `C:\Users\windo\dev\domiman-android` |
| Python 원본(레퍼런스) | `C:\Users\windo\OneDrive - 한국교통대학교\문서\Python\macro\domiman_m.py` |
| 프로젝트 내 Python 사본 | `…\domiman-android\app\src\main\python\domiman_m.py` |
| **빌드 결과 APK(기본)** | `…\domiman-android\app\build\outputs\apk\debug\app-debug.apk` |
| **배포 APK(수동 복사 대상)** | `C:\Users\windo\OneDrive - 한국교통대학교\domiman.apk` |
| 앱 아이콘 원본 | `…\macro\app.ico` (록맨 낚시 픽셀아트) |
| 위젯 아이콘 원본 | `C:\Users\windo\OneDrive - 한국교통대학교\A_clean_2x2_grid_icon_set_...png` |

**⚠️ 프로젝트가 `macro` 폴더 밖에 있는 이유(중요):** AGP/Chaquopy는 네이티브
빌드 도구라 **경로에 비ASCII(한글)가 있으면 빌드를 거부**한다. `macro` 폴더가
`…\한국교통대학교\…`라 한글을 포함해, ASCII 전용 경로 `C:\Users\windo\dev`로
옮겨 만들었다. 다음 세션에서 `macro\domiman-android`를 찾지 말 것 — 없다.

**⚠️ Python 사본 동기화(수동):** 원본 `macro\domiman_m.py`를 고치면 반드시
`Copy-Item`으로 프로젝트 안 `app\src\main\python\domiman_m.py`에도 덮어써야
반영된다. gradle 자동 동기화 태스크는 없다.

---

## 2. 패키징/빌드 도구 — 무엇이 어디 설치돼 있나

winget으로 설치했고, 필요한 SDK 컴포넌트는 커맨드라인으로 받았다.

| 도구 | 위치 / 버전 |
|---|---|
| Android Studio | `C:\Program Files\Android\Android Studio` |
| JDK(빌드용, Studio 내장 JBR) | `C:\Program Files\Android\Android Studio\jbr` |
| Android SDK 루트 | `C:\Users\windo\AppData\Local\Android\Sdk` |
| ├ cmdline-tools | `…\Sdk\cmdline-tools\latest` (22.0.0) — `sdkmanager`/`android` |
| ├ platform-tools | `…\Sdk\platform-tools` (37.0.0) — `adb` |
| ├ build-tools | `build-tools/36.1.0` |
| ├ platform | `platforms/android-36` |
| └ emulator + 시스템이미지 | `emulator` + `system-images/android-36/google_apis/x86_64`, AVD `medium_phone` |
| **buildPython** | `C:\Users\windo\AppData\Local\Programs\Python\Python313\python.exe` (python.org 정식 3.13) |
| Chaquopy(Gradle 플러그인) | `com.chaquo.python` 17.0.0 |

**환경변수(사용자 범위 영구 설정됨):** `ANDROID_HOME` / `ANDROID_SDK_ROOT` =
`C:\Users\windo\AppData\Local\Android\Sdk`, PATH에 `platform-tools`·
`cmdline-tools\latest\bin` 추가. **단 `JAVA_HOME`은 전역 설정 안 함** — 빌드할
때마다 세션에 `$env:JAVA_HOME`을 Studio JBR로 지정해야 한다(아래 빌드 명령).

**빌드 스택:** AGP 9.0.1, Kotlin 2.3.20, compileSdk/targetSdk 36, minSdk 24,
Jetpack Compose(BOM 2026.03.01) + Navigation3, kotlinx.serialization,
Chaquopy(Python 3.13 내장, pip: `requests`).

### 빌드 시 겪은 함정 4가지(재발 방지)
1. **비ASCII 경로 거부** — 위 참고. ASCII 경로로 이전해서 해결.
2. **Gradle Configuration Cache 비호환** — Chaquopy가 구성 단계에서 외부
   프로세스(빌드용 python 탐지)를 돌려 충돌. `gradle.properties`에
   `org.gradle.configuration-cache=false`로 꺼둠(템플릿 기본은 true).
3. **buildPython 자동탐지 실패** — 이 PC `py -3.13`이 Microsoft Store 스텁이라
   탐지 실패. python.org 정식 3.13을 깔고
   `chaquopy.defaultConfig.buildPython("C:/.../Python313/python.exe")`로 경로 명시.
4. **네트워크 호출은 반드시 suspend + Dispatchers.IO** — `cmd_*→requests.post`가
   블로킹이라, Compose `onClick`에서 메인 스레드로 직접 부르면 ntfy 발신이
   버벅이거나 실패한다. `DomimanRepository`의 모든 `send*`는 suspend +
   `withContext(Dispatchers.IO)`, ViewModel은 `viewModelScope.launch`로 호출.

---

## 3. 빌드·배포 워크플로 (현재 방식, 유지)

```powershell
cd C:\Users\windo\dev\domiman-android
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
.\gradlew.bat assembleDebug
```
그 뒤 **빌드된 APK를 배포 위치로 손으로 복사(덮어쓰기):**
```powershell
Copy-Item "C:\Users\windo\dev\domiman-android\app\build\outputs\apk\debug\app-debug.apk" `
          "C:\Users\windo\OneDrive - 한국교통대학교\domiman.apk" -Force
```
gradle 자동 복사 태스크는 두지 않는다(사용자 요청 — 빌드는 기본 경로에, 배포
복사는 수동). 설치는 이 `domiman.apk`를 폰으로 옮겨 사이드로드.

**로컬 git 백업(원격 없음):** 프로젝트에 git이 초기화돼 있고 기능 단위로
커밋해 백업 중. GitHub 등 원격에는 push하지 않는다. `.gitignore`가
`build/`·`.gradle/`·`local.properties` 제외(소스 + `gradle-wrapper.jar`만 추적).

---

## 4. 아키텍처 (왜 이렇게 짰나)

```
[Compose UI/ViewModel]  ──호출──▶  [DomimanRepository(앱 싱글턴)]  ──JSON문자열──▶  [domiman_m.py (Chaquopy)]  ──HTTP──▶  ntfy.sh  ◀──▶  PC domiman.py
        ▲                                  │  │
        │ StateFlow/SharedFlow             │  └─▶ [DomimanService(포그라운드)]  프로세스 생존
        └──────────────────────────────────┘  └─▶ [DomimanNotifications]/[DomimanWidgetProvider]
```

- **`DomimanRepository`는 앱 싱글턴**(`DomimanApplication.repository`). Activity가
  아니라 앱 수명에 매인다 — 화면 회전/백그라운드 복귀로 Activity가 재생성돼도
  로그인 세션(`activeClient`)과 스트림이 살아남게. (예전엔 Activity `by lazy`
  필드라 재생성 때 세션이 날아가 모든 명령이 no-op이 되던 버그가 있었다.)
- **스트림은 `appScope`(앱 수명 CoroutineScope)에서** 돎. 세션 수명 =
  로그인~로그아웃. MainScreen ViewModel의 onCleared에서 세션을 끄지 않는다.
- **Chaquopy 경계는 JSON 문자열로만 왕복(설계 결정).** PyObject의
  `Map<PyObject,PyObject>` 변환이 불확실해서, `domiman_m.py`에 Kotlin 전용
  헬퍼(`attempt_login_json`, `dispatch_json`, `LoginStore.to_json/from_json`)를
  두고 전부 JSON으로 주고받는다. Kotlin은 `kotlinx.serialization`으로 파싱
  (`DomimanModels.kt`). 새 반환값이 생기면 PyObject API를 파헤치지 말고 이
  패턴(JSON 헬퍼 추가)을 따를 것.
- **역할 분리:** `domiman_m.LoginFlow`(화면전환 규칙)는 Navigation3/ViewModel과
  겹쳐 **안 씀**. Repository는 그보다 낮은 `LoginStore`/`DomimanClient`/
  `attempt_login_json`만 직접 호출. Python=프로토콜+데이터 규칙, Kotlin=화면.

---

## 5. 파일 트리 (`app/src/main`) 와 역할

```
java/com/example/domiman/
  DomimanApplication.kt      # Python.start + repository 앱 싱글턴 생성
  MainActivity.kt            # repository 획득, 첫 실행 권한요청(알림→배터리예외), 다크모드 override
  Navigation.kt              # Navigation3. 시작화면 = 이전 로그인돼 있었으면 Main, 아니면 Login
  NavigationKeys.kt          # Login / RecentLogins / Main / NotificationSettings
  DomimanService.kt          # 포그라운드 서비스(specialUse|dataSync)
  DomimanWidgetProvider.kt   # 4x1 홈 위젯
  data/
    DomimanRepository.kt     # 핵심: 세션·스트림·명령·알림·위젯·최근로그인 전부
    DomimanModels.kt         # @Serializable DTO (DomimanStatus/DispatchResult/SavedLoginJson/LoginStoreJson)
    NotificationPrefs.kt     # 알림 설정 영속(NotifyItem enum 10개 + master), SharedPreferences
    DomimanNotifications.kt  # 알림 채널 2개 + 발송 + 탭 시 앱 열기(contentIntent)
  ui/
    login/  LoginScreen.kt + LoginScreenViewModel.kt
    recent/ RecentLoginsScreen.kt + RecentLoginsScreenViewModel.kt
    main/   MainScreen.kt + MainScreenViewModel.kt
    notif/  NotificationSettingsScreen.kt + NotificationSettingsViewModel.kt
  theme/   Color/Theme/Type.kt (템플릿 기본 테마)
python/domiman_m.py          # macro\domiman_m.py의 사본(수동 동기화)
res/
  drawable/widget_bg.xml           # 위젯 배경(둥근 밝은 패널 #F2F2F2)
  drawable/widget_btn_bg.xml       # 위젯 버튼 누름 하이라이트(원형 셀렉터)
  drawable/widget_ic_refresh|collect|play|pause.png  # 제공된 초록 원형 아이콘 세트
  drawable-nodpi/ic_launcher_scene.png               # 앱 아이콘(록맨 장면, adaptive 배경)
  layout/widget_domiman.xml        # 위젯 4칸 레이아웃
  xml/domiman_widget_info.xml      # 위젯 메타(4x1, updatePeriodMillis=0)
  mipmap-anydpi-v26/ic_launcher(.round).xml  # adaptive: 배경=장면, 전경=투명
  mipmap-*/ic_launcher(.round).png           # 밀도별 둥근 아이콘
AndroidManifest.xml          # 권한/서비스/위젯 리시버 선언
```

---

## 6. 화면별 인터페이스 (최종 — 이대로 유지)

기준 목업은 `앱UI설명.xlsx`(Sheet1=로그인, Sheet2=메인). 화면 4개를
Navigation3로 전환하며, 각 화면은 Compose 화면 + ViewModel이고 모두 앱 싱글턴
`DomimanRepository`를 공유한다.

### 6-1. 로그인 (`ui/login`, Sheet1 A1:E18)
- 제목: **"당신도 강태공이 되어보세요!"**
- 입력 3개: **ID**(ntfy에 표시될 내 이름) / **피제어 PC 이름** / **ntfy 채널명**
- 체크박스: **`[v] 자동 로그인`**
- 버튼 행: **`[로그인]` `[…]`**
  - `로그인`: 입력값으로 로그인(내부적으로 S 상태질의). 성공 시 최근 로그인에
    기록 + 자동로그인 무장 여부 반영 후 메인으로. **실패 시 폼을 비우고
    "입력값을 초기화하고 다시 입력하세요."**
  - `…`: 최근 로그인 화면으로. **이때 '자동 로그인' 체크 상태를 저장소에 전달**
    (`repository.pendingAutoLoginArm`)해서, 최근 로그인 항목으로 로그인해도 그
    체크대로 자동로그인이 무장되게 한다.
- **edit(수정) 모드**(최근 로그인에서 '수정' 선택 시): 자동 로그인 체크박스를
  숨기고 버튼이 **`[수정]` `[취소]`** 로 바뀜. 값을 고쳐 저장하거나 취소.

### 6-2. 최근 로그인 (`ui/recent`, Sheet1 A21:E39)
- 상단: **`< 최근 로그인`** (뒤로가기)
- 리스트: 행마다 **`ID`** + 그 아래 **`피제어PC · 채널`**
- **짧게 탭 = 즉시 그 정보로 로그인**
- **길게 눌러 = 드롭다운 `[수정]` / `[삭제]`**
- 저장 규칙: 같은 (ID, 피제어PC, 채널) 조합이면 중복 행 없이 갱신 후 맨 위로.
  개수 상한 없음. 로그아웃해도 목록은 남는다(캐시/앱데이터 삭제 시에만 소멸).

### 6-3. 메인 제어 (`ui/main`, Sheet2 — PC GUI와 유사, 세로 스크롤)
위에서 아래로:
1. **해상도** 라벨(`1920 x 1080 (자동 감지됨)` 등) + **`[직접 설정]` `[자동 감지]`**
   (직접 설정 = 1080/1440 선택 다이얼로그)
2. **타이머** 입력(분). "0을 입력하면 살림망 감시 모드로 작동합니다." 안내.
   **입력마다 발신하지 않고 1.5초 디바운스 후 T 명령 발신**(ntfy 한도 보호).
3. **`[v] 낚싯대 자동교체`** / **`[v] 미끼 자동교체`** +
   "낚싯대, 미끼 자동교체는 살림망 감시 모드에서만 사용 가능합니다."
4. 큰 **`시작` / `중지`** 버튼(running이면 '중지' 표시)
5. **상태 메시지** 한 줄
6. **`[예약 종료]` `[즉시 회수]`** — 예약 종료는 분 입력 다이얼로그 → `Y,n`
7. **`[실시간 수량확인]` `[다크모드]`** — 수량확인은 N 질의(PC가 창 불러 확인·재개)
8. **`로그`** 헤더 + **`x`**(지우기) + 로그 패널(**항상 펼침, 마지막 8줄만**)
9. 하단 **`[로그아웃]` `[알림 설정]`**
- **버튼 잠금:** 명령 발신 후 응답 대기(isPending) 동안 컨트롤이 잠기고, **15초
  무응답이면 자동 해제 + "응답이 없습니다." 로그**.
- **로그아웃:** '로그아웃' 버튼 → "한 번 더 누르면 로그아웃됩니다." 확인 →
  로그아웃(자동로그인 무장 해제, 세션·서비스 종료). 최근 로그인/마지막 값은 유지.
  (시스템 뒤로가기 2번 인터셉트는 아직 미구현 — 화면 내 버튼으로 대체.)

### 6-4. 알림 설정 (`ui/notif`)
- 상단 **`< 알림 설정`**
- 마스터 **`[v] 알림 켜기`** — 꺼지면 아래 10개가 봉인(disabled)되고 알림도 안 뜸.
- 이하 **10개 체크박스(순서 고정):** 살림망 회수 시작 / 회수 성공 / 회수 실패 /
  낚싯대 교체 시작 / 교체 성공 / 교체 실패 / 미끼 교체 시작 / 교체 성공 /
  교체 실패 / 게임 튕김.
- **기본값 전부 on.** SharedPreferences 영속(앱을 껐다 켜도 유지, 앱데이터
  삭제 시 소멸).

---

## 7. 홈 위젯 4×1 (`DomimanWidgetProvider`)

레이아웃: **`[새로고침] | 수량 | [다운로드] | [재생/중지]`**
아이콘은 제공된 초록 원형 세트(`A_clean_2x2_grid_icon_set...png`를 사분면 크롭 →
`widget_ic_refresh/collect/play/pause.png`). 좌상=새로고침, 우상=낚싯대(회수),
좌하=재생물고기, 우하=정지물고기.

- **새로고침** → `cmd_tank_query`(N). 응답이 오면 옆 수량 칸 갱신. 토스트
  "실시간 수량 확인". **수량은 새로고침을 눌렀을 때만 갱신**(자동 폴링 없음).
- **다운로드** → `cmd_collect_now`(즉시 회수). 토스트 "즉시 살림망 회수".
- **재생/중지** → 시작/중지 토글. running이면 정지 아이콘(`widget_ic_pause`) +
  토스트 "매크로가 중지되었습니다", 대기면 재생 아이콘(`widget_ic_play`) +
  "매크로가 시작되었습니다".
- 각 버튼 누름 시 하이라이트 깜빡임(`widget_btn_bg` 셀렉터).
- **로그인 세션 없으면 수량 칸에 '로그인' 표시**, 그 상태에선 어느 칸을 눌러도
  앱을 연다(로그인 유도). **로그인 상태에선 수량 칸을 눌러도 앱을 연다.**
- 상태 영속: `DomimanRepository`가 prefs `widget_qty`/`widget_running`에 쓰고
  `DomimanWidgetProvider.refresh()`로 다시 그린다.
- **위젯 탭 시 프로세스 보호(함정):** broadcast `onReceive`는 반환되면 프로세스가
  곧 죽어, 15초 걸리는 재로그인 전에 종료돼 갱신이 안 됐다 → `onReceive`에서
  **먼저 `DomimanService.start()`로 프로세스를 붙잡은 뒤** 명령을 던진다. 세션이
  없으면 `ensureClientReady()`가 저장된 마지막 로그인으로 재접속 후 실행.

---

## 8. 알림 (백그라운드 포함)

- 하단 '로그아웃' 옆 **'알림 설정'** 버튼 → 알림 설정 화면(위 6-4).
- 실제 발송: `DomimanRepository.onStreamMessage`가 PC의 보고(`,Z,F,*`)를 받으면
  `NotificationPrefs.isEnabled(notifyKey)`일 때 `DomimanNotifications.postEvent`.
  **화면(Compose)이 아니라 저장소(앱 스코프)에서 띄우므로 앱이 백그라운드여도
  온다.** 어느 체크박스인지는 `dispatch_json`이 넣어주는 `report_notify_key`
  (`domiman_m.notify_key_for_report`, NOTIFY_KEYS 역변환)로 판정.
- 채널 2개: `ongoing`(서비스 상시 알림, 낮은 중요도) / `events`(이벤트 알림).
- **알림/상시 알림 탭 시 앱을 연다**(contentIntent = MainActivity, CLEAR_TOP).
- **보고 코드 ↔ 알림 항목(NOTIFY_KEYS, domiman_m.py):**
  `s`=회수시작, `g`=회수성공, `f`=회수실패, `rs`=낚싯대교체시작,
  `y,r`=낚싯대성공, `x,r`=낚싯대실패, `bs`=미끼교체시작, `y,b`=미끼성공,
  `x,b`=미끼실패, `x,d`=게임튕김. (rs/bs는 PC 260728a에서 추가된 '시작' 신호.)

---

## 9. 세션·백그라운드 생존 (핵심 함정)

- **포그라운드 서비스 `DomimanService`**: 로그인 시 `beginSession()`이 띄운다.
  OS가 백그라운드에서 프로세스를 못 죽이게 해 스트림이 계속 살아 알림이 오고,
  다시 앱에 들어와도 세션이 그대로. `START_STICKY`.
  **타입 함정:** `dataSync`는 Android 14+에서 **하루 6시간 제한**이라 장기 방치
  시 종료됐다 → 34+는 시간제한 없는 **`specialUse`**, 33 이하는 `dataSync`
  폴백(매니페스트 `specialUse|dataSync` 병기, 코드에서 SDK 분기). 삼성 One UI의
  공격적 절전은 변수라, 최후엔 **설정→배터리→'제한 없음'에 앱 추가** 권장(코드로
  강제 불가).
- **복귀 리프레시:** MainScreen이 `LifecycleEventEffect(ON_RESUME)`에서
  `ensureSessionAlive()` — 세션 있으면 스트림 새로 붙이고 S로 상태 재동기화,
  세션 없고 직전 로그인 상태였으면(`session_active` 영속 플래그) 마지막 로그인으로
  재접속, 그래도 실패면 로그인 화면으로.
- **런치 신뢰성:** 시작 화면 = `isSessionConfigured()`(직전 로그인 유지 중)면
  **바로 Main**, 아니면 Login. 과거엔 실행 시 최대 15초 네트워크 로그인을 기다린
  뒤 이동해 느리거나 간헐 실패 시 로그인창이 떴는데, 지금은 즉시 Main으로 가고
  재접속은 백그라운드로 처리(실패 시에만 Login으로).
- **스트림 즉시 중단:** `domiman_m.DomimanClient.stream_disconnect()`로 열린
  응답을 강제 close해 블로킹 `iter_lines`를 깨운다(로그아웃/재접속 시 옛 스레드
  안 남게).
- **첫 실행 권한 요청(MainActivity):** POST_NOTIFICATIONS(13+) → 콜백에서 배터리
  최적화 예외(`ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`). `savedInstanceState
  == null`일 때만(회전 재요청 방지).

---

## 10. 앱 아이콘 = 둥근 록맨

PC의 `app.ico`(록맨 낚시 픽셀아트)를 **adaptive 아이콘의 배경 레이어로 꽉 채워**
One UI가 자동으로 둥글게 마스킹하게 했다(`mipmap-anydpi-v26/ic_launcher*.xml` →
background=`@drawable/ic_launcher_scene`, foreground=투명). 구버전용 밀도별
`mipmap-*/ic_launcher(.round).png`도 둥근 버전으로 생성(기존 webp·기본 로봇
아이콘 삭제). 위젯 아이콘과는 별개(위젯은 초록 원형 세트).

---

## 11. 권한 (AndroidManifest)

- `INTERNET` — ntfy HTTP(스트리밍 구독 + 발신)
- `POST_NOTIFICATIONS` — 알림(13+)
- `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_SPECIAL_USE`,
  `FOREGROUND_SERVICE_DATA_SYNC` — 포그라운드 서비스
- `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` — 절전 예외 요청
- `<service .DomimanService foregroundServiceType="specialUse|dataSync">` +
  `<property PROPERTY_SPECIAL_USE_FGS_SUBTYPE>`
- `<receiver .DomimanWidgetProvider>` + APPWIDGET_UPDATE + 위젯 메타

---

## 12. 다른 PC에서 재현하기 (프로젝트 소스는 이 리포에 포함됨)

안드로이드 프로젝트 소스가 이 리포의 **`domiman-android/`** 폴더에 들어 있다
(빌드 산출물 `build/`·`.gradle/`·기계별 `local.properties`는 제외 — 각 PC에서
생성됨). 다른 PC에서 같은 앱을 만들려면:

1. **리포를 ASCII 경로에 클론**하고 `domiman-android/`를 ASCII 전용 경로로
   옮긴다(예: `C:\Users\<계정>\dev\domiman-android`). ⚠️ **경로에 한글/비ASCII가
   있으면 AGP/Chaquopy 빌드가 거부된다.**
2. **도구 설치**(§2 참고): Android Studio(→ 내장 JBR), Android SDK 컴포넌트
   `platform-tools`·`build-tools/36.1.0`·`platforms/android-36`(+ 원하면
   `emulator`+`system-images/android-36/google_apis/x86_64`), python.org 정식
   Python 3.13(buildPython용). `ANDROID_HOME` 환경변수 지정.
3. **`local.properties` 생성**: `sdk.dir=<그 PC의 SDK 경로>` 한 줄(예:
   `C\:\\Users\\<계정>\\AppData\\Local\\Android\\Sdk`).
4. **buildPython 경로 수정**: `app/build.gradle.kts`의
   `chaquopy.defaultConfig.buildPython("C:/.../Python313/python.exe")`를 그 PC의
   python.org 3.13 경로로 바꾼다(현재 하드코딩됨).
5. **빌드**: `$env:JAVA_HOME="<Studio>\jbr"; .\gradlew.bat assembleDebug`
   → `app/build/outputs/apk/debug/app-debug.apk`.

원본 `domiman_m.py`는 리포 루트에 있고, 프로젝트 안 사본
(`domiman-android/app/src/main/python/domiman_m.py`)이 실제 빌드에 쓰인다 —
루트 원본을 고치면 사본에도 재복사(수동).

## 13. 아직 안 한 것 / 주의

- 시스템 뒤로가기 버튼 실제 인터셉트(`OnBackPressedCallback`) — 지금은 화면 내
  '로그아웃' 버튼으로만 2단계 확인.
- 안드로이드 프로젝트 GitHub 원격 연동(현재 로컬 git 백업만).
- 위젯 아이콘 다크모드 대비(초록 세트라 대체로 무난, 필요 시 벡터로 교체).
- **PC 프로토콜을 바꾸면** `domiman_m.py`의 `parse_status`(실행중 필드 위치),
  REPORT_TEXT/NOTIFY_KEYS를 반드시 같이 맞출 것. 그리고 원본 → 프로젝트 사본
  재복사 잊지 말 것.
