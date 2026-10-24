# Changelog

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/), 버전은 [SemVer](https://semver.org/lang/ko/)를 따릅니다.
패키지 버전은 명세 버전(1.3.0)·와이어 버전(`0x01`)과 별개입니다.

## [0.1.0] - 2026-10-21

### 추가
- LCP 명세 v1.3.0 전체 구현: 16바이트 헤더, TLV 메타데이터, JSON/Protobuf/Raw 직렬화기, `REQ`/`RES`, `EVT`/`EVT_ACK`, `PING`/`PONG`
- sans-IO `FrameDecoder`(명세 §6 Strict Framer 동등) 및 호환 래퍼 `StrictLCPFramer`
- asyncio `LCPServer` / `LCPClient` / `Session`: Correlation ID 멀티플렉싱, 우아한 종료, 서버 푸시 이벤트, 이벤트 중복 억제
- 백프레셔(`BUSY` 0x09): 토큰 버킷, 수용 상한, 바이트 예산, 핸들러 동시성 제한, `retry_after_ms`, 클라이언트 송신 게이트와 자동 재시도
- TLS 1.3 / mTLS 헬퍼, 플러그형 인증기(정적 토큰, HS256 JWT)
- 방어 장치: `AUTH_TOKEN`의 repr/로그 마스킹, 헤더 단계 크기 검증, 프레임/유휴/쓰기/핸들러 타임아웃, 쓰기 버퍼 상한, 잘못된 프레임 예산, lingering close
- CLI `lcp serve | request | ping | inspect | encode`, 예제 6종, 개발용 인증서 스크립트
- 벤치마크 하니스(LCP · LCP-raw · HTTP/1.1)
- 테스트: 명세 표 파싱 적합성, 골든 벡터, hypothesis 속성 테스트, 명세 참조 프레이머 차분 테스트, 종단간·TLS·CLI 테스트
- 문서: 구현 노트(명세 불일치와 결정), 명세 준수 대응표, 아키텍처, 벤치마크
