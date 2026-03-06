import frappe
import frappe.client as client
import frappe_chatwoot.api.whatsapp as whatsapp
WHATSAPP_TEMPLATE_DOCTYPE = "WhatsApp Templates";
import requests

get_inbox_detail = {
    "url": "https://app.chatwoot.com/api/v1/accounts/153201/inboxes", # Expecting inboxId in the end of url
    "method": "GET",
}

def _get_whatsapp_templates():
    print("GETTING WHATSAPP TEMPLATES")
    template_list = []
    url = f'{get_inbox_detail.get("url")}/{whatsapp.CHATWOOT_DIPESH_ACC_INBOX_ID}'

    response = requests.get(url, headers=whatsapp._HEADERS)
    data = response.json()
    messageTemplates = data.get("message_templates")
    for template in messageTemplates:
        template_name = template.get("name", "")
        body_text = ""
        footer_text = ""

        for component in template.get("components", []):
            if component.get("type") == "BODY":
                body_text = component.get("text", "")
            elif component.get("type") == "FOOTER":
                footer_text = component.get("text", "")
            elif component.get("type") == "HEADER":
                footer_text = component.get("text", "")

        template_list.append({
            "name": template_name,
            "template": body_text,
            "footer": footer_text
        })
    return template_list

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