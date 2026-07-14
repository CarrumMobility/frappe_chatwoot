import logging

from core.api.carrum_accounts import get_hub_telecaller_usernames
from core.constants.enums import EnumValues
import frappe
import random
from frappe_chatwoot.api.whatsapp import (
	_chatwoot_message_list_meta,
	_format_chat_list_timestamp,
	_get_chatwoot_ctx,
	assign_chatwoot_conversation_to_frappe_user,
)
from frappe_chatwoot.api.whatsapp_viewers import get_active_viewer_users
from crm.integrations.api import findOrCreateLead

# Frappe's default logger level is WARNING (dev) or ERROR (prod), so INFO never reaches the file.
# See frappe.utils.logger.default_log_level / get_logger().
log = frappe.logger("frappe_chatwoot:webhooks")
log.setLevel(logging.INFO)


def _get_chatwoot_webhook_payload() -> dict:
	"""Parse JSON body (Chatwoot) or fall back to form_dict."""
	req = frappe.request
	data = req.get_json(silent=True)
	if isinstance(data, dict) and data:
		payload = data
	else:
		payload = dict(frappe.form_dict or {})
	log.info("Chatwoot webhook payload: %s", payload)
	return payload


def _get_phone_from_payload(payload):
	"""Extract phone number from Chatwoot webhook payload."""
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


def _get_inbox_id_from_payload(payload: dict):
	"""Extract Chatwoot inbox id from nested or flat webhook payload."""
	msg, conversation = _extract_message_and_conversation(payload)
	return conversation.get("inbox_id") or msg.get("inbox_id") or payload.get("inbox_id")


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


def _get_inbound_lead_source_for_inbox(inbox_id) -> dict | None:
	"""Return inbound CRM Lead Source mapped to a Chatwoot inbox id."""
	if inbox_id is None or str(inbox_id).strip() == "":
		return None

	source = frappe.db.get_value(
		EnumValues.ReferenceDocType.LEAD_SOURCE,
		{
			"chatwoot_inbox_id": str(inbox_id).strip(),
			"purpose": EnumValues.LeadSourcePurpose.Inbound,
		},
		["name", "source_name"],
		as_dict=True,
	)
	if not source:
		log.info("No inbound CRM Lead Source mapped for inbox_id=%s", inbox_id)
		return None
	return source


def maybe_assign_telecaller_to_lead(hubId, lead_id):
	"""Pick a telecaller from Carrum hub users and set CRM Lead.telecaller. Returns Frappe username or None."""
	users = get_hub_telecaller_usernames(hubId)
	assignable_telecaller = users[random.randint(0, len(users) - 1)]
	# BUG: fix tc assignment logic if again plan to test it, it may have some issue
	allow_tc_assignment = False
	if allow_tc_assignment:
		db_record = frappe.db.get_doc(EnumValues.ReferenceDocType.CRM_LEAD, lead_id)
		if db_record:
			db_record.telecaller = assignable_telecaller
			db_record.save(ignore_permissions=True)
		return assignable_telecaller
	else:
		return None

def _chatwoot_ctx_matches_conversation_inbox(username: str | None, conversation_inbox_id) -> bool:
	if not username or conversation_inbox_id is None:
		return False
	ctx = _get_chatwoot_ctx(username)
	if not ctx:
		return False
	ctx_inbox_id = ctx.get("inbox_id")
	if ctx_inbox_id is None:
		return False
	return str(ctx_inbox_id).strip() == str(conversation_inbox_id).strip()



def _resolve_reference_and_emit_whatsapp_message():
	"""
	Emit two realtime events:
	
	- whatsapp_message_list → single user (telecaller / deal_owner) with chat_list payload for WaChatList.
	- whatsapp_message → CRM Lead / CRM Deal document room (subscribers viewing that doc in desk/SPA).
	"""
	try:
		return _resolve_reference_and_emit_whatsapp_message_impl()
	except Exception:
		frappe.db.rollback()
		log.exception("Failed to process Chatwoot webhook message")
		frappe.log_error(
			frappe.get_traceback(),
			"Chatwoot webhook message processing failed",
		)
		return False


def _resolve_reference_and_emit_whatsapp_message_impl():
	payload = _get_chatwoot_webhook_payload()
	phone = _get_phone_from_payload(payload)
	if not phone:
		return

	msg, conversation = _extract_message_and_conversation(payload)
	inbox_id = _get_inbox_id_from_payload(payload)
	source_detail = _get_inbound_lead_source_for_inbox(inbox_id)
	reference_doc = None
	hubId = None
	if source_detail:
		reference_doc = findOrCreateLead(
			mobileNo=phone,
			source=source_detail.get("source_name"),
			source_id=source_detail.get("name"),
		)
		hubId = source_detail.get("hub_id")
	else:
		reference_doc = findOrCreateLead(mobileNo=phone)

	reference_doctype = EnumValues.ReferenceDocType.CRM_LEAD
	if not reference_doc:
		log.info("findOrCreateLead failed for phone: %s", phone)
		return

	conv_id_raw = conversation.get("id")
	if conv_id_raw is None:
		conv_id_raw = msg.get("conversation_id")
	conversation_inbox_id = conversation.get("inbox_id")

	telecaller_user = None
	deal_owner = None
	if reference_doc.doctype == "CRM Lead":
		lead_type = reference_doc.lead_type
		telecaller_user = reference_doc.telecaller
		unassigned = not telecaller_user or str(telecaller_user).strip() in ("", "Guest")

		if lead_type == EnumValues.LeadType.LEAD and unassigned and inbox_id is not None:
			try:
				inbox_int = int(inbox_id)
			except (TypeError, ValueError):
				inbox_int = None
			if inbox_int is not None:
				
				assigned = maybe_assign_telecaller_to_lead(hubId, reference_doc.name)
				telecaller_user = reference_doc.telecaller

				if (
					assigned
					and conv_id_raw is not None
					and _chatwoot_ctx_matches_conversation_inbox(
						assigned,
						conversation_inbox_id,
					)
				):
					try:
						conv_int = int(conv_id_raw)
					except (TypeError, ValueError):
						conv_int = None
					if conv_int is not None:
						try:
							ok = assign_chatwoot_conversation_to_frappe_user(assigned, conv_int)
							if ok:
								log.info(
									"Assigned Chatwoot conversation %s to telecaller %s for lead %s",
									conv_int,
									assigned,
									reference_doc.name,
								)
						except Exception:
							log.exception("Failed to assign Chatwoot conversation %s to telecaller %s for lead %s",
								conv_int,
								assigned,
								reference_doc.name,
							)
							frappe.log_error(
								frappe.get_traceback(),
								"Failed to assign Chatwoot conversation to telecaller",
							)
	elif reference_doctype == "CRM Deal":
		deal_owner = frappe.db.get_value("CRM Deal", reference_doc.name, "deal_owner")

	chat_list = _build_chat_list_patch(payload, phone)

	if chat_list is not None:
		chat_list["display_name"] = _list_display_name(
			reference_doctype, reference_doc.name, chat_list.get("phone_number") or phone
		)

	message_detail = {
		"reference_doctype": reference_doctype,
		"reference_name": reference_doc.name,
	}
	if telecaller_user is not None:
		message_detail["telecaller"] = telecaller_user
	if deal_owner is not None:
		message_detail["deal_owner"] = deal_owner

	conv_id = None
	if chat_list and chat_list.get("conversation_id") is not None:
		conv_id = chat_list.get("conversation_id")
	else:
		msg, conversation = _extract_message_and_conversation(payload)
		conv_id = (conversation or {}).get("id")
		if conv_id is None and isinstance(msg, dict):
			conv_id = msg.get("conversation_id")
	
	if conv_id is not None:
		message_detail["conversation_id"] = conv_id

	message_list = {**message_detail, "chat_list": chat_list}

	list_user = _chat_list_recipient(reference_doctype, reference_doc.name)

	if list_user:
		frappe.publish_realtime("whatsapp_message_list", message_list, user=list_user)

	# CRM Lead: user-targeted whatsapp_message for active thread viewers; else doc room fallback
	if (
		reference_doctype == "CRM Lead"
		and message_detail.get("conversation_id") is not None
	):
		viewer_users = get_active_viewer_users(message_detail["conversation_id"])
		if viewer_users:
			for u in viewer_users:
				if u and u != "Guest":
					frappe.publish_realtime("whatsapp_message", message_detail, user=u)
		else:
			frappe.publish_realtime(
				"whatsapp_message",
				message_detail,
				doctype="CRM Lead",
				docname=reference_doc.name,
			)
	else:
		frappe.publish_realtime(
			"whatsapp_message",
			message_detail,
			doctype="CRM Lead",
			docname=reference_doc.name,
		)

	frappe.db.commit()
	return True


@frappe.whitelist()
def conversation_created():
	'''Register this webhook on chatwoot to handle conversation_created event'''
	log.info("Chatwoot webhook hit: conversation_created")
	_get_chatwoot_webhook_payload()
	return {}


@frappe.whitelist()
def conversation_status_changed():
	'''Register this webhook on chatwoot to handle conversation_status_changed event'''
	log.info("Chatwoot webhook hit: conversation_status_changed")
	_get_chatwoot_webhook_payload()
	return {}


@frappe.whitelist()
def conversation_updated():
	'''Register this webhook on chatwoot to handle conversation_updated event'''
	log.info("Chatwoot webhook hit: conversation_updated")
	_get_chatwoot_webhook_payload()
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
	log.info("Chatwoot webhook hit: contact_created")
	_get_chatwoot_webhook_payload()
	return {}


@frappe.whitelist()
def contact_updated():
	'''Register this webhook on chatwoot to handle contact_updated event'''
	log.info("Chatwoot webhook hit: contact_updated")
	_get_chatwoot_webhook_payload()
	return {}


@frappe.whitelist()
def webwidget_triggered():
	'''Register this webhook on chatwoot to handle webwidget_triggered event'''
	log.info("Chatwoot webhook hit: webwidget_triggered")
	_get_chatwoot_webhook_payload()
	return {}
