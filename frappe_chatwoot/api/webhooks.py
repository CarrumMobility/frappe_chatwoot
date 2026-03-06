import frappe


def _get_phone_from_payload(payload):
	"""Extract phone number from Chatwoot webhook payload."""
	conversation = payload.get("conversation") or {}
	# contact_inbox.source_id e.g. "917004617522"
	source_id = (conversation.get("contact_inbox") or {}).get("source_id")
	if source_id:
		return source_id
	# meta.sender.phone_number e.g. "+917004617522"
	sender = (conversation.get("meta") or {}).get("sender") or {}
	phone = sender.get("phone_number")
	if phone:
		return phone
	return None

def _resolve_reference_and_emit_whatsapp_message():
	"""
	Resolve reference_doctype and reference_name from webhook payload using phone number,
	then emit socket event whatsapp_message so the CRM UI can refresh the WhatsApp list.
	"""
	try:
		from crm.integrations.api import get_contact_lead_or_deal_from_number
	except ImportError:
		return

	phone = _get_phone_from_payload(frappe.form_dict or {})
	if not phone:
		return
	reference_name, reference_doctype = get_contact_lead_or_deal_from_number(phone)
	if not reference_name or not reference_doctype:
		return
	frappe.publish_realtime(
		"whatsapp_message",
		{
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		},
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
	'''Register this webhook on chatwoot to handle message_created event. Emits whatsapp_message socket event for CRM.'''
	frappe.logger().info("Chatwoot webhook hit: message_created")
	_resolve_reference_and_emit_whatsapp_message()
	return "message_created"

@frappe.whitelist(allow_guest=True)
def message_updated():
	'''Register this webhook on chatwoot to handle message_updated event. Emits whatsapp_message socket event for CRM.'''
	frappe.logger().info("Chatwoot webhook hit: message_updated")
	_resolve_reference_and_emit_whatsapp_message()
	return {
        "message": "message_updated"
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



