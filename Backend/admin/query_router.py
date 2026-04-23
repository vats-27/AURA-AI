"""
Admin chatbot query router.

Takes a natural-language query from the admin and:
  1. Uses Gemini to classify intent and extract parameters.
  2. Dispatches to ComposioTrelloReader (reads) or ComposioTrelloWriter (writes).
  3. Formats the result as a chat reply string.

Board ID resolution rule (per user's Doubt A = (i)):
  If the query contains a board ID, use it; else fall back to settings.workspace_id.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from automations.composio_trello_service import (
    ComposioTrelloReader,
    ComposioTrelloWriter,
)

try:
    from llm import get_llm
except Exception:
    get_llm = None


# Trello long board IDs are 24-char hex. Short IDs are 8 chars alphanumeric.
_BOARD_ID_RE = re.compile(r"\b([0-9a-fA-F]{24})\b")
_SHORT_BOARD_RE = re.compile(r"\b([A-Za-z0-9]{8})\b")


def _extract_board_id(query: str, fallback: Optional[str]) -> Optional[str]:
    m = _BOARD_ID_RE.search(query)
    if m:
        return m.group(1)
    # Only accept short ID if the user explicitly mentions 'board' to avoid
    # matching unrelated 8-char words.
    if "board" in query.lower():
        m2 = _SHORT_BOARD_RE.search(query)
        if m2 and not m2.group(1).lower() in {"everyone", "everybody"}:
            candidate = m2.group(1)
            # Short IDs are mixed-case alnum. Skip common English words.
            if not candidate.isalpha() or not candidate.islower():
                return candidate
    return fallback


def _llm(gemini_api_key: str):
    if not gemini_api_key or get_llm is None:
        return None
    try:
        return get_llm(gemini_api_key)
    except Exception:
        return None


_PARSE_PROMPT = """You are the intent router for a Trello assistant used by a team manager.
Return ONLY a compact JSON object with these keys:
  intent: one of "list_all" | "cards_for_person" | "cards_in_list" | "overdue" |
          "due_before" | "workload" | "card_details" | "assign_task" | "unknown"
  person: string or null            # employee name if mentioned
  list_name: string or null         # Trello list name if mentioned
  card_name: string or null         # Trello card name if asking about a specific card
  due_before: ISO date or null      # for "due_before" intent (YYYY-MM-DD)
  task_text: string or null         # for "assign_task"
  deadline: ISO date or null        # for "assign_task" (YYYY-MM-DD)

Rules:
- If the query contains a 24-char hex board id or phrase "board id ...", still pick the right intent; the board id is handled elsewhere.
- "this week" means due_before = the coming Sunday.
- "today" means due_before = today.
- "fetch cards of X" / "show X's tasks" / "X's pending tasks" -> cards_for_person.
- "list all cards" / "show all cards on the board" -> list_all.
- "show cards in <list>" / "what's in <list>" -> cards_in_list.
- "what's overdue" / "overdue tasks" -> overdue.
- "who has the most work" / "workload" / "distribution" -> workload.
- "details of card <name>" / "show card <name>" -> card_details.
- "assign <task> to <person>" / "add task ... for <person>" -> assign_task.
- Anything you can't classify -> unknown.

Query: {query}

JSON:"""


def parse_intent(query: str, gemini_api_key: str) -> Dict[str, Any]:
    llm = _llm(gemini_api_key)
    fallback = {
        "intent": "unknown",
        "person": None,
        "list_name": None,
        "card_name": None,
        "due_before": None,
        "task_text": None,
        "deadline": None,
    }
    if llm is None:
        return _heuristic_parse(query, fallback)

    try:
        resp = llm.invoke(_PARSE_PROMPT.format(query=query))
        text = getattr(resp, "content", str(resp)) or ""
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
        cleaned = re.sub(r"```\s*$", "", cleaned.strip())
        parsed = json.loads(cleaned)
        for k in fallback:
            parsed.setdefault(k, fallback[k])
        # Resolve relative week window if model hinted it
        if parsed["intent"] == "due_before" and parsed["due_before"] in (None, ""):
            parsed["due_before"] = _this_sunday_iso()
        return parsed
    except Exception:
        return _heuristic_parse(query, fallback)


def _this_sunday_iso() -> str:
    now = datetime.now(timezone.utc)
    days_until_sunday = (6 - now.weekday()) % 7 or 7
    target = (now + timedelta(days=days_until_sunday)).date()
    return target.isoformat()


def _heuristic_parse(query: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort parser when Gemini is not reachable."""
    q = query.strip()
    low = q.lower()
    out = dict(fallback)

    if "overdue" in low:
        out["intent"] = "overdue"
        return out
    if "workload" in low or "most work" in low or "distribution" in low:
        out["intent"] = "workload"
        return out
    if "this week" in low or "due this week" in low:
        out["intent"] = "due_before"
        out["due_before"] = _this_sunday_iso()
        return out
    if "today" in low and "due" in low:
        out["intent"] = "due_before"
        out["due_before"] = datetime.now(timezone.utc).date().isoformat()
        return out
    if low.startswith("assign ") or " assign " in low or low.startswith("add task"):
        out["intent"] = "assign_task"
        m = re.search(r'"([^"]+)"', q)
        if m:
            out["task_text"] = m.group(1)
        m2 = re.search(r"to\s+([A-Za-z][A-Za-z .'-]{1,40})", q, re.IGNORECASE)
        if m2:
            out["person"] = m2.group(1).strip().rstrip(".")
        if not out["task_text"]:
            out["task_text"] = q
        return out
    if "list all" in low or "all cards" in low:
        out["intent"] = "list_all"
        return out
    if "cards in " in low or "what's in " in low:
        out["intent"] = "cards_in_list"
        m = re.search(r"(?:cards in|what's in)\s+(?:list\s+)?([A-Za-z0-9 ]+)$", q, re.IGNORECASE)
        if m:
            out["list_name"] = m.group(1).strip()
        return out
    # default: assume "cards of <name>" pattern
    m = re.search(r"(?:cards? (?:of|for)|show|fetch|get)\s+([A-Za-z][A-Za-z .'-]{1,40})", q, re.IGNORECASE)
    if m:
        out["intent"] = "cards_for_person"
        out["person"] = m.group(1).strip().rstrip("'s").strip()
        return out

    return out


# ---------- formatting helpers ----------
def _fmt_card(c: Dict[str, Any]) -> str:
    parts = [f"• {c.get('name') or '(no name)'}"]
    if c.get("list_name"):
        parts.append(f"[{c['list_name']}]")
    if c.get("due"):
        parts.append(f"due {str(c['due'])[:10]}")
        if c.get("dueComplete"):
            parts.append("✓")
    if c.get("shortUrl"):
        parts.append(c["shortUrl"])
    return " ".join(parts)


def _fmt_cards(cards: List[Dict[str, Any]], header: str) -> str:
    if not cards:
        return f"{header}: none found."
    body = "\n".join(_fmt_card(c) for c in cards)
    return f"{header} ({len(cards)}):\n{body}"


def _fmt_cards_with_tasks(cards: List[Dict[str, Any]], header: str) -> str:
    if not cards:
        return f"{header}: none found."
    lines: List[str] = [f"{header} ({len(cards)}):"]
    for c in cards:
        lines.append("")
        lines.append(_fmt_card(c))
        tasks = c.get("tasks") or []
        if tasks:
            for t in tasks:
                mark = "✓" if t.get("state") == "complete" else "○"
                name = t.get("name") or "(unnamed task)"
                lines.append(f"    {mark} {name}")
        else:
            lines.append("    (no checklist items on this card)")
    return "\n".join(lines)


# ---------- main dispatcher ----------
def handle_query(
    query: str,
    composio_api_key: str,
    gemini_api_key: Optional[str],
    default_board_id: Optional[str],
) -> Dict[str, Any]:
    """
    Returns {success: bool, message: str, intent: str, ...}
    message is the text shown in the chatbot bubble.
    """
    if not query or not query.strip():
        return {"success": False, "message": "Query is empty.", "intent": "unknown"}
    if not composio_api_key:
        return {
            "success": False,
            "message": "Composio API key not configured in Settings.",
            "intent": "unknown",
        }

    board_id = _extract_board_id(query, default_board_id)
    if not board_id:
        return {
            "success": False,
            "message": "Board ID missing. Set it in Settings or include it in the query.",
            "intent": "unknown",
        }

    parsed = parse_intent(query, gemini_api_key or "")
    intent = parsed.get("intent") or "unknown"

    try:
        if intent in {
            "list_all",
            "cards_for_person",
            "cards_in_list",
            "overdue",
            "due_before",
            "workload",
            "card_details",
        }:
            reader = ComposioTrelloReader(composio_api_key)

            if intent == "list_all":
                cards = reader.list_all_cards(board_id)
                return {
                    "success": True,
                    "intent": intent,
                    "message": _fmt_cards(cards, "Cards on the board"),
                }

            if intent == "cards_for_person":
                person = parsed.get("person")
                if not person:
                    return {
                        "success": False,
                        "intent": intent,
                        "message": "Could not figure out whose cards to fetch.",
                    }
                cards = reader.cards_with_tasks_for_person(board_id, person)
                return {
                    "success": True,
                    "intent": intent,
                    "message": _fmt_cards_with_tasks(cards, f"{person}'s cards"),
                }

            if intent == "cards_in_list":
                lst = parsed.get("list_name")
                if not lst:
                    return {
                        "success": False,
                        "intent": intent,
                        "message": "Which list? E.g. 'cards in Done'.",
                    }
                cards = reader.cards_in_list(board_id, lst)
                return {
                    "success": True,
                    "intent": intent,
                    "message": _fmt_cards(cards, f"Cards in list '{lst}'"),
                }

            if intent == "overdue":
                cards = reader.overdue_cards(board_id)
                return {
                    "success": True,
                    "intent": intent,
                    "message": _fmt_cards(cards, "Overdue cards"),
                }

            if intent == "due_before":
                cutoff = parsed.get("due_before") or _this_sunday_iso()
                cards = reader.cards_due_before(board_id, cutoff)
                return {
                    "success": True,
                    "intent": intent,
                    "message": _fmt_cards(cards, f"Cards due on or before {cutoff}"),
                }

            if intent == "workload":
                rows = reader.workload_summary(board_id)
                if not rows:
                    return {
                        "success": True,
                        "intent": intent,
                        "message": "No cards on the board.",
                    }
                body = "\n".join(
                    f"• {r['list']} — {r['total']} total ({r['open']} open, {r['done']} done)"
                    for r in rows
                )
                return {
                    "success": True,
                    "intent": intent,
                    "message": f"Workload by list:\n{body}",
                }

            if intent == "card_details":
                name = parsed.get("card_name") or parsed.get("person")
                if not name:
                    return {
                        "success": False,
                        "intent": intent,
                        "message": "Which card? Include its name.",
                    }
                card = reader.card_details(board_id, name)
                if not card:
                    return {
                        "success": True,
                        "intent": intent,
                        "message": f"No card matching '{name}'.",
                    }
                lines = [
                    f"Card: {card['name']}",
                    f"List: {card['list_name'] or '(none)'}",
                    f"Due:  {card.get('due') or '(no due date)'}",
                    "",
                    card.get("desc") or "(no description)",
                ]
                if card.get("shortUrl"):
                    lines.append("")
                    lines.append(card["shortUrl"])
                return {"success": True, "intent": intent, "message": "\n".join(lines)}

        if intent == "assign_task":
            person = parsed.get("person")
            task = parsed.get("task_text")
            if not person or not task:
                return {
                    "success": False,
                    "intent": intent,
                    "message": "Need both a person and a task. Example: 'assign \"Send invoice\" to Vatsal by Friday'.",
                }
            writer = ComposioTrelloWriter(composio_api_key)
            result = writer.assign_standalone_task(
                board_id=board_id,
                participant_name=person,
                task_text=task,
                deadline=parsed.get("deadline"),
            )
            if result.get("success"):
                return {
                    "success": True,
                    "intent": intent,
                    "message": result.get("message", "Task created."),
                }
            return {
                "success": False,
                "intent": intent,
                "message": result.get("error", "Failed to create task."),
            }

        return {
            "success": False,
            "intent": "unknown",
            "message": (
                "Sorry, I didn't understand. Try: "
                "'show Vatsal's cards', 'list overdue', 'assign \"Task\" to Vatsal by Friday'."
            ),
        }

    except Exception as e:
        return {"success": False, "intent": intent, "message": f"Error: {e}"}
