# pretty-llm 에이전트 서버
#
# uv 공식 이미지 기반 — 로컬과 같은 잠금파일(uv.lock)로 빌드해 환경 차이를 없앤다.
# 의존성 레이어를 코드 레이어와 분리해, 코드만 바뀌면 의존성 설치를 재사용한다.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 의존성 먼저 (잠금파일 그대로 — 로컬과 동일 버전 보장)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 앱 코드
COPY app ./app

# 체크포인트 디렉토리 (compose 가 볼륨으로 덮는다 — 승인 대기 상태 영속)
RUN mkdir -p data

EXPOSE 3002

# 규격상 호출자는 Spring(BE) 하나뿐 — 컨테이너 안에서는 0.0.0.0 으로 열고,
# "외부 비공개"는 compose 의 포트 매핑(127.0.0.1:3002)으로 지킨다.
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "3002"]
