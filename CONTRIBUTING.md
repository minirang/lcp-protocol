# 기여 가이드

## 개발 환경

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## 변경 전 확인

```bash
pytest                                   # 전체 테스트 (타임아웃 60초, 경고는 오류로 취급)
ruff format . && ruff check .
mypy                                     # --strict
```

세 가지가 모두 통과해야 CI가 초록색입니다.

## 규칙

- **와이어 포맷이나 명세 해석을 바꾸면** `docs/IMPLEMENTATION_NOTES.md`와 `docs/SPEC_COMPLIANCE.md`를 함께 갱신하고,
  변경을 고정하는 테스트(골든 벡터 포함)를 추가하세요. 명세 원문(`docs/spec/`)은 수정하지 않습니다. 명세 개정 제안은 노트의 5절에 적습니다.
- 와이어 계층(`metadata`/`frame`/`framer`/`codec`)은 I/O 없이 유지합니다. 네트워크 동작은 `session`에 둡니다.
- 새 기능에는 테스트가 필요합니다. 비동기 테스트는 `pytest-asyncio`(auto 모드), 포트는 항상 `0`을 사용하세요.
- 외부 입력은 신뢰하지 않습니다: 새 필드를 파싱할 때는 길이·범위를 먼저 검증하고, 실패는 `ProtocolError`/`StatusError`로 표현하세요.
- 서버가 클라이언트에 보내는 오류 메시지에 내부 예외 내용을 넣지 마세요.
- 커밋 메시지는 `feat:`, `fix:`, `docs:`, `test:`, `perf:`, `refactor:` 접두사를 권장합니다.

## 버그 보고

재현 가능한 최소 코드(또는 `lcp encode`로 만든 hex 프레임)와 Python/OS 버전을 첨부해 주세요.
보안 문제는 [SECURITY.md](SECURITY.md)를 따라 비공개로 알려 주세요.
