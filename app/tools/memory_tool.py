"""
기억 도구 — recall(과거 회상) · doc_search(문서 근거)

★ 두 도구 모두 인자에 신원이 없다 — 서랍 열쇠(userId)는 내부에서
  run_id → user.me 역산으로만 얻는다 (사칭 불가 구조).
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend
from app.memory import vectordb
from app.memory.indexer import backfill_meetings
from app.memory.store import resolve_user_id, search_cards
from app.tools.registry import RunContext


@tool
async def recall(query: str, runtime: ToolRuntime[RunContext]) -> str:
    """예전 대화·회의록·분석 결과 등 과거의 일을 찾을 때 호출한다.

    "예전에·지난번에·저번에 ~했던" 같은 과거 참조가 나오면 답을 지어내지 말고
    반드시 이 도구로 먼저 찾아라. 결과에 meetingId 가 있으면 meeting_detail 로
    원본 전문을 확인할 수 있다.

    query: 찾을 내용을 완결된 한 문장으로
    """
    run_id = runtime.context.run_id
    uid = await resolve_user_id(run_id)
    lines: list[str] = []

    # ⓐ 카드 — 대화 요약 · 분석 결과 (내 서랍만)
    for card in await search_cards(("conv", uid), query, limit=3):
        v = card.value
        lines.append(f"- [대화·{v.get('created', '')[:10]}] {v['title']}: {v['summary']} "
                     f"(conversationId={v.get('conversationId')})")
    for card in await search_cards(("analyses", uid), query, limit=2):
        v = card.value
        lines.append(f"- [분석·{v.get('created', '')[:10]}] {v['title']}: {v['summary']}")

    # ⓑ 회의록 — 내가 속한 프로젝트만 (멤버십 = X-Run-Id 스코프의 project.search)
    projects = (await backend.get("/projects", run_id=run_id)).get("projects", [])
    project_ids = [p["projectId"] for p in projects]
    await backfill_meetings(run_id, project_ids)          # 색인 공백 보충 (≤5건)
    for doc in await vectordb.search_meetings(query, project_ids, k=3):
        m = doc.metadata
        lines.append(f"- [회의록·{m['meetingDate']}] {m['title']}: "
                     f"{doc.page_content[:120]}… "
                     f"(projectId={m['projectId']}, meetingId={m['meetingId']})")

    if not lines:
        return "관련된 기억을 찾지 못했습니다."
    return "찾은 기억 (관련도순 상위):\n" + "\n".join(lines[:5])


@tool
async def doc_search(query: str, runtime: ToolRuntime[RunContext]) -> str:
    """사내 규정·업로드된 문서에서 근거를 찾을 때 호출한다.

    "규정상 ~돼?", "정책이 뭐야" 처럼 문서 근거가 필요한 질문이면 지어내지 말고
    이 도구로 찾아라. 결과가 없으면 없다고 답하라.

    query: 찾을 내용을 완결된 한 문장으로
    """
    docs = await vectordb.search_docs(query, k=4)
    if not docs:
        return "관련된 문서를 찾지 못했습니다. 업로드된 규정·문서가 없을 수 있습니다."
    lines = [f"- [{d.metadata['source']}] {d.page_content}" for d in docs]
    return "관련 문서 내용:\n" + "\n".join(lines)
