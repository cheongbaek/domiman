# 낚시 매크로 프로젝트 컨텍스트

테일즈러너 낚시 자동화 매크로. 살림망 퀴즈(물고기 실루엣 매칭)를 자동으로
풀고 낚시를 재시작한다.

**메인 파일은 `domiman.py`(tkinter GUI, 자기완결형).** `낚시.py`는 CLI 이전
버전으로 더 이상 사용하지 않을 예정(로직 원본 참고용). domiman.py는
낚시.py의 로직을 이식한 것이라 아래 문서의 좌표·함정·사양이 그대로 적용된다.
GUI 특이사항: 자동화는 워커 스레드(stop_event), 로그는 stdout 리다이렉트
(LogWriter 큐→Text 위젯), 상태 문구는 엑셀 J8:K18 매핑, 접속 끊김 시
종료하지 않고 대기(재감지→재시작 가능), 낚시 중에는 설정 컨트롤 잠금.
exe 패키징은 domiman.spec으로 빌드(아래 [런처/코어 분리] 참고).

**종료는 `os._exit(0)` 필수(함정):** `on_exit`은 설정/로그 저장만 마치고
`os._exit(0)`로 즉시 끝낸다. `root.destroy()` 후 정상 인터프리터 종료로 두면
torch/easyocr/OpenMP 네이티브 스레드 정리를 기다리느라 (로그 저장 여부와
무관하게) 메인 스레드가 수 초 블록돼 창에 '응답 없음'이 뜨고 늦게 꺼졌다.
os._exit는 그 정리를 건너뛴다. 저장은 os._exit 전에 동기로 끝내둘 것
(원격 Q 응답도 `wait=True`로 먼저 보낸 뒤 on_exit 호출).

**런처/코어 분리 + 수동 업데이트 (구현됨):** GitHub 리포
`cheongbaek/domiman`(main 브랜치, Public)에 `domiman.py`/`version.txt`를
올려두고, GUI의 `⟳` 버튼(제어PC 이름 버튼 우측 끝)으로 수동 업데이트를
확인·적용한다. 버전 규약은 `APP_VERSION`(domiman.py [2-1] 섹션) =
"YYMMDD"+알파벳(a,b,c...) 문자열이며 고정 자릿수라 사전식 비교 그대로
날짜+알파벳 순서와 같다. `version.txt`가 더 크면 `domiman.py` 원본 전체를
받아 `os.replace`로 원자적 교체 후 재시작.
- exe 빌드 시 **컴파일 대상은 `launcher.py`(실행 파일 본체=domiman.exe)이고
  `domiman.py`는 `datas`로만 동봉**해 매 실행마다 `runpy.run_path`로 그대로
  읽어서 돌린다(`domiman.spec` 참고). 이렇게 분리한 이유(함정): 실행 중인
  exe 자체는 Windows에서 덮어쓸 수 없어(파일 잠금) exe만으로는 자체 업데이트가
  불가능 — 로직 전체를 진짜 실행 파일이 아닌 옆의 평범한 `.py` 데이터 파일에
  두면 그 파일은 언제든 자유롭게 덮어쓸 수 있다. 업데이트는 결국 이 loose
  `domiman.py`만 교체하는 것으로 끝난다.
- `domiman.py`는 `runpy`로 동적 로드되어 **PyInstaller 정적 분석 대상이
  아니므로**, 그 파일이 쓰는 의존성(pyautogui/requests/numpy/tkinter 등)은
  `domiman.spec`의 `hiddenimports`에 명시해야 묻어들여진다. 새 최상위
  `import`를 domiman.py에 추가하면 스펙의 hiddenimports도 같이 챙길 것
  (안 그러면 스크립트 모드에선 되는데 exe에서만 `ModuleNotFoundError`).
- 프리징 여부와 무관하게 `os.path.abspath(__file__)`은 실제 `domiman.py`
  경로로 정확히 해석됨(runpy가 `__file__`을 그 경로로 설정) — exe에서도
  `getattr(sys, 'frozen', False)`가 그대로 True로 유지되므로(부트로더가
  `sys` 전역에 심는 속성) 기존 frozen 분기 로직들은 그대로 동작한다.
- exe에서 재시작할 땐 `subprocess.Popen([sys.executable])`(인자 없이
  launcher 자신을 다시 띄움 — 재기동된 launcher가 방금 교체된 새
  domiman.py를 다시 읽음), 스크립트 모드는 기존처럼
  `subprocess.Popen([sys.executable, target])`.
- `launcher.py`는 거의 안 바뀌는 최소 코드(경로 해석 + runpy 디스패치 +
  domiman.py 없을 때 안내 메시지박스)만 담당. DPI awareness는 domiman.py
  자체가 이미 module-level에서 설정하므로 launcher.py에서 중복 설정하지
  않음.
- 새 버전 배포 절차: ① domiman.py 수정 ② `version.txt`를 다음 알파벳으로
  올림 ③ 두 파일만 커밋·푸시. 각 PC는 `⟳` 클릭 한 번으로 반영(재설치·재빌드
  불필요 — launcher.py/의존성 DLL 자체가 바뀌는 경우에만 재설치 필요).

---

## 현재 아키텍처 (중요)

### 해상도 두 모드 — 둘 다 동일한 윤곽선 매칭 사용
- **FHD (1080p)**: `pyautogui`로 화면 직접 캡처. 게임이 화면 (0,0) 기준에
  있다고 가정. 좌표는 FHD 기준 하드코딩.
- **QHD (1440p)**: WGC(Windows Graphics Capture)로 게임 창을 원본으로 잡아
  1920x1080으로 축소(INTER_AREA) 후 **FHD와 동일한 좌표/로직**으로
  인식. `windows-capture` 패키지 필요.

### 해상도 자동 감지 = 모니터 기반 (`detect_resolution`)
`setup_resolution`은 이제 사용자에게 묻지 않고 **게임 창이 있는 모니터**로
모드를 자동 판별한다(창을 못 찾을 때만 수동 입력 폴백).
- **주 모니터 · 1920x1080 · 100% 배율 → 1080p** (pyautogui 직접 캡처 가능)
- **그 외(보조 모니터/고배율/비1080p) → 1440p** (WGC로 창 직접 캡처)
- `_pick_game_hwnd`가 진짜 창 선택(숨은 창 제외), `MonitorFromWindow` +
  `GetMonitorInfo`로 모니터 물리 크기·주모니터 여부, `GetDpiForMonitor`로 배율.
- 최소화 상태에서도 모니터는 정확히 잡히므로 판정 가능(4케이스 검증 완료).
- 부수효과: `d) 해상도 재설정` 메뉴가 사실상 **재감지 버튼**이 됨.

**왜 창 크기가 아니라 모니터인가 (중요):** 게임은 **DPI-unaware**라
(`GetDpiForWindow`가 어느 모니터든 96=100% 고정), 창 클라이언트 크기가
상황에 따라 `1920x1080`(가상화)로도 `2400x1350`(물리)로도 **튄다**.
따라서 크기 기반 판정은 불안정. 모니터 물리 크기만이 유일하게 신뢰할 신호.
과거 노트의 "QHD=2400x1350 고정" 전제는 틀렸으니 크기로 판정하지 말 것.

### CNN은 제거됨
과거 QHD는 학습된 CNN(`fish_model.pth`)으로 분류했으나, 원인이 "확대로 인한
실루엣 뭉개짐"이 아니라 "확대 화면에 FHD 좌표를 그대로 쓴 좌표 어긋남"임을
확인. WGC로 선명한 원본을 받아 1080p로 축소하면 FHD 로직이 그대로 동작하므로
CNN·torch·torchvision·result_qhd 폴더 의존성을 모두 제거했다. **되살리지 말 것.**

---

## 힘들게 알아낸 함정들 (재발 방지)

### 1. 숨은 중복 창 함정 (가장 중요)
게임은 같은 제목 `'Tales Runner'` 창을 **두 개** 만든다:
- 진짜 렌더링 창: 가시성 1, 클라이언트 2400x1350, 화면상 (2035, -337) 등
- 숨은 내부 창: **가시성 0**, 크기 1920x1080, 위치 (0,0)

제목만으로 찾으면 숨은 창(1920x1080, (0,0))을 잡아서 좌표 변환이 무력화된다
(`ClientToScreen(0,0)`이 (1,1)을 반환해 오프셋이 안 더해짐).
**해결: 창 선택 시 반드시 `IsWindowVisible` 체크 + 클라이언트 영역이 가장 큰
창을 고른다.** `bring_game_to_front`와 `_find_game_hwnd` 둘 다 이 방식.

### 2. 좌표 변환 (`to_screen`)
좌표는 항상 FHD(1920x1080 공간) 기준으로 저장. 클릭 시 실제 화면 좌표로 변환:
- `GetWindowRect`(2403x1353)가 아니라 **`GetClientRect`(2400x1350) +
  `ClientToScreen`**을 써야 3px 테두리 오차가 없다.
- 공식: `sx = ox + fx/1920*cw`, `sy = oy + fy/1080*ch`
  (ox,oy=ClientToScreen(0,0), cw,ch=클라이언트 크기)
- 검증됨: FHD(1007,1006) → 실측 살림망 버튼 (3292,926)와 2~4px 오차로 일치.
- top이 음수(-337)일 수 있음(창이 화면 위로 삐져나감). 공식은 음수 정상 처리.
- DPI: 시작 시 `SetProcessDpiAwareness(2)` 설정. GetClientRect/ClientToScreen/
  SetCursorPos 모두 물리 픽셀로 일관됨.

### 3. WGC 캡처
- `windows_capture.WindowsCapture(window_name="Tales Runner")` — **정확 일치**
  필요(부분매칭 안 됨, 대소문자·공백 정확히).
- 프레임: `frame.frame_buffer`는 (H,W,4) BGRA numpy.
- 백그라운드 실행: `cap.start_free_threaded()` (블로킹 `start()`와 구분).
  ※ 이 메서드명은 패키지 버전에 민감. 캡처가 안 되면 여기부터 의심.
- 캡처는 루틴 시작 시 켜고 끝나면 끔(대기 중 CPU 절약). `GameCapture` 클래스가
  최신 프레임 보관, `get_frame_1080()`으로 요청 시 축소본 생성.

### 4. OCR 오인식
- easyocr이 '살림망'을 자주 '산림망'으로 읽음(conf도 낮음, ~0.18).
  → 완전일치 금지. **`'림망'` 부분매칭** 사용(공백 제거 후).
- 버튼 글자도 따옴표/공백이 섞임: "'낚시 취소", '마미롬보내기'(마이룸) 등.
  → 항상 부분매칭 + `.replace(" ","")` 전처리.

---

## 검증된 좌표·영역 (FHD 기준, 두 모드 공통)

버튼 클릭 좌표:
- 낚시 취소/시작: (1086, 988)  — 토글 버튼(글자만 바뀜)
- 살림망 확인:    (1007, 1006)
- 마이룸 보내기:  (1138, 245)
- 확인(팝업):     (958, 575)

인식 영역 (x,y,w,h):
- 질문 물고기 좌: (742, 406, 78, 69)
- 질문 물고기 우: (830, 407, 78, 69)
- 보기 그리드:    (972, 433, 226, 296)  — 4행 3열, 최대 10칸
- 성공 판정 텍스트:(832, 503, 255, 38)

OCR로 읽는 정보 (감시 모드용, 아래 참고):
- 살림망 수량:      중앙 (890, 1007), OCR 예 '17/920' (conf 1.00)
- 최소 획득 시간:   중앙 (975, 933),  OCR 예 '20초'  (conf 0.97)

---

## 타이머 0 = 살림망 감시 모드 (구현됨)

`watch_tank_mode()`로 구현. 아래 사양대로 동작한다.

**진입:** 타이머 입력(시작 시 · `t` 메뉴 둘 다)에서 `0` 또는 `o`/`O`를 가로채
`watch_tank_mode()` 호출. 무한 루프라 반환하지 않으며 메뉴의 `q` 또는 Ctrl+C로
종료. ntfy로는 `o`만 사용(ntfy `0`은 dual_input에서 ''로 변환되어
'지금 실행' 관례와 겹치므로). OCR 영역(실측 검증 완료):
`REGION_TANK_QTY=(825,989,130,36)`, `REGION_MIN_TIME=(940,916,72,34)`.
(수량은 자릿수 1~3에 따라 중앙정렬로 폭이 변해 1자리에서 잘리므로 좌우로 넓힘.
 읽기는 프레임 여러 번 재시도해 갱신 애니메이션/깜빡임을 건너뜀.)

**감시 모드 읽기는 FHD/QHD 모두 WGC 사용(중요):** 일반 낚시 루틴은 FHD=pyautogui,
QHD=WGC지만, **감시 모드는 두 모드 다 WGC로 게임 창을 직접 캡처**한다
(`_ensure_watch_capture`가 FHD에서도 `GameCapture` 생성·기동, `_watch_grab_region`이
WGC 프레임에서 crop). 이유: pyautogui는 화면 좌표를 직접 찍어 **다른 창이 하단
숫자 영역을 가리면 실패**하는데, 감시 모드는 장시간 무인 동작이라 가림·포커스에
강한 WGC가 필수. WGC로 메인/서브 모두 정확히 읽힘 검증됨(53/920 등). WGC 패키지가
없으면 pyautogui로 폴백. (단, 창이 최소화되면 WGC도 프레임을 못 받으니 감시 중
게임은 최소화하지 말 것.)

**동작(감시 루프):**
1. 매 사이클마다 두 영역을 OCR로 읽는다:
   - 살림망 수량 (890,1007): 정규식 `(\d+)\D+(\d+)`로 current/max 추출
     (슬래시가 공백·붙음·세로줄로 깨질 수 있으니 유연하게)
   - 최소 획득 시간 (975,933): 정규식 `(\d+)`로 초 추출
2. **회수 조건: `current >= max - 5`** (가득차기 5칸 전).
   만족하면 `run_fishing_routine()` 실행(기존 회수 루틴 그대로 재사용).
   판정은 `_tank_needs_collect(qty)` / `TANK_COLLECT_MARGIN = 5`로 빼두어
   낚싯대 교체 직후 확인과 같은 기준을 공유한다(아래 교체 섹션 참고).
3. **폴링 간격 = 최소 획득 시간(초) 그대로**(1.0배).
   max-5 버퍼가 있어 한 사이클에 최대 1마리 → 그대로 폴링해도 절대 못 넘김.
   낚싯대 교체로 최소 획득 시간이 바뀌면 다음 사이클부터 자동 반영.
4. 회수 후 수량은 리셋되므로 그대로 감시 계속(무한 루프).

**메뉴 진입(폴링 대기 중):** 폴링 간격 대기는 `time.sleep`이 아니라
`wait_with_keycheck`(키보드+ntfy 큐 동시 감시)로 한다. 대기 중 아무 일 없으면
다음 사이클로 넘어가고, 키가 눌리면 **감시를 일시정지**(WGC 캡처 정지, CPU 절약)한
뒤 메뉴(`_watch_menu_keyboard`)를 띄운다. 메뉴 옵션:
`r`(로그)·`n`(ntfy)·`d`(해상도 재감지)·`q`(종료)·`Enter`(재개),
그리고 `t`(타이머 재설정)는 **감시 모드를 끝내고 일반 타이머 모드로 전환**한다
(`handle_timer_change`로 새 주기 입력·즉시실행 처리 후 `run_timer_loop`로 넘어가며,
감시로 복귀하지 않는다. 단 `t` 후 `0`/`o`를 넣으면 감시 모드로 재진입).
`r`/`n`/`d`/`Enter`는 메뉴를 빠져나오면 루프 top의 `_ensure_watch_capture`가
캡처를 다시 켜 감시를 재개한다. ntfy 원격 명령은 `_watch_handle_ntfy`가 즉시
처리(입력 블로킹 없음). 메인 타이머 대기 루프는 `run_timer_loop()`로 추출돼
`__main__`과 감시 모드 `t`가 공유한다. 일반 모드 메뉴 로직
(`process_menu_choice`)과 토글 동작은 동일하게 맞춰 재사용성을 지킨다.

**안전장치(중요):**
- 파싱 실패하거나 값이 비정상이면 그 사이클은 버리고 **직전 정상 폴링 간격 유지**.
  (못 읽었다고 간격을 0으로 만들면 CPU를 태움)
- **`cur > mx`(예: 520/470)는 '비정상'이 아니다** — 최대치가 더 작은 낚싯대로
  바꾼 뒤의 정상적인 초과 상태다. 파싱에서 버리면 안 되고(그러면 영구 정체),
  회수 조건에 걸려 바로 회수돼야 한다. 아래 교체 섹션의 '초과 상태를 파싱에서
  버리면 안 된다' 항목 참고.
- 숫자·최소획득시간은 낚싯대 교체 중에도 항상 화면에 떠 있음(확인됨).

---

## 미끼·낚싯대 자동 교체 (감시 모드 전용, 구현됨)

미끼 소진('미끼가 부족합니다' 팝업) 또는 낚싯대 기간 만료('! 아이템 기간
만료' 팝업)가 뜨면 낚시가 멈춰 살림망 수량이 정체된다. 감시 루프가 이를
감지해 자동으로 교체하고 낚시를 재개한다.

**트리거 (미끼/낚싯대 서로 다름, 함정 — 만료 팝업 OCR은 폐기됨):**
- **미끼**: 수량 파싱에 성공한 매 사이클마다 `REGION_NO_BAIT=(890,499,142,36)`을
  OCR해 '미끼가'/'부족' 부분매칭 → `run_bait_swap_routine()`(팝업 글자 conf가
  0.3~0.5대로 낮아 완전일치 금지).
- **낚싯대**: 조건은 **하나뿐** — `if self.rod_swap and minsec == 1:`
  **최소 획득 시간이 정확히 `1`(초)** — 낚싯대 교체가 필요하면 게임이 최소
  획득 시간을 '1초'로 표시하는 것을 이용. 별도 OCR 없이 이미 매 사이클 읽는
  값을 재사용한다.
  ~~'! 아이템 기간 만료' 팝업 OCR~~ 방식은 폐기됐다(함정): 낚싯대가 아닌
  **다른 아이템의 만료 팝업에도 반응하는 오탐**이 있었고 이름 필터('낚' 한
  글자 부분매칭)로도 못 막았다. → **낚싯대와 무관한 원인에도 걸리는 신호를
  트리거로 쓰지 말 것.** ('낚시가 멈췄다' 같은 원인 불문 신호도 같은 이유로
  안 된다 — 실제로 그렇게 고쳤다가 되돌린 적 있다.)

  **~~② `cur + 5 >= mx`(`_tank_needs_collect`)~~ 조건은 260811a에서 제거됐다
  (같은 함정의 재발이었음):** '살림망이 가득 차' 오류를 평시에 미리 막겠다고
  회수 조건을 낚싯대 트리거에 OR로 얹었었는데, **살림망이 찬 것은 낚싯대
  상태와 무관한 신호**라 위의 '만료 팝업 오탐'과 같은 종류의 잘못이었다.
  실제 로그에서 멀쩡한 낚싯대를 `467/470`·`469/470`·`470/470`마다 계속
  갈아 끼우며 교체→회수→교체를 반복했다. 살림망이 차면 **회수 분기가**
  처리한다. → 낚싯대 트리거는 `minsec == 1` 하나로 되돌아갔다.

  **분기 순서: 낚싯대 → 회수 → 미끼 → 낚시정지 감지.** ② 시절엔 두 조건이
  같은 식이라 순서가 뒤면 회수 분기의 `continue`에 삼켜졌지만(그래서 낚싯대를
  먼저 뒀다), 지금은 조건이 겹치지 않아 순서에 의존하지 않는다.

**리스트 진입도 통일됨(함정 — 팝업 버튼을 더 이상 클릭하지 않음):** 두
루틴 모두 **ESC 3번(0.5초 간격, 혹시 떠 있을 팝업 종류를 모르니 무조건 정리)
→ 고정 좌표 클릭으로 리스트 직접 열기**로 바뀌었다. 팝업 안의 버튼을 찾아
클릭하던 방식(팝업이 이미 닫혀 있거나 다른 팝업이 겹쳐 있으면 실패)보다
안정적.
- 미끼: `COORD_BAIT_LIST_BTN = (903, 913)` (실측, 기존 팝업 속 '보유 미끼'
  버튼 (1004,578)은 폐기)
- 낚싯대: `COORD_ROD_LIST_BTN = (814, 917)` (재실측, 기존 (815,916)에서 소폭 보정)

**미끼 교체 (`run_bait_swap_routine`):**
1. `bring_game_to_front` 후 ESC×3 → `COORD_BAIT_LIST_BTN` 클릭으로 리스트 직접 진입.
2. `_find_cards_by_pattern(BAIT_TARGET_PATTERN)`으로 현재 페이지 탐색.
   미발견 시 오른쪽 화살표 (1291, 565)로 페이지 넘김 — **최대 4번**(총 5페이지).
3. 발견하면(좌상단 우선) 해당 칸 사용. 끝까지 실패하면 ntfy 경고 후
   **좌상단 (0,0) 칸을 대신 사용**(대기하지 않고 강행).

**낚싯대 교체 (`run_rod_swap_routine`):** `bring_game_to_front` 후 ESC×3 →
`COORD_ROD_LIST_BTN` 클릭으로 리스트 직접 진입.
`ROD_TARGET_PATTERN = r"매직|스타|장미|푸"`('매직 스타 낚싯대'/'푸른 장미검
낚싯대'의 조각들 중 **하나라도** 걸리면 채택 — 원래 `r"스타|장미검"`였으나
`낚싯대리스트OCR검증.py`로 실측하니 '장미검'이 conf 0.21로 '장미경'으로
오인식돼 통째로 놓치는 사례가 나와, 각 이름에서 더 잘 읽히는 부분(짧고
특징적인 음절)들로 흩어 오인식 하나에 발목 잡히지 않게 함). 페이지 넘김
없음(현재 페이지에서만 탐색). 못 찾으면 ntfy 긴급 경고(priority 5) 후
ESC → 낚시 시작만 시도.

**낚싯대 교체 직후 살림망 확인 (`_restart_or_collect_after_rod_swap`, 함정):**
낚싯대마다 **최대 살림망 개수가 다르다.** 새로 든 낚싯대의 최대치가 기존보다
작으면 이미 쌓인 물고기가 그 최대치를 넘은 상태(예: `520/470`)가 되고,
그 상태에선 **'낚시 시작'을 눌러도 낚시가 걸리지 않는다.** 교체 후 무조건
'낚시 시작'을 누르던 예전 방식은 그래서 클릭이 먹히지 않은 채 수량만 정체됐다.
지금은 '낚시 시작'을 누르기 **직전에** `read_tank_quantity()`로 한 번 읽고 갈린다:
- 여유 있음(예: `165/470`) → 평소대로 `COORD_FISHING_BTN` 클릭해 재개.
- 회수 조건(예: `467/470`)이거나 최대치 초과(예: `520/470`) → '낚시 시작'을
  **누르지 않고** 곧바로 `run_fishing_routine()`. 회수 루틴은 맨 앞에서
  `is_fishing_active()`로 버튼 글자를 읽어 '낚시 시작'(대기 중)이면 취소
  절차를 스스로 생략하므로, 별도 감시 조건 없이 바로 불러도 안전하다.
- 판독 실패 → 평소 동작(그냥 시작)으로 폴백.

판정 기준은 감시 루프와 공유(`_tank_needs_collect` / `TANK_COLLECT_MARGIN = 5`).
읽기 전 0.5초 대기는 교체한 낚싯대 기준으로 수량 표시가 갱신될 시간. 보고 순서는
`rs` → `y,r`/`x,r` → (회수 시) `s` → `g`/`f`.

**초과 상태(`cur > mx`)를 파싱에서 버리면 안 된다 (이 기능 최대의 함정):**
`read_tank_quantity`는 `cur > mx`를 **그대로 돌려준다.** 예전의
`0 <= cur <= mx` 가드는 이걸 OCR 오인식으로 보고 버려 `None`을 돌려줬는데,
그러면 스스로 못 빠져나오는 고리가 된다:

> 초과 → 낚시가 안 걸림 → 수량이 줄지 않음 → 계속 초과 → 매 사이클 파싱 실패
> → 회수 조건 판정까지 도달 못 함 → 회수 안 됨 → 초과 유지

**초과는 교체 직후 한순간의 값이 아니라 회수할 때까지 계속 남는 상태**라,
교체 루틴 쪽 호출에서만 열어주는 걸로는 부족하다 — 260809a가 `allow_overflow`
인자를 만들어 그 호출에서만 켰다가, 감시 루프는 기본값(초과 거부)으로 읽어
그대로 정체돼 260809c에서 인자를 없애고 전역으로 고쳤다. 파싱이 초과를 늘
받아들이는 덕분에 교체 루틴이 초과를 놓쳐도(예: 갱신 전 최대치를 읽어 '여유
있음'으로 판단) **감시 루프가 다음 사이클에 스스로 회수해 복구한다.**

**값 범위로 오인식을 거르는 가드는 두지 말 것(위 버그의 교훈):** 대체 가드로
`cur <= mx * 3` 상한을 잠깐 뒀다가 뺐다. 잡는 범위가 원리적이지 않다 —
`520/47`(max 절삭)은 우연히 걸리지만 앞자리가 빠진 `52/470`은 그냥 통과한다.
실효는 없으면서 "예상 못 한 정상 값을 조용히 버릴 위험"만 `cur <= mx`와 똑같이
남는다. 오인식은 프레임 4회 재시도와 감시 루프의 매 사이클 재파싱으로 씻겨
나가므로 그쪽에 맡긴다. 남은 조건은 `mx > 0` 하나이고, 이건 휴리스틱이 아니라
구조적 무의미값 차단이다(max가 0이면 `cur >= mx-5`가 항상 참이라 회수 무한 반복).

**판독 실패 진단:** 마지막 OCR 원문을 전역 `_last_tank_ocr`에 남기고, 감시
루프의 기존 '수량 파싱 실패' 로그 한 줄에 함께 찍는다(`is_fishing_active()`의
실패 로그와 같은 방식 — 줄 수는 늘지 않음).

**공통 마무리 (`_use_card` / `_use_card_and_restart`):** '사용하기' 클릭 →
클릭 후 **ESC 2번(0.5초 간격)** 까지가 `_use_card`, 거기에 '낚시 시작' =
`COORD_FISHING_BTN` (1086, 988) 클릭까지 붙인 것이 `_use_card_and_restart`다.
미끼 교체는 뒤엣것을 그대로 쓰고, 낚싯대 교체는 앞엣것만 쓴 뒤 위의 살림망
확인이 시작/회수를 결정한다.
ESC가 2번인 이유(중요): 리스트 창은 팝업 **위에 겹쳐** 뜨므로 ESC 1번은
리스트 창만 닫고 밑에 깔린 팝업이 최상단 모달로 남아 '낚시 시작' 클릭을
삼킨다(실측: 교체가 무한 반복되는 로그로 확인). ESC는 `press_esc()`
(ctypes `keybd_event` + '[Key] ESC' 로그) 사용.

**카드 이름 판독 (`_find_cards_by_pattern`):** 보유 미끼/보유 낚싯대 창은
**그리드가 동일**(실측 수 px 차이)해 좌표를 공유한다. '갯지렁이'가
'개지렇미'로 깨지는 등 오인식이 잦아 유연한 정규식 사용
(`BAIT_TARGET_PATTERN = r"[갯개]지[렁렇령]"`). 이름 바 8칸을 덮는
`REGION_BAIT_NAMES=(640,483,640,202)`를 통째로 `_watch_grab_region`(WGC)으로
읽고, 매칭 글자의 중심을 `BAIT_NAME_ROW_Y=(503,660)` / `BAIT_COL_X=(715,878,
1041,1204)`에 스냅해 (행,열) 결정. 글자가 하나도 안 읽히면(창 애니메이션 중
프레임) 0.5초 후 1회 재시도.

**좌표 (FHD 기준, 실측):** '사용하기' 버튼 2행x4열 `BAIT_USE_BTNS` =
1행 (708,544) (874,543) (1038,540) (1209,541) /
2행 (714,702) (877,699) (1042,702) (1201,701). (낚싯대 창도 동일 좌표 재사용)
클릭은 전부 `click_real`(to_screen 변환)이라 QHD·창 위치와 무관하게 정확.

## 낚시 정지 감지 · 자동 재개 (구현됨)

미끼/낚싯대 교체 트리거에 안 걸리는 원인으로 낚시가 조용히 멈춰버리면
살림망 수량이 그대로 정체된다. `COORD_FISHING_BTN=(1086,988)` 버튼은
낚시 중엔 '낚시 취소', 대기 중엔 '낚시 시작'으로 글자만 바뀌는 토글이라
이 글자를 읽어 상태를 판별한다.

**OCR 위치 (`REGION_FISHING_BTN=(1040,983,88,40)`, `낚시상태버튼OCR검증.py`로
실측 후 여유를 두어 확대):** 낚시 중인 라이브 화면에서 "'낚시 취소'"
(conf 0.92, 앞뒤에 따옴표가 붙어 나옴 — 부분매칭이라 무관), 낚시 대기 중
스크린샷에서 '낚시 시작'(conf 0.999)이 **거의 같은 좌표**에서 잡혀 "두
버튼 위치가 같다"는 전제를 확인했다. `is_fishing_active()`: '취소' 포함→
True(진행 중), '시작' 포함→False(대기 중), 둘 다 없으면 None(호출측이
무시하되, 원인 추적을 위해 원문 OCR 텍스트를 로그로 남김).

**감시 루프 트리거:** 살림망 수량(`(cur,mx)`)이 **3회 이상 연속 동일**하면
(정상적으로도 몇 사이클 안 잡힐 수 있어 약한 신호 — 그래서 곧바로 재개하지
않고) `is_fishing_active()`로 한 번 더 확인한다. `True`(여전히 낚시 중)면
그냥 정상 상황이므로 아무 것도 안 함. `False`(대기 중)로 확인되면
`_resume_fishing()` 호출. 동일 카운트는 매 사이클 초기화(qty가 바뀌면
리셋)되며, 3회 이상인 동안은 매 사이클 계속 재확인(더 빨리 잡기 위해
정확히 3번째만 보지 않음).

**실시간 수량확인 = 로컬 버튼·원격 N 공용 로직 `_tank_check_and_resume`
(260728a에서 통합):** 회수 루틴 시작 때처럼 **게임 창을 앞으로 불러**
살림망을 새로 읽고(3초 렌더 대기), **동시에 낚시 취소/시작 버튼을 확인해
'낚시 시작'(대기 중)이면 `_resume_fishing()`으로 눌러 재개**한다('낚시
취소'=진행 중이면 그대로 둠). 한 배경 스레드 안에서 창 호출→캡처→읽기→
낚시상태 확인까지 끝내 캡처 소유권 경쟁(과거 `_query_tank` 콜백 직후
finally의 `game_capture.stop()`이 상태확인 스레드와 TOCTOU로 충돌하던
문제)을 없앴다. **재개는 `is_fishing_active() is False`(정확히 '낚시 시작'=
대기 중)일 때만** — 진행 중(True)이나 판독 불가(None)면 클릭하지 않는다.
**항상 창 호출 방식으로 동작(260728b, 워커 가동 무관):** 예전엔
`self._running()`(감시 워커 가동)이면 캐시(`_last_tank`)만 반환하고 창을 안
건드렸으나, 워커 스레드는 살아 있는데 게임 낚시가 조용히 멈춘 경우 창을
안 불러 확인/재개가 안 되는 문제가 있어 그 캐시 분기를 제거했다. 이제 신호가
오면 무조건 창을 불러 새로 읽고 상태 확인 후(정상 진행 중이면 재개 클릭은
안 나가므로 워커의 정상 낚시는 방해 안 함) 재개한다.
- `on_tank_check`(로컬)는 결과를 로그로 출력, 원격 N 명령은 `reply("N,...")`
  로 응답 — 둘 다 `_tank_check_and_resume(report)`를 공유(옛 `_query_tank`는
  삭제됨). 원격 N도 워커 미가동이면 PC 창을 불러 재개까지 수행한다.
- **회수해야 할 수량이면 재개 클릭을 생략한다(260809c):** 읽은 값이
  `_tank_needs_collect`면 — 특히 최대치 초과면 — '낚시 시작'을 눌러도 낚시가
  안 걸리므로 헛클릭 대신 로그로 알리기만 한다. 감시 워커가 돌고 있으면
  다음 사이클에 회수 루틴이 받아가고, 회수가 끝나면 거기서 낚시가 재개된다.

**재개 동작 (`_resume_fishing`):** ESC 3번(0.5초 간격, 혹시 떠 있을 팝업
정리) → `COORD_FISHING_BTN` 클릭. 미끼/낚싯대 교체 루틴의 새 진입 방식과
동일한 패턴(무조건 ESC로 정리 후 고정 좌표 클릭)이라 함수로 공유.

**살림망 수거 루틴(`run_fishing_routine`) 시작부에도 같은 확인 적용:**
기존엔 무조건 "Enter+ESC 4번 반복 → `COORD_FISHING_BTN` 클릭(낚시 취소)
→ `COORD_TANK_BTN` 클릭(살림망 확인)" 순서였는데, 이 루틴이 **낚시 중이
아닐 때** 불려도 그대로 실행돼 버튼을 잘못 눌렀다(취소해야 할 게 없는데
같은 좌표를 누르면 오히려 낚시가 새로 시작돼 버림). 이제 맨 앞에서
`is_fishing_active()`로 먼저 확인해 `False`(대기 중)로 **확실히** 판명된
경우에만 Enter/ESC 반복과 취소 클릭을 생략하고 바로 `COORD_TANK_BTN`으로
건너뛴다. `True`(진행 중)나 `None`(판독 불가)이면 안전하게 기존 그대로.

**`skip_cancel=True`면 판독조차 하지 않고 생략한다:** 낚싯대 교체 →
`_restart_or_collect_after_rod_swap` → 회수로 들어오는 경로가 이 인자를
넘긴다. 교체 루틴이 방금 ESC로 화면을 정리했고 '낚시 시작'도 누르지
않았으므로 닫을 창도 취소할 낚시도 없는 것이 **확실**하다 — OCR로 되물을
이유가 없고, 취소 좌표를 헛눌러 낚시가 새로 시작되는 위 함정도 피한다.
다른 경로(감시 루프의 일반 회수)는 기존 `is_fishing_active()` 판단 그대로.

**접속 끊김 감지 (감시 모드 전용):** 게임이 튕기면 로비 배경 + '서버와
접속이 끊어졌습니다.' 대화상자만 남아 **수량 파싱이 계속 실패**한다.
→ 수량 파싱 **실패** 분기에서 `REGION_DISCONNECT=(769,386,258,40)`을 OCR해
'서버와' 부분매칭(타이틀 conf 0.64; 본문은 0.01이라 사용 금지). 판독되면
'게임이 튕겼습니다' 출력 + ntfy 긴급 알림(priority 5) 후 **매크로 종료**
(`sys.exit(1)`). 미판독이면 평소처럼 다음 사이클 재시도.
('프로그램 종료' 버튼 중심 (960,607), conf 0.97 — 현재는 클릭하지 않음)

---

## ntfy 원격 제어 프로토콜 (domiman.py, 구현됨)

같은 ntfy 채널의 domiman끼리 서로 제어한다. 발신자 식별은 ntfy Title
(=발신 PC 이름, 영문+숫자, **채널 내 중복 금지**). 대소문자 구분,
규격 외 메시지는 무시. 이름/채널/PC 리스트는 `domiman_config.json`
(exe/스크립트 옆)에 보존, 없으면 기본값(호스트명 규칙, domi_fishing_9714).

**'ntfy 메시지' 체크박스는 기본 켜짐(260809b부터).** 켜짐/꺼짐은 설정
파일에 보존하지 않으므로 매 실행 이 기본값에서 출발한다. 기본값은
`self.var_ntfy = tk.BooleanVar(value=True)` 한 곳에만 있고, 전역
`_ntfy_enabled`는 `__init__` 끝에서 `on_ntfy_toggle()`을 그대로 한 번 불러
거기서 맞춰간다(둘을 따로 True로 적어 두면 어긋나기 쉬움 — 체크박스만
켜지고 실제 수/발신은 꺼져 있는 상태가 됨). 스트림 스레드는 0.5초 주기로
`_ntfy_enabled`를 다시 보므로 스레드를 먼저 띄워도 곧바로 구독을 시작한다.

**메시지 규격:**
- 명령: `(대상PC),(명령)[,인자]` — S(상태질의) G(시작) P(중지) Y(예약확인)
  Y,n(예약 n분, 0=해제) W(즉시회수) Q(종료) V,a|1080|1440(해상도)
  T,n(타이머) C,로그[,낚싯대,미끼](체크박스, t/f) N(실시간 수량질의)
- 응답: `(요청자),Z,...` — 요청자 무명(휴대폰)이면 `,Z,...`.
  G/P/Y/W/Q는 에코, S/V/T/C는 상태 응답
  `,Z,(타이머),(1080|1440),(a|m),(로그t/f),(실행중t/f)[,(낚싯대),(미끼)]`
  (낚싯대/미끼 필드는 타이머=0(감시 모드)일 때만. 실행중은 감시모드 여부와
  무관하게 항상 5번째 고정 위치 — 낚싯대/미끼처럼 있다 없다 하면 자리가
  밀려 파싱이 꼬이기 때문).
- 수량 응답(N): `,Z,N,(cur),(mx)` 또는 `,Z,N,fail`. **항상** 창을 앞으로 불러
  3초 뒤 파싱해 응답한다(실패면 fail). OCR/해상도 미준비도 fail. 단일 왕복
  (W처럼 ack+보고로 안 나눔), 최대 ~5초. 로컬 버튼은 로그로만 출력.
  ※ "마지막 파싱값(`_last_tank`)이 있으면 즉시 응답" 하던 캐시 우선 방식은
  260728b에서 폐기됐다(아래 `_tank_check_and_resume` 항목 참고) — 이 줄은
  그 서술이 남아 있던 것을 바로잡은 것.
- 보고(무명 브로드캐스트): `,Z,F,코드` — s(루틴시작) g(회수성공) f(회수실패)
  rs/bs(낚싯대/미끼 교체 **시작**) y,r/y,b(낚싯대/미끼 교체 성공)
  x,d/x,r/x,b(튕김/낚싯대/미끼 교체 실패).
  **교체 실패 = 대상 미감지로 좌상단 폴백 사용**(낚싯대도 폴백 있음).
  기존 영어 알림은 전부 이 코드로 대체됨. rs/bs는 260728a에서 추가(모바일
  '낚싯대/미끼 교체 시작 시 알림' 옵션이 받을 신호 — 교체 루틴 진입 즉시 발신).

**제어 측(dB) 동작:** 최상단 제어PC 버튼(로컬 낚시 실행 중엔 봉인)으로
대상 선택 → S 질의로 상태 동기화(15초 무응답이면 **로컬 복귀**).
명령 발송 후 응답까지 제어PC변경·로그·다크모드 외 전부 봉인, 15초
무응답 시 '응답이 없습니다' 로그 + '의식을 잃었습니다' 5초 표시.
시작/중지는 대기 중 회색 '대기' 표시, **응답 내용 우선**(G 보냈는데
Z,P 오면 시작 상태로). 예약 종료는 2단계(Y→Z,Y 후 창, Y,n→Z,Y,n에
창 닫힘, 취소/무응답이면 Y,0 발송). 타이머는 입력 정지 3초 후 T 발송.
Q 응답 수신 시 자동 로컬 복귀. 원격 중 이름/채널/ntfy 체크박스 봉인
(ntfy는 강제 on). **원격 제어 중 자기 앞으로 온 명령은 전부 무시.**
보고(,Z,F,*)는 제어 중인 PC의 것만 로그에 변환 표기.

**피제어 측(d3) 동작:** ntfy 체크박스 on + 로컬 모드일 때만 자기 이름
앞 명령을 처리·응답. 낚시 실행 중엔 V/T/C를 적용하지 않고 현재 상태만
응답(로컬 봉인 규칙과 동일). W는 수신 즉시 ack 후 3초 카운트다운 루틴.
Q는 응답을 동기 발신 후 종료. 상태 응답에 실행 중 여부(`self._running()`)
필드가 포함돼(구현됨), dB가 접속하면 S 질의 응답 한 번으로 시작/중지
버튼이 바로 실제 상태에 맞게 뜬다(과거엔 이 필드가 없어 일단 '시작'으로
표시됐다가 버튼을 눌러야만 교정됐음 — `_apply_remote_status`가
`remote_running_shown`을 갱신하고 `_set_start_button_remote()`를 재호출).

## ntfy 수신 = 스트리밍 구독 (폴링 아님, rate-limit 함정 주의)
수신은 **전용 데몬 스레드**(`_ntfy_stream_loop`)가 `GET {채널}/json`을 **연결
하나로 계속 열어두고**(스트리밍) 도착 메시지를 실시간으로 `_ntfy_queue`에 넣는다.
메인 스레드는 `_poll_ntfy_queue`(250ms `after`)로 **큐만 비워** `_dispatch_ntfy`로
넘긴다(tkinter는 메인 스레드에서만 조작 → 큐 경유 필수).

**왜 폴링을 버리고 스트리밍으로 갔나 (중요):** ntfy.sh 무료 한도는 **GET/POST가
같은 IP 버킷을 공유**하고(visitor=IP), 버스트 60 + **5~10초당 1개 충전**뿐이다.
과거 5초 폴링(반복 GET)은 이 충전 속도를 거의 다 먹어, **같은 공유기(같은 공인
IP)를 쓰는 휴대폰 앱의 발신(POST)이 429로 실패**했다. 스트리밍은 요청 토큰을
**연결당 1회만** 소비(그 뒤 도착 메시지는 요청 수에 안 잡힘, 동시연결 한도
`visitor-subscription-limit` 기본 30만 적용)하므로 한도에 여유가 크고 수신도
사실상 즉시다. → **폴링 주기를 줄이는 방향(2~3초)은 오히려 한도를 더 태우니 금지.**

**설계 원칙:** 전송 계층만 폴링→스트리밍으로 교체했고 프로토콜 처리
(`_dispatch_ntfy`/`_handle_command`/`_handle_remote_reply`)는 **그대로**다.
- `since` 미사용: 살아있는 연결은 메시지를 정확히 한 번만 전달 → 재연결 시
  과거 명령 **중복 실행 위험 없음**. 재연결 공백에 놓친 명령은 15초 타임아웃 후
  재시도(중복 실행보다 깔끔한 실패-재시도가 안전).
- 자기 발신분(Title=PC_NAME)은 스트림 스레드에서 걸러냄(ntfy는 발신자에게도 echo).
- `iter_lines`는 블로킹이라, ntfy 비활성화/채널 변경 시 반응이 keepalive(~45s)
  주기만큼 늦다 → `ntfy_stream_disconnect()`(연결 강제 close)로 즉시 깨운다.
  토글 off·종료 시 직접 호출, 채널 변경은 1.5초 디바운스 후 재연결.
- 발신 `ntfy_send`는 기존과 동일(POST, 기본 비동기 스레드).

**모바일(`domiman_m.py`, Kotlin 포팅 레퍼런스):** `DomimanClient.stream()`이 권장
경로(연결 하나 유지). `poll()`은 폴백으로만 남김. Kotlin은 OkHttp 스트리밍
응답 줄읽기 또는 `/sse`+EventSource로 동일 구현하고 dispatch/parse는 재사용.
`parse_status`는 PC와 동일하게 `실행중` 필드(4번째 뒤, 감시모드 무관 고정
위치)를 포함하도록 갱신됨 — PC 프로토콜과 항상 같이 맞출 것.

**모바일 UI 전면 재설계(구현됨, `앱UI설명.xlsx` 기준 — Sheet1=로그인,
Sheet2=메인 제어, A-E열=대략 레이아웃/G열=상세설명):**
- **화면 3개**: 로그인(`Screen.LOGIN`) / 최근 로그인 목록(`Screen.RECENT_LOGINS`)
  / 메인 제어(`Screen.MAIN`, PC GUI와 거의 동일 — 이 화면 버튼들은 기존
  `DomimanClient.cmd_*`를 그대로 호출하면 되므로 domiman_m.py에서 다시
  감싸지 않음).
- **로그인/최근 로그인 규칙(`LoginStore`+`LoginFlow`)**: 같은
  (id,피제어PC,채널)로 재로그인하면 기존 행을 갱신 후 맨 위로(중복 없음,
  개수 상한 없음 — 사용자 확정). 로그아웃(Sheet2에서 뒤로가기 2번)은
  자동로그인 '무장'만 해제 — 최근 로그인 목록과 로그인 폼에 채워진 마지막
  값은 그대로 유지, 이 상태로 앱을 재시작해도 자동로그인은 발동하지
  않는다(로그아웃 ≠ 데이터 삭제). '최근 로그인' 저장은 캐시/앱데이터 삭제
  시 함께 지워지는 저장소 사용(계정 백업 등 더 오래 남는 저장소 금지).
- **업데이트 버튼 안 씀(함정 — Android는 PC처럼 무음 자동교체 불가):** 새
  APK를 받아와도 OS가 강제하는 '설치' 확인 탭이 매번 1회 필요해 PC의
  `os.replace` 방식과 동일한 완전 자동은 안 된다. 사용자가 이 제약을
  감수하지 않기로 확정 — Sheet2 G13 자리는 항상 '실시간 수량확인'
  버튼(`cmd_tank_query()` 연결)이고, G18의 풀투리프레시 제스처도
  구현하지 않는다.
- 로그 창은 PC(접기 가능)와 달리 항상 펼쳐진 상태로 마지막 8줄만
  유지(`MobileLogBuffer`).

---

## domiman_m.py — 모바일용 Python 모듈 (구조·API)

**정체:** 안드로이드 앱에 Chaquopy로 내장되는 Python 모듈이자 Kotlin 포팅
레퍼런스. 737행 5개 섹션. `domiman.py`에서 **데스크톱 전용 코드(tkinter,
pyautogui/win32 클릭, WGC 캡처, OCR, 낚시 자동화 루틴)를 전부 뺀** 것으로,
① ntfy 프로토콜 ② 로그인/최근로그인 저장 규칙 ③ 화면 상태·전환 규칙만 담는다.

**'제어(dB)' 역할만 맡는다(중요):** 자기 앞으로 온 명령을 받아 실행하는
**피제어(d3) 로직(`domiman.py`의 `_handle_command`)은 아예 없다.** 휴대폰은
PC를 조종하는 쪽이지 조종당하는 쪽이 아니므로 의도된 비대칭이다.

**의존성 제약(깨지 말 것):** `requests` + 표준 라이브러리(`json/re/time/
threading/queue/dataclasses/enum/collections`)만 쓴다. Chaquopy로 APK에 넣기
때문에 플랫폼 의존 패키지를 넣으면 빌드가 막힌다. 위젯 렌더링·화면 전환
애니메이션·뒤로가기 인터셉트·Toast·알림·다크모드 감지·실제 영속 저장소는
**전부 Kotlin 책임**이고, 이 모듈은 "무엇을 언제 어떻게 바꿀지" 규칙만 갖는다.

### 섹션·API 지도

| 섹션 | 내용 |
|---|---|
| 상단 (모듈 전역) | `is_valid_id`/`is_valid_channel`(PC와 같은 정규식), `STATUS_TEXT`·`REPORT_TEXT`·`REPORT_STATUS`(domiman.py에서 **그대로 복제**), `NOTIFY_KEYS`+`notify_key_for_report` |
| `[1]` 프로토콜 | `DomimanClient` — 세션 하나(`my_id`/`target_id`/`channel`) |
| `[2]` 로그인 저장 | `SavedLogin`(dataclass), `LoginStore` |
| `[3]` 화면 상태 | `Screen`(Enum: LOGIN/RECENT_LOGINS/MAIN), `LoginFormState` |
| `[4]` 화면 전환 규칙 | `LoginFlow` |
| `[5]` 로그 창 | `MobileLogBuffer`(deque, 기본 8줄) |

**`DomimanClient`** — 명령 빌더는 전부 `send_command(body, kind)` 한 겹 위에
얹혀 있고, 보내면서 `pending={"kind","sent"}`에 들어간다(`resolve_pending`,
`check_pending_timeout` = `PENDING_TIMEOUT_SEC` 15초, PC와 동일):
`cmd_login`(=`S`, **로그인은 상태 질의다**) `cmd_start`(G) `cmd_stop`(P)
`cmd_sched_exit_ask`(Y) `cmd_sched_exit_set`(Y,n) `cmd_collect_now`(W)
`cmd_exit_program`(Q) `cmd_set_resolution`(V,a|1080|1440) `cmd_set_timer`(T,n)
`cmd_set_flags`(C,로그[,낚싯대,미끼]) `cmd_tank_query`(N).
수신은 `stream()`(권장, 연결 1개 유지) / `poll()`(폴백) / `stream_disconnect()`
(다른 스레드에서 강제 close해 블로킹 `iter_lines`를 깨움 — PC의
`ntfy_stream_disconnect`와 같은 패턴). 파싱은 `dispatch()` →
`parse_status()` / `report_text()` / `parse_tank_reply()`.

**`dispatch()`가 발신자 필터까지 겸한다:** `title != target_id`면 버리므로
**내가 보낸 메시지(Title=my_id)가 저절로 걸러진다** — PC의 스트림 루프처럼
따로 자기 발신분 제외 코드를 두지 않는다. 응답은 `{my_id},Z,...`,
보고는 `,Z,F,...`(무명 브로드캐스트) 두 갈래만 인정한다.

### Chaquopy JSON 브리지 (설계 결정 — 이 패턴을 따를 것)

Kotlin이 `Map<PyObject,PyObject>` 변환을 다루지 않도록, **Python이 JSON
문자열로만 돌려준다.** 새 반환값을 Kotlin에 넘길 일이 생기면 PyObject API를
파헤치지 말고 같은 방식으로 `*_json` 헬퍼를 추가한다.
- `attempt_login_json(client)` — 실패 시 문자열 `"null"`
- `dispatch_json(client, title, body)` — 아래 스키마
- `LoginStore.to_json()` / `from_json()` — SharedPreferences에 문자열 하나로

`dispatch_json` 반환 키: `kind`(reply|report|null) · `status` · `tank`/`tank_fail`
· `echo` · `sched_minutes` · `report_text` · `report_status_key` ·
`report_notify_key`.

**`echo` 분기가 있는 이유(함정, 재발 방지):** `G/P/W/Q/Y` 응답은 domiman.py가
상태 필드 없이 **명령 글자만 되돌려주는 에코**다(시작 성공 → `,Z,G`). 예전엔
이 분기가 없어 그 응답이 `parse_status`에서 `None`으로 버려져, **모바일에서
'시작/중지'를 눌러도 running/pending이 갱신되지 않는 버그**가 있었다.
상태 응답(S/V/T/C)은 첫 필드가 타이머 숫자라 이 글자들과 겹치지 않으므로
`rest[0]`로 안전하게 구분된다.

**`parse_status`의 '실행중' 필드는 5번째 고정(PC와 반드시 같이 맞출 것):**
낚싯대/미끼 필드처럼 있다 없다 하면 자리가 밀려 파싱이 꼬인다. PC 프로토콜을
바꾸면 이 함수도 같이 고쳐야 한다.

### 실제로 쓰이는 것 / 레퍼런스로만 남은 것

`LoginFlow`는 **앱에서 쓰지 않는다** — 화면전환 책임이 Kotlin의
Navigation3/ViewModel과 겹쳐서, `DomimanRepository`는 그보다 낮은
`LoginStore`/`DomimanClient`/`attempt_login_json`만 직접 호출한다.
`Screen`/`LoginFormState`/`LoginFlow`는 **규칙을 읽기 위한 참고 구현**으로
남겨둔 것이고, 그 안의 정책(아래)은 Kotlin 쪽에 그대로 구현돼 있다:
- `add_or_bump` — 같은 (id,피제어PC,채널)이면 갱신 후 맨 위로(중복 없음, 상한 없음)
- `update` — 수정은 **맨 위로 옮기지 않는다**(재로그인이 아니라 정보 수정이므로)
- `logout` — 자동로그인 **무장만 해제**. 목록과 폼의 마지막 값은 유지
  (로그아웃 ≠ 데이터 삭제)
- `attempt_login` — 스트림을 열고 0.3초 뒤 `cmd_login()`, 15초 대기.
  앱에서는 Foreground Service가 이미 `stream()`을 돌리므로 그 큐를 재사용하면
  되고 매번 스트림을 여닫을 필요는 없다(콜드 스타트 자동로그인용 독립 버전)

**스모크 테스트:** `python domiman_m.py <내 ID> <대상PC> <채널>` — 로그인 →
최근 로그인 저장 → 로그아웃 → 자동로그인 재시도(무장 꺼져 시도 안 되는지)까지
한 번에 확인한다. 프로토콜을 고쳤을 때 PC 없이 1차 검증하는 수단.

### ⚠️ 사본 동기화 (자동화 없음)

`domiman_m.py`는 안드로이드 프로젝트의 `app/src/main/python/`으로 **손으로
복사**해야 반영된다(`Copy-Item`, gradle 자동 태스크 없음). **원본만 고치고
끝내면 앱은 옛 코드로 계속 돈다.**

**2026-08-10 점검 결과 — 아래 문서 내용은 맞다. 다만 Kotlin 소스가 이 PC에
없다(빌드 산출물만 있다).**

**① 배포된 APK가 문서 내용을 증명한다.** `C:\Users\windo\OneDrive -
한국교통대학교\domiman.apk`(약 47MB, 2026-07-28 13:32)를 열어 확인:
- 패키지 `com.example.domiman` (dex 내 클래스 참조 348건) — 문서 그대로
- 문서가 기술한 클래스가 **전부 존재**: `DomimanApplication` `MainActivity`
  `NavigationKt` `DomimanService` `DomimanWidgetProvider` `NotificationSettings`
  `data/{DomimanRepository, DomimanNotifications, NotificationPrefs, NotifyItem,
  DomimanStatus, DispatchResult, SavedLoginJson, LoginStoreJson}`
  `ui/{login, main, notif, recent}` `theme/`
- Chaquopy(Python 3.13, `libpython3.13.so`), dex 11개
- 내장 Python은 **`assets/chaquopy/app.imy` 안의 `domiman_m.pyc` 하나뿐**
  (44,422 bytes). `attempt_login_json`·`dispatch_json`·`notify_key_for_report`
  ·`NOTIFY_KEYS`·`stream_disconnect`·`parse_tank_reply`·`LoginFlow`
  ·`MobileLogBuffer` 심볼과 이 저장소 원본의 한글 docstring이 그대로 들어 있어
  **737행 최신 원본으로 빌드된 것**이 확인된다.

  ※ 검증 방법(재사용): apk는 zip이다. `unzip -o apk 'classes*.dex'
  'assets/chaquopy/app.imy'` 후 dex를 바이너리 그대로 훑어
  `Lcom/...;` 문자열을 뽑고, `app.imy`(역시 zip)에서 `domiman_m.pyc`를 꺼내
  심볼 문자열을 확인한다.

**② 그런데 Kotlin 소스는 이 PC에 없다.** 문서에 적힌
`C:\Users\windo\dev\domiman-android`가 없고(`C:\Users\windo\dev` 자체가 없음),
`DomimanService.kt`·`NotificationPrefs.kt`·`DomimanRepository.kt`
·`DomimanWidgetProvider.kt`를 **C 드라이브 전체에서 찾아도 하나도 없다.**
`domiman_android.md`·`domiman_protocol.md`·`domiman-release.jks`도 없다.
→ **Kotlin 쪽 수정 요청은 소스를 확보하기 전에는 착수할 수 없다.** 먼저
사용자에게 실제 프로젝트 위치(다른 PC인지 등)를 확인하고 이 문서를 갱신할 것.

**③ 헷갈리기 쉬운 '가짜' 폴더 3개 — 문서의 그 프로젝트가 아니다:**
| 경로 | 정체 |
|---|---|
| `C:\Users\windo\DomimanApp` | 초기 시제품. 패키지 `com.domiman.app`, 소스 폴더 `kotlin/`, 화면 2개(`LoginScreen`/`ControlScreen`), `DomimanPollService`(폴링), 위젯·알림설정 없음, git 아님. `domiman_m.py` 사본이 **287행**(재설계 이전) |
| `C:\humble\workspace\domiman` | 또 다른 시제품(2026-07-20~21). 패키지 `com.domiman.control`, `App/Bus/ControlService/Prefs/StreamCallback.kt` + `bridge.py`. 상위 git은 무관한 ROS 워크스페이스 |
| `C:\Program Files\domiman`, `macro\dist\domiman` | **PC판 exe** 설치본/빌드 산출물. 안드로이드와 무관 |

---

**실제 안드로이드 프로젝트(구현됨) — 위치가 이 저장소 밖입니다(중요):**
`C:\Users\windo\dev\domiman-android` (Kotlin/Compose, Chaquopy 17.0.0으로
`domiman_m.py`를 그대로 내장). **이 `macro` 폴더 안이 아니다** — AGP/Chaquopy가
네이티브 빌드 도구 특성상 **비ASCII 경로에서 빌드를 거부**해서(`macro` 폴더가
"한국교통대학교" 등 한글을 포함) 별도 ASCII 전용 경로로 옮겨 시작했다. 새
기능을 추가할 때 이 사실을 잊고 `macro\domiman-android`를 찾지 말 것.

### ⚠️ 현재 안드로이드 앱 상태 = 유지 대상(권위 문서, 재설계 금지)
**새 세션에서 절대 UI/구조를 새로 설계하지 말 것.** 아래가 지금 실제로
빌드·동작 중인 최종 상태다. 사용자가 이 UI를 그대로 유지하길 원한다. 변경
요청이 있을 때만 해당 부분을 고치고, 그 외엔 이 스펙을 보존한다.

**로컬 git(백업, 원격 없음):** `C:\Users\windo\dev\domiman-android`에 git
초기화돼 있고 커밋으로 백업 중(원격 push 안 함 — GitHub 미연동). 매 기능
단위로 커밋해 둘 것. `.gitignore`가 `build/`·`.gradle/`·`local.properties`를
제외(소스+`gradle-wrapper.jar`만 추적). 최신 커밋 `e082da7`
(위젯 아이콘/토스트/런치신뢰성/앱아이콘)이 현재 상태.

**파일 트리(app/src/main):**
- `java/com/example/domiman/`
  - `DomimanApplication.kt` — Python.start + `repository` 앱 싱글턴 생성
  - `MainActivity.kt` — repository는 `(application as DomimanApplication).repository`,
    첫 실행 권한요청(알림→배터리예외), 다크모드 override 상태 보유
  - `Navigation.kt` — Navigation3. **시작 화면 = 이전 로그인돼 있었으면 Main,
    아니면 Login**(`if repository.isSessionConfigured() Main else Login`)
  - `NavigationKeys.kt` — `Login`/`RecentLogins`/`Main`/`NotificationSettings`
  - `DomimanService.kt` — 포그라운드 서비스(specialUse|dataSync)
  - `DomimanWidgetProvider.kt` — 4x1 홈 위젯
  - `data/DomimanRepository.kt` — 앱 싱글턴 핵심(세션·스트림·명령·알림·위젯 연동)
  - `data/DomimanModels.kt` — `@Serializable` DTO(DomimanStatus/DispatchResult/
    SavedLoginJson/LoginStoreJson), dispatch_json/attempt_login_json JSON 파싱
  - `data/NotificationPrefs.kt` — 알림 설정 영속(`NotifyItem` enum 10개 + master)
  - `data/DomimanNotifications.kt` — 알림 채널/발송, contentIntent로 앱 열기
  - `ui/login/`·`ui/recent/`·`ui/main/`·`ui/notif/` — 각 화면 Compose + ViewModel
  - `theme/` — 템플릿 기본 테마
- `python/domiman_m.py` — **이 저장소 `domiman_m.py`의 사본. 원본 고치면 반드시
  `Copy-Item`으로 재복사**(자동 동기화 없음).
- `res/drawable/` — `widget_bg.xml`(둥근 밝은 패널), `widget_btn_bg.xml`(누름
  하이라이트 셀렉터), `widget_ic_refresh/collect/play/pause.png`(제공된 초록 세트)
- `res/drawable-nodpi/ic_launcher_scene.png` — 앱 아이콘(록맨 낚시 장면, adaptive 배경)
- `res/layout/widget_domiman.xml`, `res/xml/domiman_widget_info.xml`(4x1)
- `res/mipmap-*` — 앱 아이콘(adaptive XML + 밀도별 png)

**화면별 정확한 UI(유지):**
- **로그인(`ui/login`, 앱UI설명 Sheet1 A1:E18):** 제목 "당신도 강태공이
  되어보세요!", 입력 3개(ID / 피제어 PC 이름 / ntfy 채널명), `[v] 자동 로그인`
  체크박스, 버튼 행 `[로그인][…]`. '…'=최근 로그인 화면. 실패 시 폼 비우고
  "입력값을 초기화하고 다시 입력하세요." edit 모드일 땐 체크박스 숨기고 버튼이
  `[수정][취소]`.
- **최근 로그인(`ui/recent`, Sheet1 A21:E39):** 상단 `< 최근 로그인`, 리스트
  행마다 `ID` + `피제어PC · 채널`. **짧게 탭=즉시 로그인**, **길게 눌러 드롭다운
  [수정]/[삭제]**. 로그인 화면에서 '자동 로그인' 체크 후 '…'로 들어와 탭하면
  그 항목도 자동로그인 무장(`repository.pendingAutoLoginArm`).
- **메인(`ui/main`, Sheet2, PC GUI와 유사):** 세로 스크롤. 순서 =
  해상도 라벨 + `[직접 설정][자동 감지]` / 타이머 입력(분, 디바운스 1.5초 발신) +
  "0을 입력하면 살림망 감시 모드로 작동합니다." / `[v]낚싯대 자동교체`
  `[v]미끼 자동교체` + "…감시 모드에서만…" / 큰 `시작|중지` 버튼(running이면
  '중지') / 상태 메시지 / `[예약 종료][즉시 회수]` / `[실시간 수량확인][다크모드]`
  / `로그` 헤더 + `x`(지우기) + 로그 8줄 패널 / 하단 `[로그아웃][알림 설정]`.
  예약 종료는 분 입력 다이얼로그→`Y,n`. 해상도 직접설정은 1080/1440 다이얼로그.
  버튼은 응답 대기(isPending) 중 잠기고 15초 무응답이면 자동 해제+"응답이
  없습니다." 로그.
- **알림 설정(`ui/notif`):** 상단 `< 알림 설정`, 마스터 `[v] 알림 켜기`(꺼지면
  아래 10개 봉인·미발송), 이하 10개 체크박스(순서 고정): 살림망 회수
  시작/성공/실패, 낚싯대 교체 시작/성공/실패, 미끼 교체 시작/성공/실패, 게임 튕김.
  기본값 전부 on, SharedPreferences 영속(앱데이터 삭제 시 소멸).

**빌드·배포 워크플로(현재 방식, 유지):**
1. `cd C:\Users\windo\dev\domiman-android; $env:JAVA_HOME="C:\Program Files\Android\Android Studio\jbr"; .\gradlew.bat assembleDebug`
2. 빌드 결과 `app\build\outputs\apk\debug\app-debug.apk`를 **손으로 복사해
   `C:\Users\windo\OneDrive - 한국교통대학교\domiman.apk`를 덮어쓴다**(사용자
   지정 배포 위치). gradle 자동복사 태스크는 두지 않음(사용자 요청).
- **구조**: `Login`/`RecentLogins`/`Main` 3화면(Navigation3), 각 화면=
  Compose 화면+ViewModel, 전부 `DomimanRepository`(Chaquopy 브리지) 공유.
  `domiman_m.py`는 `app/src/main/python/domiman_m.py`에 복사돼 있음 — **원본
  (이 저장소의 `domiman_m.py`)을 고치면 반드시 그 경로로도 재복사**할 것
  (자동 동기화 없음, 지금은 수동 `Copy-Item`).
- **Chaquopy 브리지는 JSON 문자열로만 주고받는다(설계 결정):** PyObject의
  `Map<PyObject,PyObject>` 변환이 불확실해, `domiman_m.py`에 Kotlin 전용
  헬퍼(`attempt_login_json`, `dispatch_json`, `LoginStore.to_json/from_json`)
  를 추가해 전부 JSON으로 왕복시킨다. Kotlin 쪽은 `kotlinx.serialization`으로
  파싱(`DomimanModels.kt`). 새 Python 반환값을 Kotlin에 넘길 일이 생기면
  PyObject API를 직접 파헤치지 말고 이 패턴(JSON 헬퍼 추가)을 따를 것.
- **역할 분리**: `domiman_m.LoginFlow`(화면전환 규칙)는 Kotlin의
  Navigation3/ViewModel과 책임이 겹쳐 **안 씀** — `DomimanRepository`는 그보다
  낮은 `LoginStore`/`DomimanClient`/`attempt_login_json`만 직접 호출한다.
  Python은 프로토콜+데이터 규칙, Kotlin은 화면/내비게이션 상태.
- **빌드 시 겪은 함정 3가지(재발 방지)**:
  1. 위에 적은 **비ASCII 경로 거부** — AGP가 빌드 자체를 막음(우회 설정
     `android.overridePathCheck=true`도 있지만, Chaquopy가 네이티브 링킹을
     하므로 진짜 실패 위험이 있어 경로 이전으로 해결).
  2. **Configuration Cache와 비호환** — Chaquopy가 구성 단계에서 외부
     프로세스(빌드용 python 탐지)를 실행해 Gradle configuration cache와
     충돌. `gradle.properties`의 `org.gradle.configuration-cache=false`로
     꺼둠(`android create` 템플릿 기본값은 `true`).
  3. **buildPython 자동탐지 실패** — Chaquopy는 호스트에서 빌드 스크립트를
     돌릴 Python(APK에 내장되는 타겟 Python과 별개)이 필요한데, 이 PC의 `py`
     런처 `-V:3.13` 슬롯이 (도미맨 PC 프로젝트에도 이미 기록된 함정과 같은)
     **Microsoft Store 스텁**이라 자동탐지가 실패했다. python.org 정식
     3.13(`winget install Python.Python.3.13`)을 설치해
     `chaquopy.defaultConfig.buildPython("C:/.../Python313/python.exe")`로
     경로를 명시해 해결.
  4. **DomimanRepository의 send\* 함수는 반드시 suspend + Dispatchers.IO**
     (실기기 테스트에서 발견 — 처음엔 `sendStart`/`sendSetResolution` 등이
     평범한 `fun`이라 Compose `onClick`에서 바로 호출됐는데, 이게
     `cmd_*→send_command()→requests.post()`로 이어지는 **블로킹 네트워크
     호출을 메인 스레드에서 그대로 실행**해 ntfy 발신이 버벅이거나 실패했다.
     `login()`/`attemptLogin()`은 처음부터 `withContext(Dispatchers.IO)`로
     감쌌었지만 Sheet2의 명령 버튼들은 빠뜨렸던 것 — Chaquopy로 Python
     네트워크 함수를 새로 노출할 때마다 이 패턴을 반드시 지킬 것.
- 빌드: `cd C:\Users\windo\dev\domiman-android; $env:JAVA_HOME =
  "C:\Program Files\Android\Android Studio\jbr"; .\gradlew.bat assembleDebug`
  (JAVA_HOME은 전역으로 안 걸어뒀으므로 매번 지정 필요 — [런처/코어 분리]
  절의 PATH 새로고침과 같은 이유).
- 에뮬레이터: SDK에 `emulator`+`system-images/android-36/google_apis/x86_64`
  설치 완료, `android emulator create`로 `medium_phone` AVD 생성됨(기본
  device profile 자동 선택 — 이후엔 `android emulator start medium_phone`).
  단, 실기기 테스트가 더 빨라 에뮬레이터 부팅은 중간에 보류한 적 있음.

**세션을 앱 싱글턴 + 포그라운드 서비스로 이관(구현됨 — 백그라운드 복귀
문제 해결 + 알림):** 실기기에서 "앱을 완전히 안 끄고 빠져나왔다 다시 들어가면
ntfy 발신·수신이 죽어 껐다 켜야 정상" 문제를 잡기 위해 세션 구조를 바꿨다.
- **`DomimanRepository`가 앱 싱글턴**이 됨(`DomimanApplication.repository`,
  Activity당 생성 X). 이유(함정): 예전엔 저장소가 `MainActivity`의
  `by lazy` 필드라 화면 회전/백그라운드 복귀로 Activity가 재생성되면
  `activeClient`(로그인 세션)가 사라져 이후 모든 명령이 no-op이었다.
- **스트림은 `appScope`(앱 수명 `CoroutineScope`)에서 돎** — ViewModel/Activity와
  독립. 세션 수명 = 로그인~로그아웃(더 이상 MainScreen VM의 onCleared에서
  세션을 끄지 않는다).
- **포그라운드 서비스 `DomimanService`(type=`specialUse|dataSync`)**: 로그인 시
  `beginSession()`이 서비스를 띄워 OS가 백그라운드에서 프로세스를 못 죽이게
  한다 → 스트림이 계속 살아 이벤트 알림이 오고, 다시 앱에 들어와도 세션이
  그대로. 서비스는 스트림을 직접 들지 않고(스트림은 appScope) 상시 알림으로
  프로세스만 살려둔다. `START_STICKY`라 예외적으로 죽어도 재시작되며 이때
  `reviveIfNeeded()`가 저장된 마지막 로그인으로 재접속(세션 소실 시).
  **함정 — dataSync는 Android 14+에서 하루 6시간 제한**이라 장기 방치 시
  종료→프로세스 사망했다. 그래서 34+는 시간제한 없는 `specialUse`를 쓰고(권한
  `FOREGROUND_SERVICE_SPECIAL_USE` + `<property PROPERTY_SPECIAL_USE_FGS_SUBTYPE>`),
  33 이하는 `dataSync` 폴백(매니페스트에 `specialUse|dataSync` 병기, 코드에서
  SDK로 분기). 그래도 삼성 One UI 공격적 절전은 변수라, 최후엔 설정→배터리→
  '제한 없음'에 앱 추가 권장(코드로 강제 불가).
- **위젯 탭 시 프로세스 보호(함정):** 위젯 버튼 broadcast(`onReceive`)는 반환되면
  프로세스가 곧 죽어, 15초 걸리는 재로그인이 끝나기 전에 종료돼 갱신이 안 됐다
  → `onReceive`에서 **먼저 `DomimanService.start()`로 프로세스를 붙잡은 뒤**
  명령을 appScope로 던진다.
- **복귀 리프레시**: MainScreen이 `LifecycleEventEffect(ON_RESUME)`에서
  `ensureSessionAlive()` 호출 — 세션 있으면 스트림 새로 붙이고 S 질의로 상태
  재동기화, 세션 없고 직전 로그인 상태였으면(`session_active` 영속 플래그)
  마지막 로그인으로 재접속, 그래도 실패면 로그인 화면으로.
- **스트림 즉시 중단**: `domiman_m.DomimanClient.stream_disconnect()`(열린
  응답 강제 close, PC의 `ntfy_stream_disconnect`와 동일)로 블로킹 `iter_lines`를
  깨워 로그아웃/재접속 시 옛 스레드가 안 남게 함.

**알림(구현됨):** 하단 '로그아웃' 옆 '알림 설정' 버튼 → `NotificationSettings`
화면(마스터 '알림 켜기' + 10개 이벤트 체크박스, 마스터 꺼지면 하위 봉인,
기본값 전부 on). `NotificationPrefs`(SharedPreferences, 앱데이터 삭제 시 소멸)에
영속. 실제 알림은 `DomimanRepository.onStreamMessage`가 보고(`,Z,F,*`) 수신 시
`NotificationPrefs.isEnabled(notifyKey)`면 `DomimanNotifications.postEvent`로
발송 — **화면이 아니라 저장소(앱 스코프)에서 띄우므로 백그라운드에서도 온다.**
어느 체크박스인지는 `dispatch_json`이 넣어주는 `report_notify_key`
(domiman_m.`notify_key_for_report`, NOTIFY_KEYS 역변환)로 판정. 채널 2개
(ongoing=서비스 상시/낮음, events=이벤트/기본). **알림·상시알림 탭 시 앱을
연다**(`contentIntent`=MainActivity, CLEAR_TOP).
- **첫 실행 권한 요청(MainActivity)**: POST_NOTIFICATIONS(13+) 요청 후 콜백에서
  배터리 최적화 예외(`ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`) 요청 —
  절전모드가 백그라운드 서비스/네트워크를 죽이지 않도록. `savedInstanceState
  == null`일 때만(회전 재요청 방지).

**홈 위젯 4x1(`DomimanWidgetProvider`, 구현됨):** `[새로고침]|수량|[다운로드]|[재생/중지]`.
아이콘은 제공된 초록 원형 세트(`widget_ic_*`, `A_clean_2x2_grid_icon_set...png`를
사분면 크롭). 동작: 새로고침=`cmd_tank_query`(N, 토스트 "실시간 수량 확인"),
다운로드=`cmd_collect_now`(토스트 "즉시 살림망 회수"), 재생/중지=시작·중지 토글
(running이면 정지 아이콘 `widget_ic_pause`+토스트 "매크로가 중지되었습니다",
아니면 재생 `widget_ic_play`+"매크로가 시작되었습니다"). 각 버튼 누름 하이라이트
(`widget_btn_bg`). **수량 표시는 새로고침(N 응답) 때만 갱신**(자동 폴링 없음),
running 아이콘은 상태 변할 때 갱신 — 둘 다 `DomimanRepository`가 prefs
(`widget_qty`/`widget_running`)에 쓰고 `DomimanWidgetProvider.refresh()` 호출.
**로그인 세션 없으면 수량칸에 '로그인' 표시**, 그 상태에선 모든 칸 탭이 앱을
연다(로그인 유도). **로그인 상태에선 수량칸 탭도 앱을 연다.** 위젯 명령은
`repository.widget*()` → `ensureClientReady()`(세션 없으면 마지막 로그인으로
재접속) 후 실행.

**앱 아이콘 = 둥근 록맨(구현됨):** PC의 `app.ico`(록맨 낚시 픽셀아트)를 adaptive
아이콘의 **배경 레이어로 꽉 채워** One UI가 자동으로 둥글게 마스킹
(`mipmap-anydpi-v26/ic_launcher*.xml` → background=`@drawable/ic_launcher_scene`,
foreground=투명). 구버전용 밀도별 `mipmap-*/ic_launcher(.round).png`도 둥근
버전으로 생성(기존 webp·기본 로봇 아이콘 삭제).

- **아직 안 한 것**: 시스템 뒤로가기 버튼 실제 인터셉트(`OnBackPressedCallback`
  — 지금은 화면 내 '로그아웃' 버튼으로만 2단계 확인), 위젯 아이콘 다크모드
  대비(초록 세트라 대체로 무난), 안드로이드 프로젝트 GitHub 원격 연동(현재
  로컬 git 백업만).

## 값 대장 — 상수·횟수·지연의 의도 (건드리기 전에 여기부터 볼 것)

좌표는 위 '검증된 좌표·영역' 참고. 여기는 **좌표가 아닌 값**들의 근거다.
표에 있는 값은 전부 이유가 있는 값이므로 **임의로 바꾸지 않는다.**
근거가 '미확인'인 줄을 바꿔야 할 일이 생기면 **먼저 사용자에게 묻는다.**

| 값 | 위치 | 의도 |
|---|---|---|
| `TANK_COLLECT_MARGIN = 5` | 회수 조건 `cur+5>=mx` | 가득차기 5칸 전. **'살림망이 가득 차' 오류 예방**이 목적. 폴링 간격=최소 획득 시간이라 한 사이클에 최대 1마리 → 5칸 버퍼면 절대 못 넘김 |
| ESC **3번** | 교체 루틴 진입, `_resume_fishing` | 어떤 창이 떠 있을지 모르니 **무조건 정리**. 종류 불문 |
| ESC **2번** | `_use_card` ('사용하기' 직후) | **정확히 2개**를 닫는다 — 리스트 창 + 그 밑에 깔린 팝업. 리스트가 팝업 **위에** 겹쳐 뜨므로 1번이면 팝업이 최상단 모달로 남아 이후 클릭을 삼킨다. 진입의 3번과는 **무관한 상황**이니 맞추려 들지 말 것 |
| `range(4)` Enter+ESC 연타 | `run_fishing_routine` 초반 | 혹시 떠 있을 **게임 내부 창**을 닫기 위함. 게임 창은 전부 게임 자체 레이아웃이며 **별도 Windows 창으로 뜨는 경우는 없다** |
| `skip_cancel=True` | 낚싯대 교체 → 회수 경로 | 교체 루틴이 방금 ESC로 화면을 정리했고 '낚시 시작'도 안 눌렀으므로, 위 연타와 '낚시 취소' 클릭을 **판독 없이 무조건 생략**하고 '살림망 확인'으로 직행 |
| `minsec == 1` | 낚싯대 트리거(**유일**) | 교체 필요 시 게임이 '1초'로 표시. 낚싯대 **전용** 신호라는 점이 핵심. 살림망 조건(`cur+5>=mx`)을 OR로 얹었다가 멀쩡한 낚싯대를 계속 갈아 끼워 260811a에서 제거 |
| ESC **3번** + '살림망 확인' 재클릭 | 퀴즈 확인 실패 시 재시도 | 실패했다는 건 이전 시도의 창이 남아 있다는 뜻 — '마이룸 보내기'부터 다시 눌러선 같은 화면을 헛돈다. 화면을 정리하고 퀴즈 창을 새로 연다. ESC/클릭의 기본 0.5초 대기가 곧 재시도 간격(옛 `sleep(2.0)` 대체) |
| `BAIT_MAX_PAGE_MOVES = 4` | 미끼 페이지 넘김 | 총 5페이지 탐색 |
| `same_count >= 3` | 낚시 정지 감지 | 수량이 몇 사이클 안 잡히는 건 정상이라 **약한 신호** — 그래서 바로 재개하지 않고 `is_fishing_active()`로 한 번 더 확인 |
| `retries=4, delay=0.3` | `read_tank_quantity` | 갱신 애니메이션/깜빡임을 건너뛰려 프레임 여러 번 재시도 |
| `mx > 0` (유일한 파싱 가드) | `read_tank_quantity` | 오인식 휴리스틱 **아님**. max가 0이면 `cur>=mx-5`가 항상 참이라 회수 무한 반복 |
| 값 범위 가드 **없음** | `read_tank_quantity` | `cur<=mx`, `cur<=mx*3` 모두 폐기됨. 정상 값(초과 상태)을 버려 영구 정체를 만든 전력 |
| `click_real` `delay=0.5` | **전 호출부 통일** | 클릭 후 대기는 **0.5초 하나로 통일**한다. 예전엔 대상마다 1.0/1.2/1.5/2.0/2.5로 제각각이었으나 통일하기로 결정됨. **호출부에서 `delay=`를 따로 넘기지 말 것** — 기본값을 쓴다 |
| 루틴 후 `stop_event.wait(0.5)` | 감시 루프 | **단순 대기다. OCR 시간이 아니다.** 루틴이 끝나고 화면이 안정될 여유. `time.sleep`이 아니라 `Event.wait`인 이유는 단 하나 — **'중지'를 누르면 즉시 깨어나기 위해서**. 예전 2.0/3.0에서 0.5로 통일됨 |
| `time.sleep(0.5)` | `_restart_or_collect_after_rod_swap` | 교체한 낚싯대 기준으로 수량 표시가 갱신될 여유. **갱신 자체는 즉시 된다** — 화면 지연·순간 오류 대비분 |
| `retries=3/delay=0.2`, `0<sec<=600`, `best_diff<80.0`, `max_retries=10`, `max(3.0,…)`, `last_interval=20.0`, `w*0.15`, `행Y 오차 30` | 각처 | **지연을 감안해 여유를 둔 값들이며 현재 문제없음.** 굳이 건드릴 이유 없음 |
| `fail_streak` | 감시 루프 파싱 실패 | **로그 표시 전용(의도됨).** 세기만 하고 임계 동작은 없다 — 몇 회에서 뭘 하려다 만 흔적이 아니다 |

`wait(interval)` / `wait(interval_sec)`는 위 통일 대상이 **아니다** — 폴링 주기
(최소 획득 시간)와 타이머 모드 주기 그 자체다.

---

## 코딩 스타일
- **실측·근거가 적힌 값은 건드리지 말 것 (좌표·횟수·지연·임계값).** 위 '값 대장'과
  docstring에 "왜 그 값인지"가 적혀 있으면 그건 실측으로 확정된 값이다. 바꾸라는
  지시처럼 보여도 **먼저 적힌 근거를 제시하고 확인받는다.** 값이 서로 다른 건
  상황이 달라서지, 맞춰야 할 불일치가 아니다.
  - 실제 사고 사례: `_use_card`의 ESC는 **정확히 2번**이다(리스트 창 + 밑에 깔린
    팝업 = 닫을 것이 정확히 2개. 아래 '공통 마무리' 항목에 이유가 적혀 있다).
    교체 진입 ESC×3과 숫자가 달라 "3으로 맞춰야 할 유일한 곳"처럼 보이지만 서로
    무관한 상황이다. 이 착각으로 2를 3으로 바꿨다가 되돌렸다.
  - 질문("몇 번 누르는가?")과 지시("3번으로 바꾸자")가 같이 오면 **질문에 먼저
    답한다.** 답을 내놓아야 지시가 모호하다는 게 드러난다.
- 콘솔 출력/주석 한국어. 섹션은 `# === [n. 제목] ===` 형식.
- 기존 로깅(Tee), 예외 훅, atexit 정리, 연속 실패 5회 차단, ntfy 양방향
  원격 제어(dual_input) 구조는 유지.
- 좌표는 절대 QHD 기준으로 바꾸지 말 것 — 항상 FHD 기준 + to_screen 변환.
