
"""
AdminPerspective API routes - Assign tasks to employees via Trello
"""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer
from pydantic import BaseModel
from bson import ObjectId
from typing import Optional
import asyncio
import logging

from auth.auth_routes import get_current_user
from admin.query_router import handle_query

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Database instance (will be set by main.py)
database = None


def set_database(db):
    global database
    database = db


router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBearer()


class AssignTaskRequest(BaseModel):
    query: str
    workspace_id: Optional[str] = None
    board_id: Optional[str] = None
    list_name: Optional[str] = None


class AssignTaskResponse(BaseModel):
    success: bool
    message: str
    employee_name: Optional[str] = None
    task_description: Optional[str] = None


@router.post("/assign-task", response_model=AssignTaskResponse)
async def assign_task_to_employee(
    request: AssignTaskRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Admin chatbot endpoint.

    Accepts a natural-language query and dispatches read queries
    (e.g. "show Vatsal's cards", "what's overdue") or write queries
    (e.g. "assign 'Send invoice' to Vatsal by Friday") through Composio
    using the admin's own Composio + Gemini API keys from Settings.
    """
    if current_user.get("persona") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can use this endpoint")

    if not request.query or not request.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    if database is None:
        raise HTTPException(status_code=500, detail="Database not initialized. Please restart the server.")

    user = await database.users.find_one({"_id": ObjectId(current_user["id"])})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    settings = user.get("settings", {}) if isinstance(user.get("settings", {}), dict) else {}
    composio_api_key = (settings.get("composio_api_key") or "").strip()
    gemini_api_key = (settings.get("gemini_api_key") or "").strip()
    default_board_id = request.board_id or (settings.get("workspace_id") or "").strip()

    if not composio_api_key:
        raise HTTPException(
            status_code=400,
            detail="Composio API key not configured. Please add it in Settings."
        )

    try:
        result = await asyncio.to_thread(
            handle_query,
            request.query,
            composio_api_key,
            gemini_api_key,
            default_board_id or None,
        )
        return AssignTaskResponse(
            success=bool(result.get("success")),
            message=result.get("message", ""),
            employee_name=None,
            task_description=None,
        )
    except Exception as e:
        logger.exception("Error in assign_task_to_employee")
        raise HTTPException(status_code=500, detail=f"Error processing query: {str(e)}")
