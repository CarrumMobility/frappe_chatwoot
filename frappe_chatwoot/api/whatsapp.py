import random
import re
from core.api import carrum_accounts, carrum_utils
import frappe
import requests
from datetime import datetime

CHATWOOT_BASE = frappe.conf.get("chatwoot_base_url")
# CHATWOOT_ACC_ID = frappe.conf.get("chatwoot_account_id")
LEAD_DOCTYPE = "CRM Leads"
FRAPPE_CHATWOOT_MESSAGE_TYPE_MAPPING = {1: "Outgoing",2: "Incoming"}

@frappe.whitelist()
def is_whatsapp_enabled():
    '''Give status should we show the WhatsApp chat tab to user or not'''
    return True

@frappe.whitelist()
def is_whatsapp_installed():
    '''Used to show the whatsapp-integration option in frappe-com setting'''
    return True

@frappe.whitelist()
def get_whatsapp_messages(reference_doctype: str, reference_name: str):
    '''Get the list of messages under by a lead'''

    # Find lead phone number
    leadData = frappe.get_doc(LEAD_DOCTYPE, reference_name)
    leadPhoneNumber = leadData.get("mobile_no")

    if not leadPhoneNumber:
        return []
    # find the contact Id for this number
    contactInfo = get_or_create_contact(leadPhoneNumber)

    # find conversations for this contact, then filter by WhatsApp inbox
    conversations = get_conversation(contactInfo["contact_id"])
    if not conversations or len(conversations) == 0:
        return []

    conversations = [c for c in conversations if c.get("inbox_id") == CHATWOOT_DIPESH_ACC_INBOX_ID]
    conversations = [conversations[0]]
    all_messages = []
    lastMsgId = None
    for conversation in conversations:
        conversation_id = conversation.get("id")
        if not conversation_id:
            continue
        while(True):
            # print("Previous Message Id: ", lastMsgId)
            raw_messages = get_messages(conversation_id, before_msg_id=lastMsgId)
            # print("Raw Messages: ", raw_messages)
            if len(raw_messages) == 0:
                break
            if(len(raw_messages) > 0):
                # print("New Message Id: ", raw_messages[0].get("id"))
                lastMsgId = raw_messages[0].get("id")
            else:
                break
            all_messages.extend(raw_messages)
    data = []
    for msg in all_messages:
        sender = msg.get("sender")
        if not sender:
            continue
        msg_type = FRAPPE_CHATWOOT_MESSAGE_TYPE_MAPPING.get(msg.get("message_type"))
        from_name = sender.get("name") if isinstance(sender, dict) else None
        created_at = msg.get("created_at")
        creation_str = None
        if created_at is not None:
            try:
                dt = (
                    datetime.fromtimestamp(created_at)
                    if isinstance(created_at, (int, float))
                    else frappe.utils.get_datetime(created_at)
                )
                creation_str = frappe.utils.format_datetime(dt, "yyyy-MM-dd HH:mm:ss") if dt else None
            except Exception:
                creation_str = None
        if msg.get("content") is None:
            continue
        data.append({
            "name": str(msg.get("id")),
            "type": msg_type,
            "to": None,
            "from": contactInfo.get("source_id"),
            "content_type": msg.get("content_type"),
            "message_type": "Manual",
            "attach": msg.get("attachment"),
            "template": msg.get("template"),
            "use_template": msg.get("use_template"),
            "message_id": msg.get("source_id") or f"msg_{random.randint(100000, 999999)}",
            "is_reply": msg.get("is_reply") or 0,
            "reply_to_message_id": msg.get("reply_to_message_id"),
            "creation": creation_str,
            "message": msg.get("content"),
            "status": msg.get("status"),
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "template_parameters": msg.get("template_parameters"),
            "template_header_parameters": msg.get("template_header_parameters"),
            "from_name": from_name or "Administrator",
        })
    
    return data

@frappe.whitelist()
def create_whatsapp_message(
	reference_doctype: str,
	reference_name: str,
	message: str,
	to: str,
	attach: str,
	reply_to: str,
	content_type: str = "text",
):
    """Find contact and conversation by phone/inbox, then send the message in that thread."""
    phone_no = _sanitize_phone(to)

    chatwootConfig = carrum_accounts.get_chatwoot_config_by_frappe_user(frappe.session.user)
    apiAccessToken = chatwootConfig.api_access_token

    contact_info = find_contact(phone_no, apiAccessToken)

    conversation = find_conversation(contact_info["contact_id"], contact_info["inbox_id"])
    if conversation.get("can_reply") is False:
        raise frappe.throw("Send a template message to resume the conversation")
    conversation_id = conversation["id"]

    msg_response = send_message(conversation_id, message)
    
    return {
        "contact_id": contact_info["contact_id"],
        "inbox_id": contact_info["inbox_id"],
        "source_id": contact_info["source_id"],
        "conversation_id": conversation_id,
        "message": msg_response,
    }

@frappe.whitelist()
def react_on_whatsapp_message(emoji: str, reply_to_name: str):
    return {}


@frappe.whitelist()
def send_whatsapp_template(reference_doctype: str, reference_name: str, template: str, to: str):
    """
    Validate template via inbox API, find or create contact and conversation,
    then send a WhatsApp template message with body variables set to TEST_VAL.
    """
    template_info = get_template_info(template)
    body_variable_indices = template_info["body_variable_indices"]
    body_params = {str(i): DUMMY_TEMPLATE_VAL for i in body_variable_indices}
    content = _fill_template_body(template_info.get("body_text", ""), body_params)

    phone_no = _sanitize_phone(to)
    contact_info = get_or_create_contact(phone_no)
    conversation_id = get_or_create_conversation(
        contact_id=contact_info["contact_id"],
        inbox_id=contact_info["inbox_id"],
        source_id=contact_info["source_id"],
    )
    msg_response = send_template_message(
        conversation_id=conversation_id,
        template_name=template,
        content=content,
        body_params=body_params,
        category=str(template_info.get("category", "UTILITY")),
        language=str(template_info.get("language", "en")),
    )
    return {
        "contact_id": contact_info["contact_id"],
        "inbox_id": contact_info["inbox_id"],
        "source_id": contact_info["source_id"],
        "conversation_id": conversation_id,
        "message": msg_response,
    }



# UTILITY FUNCTIONS


def _sanitize_phone(phoneNo: str):
    if len(phoneNo) == 10:
        return phoneNo
    if len(phoneNo) < 10:
        raise Exception("Invalid Phone number Length")
    return phoneNo.replace("+91", "")


def _parse_contact_info(contact: dict) -> dict:
    """Extract contact_id, inbox_id, source_id from a contact payload (with contact_inboxes)."""
    contact_id = contact["id"]
    contact_inboxes = contact.get("contact_inboxes") or []
    whatsapp_inbox = next(
        (
            ci
            for ci in contact_inboxes
            if (ci.get("inbox") or {}).get("channel_type") == "Channel::Whatsapp"
        ),
        contact_inboxes[0] if contact_inboxes else None,
    )
    if not whatsapp_inbox:
        raise Exception("Contact has no WhatsApp inbox")
    return {
        "contact_id": contact_id,
        "inbox_id": whatsapp_inbox["inbox"]["id"],
        "source_id": whatsapp_inbox["source_id"],
    }


def find_contact(phone_no: str, accessToken: str, accountId: str) -> dict:
    """
    Search contact by phone; do not create. Returns contact_id, inbox_id, source_id
    for the WhatsApp channel. Raises if not found or multiple matches.
    """
    search_url = f"{CHATWOOT_BASE}/accounts/{accountId}/contacts/search?page=1&q={phone_no}"
    resp = requests.get(search_url, headers=getHeaders(accessToken))
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload") or []

    if len(payload) == 0:
        raise Exception(f"Contact not found for phone {phone_no}")
    if len(payload) > 1:
        raise Exception("Multiple contacts found for this phone number")
    return _parse_contact_info(payload[0])


def get_or_create_contact(phone_no: str) -> dict:
    """
    Search contact by phone; create if not found. Returns contact_id, inbox_id, source_id
    for the WhatsApp channel.
    """

    # apiAccessToken = frappe.conf.get("chatwoot_")
    # Execute nest api to find the inboxId

    try:
        return find_contact(phone_no)
    except Exception as e:
        if "Contact not found" not in str(e):
            raise
    create_url = f"{CHATWOOT_BASE}/api/v1/accounts/{CHATWOOT_ACC_ID}/contacts"
    create_resp = requests.post(
        create_url,
        headers={"api_access_token": apiAccessToken, "Content-Type": "application/json"},
        json={
            "inbox_id": inboxId,
            "phone_number": f"+91{phone_no}",
            "name": phone_no,
            "identifier": phone_no,
        },
    )
    create_resp.raise_for_status()
    created = create_resp.json()
    contact_obj = created.get("payload", created)
    if isinstance(contact_obj, list):
        contact_obj = contact_obj[0]
    contact_inboxes = contact_obj.get("contact_inboxes") or []
    if not contact_inboxes:
        raise Exception("Created contact has no contact_inboxes")
    return _parse_contact_info(contact_obj)


def get_conversation(contact_id: int) -> list | None:
    """
    List contact conversations; return the first open/pending one for this inbox.
    Returns conversations list if found, else None.
    """
    list_url = f"{CHATWOOT_BASE}/accounts/{CHATWOOT_ACC_ID}/contacts/{contact_id}/conversations"
    resp = requests.get(list_url, headers=_HEADERS)
    if resp.status_code == 404:
        raise Exception("Contact not found while fetching conversations")
    resp.raise_for_status()
    data = resp.json()
    conversations = data.get("payload") or []
    return conversations

def create_conversation(
    contact_id: int,
    inbox_id: int,
    source_id: str,
    initial_message: str | None = None,
) -> int:
    """Create a new conversation for the contact in the given inbox. Returns conversation_id."""
    create_url = f"{CHATWOOT_BASE}/accounts/{CHATWOOT_ACC_ID}/conversations"
    body = {"source_id": source_id, "inbox_id": inbox_id, "contact_id": contact_id}
    if initial_message:
        body["message"] = {"content": initial_message}
    conv_resp = requests.post(create_url, headers=_HEADERS, json=body)
    conv_resp.raise_for_status()
    conv_data = conv_resp.json()
    return conv_data["id"]

def find_conversation(contact_id: int, inbox_id: int) -> dict:
    """
    Find an existing conversation for this contact in the given inbox.
    Returns the conversation dict (includes id, can_reply, etc.). Raises if no conversation found.
    """
    conversations = get_conversation(contact_id)
    by_inbox = [c for c in conversations if c.get("inbox_id") == inbox_id]
    if not by_inbox:
        raise Exception("No conversation found for this contact in the given inbox")
    return by_inbox[0]


def get_or_create_conversation(
    contact_id: int,
    inbox_id: int,
    source_id: str,
    initial_message: str | None = None,
) -> int:
    """
    Get existing open/pending conversation for this contact+inbox, or create one.
    Returns conversation_id.
    """
    conversations = get_conversation(contact_id)
    by_inbox = [c for c in conversations if c.get("inbox_id") == inbox_id]
    if by_inbox:
        return by_inbox[0]["id"]
    return create_conversation(contact_id, inbox_id, source_id, initial_message)

def send_message(conversation_id: int, content: str) -> dict:
    """Send a message in an existing conversation. Returns the message response."""
    url = f"{CHATWOOT_BASE}/accounts/{CHATWOOT_ACC_ID}/conversations/{conversation_id}/messages"
    resp = requests.post(url, headers=_HEADERS, json={"content": content})
    resp.raise_for_status()
    return resp.json()

def send_template_message(
    conversation_id: int,
    template_name: str,
    content: str,
    body_params: dict | None = None,
    category: str = "UTILITY",
    language: str = "en",
) -> dict:
    """
    Send a WhatsApp template message in an existing conversation.
    body_params: map of variable index to value, e.g. {"1": "val1", "2": "val2"}.
    Returns the message response.
    """
    body_params = body_params or {}
    payload = {
        "content": content,
        "template_params": {
            "name": template_name,
            "category": category,
            "language": language,
            "processed_params": {
                "body": body_params,
            },
        },
    }
    url = f"{CHATWOOT_BASE}/accounts/{CHATWOOT_ACC_ID}/conversations/{conversation_id}/messages"
    resp = requests.post(url, headers=_HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()


def get_messages(conversation_id: int, before_msg_id: str | int = None) -> list[dict]:
    """
    List all messages of a conversation.
    API: GET /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages
    https://developers.chatwoot.com/api-reference/messages/get-messages
    Returns the payload array of message objects.
    """

    url = f"{CHATWOOT_BASE}/accounts/{CHATWOOT_ACC_ID}/conversations/{conversation_id}/messages?"
    if before_msg_id:
        url += f"&before={before_msg_id}"
    print("URL: ", url)
    resp = requests.get(url, headers=_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    return data.get("payload") or []

DUMMY_TEMPLATE_VAL = "TEST_VAL"

def _parse_body_variable_indices(body_text: str) -> list[int]:
    """Extract WhatsApp body variable indices from BODY text (e.g. {{1}}, {{2}}). Returns sorted unique indices."""
    if not body_text:
        return []
    matches = re.findall(r"\{\{(\d+)\}\}", body_text)
    return sorted(set(int(m) for m in matches))


def _fill_template_body(body_text: str, body_params: dict) -> str:
    """Replace {{1}}, {{2}}, ... in body text with values from body_params."""
    result = body_text
    for key, value in body_params.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result

def get_template_info(template_name: str, inbox_id: int | None = None) -> dict:
    """
    Fetch template by name from Chatwoot inbox API. Validates template exists and
    returns body variable indices (from BODY component {{1}}, {{2}}, etc.).
    Returns dict with keys: name, body_variable_indices, language, category.
    Raises if template not found.
    """
    inbox_id = inbox_id or CHATWOOT_DIPESH_ACC_INBOX_ID
    url = f"{CHATWOOT_BASE}/accounts/{CHATWOOT_ACC_ID}/inboxes/{inbox_id}"
    resp = requests.get(url, headers=_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload")
    if isinstance(payload, dict):
        message_templates = payload.get("message_templates") or []
    else:
        message_templates = data.get("message_templates") or []

    for t in message_templates:
        if t.get("name") == template_name:
            body_text = ""
            language = t.get("language", {}).get("code", "en") if isinstance(t.get("language"), dict) else t.get("language") or "en"
            category = t.get("category", "UTILITY")
            for comp in t.get("components") or []:
                if comp.get("type") == "BODY":
                    body_text = comp.get("text") or ""
                    break
            body_variable_indices = _parse_body_variable_indices(body_text)
            return {
                "name": template_name,
                "body_text": body_text,
                "body_variable_indices": body_variable_indices,
                "language": language,
                "category": category,
            }
    raise Exception(f"Template not found: {template_name}")

def _fill_template_body(body_text: str, body_params: dict) -> str:
    """Replace {{1}}, {{2}}, ... in body text with values from body_params."""
    result = body_text
    for key, value in body_params.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


def _sanitize_phone(phoneNo: str):
    if len(phoneNo) == 10:
        return phoneNo
    if len(phoneNo) < 10:
        raise Exception("Invalid Phone number Length")
    return phoneNo.replace("+91", "")

def get_or_create_contact(phone_no: str) -> dict:
    """
    Search contact by phone; create if not found. Returns contact_id, inbox_id, source_id
    for the WhatsApp channel.
    """
    try:
        return find_contact(phone_no)
    except Exception as e:
        if "Contact not found" not in str(e):
            raise
    create_url = f"{CHATWOOT_BASE}/accounts/{CHATWOOT_ACC_ID}/contacts"
    create_resp = requests.post(
        create_url,
        headers=_HEADERS,
        json={
            "inbox_id": CHATWOOT_DIPESH_ACC_INBOX_ID,
            "phone_number": f"+91{phone_no}",
            "name": phone_no,
            "identifier": phone_no,
        },
    )
    create_resp.raise_for_status()
    created = create_resp.json()
    contact_obj = created.get("payload", created)
    if isinstance(contact_obj, list):
        contact_obj = contact_obj[0]
    contact_inboxes = contact_obj.get("contact_inboxes") or []
    if not contact_inboxes:
        raise Exception("Created contact has no contact_inboxes")
    return _parse_contact_info(contact_obj)

def get_or_create_conversation(
    contact_id: int,
    inbox_id: int,
    source_id: str,
    initial_message: str | None = None,
) -> int:
    """
    Get existing open/pending conversation for this contact+inbox, or create one.
    Returns conversation_id.
    """
    conversations = get_conversation(contact_id)
    by_inbox = [c for c in conversations if c.get("inbox_id") == inbox_id]
    if by_inbox:
        return by_inbox[0]["id"]
    return create_conversation(contact_id, inbox_id, source_id, initial_message)


def send_template_message(
    conversation_id: int,
    template_name: str,
    content: str,
    body_params: dict | None = None,
    category: str = "UTILITY",
    language: str = "en",
) -> dict:
    """
    Send a WhatsApp template message in an existing conversation.
    body_params: map of variable index to value, e.g. {"1": "val1", "2": "val2"}.
    Returns the message response.
    """
    body_params = body_params or {}
    payload = {
        "content": content,
        "template_params": {
            "name": template_name,
            "category": category,
            "language": language,
            "processed_params": {
                "body": body_params,
            },
        },
    }
    url = f"{CHATWOOT_BASE}/accounts/{CHATWOOT_ACC_ID}/conversations/{conversation_id}/messages"
    resp = requests.post(url, headers=_HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()


def getHeaders(accessToken: str):
    return {"api_access_token": accessToken}