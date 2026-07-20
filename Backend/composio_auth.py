
# composio_auth.py
"""
Composio authentication helper for Trello integration.

Functions exported:
- initiate_trello_connection(api_key, redirect_url=None)
- check_trello_connection(api_key)
- get_trello_boards(api_key, board_id=None)
- get_trello_cards(api_key, board_id)
- disconnect_trello(api_key)

This implementation uses the official `composio` Python SDK:
https://pypi.org/project/composio/
Docs: https://docs.composio.dev/
"""

import os
import traceback
from typing import Optional, Dict, Any, List

try:
    from composio import Composio
except Exception as e:
    raise ImportError(
        "Unable to import 'composio'. Make sure 'composio' is installed in your environment "
        "(pip install composio). Original error: " + str(e)
    )


# ---------------------------------------------------------------------------
# Workaround for a bug in composio 0.8.x _files.py
# `_substitute_file_downloads_recursively` does `params[_param]["type"]`
# without checking if "type" exists, crashing successful API calls.
# We wrap the method so if anything inside throws, we return the raw response.
# ---------------------------------------------------------------------------
try:
    from composio.core.models import _files as _composio_files  # type: ignore
    if not getattr(_composio_files.FileHelper, "_aura_patched", False):
        _orig_substitute = _composio_files.FileHelper._substitute_file_downloads_recursively

        def _safe_substitute(self, tool, schema, request):
            try:
                return _orig_substitute(self, tool, schema, request)
            except (KeyError, TypeError, AttributeError):
                return request

        _composio_files.FileHelper._substitute_file_downloads_recursively = _safe_substitute
        _composio_files.FileHelper._aura_patched = True
except Exception:
    # If the internal module name changes in a future SDK version, skip silently.
    pass


def _tool_execute(client, slug: str, arguments: Dict[str, Any], user_id: Optional[str] = None):
    """Call client.tools.execute in a way that works across composio SDK versions.

    Composio 0.8.x uses keyword args `slug` and `arguments`.
    Older shim variants used `action` and `params`.
    Fall back through the known signatures.
    """
    try:
        return client.tools.execute(slug=slug, arguments=arguments, user_id=user_id)
    except TypeError:
        pass
    try:
        return client.tools.execute(slug, arguments, user_id=user_id)
    except TypeError:
        pass
    return client.tools.execute(action=slug, params=arguments, user_id=user_id)


def _client_for_key(api_key: str) -> Composio:
    """
    Create a Composio client bound to the given API key.
    We set COMPOSIO_API_KEY in env for the SDK to pick up.
    """
    if not api_key:
        raise ValueError("api_key is required")
    # set env so SDK picks it up
    os.environ["COMPOSIO_API_KEY"] = api_key
    return Composio()


def _make_user_id(api_key: str) -> str:
    """
    Make a deterministic user_id from the composio api key when real user_id is not available.
    Ideally your app should pass a real user id (e.g. user email or DB id).
    """
    # Keep it short and deterministic
    preview = api_key[:8] if api_key else "anon"
    return f"project_user_{preview}"


def _as_list(resp):
    """Normalize a Composio list response (list, paginated object, or dict-with-items) to a Python list."""
    if resp is None:
        return []
    if isinstance(resp, list):
        return resp
    # SDK paginated response objects
    for attr in ("items", "data", "results"):
        val = getattr(resp, attr, None)
        if isinstance(val, list):
            return val
    if isinstance(resp, dict):
        for key in ("items", "data", "results"):
            val = resp.get(key)
            if isinstance(val, list):
                return val
    try:
        return list(resp)
    except Exception:
        return []


def _pick(obj, *names):
    """Return the first non-empty attribute / dict key value from obj."""
    if obj is None:
        return None
    for n in names:
        try:
            val = getattr(obj, n, None)
        except Exception:
            val = None
        if val:
            return val
        if isinstance(obj, dict):
            val = obj.get(n)
            if val:
                return val
    return None


def initiate_trello_connection(api_key: str, redirect_url: Optional[str] = None) -> Dict[str, Any]:
    """
    Initiate Trello OAuth/link flow using Composio toolkits.authorize.
    Returns a dict with keys: redirect_url, connection_request_id, connection_status.

    If the user already has an ACTIVE Trello connection, return that immediately
    instead of opening a new OAuth flow. The newer composio SDK returns a
    ConnectionRequest object with snake_case attributes (redirect_url, id, status),
    so we look for both naming conventions to stay compatible across versions.
    """
    try:
        client = _client_for_key(api_key)
        user_id = _make_user_id(api_key)

        # Reuse an existing ACTIVE connection if present.
        try:
            existing = _as_list(client.connected_accounts.list(
                user_ids=[user_id], toolkits=["trello"]
            ))
            for acct in existing:
                status_val = (
                    acct.get("status") if isinstance(acct, dict)
                    else getattr(acct, "status", None)
                )
                if status_val == "ACTIVE":
                    conn_id = (
                        acct.get("id") if isinstance(acct, dict)
                        else getattr(acct, "id", None)
                    )
                    return {
                        "redirect_url": None,
                        "connection_request_id": conn_id,
                        "connection_status": "ACTIVE",
                    }
        except Exception:
            pass

        # Authorize the user for the Trello toolkit.
        try:
            conn_request = client.toolkits.authorize(
                user_id=user_id, toolkit="trello", allow_multiple=True
            )
        except TypeError:
            conn_request = client.toolkits.authorize(user_id=user_id, toolkit="trello")

        redirect = _pick(conn_request, "redirect_url", "redirectUrl")
        connection_id = _pick(conn_request, "id", "connection_request_id")
        status = _pick(conn_request, "status", "connection_status")

        return {
            "redirect_url": redirect,
            "connection_request_id": connection_id,
            "connection_status": status or "PENDING",
        }
    except Exception as e:
        traceback.print_exc()
        raise RuntimeError(f"Failed to initiate Trello connection: {str(e)}") from e


def _list_trello_accounts(client, user_id: str):
    """
    Try every known Composio SDK param shape for listing Trello connected accounts.
    Returns a list of account objects (may be empty).
    """
    attempts = [
        {"user_ids": [user_id], "toolkits": ["trello"]},
        {"user_ids": [user_id], "toolkit_slugs": ["trello"]},
        {"user_id": user_id, "toolkit": "trello"},
        {"user_id": user_id, "toolkits": ["trello"]},
        {"user_ids": [user_id]},            # no toolkit filter
        {"user_id": user_id},
        {},                                  # unfiltered; we'll match locally
    ]
    seen_err = None
    for kwargs in attempts:
        try:
            raw = client.connected_accounts.list(**kwargs)
        except TypeError as e:
            seen_err = e
            continue
        except Exception as e:
            seen_err = e
            continue
        accounts = _as_list(raw)
        if accounts:
            return accounts, None
    return [], seen_err


def _account_field(acct, *names):
    """Dict- or attr-style field access."""
    for n in names:
        if isinstance(acct, dict) and n in acct and acct[n] is not None:
            return acct[n]
        v = getattr(acct, n, None)
        if v is not None:
            return v
    return None


def check_trello_connection(api_key: str) -> Dict[str, Any]:
    """
    Check Trello connection status.
    Returns { is_connected: bool, connection_id, status, app_unique_id, error? }
    """
    try:
        client = _client_for_key(api_key)
        user_id = _make_user_id(api_key)

        accounts, err = _list_trello_accounts(client, user_id)

        if not accounts:
            return {
                "is_connected": False,
                "connection_id": None,
                "status": None,
                "error": (
                    "No connected accounts found"
                    if err is None
                    else f"No connected accounts found (last SDK error: {err})"
                ),
            }

        # Local filter: keep only Trello toolkit, since unfiltered calls above
        # may have returned accounts from every toolkit.
        def _is_trello(a):
            toolkit = _account_field(a, "toolkit_slug", "toolkit", "app_name", "appName", "appUniqueId", "app_unique_id") or ""
            return "trello" in str(toolkit).lower()

        trello_only = [a for a in accounts if _is_trello(a)] or accounts

        # Local filter by user_id too, in case the unfiltered call returned everyone.
        def _matches_user(a):
            uid = _account_field(a, "user_id", "userId")
            return (not uid) or str(uid) == user_id

        scoped = [a for a in trello_only if _matches_user(a)] or trello_only

        # Prefer ACTIVE account.
        active = next(
            (a for a in scoped if _account_field(a, "status") == "ACTIVE"),
            None,
        )
        chosen = active or scoped[0]

        conn_id = _account_field(chosen, "id")
        status = _account_field(chosen, "status")
        app_unique_id = _account_field(chosen, "app_unique_id", "appUniqueId", "toolkit_slug")

        return {
            "is_connected": status == "ACTIVE",
            "connection_id": conn_id,
            "status": status,
            "app_unique_id": app_unique_id,
        }
    except Exception as e:
        traceback.print_exc()
        return {"is_connected": False, "connection_id": None, "status": None, "error": str(e)}


def get_trello_boards(api_key: str) -> Dict[str, Any]:
    """
    Fetch Trello boards for the connected Trello account.
    Returns { boards: [...], count: n, error?: str }
    """
    try:
        client = _client_for_key(api_key)
        user_id = _make_user_id(api_key)

        # Use Composio tools to execute Trello action that lists boards for "me"
        # Known action slug from Composio docs: TRELLO_GET_MEMBERS_BOARDS_BY_ID_MEMBER
        result = _tool_execute(
            client,
            slug="TRELLO_GET_MEMBERS_BOARDS_BY_ID_MEMBER",
            arguments={"id": "me"},
            user_id=user_id,
        )

        boards: List[Dict[str, Any]] = []
        # Result may be dict or list - normalize
        if isinstance(result, list):
            boards = result
        elif isinstance(result, dict):
            if "data" in result and isinstance(result["data"], list):
                boards = result["data"]
            elif "boards" in result and isinstance(result["boards"], list):
                boards = result["boards"]
            else:
                # try to find lists inside
                for v in result.values():
                    if isinstance(v, list):
                        boards = v
                        break

        formatted = []
        for b in boards:
            if isinstance(b, dict):
                formatted.append({
                    "id": b.get("id", ""),
                    "name": b.get("name", ""),
                    "url": b.get("url", ""),
                    "closed": b.get("closed", False),
                    "organization": b.get("organization", {}).get("name", "") if isinstance(b.get("organization"), dict) else ""
                })

        return {"boards": formatted, "count": len(formatted)}
    except Exception as e:
        traceback.print_exc()
        return {"boards": [], "count": 0, "error": str(e)}


def get_trello_cards(api_key: str, board_id: str) -> Dict[str, Any]:
    """
    Fetch Trello cards for a given board id.
    Returns { cards: [...], count: n, error?: str }
    """
    try:
        if not board_id:
            raise ValueError("board_id is required")

        client = _client_for_key(api_key)
        user_id = _make_user_id(api_key)

        # Try main board-cards action
        result = _tool_execute(
            client,
            slug="TRELLO_GET_BOARDS_CARDS_BY_ID_BOARD",
            arguments={"idBoard": board_id},
            user_id=user_id,
        )

        cards = []
        if isinstance(result, list):
            cards = result
        elif isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                cards = data
            elif isinstance(data, dict) and "cards" in data:
                cards = data["cards"]
            elif "cards" in result and isinstance(result["cards"], list):
                cards = result["cards"]
            else:
                # search for any list-looking key
                for v in result.values():
                    if isinstance(v, list):
                        cards = v
                        break

        formatted = []
        for c in cards:
            if not isinstance(c, dict):
                continue
            formatted.append({
                "id": c.get("id", ""),
                "name": c.get("name", ""),
                "desc": c.get("desc", ""),
                "shortUrl": c.get("shortUrl", "") or c.get("url", ""),
                "dateLastActivity": c.get("dateLastActivity", ""),
                "due": c.get("due", ""),
                "dueComplete": c.get("dueComplete", False),
                "list_id": c.get("idList", ""),
                "labels": c.get("labels", []),
                "members": c.get("idMembers", []),
            })

        return {"cards": formatted, "count": len(formatted)}
    except Exception as e:
        traceback.print_exc()
        return {"cards": [], "count": 0, "error": str(e)}


def disconnect_trello(api_key: str) -> Dict[str, Any]:
    """
    Disconnect (delete) the Trello connected account for our generated user_id.
    Returns { success: bool, message: str }
    """
    try:
        client = _client_for_key(api_key)
        user_id = _make_user_id(api_key)

        # list connected accounts for this user and toolkit trello
        accounts = _as_list(
            client.connected_accounts.list(user_ids=[user_id], toolkits=["trello"])
        )

        if not accounts:
            return {"success": False, "message": "No Trello connected accounts found to disconnect."}

        # delete each found account (or just the first)
        deleted = []
        errors = []
        for acct in accounts:
            acct_id = acct.get("id") if isinstance(acct, dict) else getattr(acct, "id", None)
            if not acct_id:
                continue
            try:
                client.connected_accounts.delete(acct_id)
                deleted.append(acct_id)
            except Exception as e:
                errors.append(f"{acct_id}: {str(e)}")

        if deleted:
            return {"success": True, "message": f"Deleted accounts: {deleted}"}
        else:
            return {"success": False, "message": "Failed to delete accounts: " + "; ".join(errors)}
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "message": f"Failed to disconnect Trello: {str(e)}"}
