import httpx
from app.config import settings


async def get_internal_client() -> httpx.AsyncClient:
    """내부 API 호출용 HTTP 클라이언트"""
    async with httpx.AsyncClient(base_url=settings.internal_api_base_url, timeout=10.0) as client:
        yield client
