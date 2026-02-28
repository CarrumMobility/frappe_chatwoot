import frappe
import frappe.client as client

WHATSAPP_TEMPLATE_DOCTYPE = "WhatsApp Templates";

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
        message = [{
            "name": "ASDF_ASDF_ASDF",
            "template": "Welcome Kapil Rohilla",
            "footer": "WhatsApp Business "
        }]
        return message
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