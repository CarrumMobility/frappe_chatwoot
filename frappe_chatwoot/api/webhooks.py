import logging

import frappe

from frappe_chatwoot.api.whatsapp import _chatwoot_message_list_meta, _format_chat_list_timestamp
from crm.integrations.api import get_contact_lead_or_deal_from_number

# Frappe's default logger level is WARNING (dev) or ERROR (prod), so INFO never reaches the file.
# See frappe.utils.logger.default_log_level / get_logger().
log = frappe.logger("frappe_chatwoot:webhooks")
log.setLevel(logging.INFO)


def _get_chatwoot_webhook_payload() -> dict:
	"""Parse JSON body (Chatwoot) or fall back to form_dict."""
	req = frappe.request
	data = req.get_json(silent=True)
	if isinstance(data, dict) and data:
		return data
	return dict(frappe.form_dict or {})


def _get_phone_from_payload(payload):
	"""Extract phone number from Chatwoot webhook payload."""
	log.info("Payload: " + str(payload))
	conversation = payload.get("conversation") or {}
	# contact_inbox.source_id e.g. "917004617522"
	source_id = (conversation.get("contact_inbox") or {}).get("source_id")
	if source_id:
		return str(source_id)
	# meta.sender.phone_number e.g. "+917004617522"
	sender = (conversation.get("meta") or {}).get("sender") or {}
	phone = sender.get("phone_number")
	if phone:
		return str(phone)
	return None


def _extract_message_and_conversation(payload: dict):
	"""Support message nested under \"message\" or flat webhook body."""
	msg = payload.get("message")
	if not isinstance(msg, dict):
		if payload.get("content") is not None or payload.get("conversation_id") is not None:
			msg = payload
		else:
			msg = {}
	conversation = payload.get("conversation") or {}
	if not conversation and isinstance(msg.get("conversation"), dict):
		conversation = msg["conversation"]
	return msg, conversation


def _chat_list_recipient(reference_doctype: str, reference_name: str) -> str | None:
	"""User who receives WaChatList updates: telecaller (CRM Lead) or deal_owner (CRM Deal)."""
	if reference_doctype == "CRM Lead":
		u = frappe.db.get_value("CRM Lead", reference_name, "telecaller")
	else:
		return None
	if u and u != "Guest":
		return u
	return None


def _list_display_name(reference_doctype: str, reference_name: str, phone: str | None) -> str:
	if reference_doctype == "CRM Lead":
		title = frappe.db.get_value("CRM Lead", reference_name, "lead_name")
		return (title or reference_name or phone or "Chat").strip()
	if reference_doctype == "CRM Deal":
		title = frappe.db.get_value("CRM Deal", reference_name, "organization")
		return (title or reference_name or phone or "Chat").strip()
	return (reference_name or phone or "Chat").strip()


def _build_chat_list_patch(payload: dict, phone: str | None) -> dict | None:
	msg, conversation = _extract_message_and_conversation(payload)
	conv_id = conversation.get("id")
	if conv_id is None:
		conv_id = msg.get("conversation_id")
	if conv_id is None:
		return None

	inbox_id = conversation.get("inbox_id") or msg.get("inbox_id")
	list_meta = _chatwoot_message_list_meta(msg)
	preview = list_meta.get("preview") or ""
	last_thumb = list_meta.get("thumb_url")

	created_at = msg.get("updated_at") or msg.get("created_at")
	last_at = _format_chat_list_timestamp(created_at) if created_at else ""
	if not last_at:
		last_at = frappe.utils.format_datetime(frappe.utils.now_datetime())

	# Chatwoot message_type: 0 = incoming, 1 = outgoing
	message_type = msg.get("message_type")
	if message_type == 0 or message_type == "incoming":
		is_incoming = True
	elif message_type == 1 or message_type == "outgoing":
		is_incoming = False
	else:
		# webhook event message_created is usually a new message; treat unknown as incoming for unread
		is_incoming = True

	meta = conversation.get("meta") or {}
	sender = meta.get("sender") or {}
	avatar_url = (sender.get("thumbnail") or "").strip() or None

	return {
		"conversation_id": conv_id,
		"inbox_id": inbox_id,
		"phone_number": phone or sender.get("phone_number") or "",
		"last_message": preview,
		"last_message_at": last_at,
		"last_message_thumb": last_thumb,
		"is_incoming": is_incoming,
		"unread_increment": 1 if is_incoming else 0,
		"avatar_url": avatar_url,
	}


def _resolve_reference_and_emit_whatsapp_message():
	"""
	Emit two realtime events:
	
	- whatsapp_message_list → single user (telecaller / deal_owner) with chat_list payload for WaChatList.
	- whatsapp_message → CRM Lead / CRM Deal document room (subscribers viewing that doc in desk/SPA).
	"""

	payload = _get_chatwoot_webhook_payload()
	phone = _get_phone_from_payload(payload)
	if not phone:
		return

	reference_name, reference_doctype = get_contact_lead_or_deal_from_number(phone)
	if not reference_name or not reference_doctype:
		return
	
	telecaller_user = None
	deal_owner = None
	if reference_doctype == "CRM Lead":
		telecaller_user = frappe.db.get_value("CRM Lead", reference_name, "telecaller")
	elif reference_doctype == "CRM Deal":
		deal_owner = frappe.db.get_value("CRM Deal", reference_name, "deal_owner")

	chat_list = _build_chat_list_patch(payload, phone)
	
	if chat_list is not None:
		chat_list["display_name"] = _list_display_name(
			reference_doctype, reference_name, chat_list.get("phone_number") or phone
		)

	message_detail = {
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
	}
	if telecaller_user is not None:
		message_detail["telecaller"] = telecaller_user
	if deal_owner is not None:
		message_detail["deal_owner"] = deal_owner

	message_list = {**message_detail, "chat_list": chat_list}

	list_user = _chat_list_recipient(reference_doctype, reference_name)
	
	if list_user:
		frappe.publish_realtime("whatsapp_message_list", message_list, user=list_user)

	if reference_doctype in ("CRM Lead", "CRM Deal"):
		frappe.publish_realtime(
			"whatsapp_message",
			message_detail,
		)


@frappe.whitelist()
def conversation_created():
	'''Register this webhook on chatwoot to handle conversation_created event'''
	return {}


@frappe.whitelist()
def conversation_status_changed():
	'''Register this webhook on chatwoot to handle conversation_status_changed event'''
	return {}


@frappe.whitelist()
def conversation_updated():
	'''Register this webhook on chatwoot to handle conversation_updated event'''
	return {}


@frappe.whitelist(allow_guest=True)
def message_created():
	'''Chatwoot message_created: emits whatsapp_message_list (owner) and whatsapp_message (doc room).'''
	log.info("Chatwoot webhook hit: message_created")
	_resolve_reference_and_emit_whatsapp_message()
	return "message_created"


@frappe.whitelist(allow_guest=True)
def message_updated():
	'''Chatwoot message_updated: emits whatsapp_message_list (owner) and whatsapp_message (doc room).'''
	log.info("Chatwoot webhook hit: message_updated")
	_resolve_reference_and_emit_whatsapp_message()
	return {
		"message": "message_updated",
	}


@frappe.whitelist()
def contact_created():
	'''Register this webhook on chatwoot to handle contact_created event'''
	return {}


@frappe.whitelist()
def contact_updated():
	'''Register this webhook on chatwoot to handle contact_updated event'''
	return {}


@frappe.whitelist()
def webwidget_triggered():
	'''Register this webhook on chatwoot to handle webwidget_triggered event'''
	return {}
