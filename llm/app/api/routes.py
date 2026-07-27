from fastapi import APIRouter

from app.api import project, meeting, vacation, chat

router = APIRouter()

router.include_router(project.router, prefix="/projects", tags=["project"])
router.include_router(meeting.router, prefix="/meetings", tags=["meeting"])
router.include_router(vacation.router, prefix="/vacations", tags=["vacation"])
router.include_router(chat.router, prefix="/chat", tags=["chat"])
