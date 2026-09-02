# domiman 웹 원격제어 (`cheongbaek.github.io/domiman`)

브라우저에서 피제어 PC(낚시 매크로)를 조작한다. **모바일 앱(`domiman_m.py` +
Kotlin)의 메인 화면과 같은 기능**을 PC·휴대폰 한 화면으로 제공하며, 위젯·알림은
없다(브라우저에는 포그라운드 서비스가 없다).

| 파일 | 역할 |
|---|---|
| `domiweb.py` | BGOD에서 상시 구동되는 **중계 서버**(브라우저 ↔ domiserver) |
| `web/` | 정적 웹앱(Vite + React + TS). GitHub Pages로 배포 |
| `.github/workflows/deploy-pages.yml` | `web/**` 변경 시 Pages 배포(`BASE_PATH=/domiman/`) |

```
브라우저 ──wss://domiman.duckdns.org:47822/ws──▶ domiweb.py ──TLS 47821──▶ domiserver
  (GitHub Pages에서 받은 정적 파일)                                            ▲
                                              피제어 PC(seoul·chungju·domi) ──┘
```

## 왜 중계인가 (설계 결정 — 되돌리지 말 것)

1. **브라우저는 raw TCP를 열 수 없다.** domichat은 길이접두 TCP 프레임이라
   그대로는 붙지 못한다 → WebSocket 한 겹이 반드시 필요하다.
2. **브라우저는 지문 고정(TOFU)을 할 수 없다.** domiserver의 자체 서명 인증서로는
   `wss://`가 **경고 없이 그냥 실패**한다(사용자가 예외를 누를 화면조차 없다).
   그래서 domiweb만 **브라우저가 신뢰하는 인증서**(Let's Encrypt)를 든다.
   GitHub Pages는 항상 https이므로 `ws://` 평문은 혼합 콘텐츠로 차단된다.
3. **domiserver는 같은 ID 동시 접속을 불허한다.** 브라우저마다 로그인시키면 기기
   하나만 쓸 수 있다 → domiweb 하나가 `web` 계정으로 붙고 브라우저 여러 대를
   그 세션에 **다중화**한다. 덕분에 **웹앱에는 계정도 비밀번호도 없다**(공개
   정적 사이트에 자격을 박는 문제가 구조적으로 사라진다).
4. domiserver·domiman.py는 **한 줄도 고치지 않는다.** 피제어 PC는 `{sender},Z,...`로
   답하고 sender는 서버가 찍는 `from`이므로, 웹 계정이 끼어도 규격이 그대로다.

## 프로토콜

- domiweb ↔ domiserver: `domichat.md` 규격 그대로(방 `domi_fishing_{PC}`, 고정 비번).
- 브라우저 ↔ domiweb: JSON over WebSocket.
  - 받는 것: `hello` / `select{pc}` / `cmd{pc,body}` / `add_pc{pc}` / `del_pc{pc}`
  - 주는 것: `ready` / `snap` / `msg{pc,body}` / `pcs` / `pc` / `up` / `shot` / `err`
- **방에서 온 원문(`web,Z,...` 등)은 해석하지 않고 그대로 넘긴다.** 파싱은
  `web/src/lib/protocol.ts`가 한다 — 규격의 소유자를 늘리지 않기 위해서다.
  domiweb가 구분하는 것은 캐시를 위한 접두어 셋(상태 응답·수량 방송·보고)뿐이다.
- 예외는 **스크린샷**이다: `'B'` 이진 청크를 domiweb가 조립해 크기·sha256을 확인한
  뒤 완성된 PNG를 base64로 넘긴다(브라우저에 청크 조립 로직을 또 두지 않는다).
  사진은 **요청한 브라우저에게만** 준다(남이 찍은 3MB가 갑자기 뜨지 않게).

## domiweb.py의 함정 (이미 값을 치른 것들)

| 항목 | 이유 |
|---|---|
| 접속 직후 `settimeout(READ_TIMEOUT=60)` | `create_connection`의 타임아웃이 소켓에 남으면 조용할 때마다 끊긴다(domichat.md '최대 함정') |
| **TLS 1.2 고정**(상류·하류 모두) | 연결마다 수신 스레드와 송신 스레드가 한 소켓을 나눠 쓴다 — 1.3의 NewSessionTicket/KeyUpdate가 record layer를 깬다 |
| 발신 스로틀 `12건/10초` | domiserver는 한 연결에 `MSG_BURST=20/10초`. 브라우저 여러 대의 명령이 **한 연결로 합쳐지므로** 쉽게 넘긴다 |
| 상류 끊김 시 대기 큐 폐기 | 재접속 뒤 늦게 나가는 `G`(낚시 시작)는 사용자가 이미 포기한 명령이라 위험하다 |
| 명령 화이트리스트(`CMD_RE`) | 누구나 붙는 공개 중계다. 방에 흘려보낼 문자열을 domiman 명령 규격으로만 제한해 채팅방 스팸 통로가 되지 않게 한다 |
| 콘솔 없이 떠도 종료하지 않음 | `input()`이 EOF를 받으면 그대로 죽었다 — 작업 스케줄러로 띄우면 즉시 종료된다 |
| 설정을 `utf-8-sig`로 읽음 | PowerShell `Set-Content -Encoding UTF8`은 **BOM을 붙인다.** 그냥 utf-8로 읽으면 설정이 조용히 기본값으로 돌아가 인증서 경로가 비고 평문 ws로 열린다 |
| 인증서 mtime 감시 → 자동 재적재 | Let's Encrypt는 60~90일마다 갱신된다. 재시작이 필요한 구조면 어느 날 조용히 만료된다 |
| 방이 없는 PC는 60초마다 재조회 | 그 PC가 domichat에 접속한 적이 없으면 방이 없다. 나중에 켜면 저절로 붙어야 한다 |

## 웹앱 쪽 함정

- **서비스워커를 넣지 않는다.** 같은 오리진(`cheongbaek.github.io`)에 남은 옛 PWA
  워커가 화면을 캐시에서 돌려주던 사고가 `find_wc`에 기록돼 있다. `main.tsx`가
  기존 등록을 걷어내는 그물까지 둔다.
- **스냅샷 재생은 응답 대기(pending)를 풀지 않는다**(`applyBody(body, hist=true)`).
  나중에 붙은 브라우저가 옛 응답으로 pending을 소모하면 UI가 잘못 열린다.
- **수량 방송(`,Z,N,*`)도 pending을 풀지 않는다** — 요청 없이 사이클마다 온다.
- 모바일 브라우저는 백그라운드 탭의 소켓을 얼린다 → `visibilitychange`에서 즉시
  재접속하고 `select`로 상태를 다시 받는다(앱의 `resync`와 같은 취지).
- 사진 Blob URL은 새 사진을 받을 때·창을 닫을 때 `revokeObjectURL`한다(안 풀면
  사진마다 2MB가 남는다).
- 테마는 세 상태(system/light/dark)다. 색은 **모두 bare `:root`에 정의**하고
  다크에서 같은 토큰만 덮어쓴다 — 미디어쿼리 안에만 정의하면 토글이 깨진다.
- 접속 주소는 `web/src/lib/relay.ts`의 `DEFAULT_WS` 한 줄이며, 개발 중에는
  `?ws=ws://localhost:47822/ws`로 덮어쓴다.

## BGOD 설치 절차

1. **domichat 계정 `web`** 생성 후 서버 콘솔 `approve web`. 그 계정으로 다른
   클라이언트가 접속해 있으면 domiweb가 붙지 못한다(`online` → `kick web`).
2. **호스트명·인증서**: DuckDNS(`domiman.duckdns.org` → 211.196.44.3) +
   Posh-ACME(DNS-01, `-UseSerialValidation` 필수 — DuckDNS는 TXT를 하나만 둔다).
   `Submit-Renewal`을 매일 도는 작업으로 걸어 둔다.
3. `domiweb.py`를 아무 폴더에 두고 그 옆에 `domiweb.json`을 만든다
   (`certfile`은 **`fullchain.cer`**, `keyfile`은 `cert.key`).
4. **domiserver를 띄운 것과 같은 `python.exe`로 실행**한다 — 방화벽이 프로그램
   이름으로 열려 있어 그래야 포트 규칙이 필요 없다.
5. 브라우저로 `https://domiman.duckdns.org:47822/` → `domiweb ... 살아 있습니다`와
   **경고 없는 자물쇠**가 보이면 인증서가 먹은 것이다. 여기서 실패하면 웹앱도 못 붙는다.
6. GitHub: `Settings → Pages → Source = GitHub Actions`.

## 검증 기록 (260902a)

격리 서버(`domiserver.py` 사본, 포트 47899 평문) + 피제어 PC 흉내로:
- 명령 왕복(S/G/P/W/N/V/C/T/Y/I), 상태·수량 방송·보고 전달
- 브라우저 2대 동시 접속 — 방 메시지는 둘 다, **사진은 요청자만**
- 스크린샷 150KB **sha256 일치**, 규격 밖 명령 차단, PC 목록 추가·삭제
- `wss`(TLS 1.2) 핸드셰이크, 인증서 파일 교체 시 재적재
- 실제 Chromium으로 라이트/다크 · PC(1280px)/모바일(430px) 두 폭에서 전 흐름
  (수량확인 → 시작/중지 → 스크린샷 창 → 해상도 대화상자 → 예약 종료 2단계)
  **콘솔 오류 0**
