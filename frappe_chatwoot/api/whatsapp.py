import json
import random
import frappe
import requests
from datetime import datetime

CHATWOOT_ACC_ID = "153201"
CHATWOOT_DIPESH_ACC_API_ACCESS_TOKEN = "68sCyks2Sdowv1nVCRDJQXBJ"
CHATWOOT_DIPESH_ACC_INBOX_ID = 97272
CHATWOOT_KAPIL_ACC_API_ACCESS_TOKEN = "sMMqKcJXPMBYNHqQ4nQyJEvL"
CHATWOOT_BASE = "https://app.chatwoot.com/api/v1"
_HEADERS = {"api_access_token": CHATWOOT_DIPESH_ACC_API_ACCESS_TOKEN, "Content-Type": "application/json"}
LEAD_DOCTYPE = "CRM Leads"

FRAPPE_CHATWOOT_MESSAGE_TYPE_MAPPING = {
    1: "Outgoing",
    2: "Incoming"
}
'''
Agent - [
  {
    "id": 162976,
    "account_id": 153201,
    "availability_status": "online",
    "auto_offline": true,
    "confirmed": true,
    "email": "dipesh.gupta@carrum.co.in",
    "provider": "email",
    "available_name": "Dipesh Gupta",
    "name": "Dipesh Gupta",
    "role": "administrator",
    "thumbnail": "https://app.chatwoot.com/rails/active_storage/representations/redirect/eyJfcmFpbHMiOnsibWVzc2FnZSI6IkJBaHBCTjlIUXdRPSIsImV4cCI6bnVsbCwicHVyIjoiYmxvYl9pZCJ9fQ==--9042c309e0e2dbfa7a3bb53591933531570d8d4c/eyJfcmFpbHMiOnsibWVzc2FnZSI6IkJBaDdCem9MWm05eWJXRjBTU0lJY0c1bkJqb0dSVlE2RTNKbGMybDZaVjkwYjE5bWFXeHNXd2RwQWZvdyIsImV4cCI6bnVsbCwicHVyIjoidmFyaWF0aW9uIn19--624b3ceb3fdf42c4b07c7818563fe60603b6095b/unnamed.png",
    "custom_role_id": null
  },
  {
    "id": 163048,
    "account_id": 153201,
    "availability_status": "online",
    "auto_offline": true,
    "confirmed": true,
    "email": "kapil.rohilla@carrum.co.in",
    "provider": "email",
    "available_name": "kapil rohilla",
    "name": "kapil rohilla",
    "role": "agent",
    "thumbnail": "",
    "custom_role_id": null
  }
]
'''

# Example usage
# CRM_LEAD_ID = "LEAD-00045"
# INBOX_HMAC_KEY = "your_chatwoot_inbox_hmac_token_here"

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
    search_url = f"{CHATWOOT_BASE}/accounts/{CHATWOOT_ACC_ID}/contacts/search?page=1&q={phone_no}"
    resp = requests.get(search_url, headers=_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload") or []

    if len(payload) == 0:
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
        contact_id = contact_obj["id"]
        contact_inboxes = contact_obj.get("contact_inboxes") or []
        if not contact_inboxes:
            raise Exception("Created contact has no contact_inboxes")
        source_id = contact_inboxes[0]["source_id"]
        inbox_id = contact_inboxes[0]["inbox"]["id"]
        return {"contact_id": contact_id, "inbox_id": inbox_id, "source_id": source_id}

    if len(payload) == 1:
        contact = payload[0]
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
        source_id = whatsapp_inbox["source_id"]
        inbox_id = whatsapp_inbox["inbox"]["id"]
        return {"contact_id": contact_id, "inbox_id": inbox_id, "source_id": source_id}

    raise Exception("Multiple contacts found for this phone number")


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
    if conversations:
        conversation_id = conversations[0]["id"]
        return conversation_id
    else:
        return create_conversation(contact_id, inbox_id, source_id, initial_message)


def send_message(conversation_id: int, content: str) -> dict:
    """Send a message in an existing conversation. Returns the message response."""
    url = f"{CHATWOOT_BASE}/accounts/{CHATWOOT_ACC_ID}/conversations/{conversation_id}/messages"
    resp = requests.post(url, headers=_HEADERS, json={"content": content})
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
    leadData = frappe.get_doc("CRM Lead", reference_name)
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
            print("Previous Message Id: ", lastMsgId)
            raw_messages = get_messages(conversation_id, before_msg_id=lastMsgId)
            print("Raw Messages: ", raw_messages)
            if len(raw_messages) == 0:
                break
            if(len(raw_messages) > 0):
                print("New Message Id: ", raw_messages[0].get("id"))
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
    """Get or create contact and conversation, then send the message in the same thread."""
    phone_no = _sanitize_phone(to)
    contact_info = get_or_create_contact(phone_no)
    conversation_id = get_or_create_conversation(
        contact_id=contact_info["contact_id"],
        inbox_id=contact_info["inbox_id"],
        source_id=contact_info["source_id"],
    )
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

