"""Active WhatsApp (Chatwoot) thread viewers: heartbeat + multi-tab via Redis.

Shape: ``chat_viewers:{conversation_id}`` → JSON
``{ user: { tab_id: last_seen_unix_ts } }``. Stale tab entries are pruned using VIEWER_TTL_SEC.
"""

from __future__ import annotations

import json
import time
from typing import Any

import frappe

VIEWER_TTL_SEC = 120
CHAT_VIEWERS_KEY_PREFIX = "chat_viewers:"


def _normalize_conversation_id(conversation_id) -> str:
	if conversation_id is None:
		return ""
	try:
		# Chatwoot ids are numeric; keep stable string key
		if isinstance(conversation_id, (int, float)) and not isinstance(conversation_id, bool):
			return str(int(conversation_id))
		s = str(conversation_id).strip()
		if s.isdigit():
			return str(int(s))
		return s
	except (TypeError, ValueError):
		return str(conversation_id) if conversation_id is not None else ""


def _key(conversation_id: str) -> str:
	return f"{CHAT_VIEWERS_KEY_PREFIX}{conversation_id}"


def _prune_viewers(
	viewers: dict[str, Any] | None,
	now: float,
	ttl: float = VIEWER_TTL_SEC,
) -> dict[str, dict[str, float]]:
	if not isinstance(viewers, dict):
		return {}
	out: dict[str, dict[str, float]] = {}
	for user, tabs in viewers.items():
		if not user or not isinstance(tabs, dict):
			continue
		pruned_tabs: dict[str, float] = {}
		for tab_id, ts in tabs.items():
			try:
				age = now - float(ts)
			except (TypeError, ValueError):
				continue
			if age <= ttl:
				pruned_tabs[str(tab_id)] = float(ts)
		if pruned_tabs:
			out[str(user)] = pruned_tabs
	return out


def _load_and_prune(
	raw: Any,
	now: float,
) -> dict[str, dict[str, float]]:
	if raw is None:
		return {}
	if isinstance(raw, bytes):
		try:
			raw = raw.decode("utf-8")
		except Exception:
			return {}
	if isinstance(raw, str):
		if not raw.strip():
			return {}
		try:
			parsed: Any = json.loads(raw)
		except (json.JSONDecodeError, TypeError):
			return {}
		if not isinstance(parsed, dict):
			return {}
		return _prune_viewers(parsed, now, VIEWER_TTL_SEC)
	if isinstance(raw, dict):
		return _prune_viewers(raw, now, VIEWER_TTL_SEC)
	return {}


def get_active_viewer_users(conversation_id) -> list[str]:
	"""Return Frappe usernames with at least one non-stale tab. Prunes and persists."""
	conv = _normalize_conversation_id(conversation_id)
	if not conv:
		return []
	cache = frappe.cache()
	now = time.time()
	k = _key(conv)
	raw = cache.get_value(k)
	viewers = _load_and_prune(raw, now)
	if not viewers:
		cache.delete_value(k)
		return []
	cache.set_value(k, json.dumps(viewers), expires_in_sec=VIEWER_TTL_SEC)
	return list(viewers.keys())


@frappe.whitelist()
def heartbeat_whatsapp_view(conversation_id, tab_id):
	"""Record this tab as viewing the conversation. Reject Guest."""
	if frappe.session.user == "Guest":
		frappe.throw(frappe._("Login required"), frappe.PermissionError)

	conv = _normalize_conversation_id(conversation_id)
	if not conv:
		frappe.throw(frappe._("conversation_id is required"), frappe.MandatoryError)
	tid = (str(tab_id).strip() if tab_id is not None else "") or None
	if not tid:
		frappe.throw(frappe._("tab_id is required"), frappe.MandatoryError)

	user = frappe.session.user
	now = time.time()
	k = _key(conv)
	cache = frappe.cache()
	raw = cache.get_value(k)
	viewers = _load_and_prune(raw, now)
	if user not in viewers:
		viewers[user] = {}
	viewers[user][tid] = now
	viewers = _prune_viewers(viewers, now, VIEWER_TTL_SEC)
	cache.set_value(k, json.dumps(viewers), expires_in_sec=VIEWER_TTL_SEC)
	return {"ok": True}
