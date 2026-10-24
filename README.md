# LCP — Lightweight Communication Protocol

> HTTP의 텍스트 파싱 오버헤드와 MQTT의 비동기 요청 매핑 한계를 보완하기 위해 설계된
> **바이너리 헤더 + TLV 메타데이터** 기반 통신 프로토콜의 **Python 참조 구현**입니다.
> 명세 **v1.3.0** (와이어 `Version` 바이트 `0x01`)을 구현하며, 명세 원문은
> [`docs/spec/LCP_Protocol_Specification.md`](docs/spec/LCP_Protocol_Specification.md)에 그대로 들어 있습니다.

[English summary → README.en.md](README.en.md)

## 특징

- **16바이트 고정 헤더 + TLV 메타데이터 + 가변 페이로드** — 페이로드를 파싱하지 않고도 인증·라우팅 가능
- **asyncio 서버/클라이언트** — 요청/응답(`REQ`/`RES`), 이벤트(`EVT`/`EVT_ACK`), 킵얼라이브(`PING`/`PONG`)
- **Correlation ID 멀티플렉싱** — 한 연결에서 수천 개의 동시 요청, 응답 순서 무관
- **애플리케이션 백프레셔(`BUSY` 0x09)** — 토큰 버킷 · 수용 상한 · 바이트 예산, `retry_after_ms` 힌트, 클라이언트 자동 대기/재시도
- **TLS 1.3 / mTLS**, 플러그형 인증(`AUTH_TOKEN`; HS256 JWT·정적 토큰 포함, 실패 시 `UNAUTHORIZED`)
- **Strict Framer** — 명세 §6의 모든 검증(Magic/Version/Type/PING·PONG 길이/크기)을 구현한 sans-IO `FrameDecoder`
  (명세의 참조 코드를 그대로 실행해 비교하는 차분 테스트 포함)
- **JSON · Protobuf · Raw** 직렬화(Protobuf는 duck-typing이라 `protobuf` 패키지 불필요)
- CLI(`lcp serve | request | ping | inspect | encode`), 벤치마크 하니스(명세 §7), 문서화된 명세 해석 노트
- **런타임 의존성 0개** (표준 라이브러리만), 타입 힌트 완비(`mypy --strict`)

## 설치

```bash
git clone https://github.com/minirang/lcp-protocol.git
cd lcp-protocol
pip install -e .            # Python >= 3.10, 런타임 의존성 없음
pip install -e ".[dev]"     # 테스트 · 린트 · 타입체크 도구까지
```

## 빠른 시작

```python
import asyncio
from lcp import LCPClient, LCPServer, Request, Status, StatusError, status_name


async def main() -> None:
    server = LCPServer("127.0.0.1", 0)  # 포트 0: OS가 빈 포트 선택

    @server.route("/v1/user/profile")
    async def profile(request: Request):
        user_id = request.json()["id"]
        if user_id != 7:
            raise StatusError(Status.NOT_FOUND, f"no user {user_id}")
        return {"id": 7, "name": "Ada"}  # dict → JSON(0x00)으로 자동 직렬화

    async with server, LCPClient("127.0.0.1", server.port) as client:
        print(await client.call("/v1/user/profile", {"id": 7}))  # {'id': 7, 'name': 'Ada'}
        missing = await client.request("/v1/user/profile", {"id": 8})
        print(status_name(missing.status), missing.error)  # NOT_FOUND {...}


asyncio.run(main())
```

CLI로 바로 확인할 수도 있습니다.

```bash
lcp serve --port 19523 &                              # /v1/echo, /v1/time 데모 서버
lcp request /v1/echo --json '{"hello": "LCP"}'        # status: OK (0x00) + 응답 본문
lcp ping -c 3                                         # PING/PONG 왕복 시간
lcp encode req --cid 1 --path /v1/echo --json '{"a":1}'   # 프레임을 hex로 출력
lcp inspect "4c43 01 01 00 00 000b 00000001 00000007 02 0008 2f76312f6563686f 7b2261223a317d"
```

더 많은 예제는 [`examples/`](examples/)에 있습니다(에코 · 이벤트 · JWT · 백프레셔 · mTLS · 프레임 직접 다루기).

## 프로토콜 한눈에 보기

```text
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-------------------------------+---------------+---------------+
|         Magic (0x4C43)        |  Version 0x01 | Message Type  |
+---------------+---------------+---------------+---------------+
|   Serializer  |  Status Code  |        Metadata Length        |
+---------------+---------------+-------------------------------+
|                        Correlation ID                         |
+---------------------------------------------------------------+
|                        Payload Length                         |
+---------------------------------------------------------------+
|              Metadata (TLV: Tag 1B | Length 2B | Value)        |
+---------------------------------------------------------------+
|                    Payload (JSON / Protobuf / Raw)            |
+---------------------------------------------------------------+
```

모든 정수는 **빅엔디안**입니다.

| Message Type      |     값      | 의미                                                     |
| :---------------- | :---------: | :------------------------------------------------------- |
| `REQ` / `RES`     | 0x01 / 0x02 | 요청 / 응답 (`TARGET_PATH` 필요, 응답의 `Status`가 결과) |
| `EVT` / `EVT_ACK` | 0x03 / 0x04 | 이벤트 / 처리 확인 (양방향, at-least-once + 중복 억제)   |
| `PING` / `PONG`   | 0x05 / 0x06 | 킵얼라이브 (메타데이터·페이로드 길이 0 필수)             |

| Status                  |  값  | 구현 동작                                               |
| :---------------------- | :--: | :------------------------------------------------------ |
| `OK`                    | 0x00 | 정상                                                    |
| `BAD_REQUEST`           | 0x01 | 규격 위반·파싱 실패                                     |
| `UNAUTHORIZED`          | 0x02 | 인증 실패 (연속 N회 실패 시 연결 종료)                  |
| `NOT_FOUND`             | 0x04 | 라우트/이벤트 핸들러 없음                               |
| `PAYLOAD_TOO_LARGE`     | 0x08 | 페이로드 한도 초과 (응답 후 연결 종료)                  |
| `BUSY` / `RATE_LIMITED` | 0x09 | 백프레셔: `retry_after_ms` 만큼 송신 자제               |
| `INTERNAL_ERROR`        | 0x0F | 서버 내부 오류 (상세 내용은 클라이언트에 노출하지 않음) |

| Tag           |  값  | 내용                                                        |
| :------------ | :--: | :---------------------------------------------------------- |
| `AUTH_TOKEN`  | 0x01 | 인증 토큰 (UTF-8)                                           |
| `TARGET_PATH` | 0x02 | 라우팅 경로, 예: `/v1/user/profile`                         |
| `TRACE_ID`    | 0x03 | 분산 트레이싱 ID (16바이트 또는 문자열, 응답에 그대로 echo) |
| `CLIENT_ID`   | 0x04 | 클라이언트 식별자 (인증되지 않은 참고 정보)                 |

## 가이드

### 핸들러와 반환값

핸들러는 동기/비동기 모두 가능하고, `Request`를 받아 다음 중 하나를 돌려줍니다.

| 반환값                                    | 응답                                 |
| :---------------------------------------- | :----------------------------------- |
| `None`                                    | 빈 페이로드, `OK`                    |
| `dict` / `list` / `str` / 숫자            | JSON (0x00)                          |
| `bytes`                                   | Raw (0x02)                           |
| protobuf 메시지                           | Protobuf (0x01)                      |
| `Encoded(payload, serializer)`            | 이미 직렬화된 페이로드를 그대로 전송 |
| `Reply(body, status=..., serializer=...)` | 상태 코드까지 직접 지정              |

`raise StatusError(Status.NOT_FOUND, "…", details=…)` 로 임의의 상태를 돌려줄 수 있고, 그 외 예외는
`INTERNAL_ERROR`로 바뀌어 전송됩니다(예외 메시지는 서버 로그에만 남습니다).
라우트 패턴: 정확한 경로, `/prefix/*`(가장 긴 접두사 우선), `*`(기본).

### 이벤트 (`EVT` / `EVT_ACK`)

```python
@server.on_event("/v1/events/click")
async def on_click(request: Request) -> None: ...


ack = await client.send_event("/v1/events/click", {"x": 1})  # 핸들러 실행 후 ACK
await server.broadcast_event("/v1/news", {"headline": "hi"})  # 서버 → 모든 클라이언트


@client.on_event("/v1/news")
async def on_news(request: Request) -> None: ...
```

ACK 타임아웃 시 `send_event(..., retries=3)`은 **같은 Correlation ID**로 재전송하고, 수신 측은 최근 ID
(`event_dedupe_window`, 기본 1024)를 기억해 핸들러를 다시 실행하지 않습니다. 핸들러가 `BUSY`를 돌려준 이벤트는
처리되지 않은 것으로 간주되어 자동 재시도됩니다.

### 인증

```python
from lcp import HS256JWTAuthenticator, StaticTokenAuthenticator, issue_hs256_jwt

server = LCPServer(authenticator=HS256JWTAuthenticator("32바이트 이상의 비밀키", audience="my-api"))
client = LCPClient(
    auth_token=issue_hs256_jwt("32바이트 이상의 비밀키", {"sub": "ada", "aud": "my-api"})
)
```

인증기는 `(token) -> Principal | bool | None`(동기/비동기) 형태의 호출 가능 객체이면 됩니다. `AUTH_TOKEN`은
`REQ`/`EVT`마다 검증되며 `PING`/`PONG`에는 필요 없습니다. 번들된 JWT 검증기는 HS256 전용(`alg: none` 거부, 서명 선검증,
`exp` 필수)의 소형 구현이므로 운영 환경에서는 검증된 JWT/OIDC 라이브러리를 같은 인터페이스 뒤에 연결하세요.
`SessionConfig(max_auth_failures=1)`이면 첫 실패 직후 소켓을 닫습니다(명세 §2.1).

### 백프레셔

서버는 세 가지로 과부하를 판단하고 `BUSY` + `retry_after_ms`로 응답합니다.

- `SessionConfig(rate_limit=RateLimit(rate=1000, burst=200))` — 연결당 토큰 버킷
- `max_in_flight` (수용 상한, 기본 1024) · `max_in_flight_bytes` (대기 페이로드 바이트 예산, 기본 64 MiB)
- `max_concurrency` (동시 실행 핸들러, 기본 128; 초과분은 거절하지 않고 대기열에서 순서를 기다림)

클라이언트는 `BUSY`를 받으면 해당 세션의 **모든 송신을 힌트만큼 멈추고**(지터 포함) 최대 `busy_retries`(기본 3)회 재시도한 뒤,
그래도 `BUSY`면 `ServerBusyError`(`call()`) 또는 `Response.status == BUSY`(`request()`)로 알립니다.

### TLS / mTLS

```python
from lcp import create_server_ssl_context, create_client_ssl_context

server = LCPServer(
    "0.0.0.0",
    19523,
    ssl=create_server_ssl_context("server.pem", client_ca="ca.pem", require_client_cert=True),
)
client = LCPClient(
    "service.internal", 19523, ssl=create_client_ssl_context(cafile="ca.pem", certfile="client.pem")
)
```

헬퍼는 **TLS 1.3 이상**만 허용하고 서버 인증서·호스트명을 검증합니다. TLS 없이 루프백이 아닌 주소에 바인딩하면 경고가
발생합니다(명세: 상용 환경 TLS 필수). 개발용 인증서는 `python scripts/gen_dev_certs.py certs/`로 만들 수 있습니다.

### 연결 관리와 방어

모두 `SessionConfig`에서 조정합니다.

| 설정                              | 기본값 | 동작                                                 |
| :-------------------------------- | :----: | :--------------------------------------------------- |
| `max_payload`                     | 16 MiB | 초과 시 헤더만 보고 `PAYLOAD_TOO_LARGE` 응답 후 종료 |
| `idle_timeout` (서버)             | 120 s  | 바이트가 오지 않으면 종료                            |
| `frame_timeout`                   |  30 s  | 시작된 프레임이 끝나지 않으면 종료 (slow-loris 방어) |
| `write_timeout`                   |  30 s  | 상대가 읽지 않으면 연결 중단                         |
| `handler_timeout`                 |  30 s  | 핸들러 실행 상한                                     |
| `keepalive_interval` (클라이언트) |  30 s  | `PING` 전송, `PONG` 없으면 중단                      |
| `max_bad_requests`                |   16   | 잘못된(비치명적) 프레임이 이 횟수를 넘으면 종료      |
| 서버 `max_connections`            | 10,000 | 초과 연결에는 `BUSY`(correlation id 0) 후 종료       |

치명적 위반(Magic·Version·Type·PING/PONG 길이·크기)은 명세대로 연결을 종료합니다. 종료 직전의 오류 응답이 TCP RST로
유실되지 않도록 짧은 *lingering close*를 수행합니다. 자세한 표는 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)를 보세요.

### 프레임 직접 다루기 (sans-IO)

```python
from lcp import Frame, FrameDecoder, MessageType, Metadata

wire = Frame(MessageType.REQ, 42, metadata=Metadata(target_path="/v1/ping"), payload=b"{}").encode()
decoder = FrameDecoder()
decoder.feed(wire[:5])
decoder.feed(wire[5:])  # TCP는 임의의 조각으로 도착해도 됨
for frame in decoder:
    print(frame)
```

`FrameDecoder.next_raw()`는 명세 §6 참조 프레이머와 동일한 헤더 검증만 수행하고, `next_frame()`은 여기에 Serializer·TLV
검증을 더합니다. 명세의 `StrictLCPFramer` 인터페이스는 `lcp.StrictLCPFramer`로 호환 제공됩니다.

## 프로젝트 구조

```text
src/lcp/
  constants.py enums.py errors.py     명세 상수 · Status/MessageType/Serializer/Tag · 예외
  metadata.py frame.py framer.py      TLV 메타데이터 · Frame · 증분 디코더(StrictLCPFramer 포함)
  codec.py                            JSON / Protobuf / Raw 직렬화기
  session.py                          프로토콜 엔진 (상관관계, 인증, 백프레셔, 타임아웃, 종료)
  server.py client.py router.py       LCPServer · LCPClient · 라우팅
  auth.py ratelimit.py tls.py         인증기 · 토큰 버킷 · SSL 컨텍스트 헬퍼
  cli.py                              lcp 명령
docs/    spec/(명세 원문) · IMPLEMENTATION_NOTES · SPEC_COMPLIANCE · ARCHITECTURE · BENCHMARKS
tests/   단위 · 속성(hypothesis) · 차분 · 종단간 · TLS · CLI 테스트
benchmarks/  examples/  scripts/
```

## 개발

```bash
pip install -e ".[dev]"
pytest                      # 전체 테스트 (hypothesis 포함)
ruff check . && ruff format --check .
mypy                        # --strict
python -m benchmarks.bench --sizes 100,1024 --concurrency 1,16 --duration 2
```

## 알려진 한계와 로드맵

- 자동 재연결 없음 (연결이 끊기면 `ConnectionClosedError`) · 스트리밍 페이로드 없음(프레임 전체를 메모리에 버퍼링)
- Protobuf 스키마 협상·코드 생성은 범위 밖 (codec은 `SerializeToString`/`FromString`만 사용)
- 벤치마크는 LCP · HTTP/1.1 베이스라인만 포함 (HTTP/2·gRPC, MQTT 대상은 확장 지점만 제공)
- **순수 파이썬 구현의 성능은 최소 HTTP/1.1 베이스라인보다 낮습니다** — [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) 참고.
  바이너리 프레이밍의 이점은 컴파일 언어 구현(Go/Rust 등)에서 의미가 있으며, 이 저장소는 의미론의 기준 구현입니다.
- CI는 Linux에서만 검증 (Windows/macOS 미검증)

명세에서 모호하거나 모순되는 부분과 이 구현의 결정은 [`docs/IMPLEMENTATION_NOTES.md`](docs/IMPLEMENTATION_NOTES.md),
조항별 구현·테스트 대응표는 [`docs/SPEC_COMPLIANCE.md`](docs/SPEC_COMPLIANCE.md)에 정리했습니다.

## 라이선스

[MIT](LICENSE)
