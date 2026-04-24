import frappe
import frappe.client as client
import frappe_chatwoot.api.whatsapp as whatsapp
import requests

WHATSAPP_TEMPLATE_DOCTYPE = "WhatsApp Templates"


def _get_whatsapp_templates():
    ctx = whatsapp._get_chatwoot_ctx()
    if ctx is None:
        return []
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/inboxes/{ctx['inbox_id']}"

    response = requests.get(url, headers=ctx["headers"], timeout=60)
    data = response.json()
    payload = data.get("payload")
    if isinstance(payload, dict):
        message_templates = payload.get("message_templates") or []
    else:
        message_templates = data.get("message_templates") or []
    return [whatsapp.enrich_message_template(t) for t in (message_templates or [])]

@frappe.whitelist()
def get_list(
	doctype: str,
	fields: list | None = None,
	filters: dict | None = None,
	group_by: str | None = None,
	order_by: str | None = None,
	limit_start: int | None = None,
	limit_page_length: int = 20,
	parent: str | None = None,
	debug: bool = False,
	as_dict: bool = True,
	or_filters: dict | None = None,
	expand: list | None = None,
):
    if doctype == WHATSAPP_TEMPLATE_DOCTYPE:
        return _get_whatsapp_templates()
    else:
        return client.get_list(
            doctype=doctype,
            fields=fields,
            filters=filters,
            group_by=group_by,
            order_by=order_by,
            limit_start=limit_start,
            limit_page_length=limit_page_length,
            parent=parent,
            debug=debug,
            as_dict= as_dict,
            or_filters=or_filters,
            expand=expand
        )