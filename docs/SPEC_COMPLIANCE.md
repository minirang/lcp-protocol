# 명세 준수 대응표

명세 v1.3.0의 각 요구사항이 어디에 구현되어 있고 어떤 테스트가 검증하는지 정리한 표입니다.
명세와 어긋나거나 명세가 침묵하는 부분은 [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md)의 번호(E#, D#)로 연결됩니다.

| § | 요구사항 | 구현 | 검증 테스트 |
| :-- | :-- | :-- | :-- |
| 1 | 고정 16바이트 바이너리 헤더 + TLV + 페이로드 | `constants.HEADER_STRUCT`, `frame.Frame` | `test_spec_conformance::test_header_field_offsets_and_big_endian`, `test_golden_vectors` |
| 1 | 페이로드는 텍스트 포맷(JSON 등) 사용 가능 | `codec.JsonCodec` | `test_codec::test_json_is_compact_and_keeps_unicode` |
| 1 | 페이로드를 파싱하지 않고 라우팅·인증 | `Session._run_handler`가 메타데이터만으로 인증·라우팅 후 핸들러가 `request.body()`를 호출할 때 디코딩 | `test_auth_e2e::test_unauthorized_never_runs_the_handler` |
| 2.1 | 상용 환경 TLS 1.3 이상 | `tls.create_*_ssl_context` (`minimum_version = TLSv1_3`) · 평문 비루프백 경고 (D17) | `test_tls::test_tls13_roundtrip`, `test_tls12_only_clients_are_rejected`, `test_plaintext_on_a_public_address_warns` |
| 2.1 | 서버 인증서 검증, mTLS | `create_client_ssl_context`, `create_server_ssl_context(require_client_cert=True)` | `test_tls::test_untrusted_server_certificate_is_rejected`, `test_host_name_is_verified`, `test_mutual_tls_requires_a_client_certificate` |
| 2.1 | `AUTH_TOKEN`(0x01)으로 토큰 전달, 실패 시 `UNAUTHORIZED`(0x02), 필요 시 연결 종료 | `Session._authenticate`, `SessionConfig.max_auth_failures` (D8) | `test_auth_e2e` 전체, `test_connection_is_closed_after_repeated_failures` |
| 3.2 | Magic `0x4C43` 불일치 → 연결 강제 종료 | `FrameDecoder.next_raw` (E6) | `test_framer::test_fatal_violations`, `test_protocol_violations::test_bad_magic_closes_without_any_reply` |
| 3.2 | Version `0x01`만 지원, 아니면 `BAD_REQUEST` 후 종료 | 〃 (E3) | `test_protocol_violations::test_header_violations_get_bad_request_then_close` |
| 3.2 | Message Type `0x01`~`0x06` 검증 | 〃 | `test_fatal_violations`, `test_protocol_violations` |
| 3.2 | Serializer `0x00` JSON / `0x01` Protobuf / `0x02` Raw | `enums.Serializer`, `codec` (D18) | `test_codec`, `test_unknown_serializer_is_answered_and_the_connection_stays_open` |
| 3.2 | Status Code | `enums.Status` (E5) | `test_spec_conformance::test_status_codes_match_spec_section_5_2` (명세 표를 직접 파싱) |
| 3.2 | Metadata Length `uint16`(최대 65,535) | `metadata.Metadata.encode/decode` (D12) | `test_metadata::test_size_limits` |
| 3.2 | Correlation ID `uint32`로 비동기 요청/응답 매핑 | `Session._pending`, `_alloc_cid` (D2) | `test_client_server::test_concurrent_requests_complete_out_of_order`, `test_many_concurrent_requests` |
| 3.2 | Payload Length 최대 16MB | `FrameDecoder` (D13) | `test_max_payload_boundary`, `test_payload_of_exactly_16_mib_is_accepted`, `test_oversized_payload_is_rejected_from_the_header_alone` |
| 3 | 4바이트 정렬 | **구현하지 않음 — 명세 자체가 규칙을 정의하지 않음 (E2)** | `test_no_padding_between_header_metadata_and_payload` |
| 4 | TLV 레이아웃 `Tag(1) Length(2) Value(N)` | `metadata.py` | `test_metadata` (골든 벡터·속성 테스트·오류 케이스) |
| 4 | 표준 태그 `0x01`~`0x04` | `enums.Tag` | `test_spec_conformance::test_tags_match_spec_section_4` |
| 5.1 | 메시지 타입 `REQ/RES/EVT/EVT_ACK/PING/PONG` | `enums.MessageType`, `Session._dispatch` (D3, D6) | `test_spec_conformance::test_message_types_match_spec_section_5_1`, `test_events`, `test_ping_measures_round_trip`, `test_ping_is_answered_with_the_same_correlation_id` |
| 5.2 | `BUSY`/`RATE_LIMITED`: 서버 한도 초과 시 송신 자제 요청 | `Session._overload_retry_after`, 클라이언트 `_wait_gate` (D15) | `test_backpressure` 전체 |
| 6 | PING/PONG은 MetaLen = PayloadLen = 0 | `FrameDecoder.next_raw`, `Frame.__post_init__` | `test_fatal_violations`, `test_header_violations_get_bad_request_then_close` |
| 6 | DoS 방어: 크기를 먼저 검증 | 헤더 16바이트만으로 `PAYLOAD_TOO_LARGE` 판정 | `test_oversized_payload_is_rejected_from_the_header_alone` |
| 6 | 데이터가 모일 때까지 대기 후 프레임 추출, 버퍼 소비 | `FrameDecoder` (증분 · 압축 버퍼) | `test_partial_data_waits_for_more`, `test_roundtrip_with_arbitrary_chunking` |
| 6 | 참조 `StrictLCPFramer` 인터페이스 | `lcp.StrictLCPFramer` (호환 래퍼) | `test_strict_framer_wrapper_mirrors_the_reference_api`, **`test_differential_against_the_spec_reference_framer`** (명세의 코드 블록을 직접 실행해 비교) |
| 7 | 벤치마크 지표 TPS, p50/p99, CPU·메모리·할당, 메시지 100B/1KB/64KB, 동시 접속 | `benchmarks/` (대상: LCP, HTTP/1.1 베이스라인) | `test_examples::test_benchmark_harness_smoke` — HTTP/2(gRPC)·MQTT는 [BENCHMARKS.md](BENCHMARKS.md) 참고 |
