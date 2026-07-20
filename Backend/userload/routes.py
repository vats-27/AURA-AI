"""
UserLoad API routes - Fetch and manage user tasks from Trello (Direct API)
"""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer
from pydantic import BaseModel
from bson import ObjectId
from typing import List, Optional, Dict, Any

from auth.auth_routes import get_current_user
from userload.trello_service import UserLoadTrelloService, database

router = APIRouter(prefix="/userload", tags=["userload"])
security = HTTPBearer()

# --- Database hook (required by main.py) ---
database = None

def set_database(db):
    global database
    database = db


# ---------- RESPONSE MODELS ----------

class TaskResponse(BaseModel):
    id: str
    name: str
    state: str
    checklist_id: str


class FetchTasksResponse(BaseModel):
    tasks: List[TaskResponse]


class UpdateTaskRequest(BaseModel):
    board_id: str
    task_id: str
    checklist_id: str
    completed: bool


class UpdateTaskResponse(BaseModel):
    success: bool
    message: str


# ---------- FETCH TASKS ----------

@router.get("/tasks", response_model=FetchTasksResponse)
async def fetch_user_tasks(
    board_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """
    Fetch ALL checklist tasks from ALL cards on the board
    """

    if not board_id:
        raise HTTPException(
            status_code=400,
            detail="board_id is required"
        )

    try:
        service = UserLoadTrelloService(board_id=board_id)
        tasks = service.get_all_tasks()

        return FetchTasksResponse(
            tasks=[
                TaskResponse(
                    id=t["id"],
                    name=t["name"],
                    state=t["state"],
                    checklist_id=t["checklist_id"]
                )
                for t in tasks
            ]
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )



# ---------- UPDATE TASK STATUS ----------

@router.post("/update-task", response_model=UpdateTaskResponse)
async def update_task_status(
    request: UpdateTaskRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Mark checklist item complete / incomplete
    """

    try:
        service = UserLoadTrelloService(board_id=request.board_id)

        service.update_task_status(
            card_id=f"{current_user.get('name', 'User')}'s Todo",  # logical reference
            checklist_id=request.checklist_id,
            checkitem_id=request.task_id,
            completed=request.completed
        )

        return UpdateTaskResponse(
            success=True,
            message="Task updated successfully"
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )
