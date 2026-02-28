import frappe


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

@frappe.whitelist()
def message_created():
    '''Register this webhook on chatwoot to handle conversation_updated event'''
    return {}

@frappe.whitelist()
def message_updated():
    '''Register this webhook on chatwoot to handle message_updated event'''
    return {}

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



