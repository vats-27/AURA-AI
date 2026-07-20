
"""
Composio OAuth routes - handle Trello authentication via Composio
Clean, production-ready version that relies on composio_auth.py helper functions.
Replace your existing Backend/composio_routes.py with this file.
"""
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer
from pydantic import BaseModel
from bson import ObjectId
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
import os

load_dotenv()

from auth.auth_routes import get_current_user
from composio_auth import (
    initiate_trello_connection,
    check_trello_connection,
    get_trello_boards,
    get_trello_cards,
    disconnect_trello,
)

# Database will be set from main after initialization
database = None

def set_database(db):
    global database
    database = db

router = APIRouter(prefix="/composio", tags=["composio"])
security = HTTPBearer()


class InitiateConnectionResponse(BaseModel):
    redirect_url: Optional[str] = None
    connected_account_id: Optional[str] = None
    connection_status: Optional[str] = None


class ConnectionStatusResponse(BaseModel):
    is_connected: bool
    connection_id: Optional[str] = None
    status: Optional[str] = None
    app_unique_id: Optional[str] = None
    error: Optional[str] = None


class BoardInfo(BaseModel):
    id: str
    name: str
    url: Optional[str] = None
    closed: Optional[bool] = False
    organization: Optional[str] = None


class BoardsResponse(BaseModel):
    boards: List[BoardInfo]
    count: int
    error: Optional[str] = None
    note: Optional[str] = None


class CardInfo(BaseModel):
    id: str
    name: str
    desc: Optional[str] = None
    closed: bool = False
    shortUrl: Optional[str] = None
    url: Optional[str] = None
    dateLastActivity: Optional[str] = None
    due: Optional[str] = None
    dueComplete: bool = False
    list_id: Optional[str] = None
    list_name: Optional[str] = None
    comments_count: int = 0
    badges: Optional[Dict[str, Any]] = None
    labels: Optional[List[Dict[str, Any]]] = None
    members: Optional[List[str]] = None
    checklists_count: int = 0


class CardsResponse(BaseModel):
    cards: List[CardInfo]
    count: int


@router.post("/trello/initiate", response_model=InitiateConnectionResponse)
async def initiate_trello_auth(
    redirect_url: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """
    Initiate Trello OAuth connection via Composio
    Returns a redirect URL that the user needs to visit to authorize Trello
    """
    user = await database.users.find_one({"_id": ObjectId(current_user["id"])})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    settings = user.get("settings", {}) if isinstance(user.get("settings", {}), dict) else {}
    composio_api_key = settings.get("composio_api_key", "")
    if composio_api_key:
        composio_api_key = composio_api_key.strip()

    if not composio_api_key:
        raise HTTPException(status_code=400, detail="Composio API key not configured. Please add it in Settings first.")

    try:
        res = initiate_trello_connection(api_key=composio_api_key, redirect_url=redirect_url)
        return InitiateConnectionResponse(
            redirect_url=res.get("redirect_url"),
            connected_account_id=res.get("connection_request_id") or res.get("connected_account_id"),
            connection_status=res.get("connection_status") or res.get("status"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initiate Trello connection: {str(e)}")


@router.post("/trello/disconnect")
async def disconnect_trello_endpoint(current_user: dict = Depends(get_current_user)):
    user = await database.users.find_one({"_id": ObjectId(current_user["id"])})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    composio_api_key = ""
    try:
        composio_api_key = user.get("settings", {}).get("composio_api_key", "").strip()
    except Exception:
        composio_api_key = ""

    if not composio_api_key:
        raise HTTPException(status_code=400, detail="Composio API key not configured. Please add it in Settings first.")

    try:
        result = disconnect_trello(api_key=composio_api_key)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to disconnect Trello: {str(e)}")


@router.get("/trello/status", response_model=ConnectionStatusResponse)
async def get_trello_connection_status(current_user: dict = Depends(get_current_user)):
    user = await database.users.find_one({"_id": ObjectId(current_user["id"])})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    settings = user.get("settings", {}) if isinstance(user.get("settings", {}), dict) else {}
    composio_api_key = settings.get("composio_api_key", "")
    if composio_api_key:
        composio_api_key = composio_api_key.strip()

    if not composio_api_key:
        raise HTTPException(status_code=400, detail="Composio API key not configured. Please add it in Settings first.")

    try:
        res = check_trello_connection(api_key=composio_api_key)
        return ConnectionStatusResponse(**res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check Trello connection: {str(e)}")


@router.get("/trello/boards", response_model=BoardsResponse)
async def get_trello_boards_endpoint(current_user: dict = Depends(get_current_user)):
    user = await database.users.find_one({"_id": ObjectId(current_user["id"])})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    composio_api_key = user.get("settings", {}).get("composio_api_key", "").strip()
    if not composio_api_key:
        raise HTTPException(status_code=400, detail="Composio API key not configured. Please add it in Settings first.")

    try:
        res = get_trello_boards(api_key=composio_api_key)
        return BoardsResponse(**res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch Trello boards: {str(e)}")


@router.get("/trello/cards", response_model=CardsResponse)
async def get_trello_cards_endpoint(board_id: str = Query(..., description="Trello board ID"), current_user: dict = Depends(get_current_user)):
    user = await database.users.find_one({"_id": ObjectId(current_user["id"])})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    settings = user.get("settings", {}) if isinstance(user.get("settings", {}), dict) else {}
    composio_api_key = settings.get("composio_api_key", "")
    if composio_api_key:
        composio_api_key = composio_api_key.strip()

    if not composio_api_key:
        raise HTTPException(status_code=400, detail="Composio API key not configured. Please add it in Settings first.")

    # Get board_id from settings if not provided
    workspace_id = user.get("settings", {}).get("workspace_id")
    if not board_id and workspace_id:
        board_id = workspace_id

    if not board_id:
        raise HTTPException(status_code=400, detail="Board ID is required. Please provide board_id parameter or configure it in Settings.")

    try:
        res = get_trello_cards(api_key=composio_api_key, board_id=board_id)
        return CardsResponse(**res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch Trello cards: {str(e)}")
