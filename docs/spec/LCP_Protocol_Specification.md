# LCP (Lightweight Communication Protocol) 명세서

**버전:** 1.3.0  
**상태:** 표준 초안 (Draft Standard)  
**기반 전송 계층:** TCP / TLS (Stream-oriented)  

---

## 1. 개요 (Overview)

LCP(Lightweight Communication Protocol)는 기존 **HTTP**의 무거운 텍스트 파싱 오버헤드와 **MQTT**의 비동기 요청 매핑 한계를 보완하기 위해 설계된 **고성능·고효율 커스텀 통신 프로토콜**입니다.

* **인간 친화성:** 페이로드 영역에 표준 텍스트 기반 포맷(JSON 등)을 사용하여 개발자가 로그 및 디버깅을 쉽게 수행할 수 있습니다.
* **컴퓨터 친화성:** 4바이트 정렬(Alignment)이 적용된 고정 16바이트 바이너리 헤더와 명확한 TLV 메타데이터 영역을 두어, 페이로드 전체를 파싱하지 않고도 초고속 라우팅 및 인증 검사를 수행합니다.

---

## 2. 보안 및 전송 계층 (Security & Transport)

### 2.1 LCP over TLS (LCPS)
* **보안 필수의무:** 상용 환경에서 LCP 통신은 **TLS 1.3 이상** 적용을 필수 원칙으로 합니다 (`LCP over TLS`).
* **인증서 검증:** 클라이언트는 서버의 TLS 인증서 유효성을 검증해야 하며, 높은 보안이 요구되는 서비스 간 통신(mTLS) 시 Mutual TLS를 활성화합니다.
* **토큰 전송 및 보안:** 
  * 인증 토큰(예: JWT)은 메타데이터 영역의 `AUTH_TOKEN` (Tag `0x01`)에 탑재되어 전달됩니다.
  * 인증 실패 시 서버는 `Status Code = 0x02 (UNAUTHORIZED)`를 반환하고, 필요 시 소켓 연결을 즉시 종료할 수 있습니다.

---

## 3. 패킷 구조 (Packet Structure)

LCP 패킷은 **16바이트 고정 헤더**, 가변 **메타데이터(Metadata)**, 가변 **페이로드(Payload)** 3단계 구조로 구성되며 **4바이트 정렬(4-Byte Alignment)**을 완벽히 준수합니다.

### 3.1 패킷 오프셋 레이아웃

```text
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|         Magic Number          |    Version    | Message Type  |  [0 - 3 Bytes]
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|   Serializer  |  Status Code  |        Metadata Length        |  [4 - 7 Bytes]
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                        Correlation ID                         |  [8 - 11 Bytes]
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                        Payload Length                         |  [12 - 15 Bytes]
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                            Metadata                           |  (TLV Format)
|               (Tag-Length-Value Binary Sequence)              |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                            Payload                            |  (Dynamic)
|                       (JSON / Binary...)                      |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

### 3.2 고정 헤더 필드 규격 (16 Bytes)

| 필드명 | 크기 | 데이터 타입 | 검증 규정 |
| :--- | :---: | :---: | :--- |
| **Magic Number** | 2 Bytes | `uint16` | 반드시 `0x4C43` ('LC'). 불일치 시 연결 강제 종료 |
| **Version** | 1 Byte | `uint8` | 현재 버전 `0x01`. 지원하지 않는 버전 시 `BAD_REQUEST` 후 연결 종료 |
| **Message Type** | 1 Byte | `uint8` | `0x01` ~ `0x06` 범위 검증 |
| **Serializer** | 1 Byte | `uint8` | `0x00`: JSON, `0x01`: Protobuf, `0x02`: Raw Binary |
| **Status Code** | 1 Byte | `uint8` | 처리 결과 상태 코드 (표 4.2 참조) |
| **Metadata Length** | 2 Bytes | `uint16` | TLV 메타데이터 바이트 길이를 나타냄 (최대 65,535 바이트) |
| **Correlation ID** | 4 Bytes | `uint32` | 비동기 요청/응답 매핑 식별자 |
| **Payload Length** | 4 Bytes | `uint32` | 페이로드 크기 (최대 16MB) |

---

## 4. 메타데이터 규격 (TLV Format)

메타데이터 파싱 모호성을 제거하기 위해 **TLV (Tag-Length-Value)** 바이너리 레이아웃을 엄격히 적용합니다.

```text
+------------------+--------------------+-----------------------+
|  Tag (1 Byte)    |  Length (2 Bytes)  |  Value (N Bytes)      |
+------------------+--------------------+-----------------------+
```

### 표준 태그 정의
* `0x01` (`AUTH_TOKEN`): 인증 토큰 (UTF-8 String)
* `0x02` (`TARGET_PATH`): 라우터 API 경로 (예: `/v1/user/profile`)
* `0x03` (`TRACE_ID`): 분산 트레이싱 식별자 (16 Bytes / String)
* `0x04` (`CLIENT_ID`): 클라이언트 고유 식별자

---

## 5. 메시지 타입, 상태 코드 및 흐름 제어

### 5.1 메시지 타입
* `0x01` (`REQ`), `0x02` (`RES`), `0x03` (`EVT`), `0x04` (`EVT_ACK`), `0x05` (`PING`), `0x06` (`PONG`)

### 5.2 상태 코드 & 애플리케이션 흐름 제어
| 코드 (Hex) | 상수명 | 설명 |
| :--- | :--- | :--- |
| `0x00` | `OK` | 정상 처리 |
| `0x01` | `BAD_REQUEST` | 패킷 규격 위반 / 파싱 실패 |
| `0x02` | `UNAUTHORIZED` | 인증 실패 / 토큰 유효성 오류 |
| `0x04` | `NOT_FOUND` | 요청 대상 리소스 없음 |
| `0x08` | `PAYLOAD_TOO_LARGE` | 허용 페이로드(16MB) 초과 |
| `0x09` | `BUSY` / `RATE_LIMITED` | **애플리케이션 백프레셔:** 서버 처리 한도 초과 시 송신 자제 요청 |
| `0x0F` | `INTERNAL_ERROR` | 서버 내부 처리 오류 |

---

## 6. Strict Framer 구현 (Python)

명세의 모든 규격(PING/PONG 길이 검증, 유효 범위 체크)을 엄격히 준수하는 파싱 가이드 코드입니다.

```python
import struct

MAGIC_NUMBER = 0x4C43
HEADER_SIZE = 16
MAX_PAYLOAD_LIMIT = 16 * 1024 * 1024  # 16 MB

# Message Types
MSG_REQ = 0x01
MSG_RES = 0x02
MSG_EVT = 0x03
MSG_EVT_ACK = 0x04
MSG_PING = 0x05
MSG_PONG = 0x06


class StrictLCPFramer:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes):
        self.buffer.extend(data)

    def parse_frame(self, sock):
        if len(self.buffer) < HEADER_SIZE:
            return None

        magic, ver, msg_type, serializer, status, meta_len, corr_id, payload_len = struct.unpack(
            ">HBBBBHII", self.buffer[:HEADER_SIZE]
        )

        # 1. Magic Number 검증
        if magic != MAGIC_NUMBER:
            self._abort(sock, f"Invalid Magic Number ({hex(magic)}). Stream corrupted.")

        # 2. Version 및 Message Type 유효성 검증
        if ver != 0x01:
            self._abort(sock, f"Unsupported protocol version: {ver}")

        if not (MSG_REQ <= msg_type <= MSG_PONG):
            self._abort(sock, f"Invalid Message Type: {msg_type}")

        # 3. PING / PONG 검증 규칙 (MetaLen = 0, PayloadLen = 0)
        if msg_type in (MSG_PING, MSG_PONG):
            if meta_len != 0 or payload_len != 0:
                self._abort(
                    sock,
                    f"PING/PONG must have 0 MetaLen and PayloadLen (got {meta_len}, {payload_len})",
                )

        # 4. DoS 방어 (크기 검증)
        if payload_len > MAX_PAYLOAD_LIMIT:
            self._abort(sock, f"Payload limit exceeded ({payload_len} bytes)")

        total_frame_size = HEADER_SIZE + meta_len + payload_len

        # 데이터 충족 여부 확인
        if len(self.buffer) < total_frame_size:
            return None

        # 프레임 추출
        metadata = bytes(self.buffer[HEADER_SIZE : HEADER_SIZE + meta_len])
        payload = bytes(self.buffer[HEADER_SIZE + meta_len : total_frame_size])

        del self.buffer[:total_frame_size]

        return {
            "version": ver,
            "msg_type": msg_type,
            "serializer": serializer,
            "status": status,
            "corr_id": corr_id,
            "metadata": metadata,
            "payload": payload,
        }

    def _abort(self, sock, reason: str):
        self.buffer.clear()
        if sock:
            sock.close()
        raise ValueError(f"[LCP Protocol Error] {reason}")
```

---

## 7. 벤치마크 및 비교 측정 가이드라인 (v1.5 준비)

LCP 성능 검증을 위해 아래 조건으로 벤치마크 테스트 환경을 구성합니다:
1. **비교 대상:** HTTP/1.1 (JSON), HTTP/2 (gRPC / Protobuf), MQTT (QoS 1)
2. **측정 지표:** 
   * TPS (초당 처리 건수) & Latency (p50, p99 지연 시간)
   * 연결당 CPU/메모리 사용량 및 GC 할당 횟수 (Allocations/op)
3. **조건:** 동일 메시지 크기 (100B, 1KB, 64KB) 및 동일 동시 접속 수 (1,000 ~ 50,000 Conns)