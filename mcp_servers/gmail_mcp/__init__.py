"""Gmail MCP 서버.

Company Copilot Agent(app/)와는 별개 프로세스로 뜨는 독립 서비스다.
OAuth 인증/토큰 보관을 전담하고, Agent에게는 MCP 툴만 노출한다.
Agent는 access_token/refresh_token을 절대 보지 못한다 — user_id만 넘긴다.
"""
