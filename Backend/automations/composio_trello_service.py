"""
Composio-backed Trello service (read + write).
All Trello I/O goes through the user's Composio API key — no TRELLO_API_KEY /
TRELLO_API_TOKEN environment variables are required.

Reuses the pattern established in Backend/composio_auth.py:
  os.environ["COMPOSIO_API_KEY"] = api_key
  client = Composio()
  user_id = f"project_user_{api_key[:8]}"
  client.tools.execute(action="TRELLO_...", params={...}, user_id=user_id)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Importing composio_auth first applies the SDK patch (KeyError workaround in
# composio._files) and exposes the cross-version _tool_execute helper.
import composio_auth  # noqa: F401  (side effect: SDK monkey-patch)
from composio_auth import _tool_execute
from composio import Composio


def _client_for_key(api_key: str) -> Composio:
    if not api_key:
        raise ValueError("Composio API key is required")
    os.environ["COMPOSIO_API_KEY"] = api_key
    return Composio()


def _user_id(api_key: str) -> str:
    preview = api_key[:8] if api_key else "anon"
    return f"project_user_{preview}"


def _extract_id(obj: Any) -> Optional[str]:
    """Pull the 'id' out of a Composio tool response, regardless of nesting."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        if obj.get("id"):
            return obj["id"]
        for key in ("data", "response_data", "result"):
            inner = obj.get(key)
            if isinstance(inner, dict) and inner.get("id"):
                return inner["id"]
        for v in obj.values():
            if isinstance(v, dict) and v.get("id"):
                return v["id"]
    if hasattr(obj, "id"):
        val = getattr(obj, "id", None)
        if val:
            return str(val)
    return None


def _unwrap(result: Any) -> Any:
    """Composio results can be dicts with 'data' keys, raw lists, or SDK objects.
    Return the inner payload."""
    if result is None:
        return None
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("data", "result", "response"):
            inner = result.get(key)
            if inner is not None:
                return inner
        return result
    # SDK object
    for attr in ("data", "result", "response"):
        if hasattr(result, attr):
            return getattr(result, attr)
    return result


class _ComposioBase:
    def __init__(self, api_key: str):
        self.api_key = (api_key or "").strip()
        if not self.api_key:
            raise ValueError("Composio API key is required")
        self.client = _client_for_key(self.api_key)
        self.user_id = _user_id(self.api_key)

    def _execute(self, slugs: List[str], params: Dict[str, Any]) -> Any:
        """Try each action slug in order; return first successful result."""
        last_err: Optional[Exception] = None
        for slug in slugs:
            try:
                res = _tool_execute(
                    self.client, slug=slug, arguments=params, user_id=self.user_id
                )
                return _unwrap(res)
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(
            f"All Composio action slugs failed: {slugs}. Last error: {last_err}"
        )

    def _connected_account_id(self) -> Optional[str]:
        """Look up the connected Trello account id for this user, cached on the instance."""
        cached = getattr(self, "_cached_conn_id", None)
        if cached:
            return cached
        try:
            raw = self.client.connected_accounts.list(
                user_ids=[self.user_id], toolkits=["trello"]
            )
        except Exception:
            raw = None
        if raw is None:
            return None
        if hasattr(raw, "items"):
            raw = raw.items or []
        elif isinstance(raw, dict):
            raw = raw.get("items") or raw.get("data") or []
        chosen = None
        for a in (raw or []):
            status = a.get("status") if isinstance(a, dict) else getattr(a, "status", None)
            if status == "ACTIVE":
                chosen = a
                break
        if chosen is None and raw:
            chosen = raw[0]
        if chosen is None:
            return None
        conn_id = chosen.get("id") if isinstance(chosen, dict) else getattr(chosen, "id", None)
        self._cached_conn_id = conn_id
        return conn_id

    TRELLO_BASE_URL = "https://api.trello.com/1"

    def _proxy(
        self,
        method: str,
        endpoint: str,
        query: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Call the Trello REST API through Composio's proxy using the user's
        connected account — no need to know Composio's action slug name."""
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            abs_endpoint = endpoint
        else:
            abs_endpoint = self.TRELLO_BASE_URL + (
                endpoint if endpoint.startswith("/") else "/" + endpoint
            )

        params_list: List[Dict[str, str]] = []
        if query:
            for k, v in query.items():
                if v is None:
                    continue
                params_list.append({"name": str(k), "type": "query", "value": str(v)})
        kwargs: Dict[str, Any] = {
            "endpoint": abs_endpoint,
            "method": method.upper(),
        }
        if params_list:
            kwargs["parameters"] = params_list
        if body is not None:
            kwargs["body"] = body
        conn_id = self._connected_account_id()
        if conn_id:
            kwargs["connected_account_id"] = conn_id

        import logging
        log = logging.getLogger(__name__)
        log.info("[proxy] %s %s  query=%s  conn=%s", method, abs_endpoint, query, conn_id)

        resp = self.client.tools.proxy(**kwargs)
        # Log the full response shape so we can see if Composio returned an error body.
        try:
            log.info("[proxy] raw response type=%s", type(resp).__name__)
            if hasattr(resp, "status"):
                log.info("[proxy] status=%s", resp.status)
            if hasattr(resp, "data"):
                log.info("[proxy] data=%s", resp.data)
        except Exception:
            pass

        if hasattr(resp, "data"):
            data = resp.data
        elif isinstance(resp, dict):
            data = resp.get("data", resp)
        else:
            data = resp
        return data


class ComposioTrelloWriter(_ComposioBase):
    """Create/ensure lists, cards, checklists and check-items on a Trello board via Composio."""

    # ---------------- Lists ----------------
    def get_board_lists(self, board_id: str) -> List[Dict[str, Any]]:
        data = self._execute(
            ["TRELLO_GET_BOARDS_LISTS_BY_ID_BOARD"],
            {"idBoard": board_id, "fields": "name,closed"},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []

    def ensure_list(self, board_id: str, list_name: str) -> str:
        target = list_name.strip().lower()
        for lst in self.get_board_lists(board_id):
            if (
                isinstance(lst, dict)
                and lst.get("name", "").strip().lower() == target
                and not lst.get("closed", False)
            ):
                return lst["id"]
        created = self._execute(
            ["TRELLO_ADD_LISTS", "TRELLO_ADD_BOARDS_LISTS_BY_ID_BOARD"],
            {"idBoard": board_id, "name": list_name},
        )
        new_id = _extract_id(created)
        if new_id:
            return new_id
        raise RuntimeError(f"Could not create list '{list_name}': {created}")

    # ---------------- Cards ----------------
    def get_list_cards(self, list_id: str) -> List[Dict[str, Any]]:
        data = self._execute(
            ["TRELLO_GET_LISTS_CARDS_BY_ID_LIST"],
            {"idList": list_id, "fields": "name,closed,desc,due,dueComplete"},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []

    def ensure_card(
        self,
        list_id: str,
        name: str,
        desc: Optional[str] = None,
        due: Optional[str] = None,
    ) -> str:
        target = name.strip().lower()
        for card in self.get_list_cards(list_id):
            if (
                isinstance(card, dict)
                and card.get("name", "").strip().lower() == target
                and not card.get("closed", False)
            ):
                return card["id"]
        return self.create_card(list_id, name, desc=desc, due=due)

    def create_card(
        self,
        list_id: str,
        name: str,
        desc: Optional[str] = None,
        due: Optional[str] = None,
    ) -> str:
        params: Dict[str, Any] = {"idList": list_id, "name": name}
        if desc:
            params["desc"] = desc
        if due:
            params["due"] = due
        created = self._execute(
            ["TRELLO_ADD_CARDS", "TRELLO_ADD_LISTS_CARDS_BY_ID_LIST"],
            params,
        )
        new_id = _extract_id(created)
        if new_id:
            return new_id
        raise RuntimeError(f"Could not create card '{name}': {created}")

    # ---------------- Checklists ----------------
    def get_card_checklists(self, card_id: str) -> List[Dict[str, Any]]:
        data = self._execute(
            ["TRELLO_GET_CARDS_CHECKLISTS_BY_ID_CARD"],
            {"idCard": card_id, "fields": "name"},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []

    def ensure_checklist(self, card_id: str, name: str = "Tasks") -> str:
        target = name.strip().lower()
        for cl in self.get_card_checklists(card_id):
            if isinstance(cl, dict) and cl.get("name", "").strip().lower() == target:
                return cl["id"]
        created = self._execute(
            ["TRELLO_ADD_CHECKLISTS", "TRELLO_ADD_CARDS_CHECKLISTS_BY_ID_CARD"],
            {"idCard": card_id, "name": name},
        )
        new_id = _extract_id(created)
        if new_id:
            return new_id
        raise RuntimeError(f"Could not create checklist '{name}': {created}")

    def add_check_item(
        self, checklist_id: str, text: str, deadline: Optional[str] = None
    ) -> Dict[str, Any]:
        label = f"{text} (Due: {deadline})" if deadline else text
        return self._execute(
            [
                "TRELLO_ADD_CHECKLISTS_CHECK_ITEMS_BY_ID_CHECKLIST",
                "TRELLO_ADD_CHECKLIST_ITEM",
            ],
            {"idChecklist": checklist_id, "name": label},
        ) or {}

    # ---------------- High-level flow used by routes ----------------
    def convert_action_item_to_task(
        self,
        board_id: str,
        participant_name: str,
        task_text: str,
        deadline: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            list_name = f"{participant_name}'s Todo"
            list_id = self.ensure_list(board_id, list_name)
            card_id = self.ensure_card(list_id, list_name)
            checklist_id = self.ensure_checklist(card_id, "Tasks")
            self.add_check_item(checklist_id, task_text, deadline)
            return {
                "success": True,
                "list_id": list_id,
                "card_id": card_id,
                "checklist_id": checklist_id,
                "message": f"Added '{task_text}' to {list_name}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def assign_standalone_task(
        self,
        board_id: str,
        participant_name: str,
        task_text: str,
        deadline: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Admin chatbot write path: create a card named task_text in the user's list."""
        try:
            list_name = f"{participant_name}'s Todo"
            list_id = self.ensure_list(board_id, list_name)
            card_id = self.create_card(
                list_id,
                task_text,
                desc=f"Assigned to {participant_name}",
                due=deadline,
            )
            return {
                "success": True,
                "list_id": list_id,
                "card_id": card_id,
                "message": (
                    f"Created card '{task_text}' in {list_name}"
                    + (f" due {deadline}" if deadline else "")
                ),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class ComposioTrelloReader(_ComposioBase):
    """Read lists / cards / members from a Trello board via Composio for chatbot queries."""

    def _board_cards(self, board_id: str) -> List[Dict[str, Any]]:
        data = self._execute(
            ["TRELLO_GET_BOARDS_CARDS_BY_ID_BOARD"],
            {"idBoard": board_id},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("cards", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []

    def _card_checklists(self, card_id: str) -> List[Dict[str, Any]]:
        data = self._execute(
            ["TRELLO_GET_CARDS_CHECKLISTS_BY_ID_CARD"],
            {"idCard": card_id},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []

    def _checklist_items(self, card_id: str, checklist_id: str) -> List[Dict[str, Any]]:
        data = self._execute(
            ["TRELLO_GET_CARDS_CHECKLIST_CHECK_ITEMS_BY_ID_CARD_BY_ID_CHECKLIST"],
            {"idCard": card_id, "idChecklist": checklist_id},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("checkItems", "check_items", "items", "data"):
                v = data.get(key)
                if isinstance(v, list):
                    return v
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []

    def get_card_tasks(self, card_id: str) -> List[Dict[str, Any]]:
        """Return all check-items across every checklist on a card,
        flattened into a simple list of {name, state, checklist_name}."""
        out: List[Dict[str, Any]] = []
        for cl in self._card_checklists(card_id):
            if not isinstance(cl, dict):
                continue
            cl_id = cl.get("id")
            cl_name = cl.get("name", "")
            items = cl.get("checkItems") or cl.get("check_items")
            if not isinstance(items, list):
                items = self._checklist_items(card_id, cl_id) if cl_id else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                out.append(
                    {
                        "name": item.get("name", ""),
                        "state": item.get("state", "incomplete"),
                        "checklist_name": cl_name,
                    }
                )
        return out

    def _board_lists(self, board_id: str) -> List[Dict[str, Any]]:
        data = self._execute(
            ["TRELLO_GET_BOARDS_LISTS_BY_ID_BOARD"],
            {"idBoard": board_id, "fields": "name,closed"},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []

    def _enrich(self, cards: List[Dict[str, Any]], board_id: str) -> List[Dict[str, Any]]:
        """Attach human-readable list names to each card."""
        lists_by_id = {
            lst["id"]: lst.get("name", "")
            for lst in self._board_lists(board_id)
            if isinstance(lst, dict) and lst.get("id")
        }
        out: List[Dict[str, Any]] = []
        for c in cards:
            if not isinstance(c, dict):
                continue
            out.append(
                {
                    "id": c.get("id", ""),
                    "name": c.get("name", ""),
                    "desc": c.get("desc", ""),
                    "list_id": c.get("idList", ""),
                    "list_name": lists_by_id.get(c.get("idList", ""), ""),
                    "due": c.get("due"),
                    "dueComplete": c.get("dueComplete", False),
                    "closed": c.get("closed", False),
                    "shortUrl": c.get("shortUrl", "") or c.get("url", ""),
                    "dateLastActivity": c.get("dateLastActivity"),
                }
            )
        return out

    # ---------------- Query methods ----------------
    def list_all_cards(self, board_id: str) -> List[Dict[str, Any]]:
        return self._enrich(self._board_cards(board_id), board_id)

    def cards_for_person(self, board_id: str, name: str) -> List[Dict[str, Any]]:
        target = f"{name.strip().lower()}'s todo"
        cards = self._enrich(self._board_cards(board_id), board_id)
        matched = [c for c in cards if c["list_name"].strip().lower() == target]
        if matched:
            return matched
        # fallback: cards whose name contains the person's name
        lowname = name.strip().lower()
        return [c for c in cards if lowname in c["name"].lower()]

    def cards_with_tasks_for_person(self, board_id: str, name: str) -> List[Dict[str, Any]]:
        """Like cards_for_person but also expands each card's checklist items
        so the caller can show the actual assigned tasks, not just the card title."""
        out: List[Dict[str, Any]] = []
        for c in self.cards_for_person(board_id, name):
            tasks = self.get_card_tasks(c["id"]) if c.get("id") else []
            out.append({**c, "tasks": tasks})
        return out

    def cards_in_list(self, board_id: str, list_name: str) -> List[Dict[str, Any]]:
        target = list_name.strip().lower()
        cards = self._enrich(self._board_cards(board_id), board_id)
        return [c for c in cards if c["list_name"].strip().lower() == target]

    def overdue_cards(self, board_id: str) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        out = []
        for c in self._enrich(self._board_cards(board_id), board_id):
            due = c.get("due")
            if not due or c.get("dueComplete"):
                continue
            try:
                due_dt = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
                if due_dt < now:
                    out.append(c)
            except Exception:
                continue
        return out

    def cards_due_before(self, board_id: str, iso_date: str) -> List[Dict[str, Any]]:
        try:
            cutoff = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except Exception:
            return []
        out = []
        for c in self._enrich(self._board_cards(board_id), board_id):
            due = c.get("due")
            if not due:
                continue
            try:
                due_dt = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
                if due_dt <= cutoff:
                    out.append(c)
            except Exception:
                continue
        return out

    def workload_summary(self, board_id: str) -> List[Dict[str, Any]]:
        counts: Dict[str, Dict[str, int]] = {}
        for c in self._enrich(self._board_cards(board_id), board_id):
            list_name = c["list_name"] or "(no list)"
            row = counts.setdefault(list_name, {"total": 0, "done": 0, "open": 0})
            row["total"] += 1
            if c.get("closed") or c.get("dueComplete"):
                row["done"] += 1
            else:
                row["open"] += 1
        return [{"list": k, **v} for k, v in counts.items()]

    def card_details(self, board_id: str, card_name: str) -> Optional[Dict[str, Any]]:
        target = card_name.strip().lower()
        for c in self._enrich(self._board_cards(board_id), board_id):
            if c["name"].strip().lower() == target:
                return c
        # partial match fallback
        for c in self._enrich(self._board_cards(board_id), board_id):
            if target in c["name"].strip().lower():
                return c
        return None
