#!/usr/bin/env bash
# 전체 상태 점검 — 테스트 8종 배터리 + 서버 기동 스모크
#
#   ./scripts/check.sh          # 전체 (테스트 8종 + 서버 스모크, ~10-15분)
#   ./scripts/check.sh fast     # 빠른 판 (LLM 안 쓰는 도구 테스트 + 서버 스모크, ~1분)
#
# Docker 검증·리허설 전 점검용. 어떤 단계든 실패하면 즉시 멈춘다.
set -euo pipefail
cd "$(dirname "$0")/.."

SUITES_FAST=(test_tools)
SUITES_FULL=(test_tools test_sse test_question test_router test_composite
             test_action test_domains test_engine_b)

if [[ "${1:-}" == "fast" ]]; then
  SUITES=("${SUITES_FAST[@]}")
else
  SUITES=("${SUITES_FULL[@]}")
fi

echo "━━ ① 테스트 배터리 (${#SUITES[@]}종) ━━"
for t in "${SUITES[@]}"; do
  echo "── $t"
  uv run python -m "app.tests.$t" | tail -2
done

echo "━━ ② 서버 기동 스모크 ━━"
uv run uvicorn app.main:app --port 3999 --log-level warning &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
for i in {1..20}; do
  sleep 0.5
  if curl -sf localhost:3999/health > /dev/null; then break; fi
  [[ $i == 20 ]] && { echo "❌ 서버가 10초 안에 안 뜸"; exit 1; }
done
curl -sf localhost:3999/health
echo
echo "✅ 서버 기동·헬스체크 통과"

echo
echo "━━ 전부 통과 — FastAPI 정상 ━━"
