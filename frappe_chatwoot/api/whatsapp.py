import random
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode

from core.api import carrum_accounts
import frappe
import requests

LEAD_DOCTYPE = "CRM Lead"
FRAPPE_CHATWOOT_MESSAGE_TYPE_MAPPING = {1: "Outgoing", 2: "Incoming"}


def _get_chatwoot_ctx(username: str | None = None) -> dict | None:
    """Load per-user Chatwoot token and inbox from Carrum; account id and base URL from site config."""
    user = username or frappe.session.user
    cfg = carrum_accounts.get_chatwoot_config_by_frappe_user(user)
    if cfg is None:
        return None

    account_id = frappe.conf.get("chatwoot_account_id")
    if account_id is None:
        frappe.throw("Chatwoot account id is not configured (chatwoot_account_id).")
    base_url = (frappe.conf.get("chatwoot_base_url") or "").rstrip("/")
    print('cfg: ' + str(cfg))
    token = (cfg.token or "").strip()
    if not token:
        return None

    # Prefer site override; otherwise use inbox from Carrum user credentials.
    inbox_id = cfg.inboxId

    return {
        "api_access_token": token,
        "inbox_id": inbox_id,
        "account_id": account_id,
        "base_url": base_url,
        "headers": {"api_access_token": token, "Content-Type": "application/json"},
    }


@frappe.whitelist()
def is_whatsapp_enabled():
    """Give status should we show the WhatsApp chat tab to user or not"""
    ctx = _get_chatwoot_ctx()
    if ctx is None:
        return False
    return True

@frappe.whitelist()
def is_whatsapp_installed():
    """Used to show the whatsapp-integration option in frappe-com setting"""
    return True


@frappe.whitelist()
def get_whatsapp_messages(reference_doctype: str, reference_name: str):
    """Get the list of messages under by a lead"""
    ctx = _get_chatwoot_ctx()
    print("CTX: "+str(ctx), file=sys.stderr, flush=True)
    if ctx is None:
        return []
    print("REFERENCE NAME: "+LEAD_DOCTYPE, file=sys.stderr, flush=True)
    leadData = frappe.get_doc(LEAD_DOCTYPE, reference_name)
    print("LEAD DATA: "+str(leadData), file=sys.stderr, flush=True)
    leadPhoneNumber = leadData.get("mobile_no")
    print("LEAD PHONE NUMBER: "+str(leadPhoneNumber), file=sys.stderr, flush=True)
    if not leadPhoneNumber:
        return []
    contactInfo = get_or_create_contact(leadPhoneNumber, ctx)
    print("CONTACT INFO: "+str(contactInfo), file=sys.stderr, flush=True)
    conversations = get_conversation(contactInfo["contact_id"], ctx)
    print('CONVERSATIONS: '+str(conversations), file=sys.stderr, flush=True)
    if not conversations or len(conversations) == 0:
        return []

    conversations = [c for c in conversations if c.get("inbox_id") == ctx["inbox_id"]]
    print("conversations", str(conversations))
    if not conversations:
        return []
    conversations = [conversations[0]]
    all_messages = []
    lastMsgId = None
    for conversation in conversations:
        conversation_id = conversation.get("id")
        if not conversation_id:
            continue
        while True:
            raw_messages = get_messages(conversation_id, ctx, before_msg_id=lastMsgId)
            if len(raw_messages) == 0:
                break
            lastMsgId = raw_messages[0].get("id")
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
    """Find or create contact and conversation by phone/inbox, then send the message in that thread."""
    phone_no = _sanitize_phone(to)
    ctx = _get_chatwoot_ctx()
    if ctx is None:
        frappe.throw("Chatwoot is not configured for this user. Check Carrum chatwoot credentials.")

    contact_info = get_or_create_contact(phone_no, ctx)
    conversation_id = get_or_create_conversation(
        contact_id=contact_info["contact_id"],
        inbox_id=contact_info["inbox_id"],
        source_id=contact_info["source_id"],
        ctx=ctx,
        initial_message=None,
        phone_number=phone_no,
    )
    conversations = get_conversation(contact_info["contact_id"], ctx)
    conv = next((c for c in conversations if c.get("id") == conversation_id), None)
    if conv is not None and conv.get("can_reply") is False:
        frappe.throw("Send a template message to resume the conversation")

    msg_response = send_message(conversation_id, message, ctx)

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
    ctx = _get_chatwoot_ctx()
    if ctx is None:
        frappe.throw("Chatwoot is not configured for this user. Check Carrum chatwoot credentials.")
    template_info = get_template_info(template, ctx)
    body_variable_indices = template_info["body_variable_indices"]
    body_params = {str(i): DUMMY_TEMPLATE_VAL for i in body_variable_indices}
    content = _fill_template_body(template_info.get("body_text", ""), body_params)

    phone_no = _sanitize_phone(to)
    contact_info = get_or_create_contact(phone_no, ctx)
    conversation_id = get_or_create_conversation(
        contact_id=contact_info["contact_id"],
        inbox_id=contact_info["inbox_id"],
        source_id=contact_info["source_id"],
        initial_message=None,
        ctx=ctx,
        phone_number=phone_no,
    )
    msg_response = send_template_message(
        conversation_id=conversation_id,
        template_name=template,
        content=content,
        body_params=body_params,
        category=str(template_info.get("category", "UTILITY")),
        language=str(template_info.get("language", "en")),
        ctx=ctx,
    )
    return {
        "contact_id": contact_info["contact_id"],
        "inbox_id": contact_info["inbox_id"],
        "source_id": contact_info["source_id"],
        "conversation_id": conversation_id,
        "message": msg_response,
    }

def _format_chat_list_timestamp(value) -> str:
    """Normalize Chatwoot ISO / unix timestamps to 'YYYY-MM-DD HH:MM:SS' (UTC)."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    if isinstance(value, str) and value.strip():
        try:
            s = value.strip().replace("Z", "+00:00")
            d = datetime.fromisoformat(s)
            if d.tzinfo:
                d = d.astimezone(timezone.utc).replace(tzinfo=None)
            return d.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return ""
    return ""


def _conversation_to_chat_list_row(conv: dict) -> dict:
    """
    Map Chatwoot conversation (payload item) to CRM chat list row for WaChatListModal.
    """
    meta = conv.get("meta") or {}
    sender = meta.get("sender") or {}
    name = (sender.get("name") or "").strip() or (sender.get("phone_number") or "Unknown")
    phone = sender.get("phone_number") or ""

    last_msg = conv.get("last_non_activity_message") or {}
    if not last_msg:
        msgs = conv.get("messages") or []
        last_msg = msgs[0] if msgs else {}

    last_text = (
        last_msg.get("processed_message_content")
        or last_msg.get("content")
        or ""
    )
    unread = int(conv.get("unread_count") or 0)
    # No unread messages for the agent → treat as "read" for list UI
    is_read = unread == 0

    last_at = _format_chat_list_timestamp(last_msg.get("updated_at"))
    if not last_at:
        last_at = _format_chat_list_timestamp(last_msg.get("created_at"))
    if not last_at:
        last_at = _format_chat_list_timestamp(conv.get("last_activity_at"))

    thumb = (sender.get("thumbnail") or "").strip()
    avatar_url = thumb if thumb else None

    row = {
        "conversation_id": conv.get("id"),
        "inbox_id": conv.get("inbox_id"),
        "name": name,
        "phone_number": phone,
        "last_message": last_text,
        "last_message_at": last_at,
        "unread_count": unread,
        "is_read": is_read,
        "muted": bool(conv.get("muted")),
    }
    if avatar_url:
        row["avatar_url"] = avatar_url

    lead_name = _find_lead_name_by_phone(phone)
    if lead_name:
        row["reference_doctype"] = LEAD_DOCTYPE
        row["reference_name"] = lead_name

    return row


def _find_lead_name_by_phone(phone: str) -> str | None:
    """Best-effort CRM Lead name for opening the lead from the chat list."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) < 10:
        return None
    # Match common stored formats: with/without +91
    variants = {phone, digits, f"+{digits}", f"+91{digits[-10:]}", digits[-10:]}
    for v in variants:
        if not v:
            continue
        name = frappe.db.get_value(LEAD_DOCTYPE, {"mobile_no": v}, "name")
        if name:
            return name
    return None


def _normalize_phone_numbers_arg(phoneNumbers) -> list[str]:
    """Coerce whitelist arg to a list of non-empty phone strings (JSON list string supported)."""
    if phoneNumbers is None:
        return []
    raw = phoneNumbers
    if isinstance(phoneNumbers, str):
        s = phoneNumbers.strip()
        if not s:
            return []
        try:
            parsed = frappe.parse_json(s)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            raw = parsed
        else:
            return [s]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for p in raw:
        if p is None:
            continue
        t = str(p).strip()
        if t:
            out.append(t)
    return out


def _parse_conversation_list_body(body: dict) -> list[dict]:
    inner = body.get("data")
    if isinstance(inner, dict):
        return inner.get("payload") or []
    if isinstance(inner, list):
        return inner
    return []


def _fetch_conversations_by_phone_filter(ctx: dict, phones: list[str]) -> list[dict]:
    """
    POST /accounts/:id/conversations/filter — filter by contact phone_number (OR across values).
    https://developers.chatwoot.com/api-reference/conversations/conversations-filter
    Phone strings must match how Chatwoot stores them or no rows will match.
    """
    if not phones:
        return []

    payload_filters: list[dict] = []
    payload_filters.append({
        "attribute_key": "phoneNumber",
        "filter_operator": "contains",
        "values": phones,
    })

    print(payload_filters)
    base = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations/filter"
    merged: list[dict] = []
    seen_ids: set = set()
    max_pages = 50
    print(ctx["headers"])
    for page in range(1, max_pages + 1):
        url = f"{base}?{urlencode({'page': page})}"
        print("URL: " + url)
        resp = requests.post(
            url,
            headers=ctx["headers"],
            json={"payload": payload_filters},
        )
        print("RESPONSE: " + str(resp.json()))
        resp.raise_for_status()
        # batch = _parse_conversation_list_body(resp.json())
        batch = resp.json().get("payload") or []
        print("BATCH: " + str(batch))
        if not batch:
            break
        for c in batch:
            cid = c.get("id")
            if cid is not None and cid not in seen_ids:
                seen_ids.add(cid)
                merged.append(c)

    return merged


@frappe.whitelist()
def get_chat_list(searchKey=None, phoneNumbers=None):
    search_val = None
    if searchKey is not None:
        s = str(searchKey).strip()
        if s:
            search_val = s

    phones = _normalize_phone_numbers_arg(phoneNumbers)
    used_phone_filter = bool(phones)

    ctx = _get_chatwoot_ctx()
    if ctx is None:
        frappe.throw("Chatwoot is not configured for this user. Check Carrum chatwoot credentials.")

    raw_list = get_conversations(ctx, search=search_val, phone_numbers=phones)
    print("RAW LIST: " + str(raw_list))
    inbox_id = ctx.get("inbox_id")
    if inbox_id is not None and inbox_id != "":
        try:
            want_inbox = int(str(inbox_id).strip())
            raw_list = [
                c
                for c in raw_list
                if str(c.get("inbox_id")).strip() == str(want_inbox)
            ]
        except (TypeError, ValueError):
            pass

    rows = []
    for c in raw_list:
        if not c.get("id"):
            continue
        rows.append(_conversation_to_chat_list_row(c))

    if search_val and used_phone_filter:
        sk = search_val.lower()
        rows = [
            r
            for r in rows
            if sk in (r.get("name") or "").lower()
            or sk in (r.get("phone_number") or "").lower()
            or sk in (r.get("last_message") or "").lower()
        ]

    rows.sort(key=lambda r: r.get("last_message_at") or "", reverse=True)

    return {"count": len(rows), "data": rows}

# UTILITY FUNCTIONS
def _sanitize_phone(phoneNo: str):
    if len(phoneNo) == 10:
        return phoneNo
    if len(phoneNo) < 10:
        raise Exception("Invalid Phone number Length")
    return phoneNo.replace("+91", "")

def _contact_whatsapp_source_id(contact: dict) -> str | None:
    """Phone / identifier Chatwoot uses as WhatsApp source_id (E.164 or channel-specific)."""
    for key in ("phone_number", "identifier"):
        val = contact.get(key)
        if val is not None and str(val).strip():
            return str(val).strip().replace("+", "")
    return None


def assign_whatsapp_inbox_to_contact(contact_id: int, ctx: dict, source_id: str) -> dict:
    """
    POST /accounts/:account_id/contacts/:id/contact_inboxes — link the site WhatsApp inbox
    to an existing contact. source_id is the customer's WhatsApp identifier (contact number).
    Returns a contact_inbox-shaped dict (nested inbox + source_id) for _parse_contact_info.
    """
    inbox_id = ctx.get("inbox_id")
    if inbox_id is None or inbox_id == "":
        frappe.throw("WhatsApp inbox_id is missing in Chatwoot context.")
    try:
        inbox_id_int = int(inbox_id)
    except (TypeError, ValueError):
        frappe.throw("Invalid inbox_id in Chatwoot context.")

    source = str(source_id).strip()
    if not source:
        frappe.throw("source_id (contact number) is required to assign WhatsApp inbox.")

    url = (
        f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/contacts/"
        f"{contact_id}/contact_inboxes"
    )
    print("assign inbox to contact url: " + url)
    payload = {"inbox_id": inbox_id_int, "source_id": source}
    print("payload: " + str(payload))
    resp = requests.post(
        url,
        headers=ctx["headers"],
        json=payload
    )
    resp.raise_for_status()
    raw = resp.json()
    ci = raw.get("payload", raw)
    if isinstance(ci, list):
        ci = ci[0] if ci else {}
    if not isinstance(ci, dict):
        frappe.throw("Unexpected Chatwoot response when assigning contact inbox.")

    if not ci.get("inbox"):
        iid = ci.get("inbox_id", inbox_id_int)
        ci = {**ci, "inbox": {"id": iid}}
    if not ci.get("source_id"):
        ci = {**ci, "source_id": source}
    return ci


def _parse_contact_info(contact: dict, ctx: dict) -> dict:
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
        sid = _contact_whatsapp_source_id(contact)
        if not sid:
            raise Exception(
                "Contact has no phone_number/identifier; cannot assign WhatsApp inbox"
            )
        whatsapp_inbox = assign_whatsapp_inbox_to_contact(contact_id, ctx, sid)
    return {
        "contact_id": contact_id,
        "inbox_id": whatsapp_inbox["inbox"]["id"],
        "source_id": whatsapp_inbox["source_id"],
    }


def find_contact(phone_no: str, ctx: dict) -> dict:
    """
    Search contact by phone; do not create. Returns contact_id, inbox_id, source_id
    for the WhatsApp channel. Raises if not found or multiple matches.
    """
    search_url = (
        f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/contacts/search?page=1&q={phone_no}"
    )
    resp = requests.get(
        search_url,
        headers={"api_access_token": ctx["api_access_token"]},
    )
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload") or []
    print("CONTACT SEARCH PAYLOAD: " + str(payload), file=sys.stderr, flush=True)
    if len(payload) == 0:
        raise Exception(f"Contact not found for phone {phone_no}")
    # if len(payload) > 1:
    #     raise Exception("Multiple contacts found for this phone number")
    return _parse_contact_info(payload[0], ctx)


def get_or_create_contact(phone_no: str, ctx: dict) -> dict:
    """
    Search contact by phone; create if not found. Returns contact_id, inbox_id, source_id
    for the WhatsApp channel.
    """
    try:
        return find_contact(phone_no, ctx)
    except Exception as e:
        if "Contact not found" not in str(e):
            raise
    create_url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/contacts"
    create_resp = requests.post(
        create_url,
        headers=ctx["headers"],
        json={
            "inbox_id": ctx["inbox_id"],
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
    return _parse_contact_info(contact_obj, ctx)


def get_conversation(contact_id: int, ctx: dict) -> list | None:
    """
    List contact conversations; return the first open/pending one for this inbox.
    Returns conversations list if found, else None.
    """
    list_url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/contacts/{contact_id}/conversations"
    resp = requests.get(list_url, headers=ctx["headers"])
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
    ctx: dict,
    initial_message: str | None = None,
    phone_number: str | None = None,
) -> int:
    """Create a new conversation for the contact in the given inbox. Returns conversation_id."""
    create_url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations"
    body = {"source_id": source_id, "inbox_id": inbox_id, "contact_id": contact_id}
    pn = (phone_number or "").strip() or (source_id or "").strip()
    if pn:
        body["custom_attributes"] = {"phoneNumber": pn}
    if initial_message:
        body["message"] = {"content": initial_message}
    conv_resp = requests.post(create_url, headers=ctx["headers"], json=body)
    conv_resp.raise_for_status()
    conv_data = conv_resp.json()
    return conv_data["id"]


def find_conversation(contact_id: int, inbox_id: int, ctx: dict) -> dict:
    """
    Find an existing conversation for this contact in the given inbox.
    Returns the conversation dict (includes id, can_reply, etc.). Raises if no conversation found.
    """
    conversations = get_conversation(contact_id, ctx)
    by_inbox = [c for c in conversations if c.get("inbox_id") == inbox_id]
    if not by_inbox:
        raise Exception("No conversation found for this contact in the given inbox")
    return by_inbox[0]


def get_or_create_conversation(
    contact_id: int,
    inbox_id: int,
    source_id: str,
    ctx: dict,
    initial_message: str | None = None,
    phone_number: str | None = None,
) -> int:
    """
    Get existing open/pending conversation for this contact+inbox, or create one.
    On create, sets conversation custom_attributes.phoneNumber when phone_number (or source_id) is set.
    Returns conversation_id.
    """
    conversations = get_conversation(contact_id, ctx)
    by_inbox = [c for c in conversations if c.get("inbox_id") == inbox_id]
    if by_inbox:
        return by_inbox[0]["id"]
    return create_conversation(
        contact_id,
        inbox_id,
        source_id,
        ctx,
        initial_message,
        phone_number=phone_number,
    )


def send_message(conversation_id: int, content: str, ctx: dict) -> dict:
    """Send a message in an existing conversation. Returns the message response."""
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations/{conversation_id}/messages"
    resp = requests.post(url, headers=ctx["headers"], json={"content": content})
    resp.raise_for_status()
    return resp.json()


def send_template_message(
    conversation_id: int,
    template_name: str,
    content: str,
    ctx: dict,
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
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations/{conversation_id}/messages"
    resp = requests.post(url, headers=ctx["headers"], json=payload)
    resp.raise_for_status()
    return resp.json()


def get_messages(
    conversation_id: int,
    ctx: dict,
    before_msg_id: str | int | None = None,
) -> list[dict]:
    """
    List all messages of a conversation.
    API: GET /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages
    https://developers.chatwoot.com/api-reference/messages/get-messages
    Returns the payload array of message objects.
    """
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations/{conversation_id}/messages"
    if before_msg_id:
        url = f"{url}?before={before_msg_id}"
    resp = requests.get(url, headers=ctx["headers"])
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


def get_template_info(template_name: str, ctx: dict, inbox_id: int | None = None) -> dict:
    """
    Fetch template by name from Chatwoot inbox API. Validates template exists and
    returns body variable indices (from BODY component {{1}}, {{2}}, etc.).
    Returns dict with keys: name, body_variable_indices, language, category.
    Raises if template not found.
    """
    inbox_id = inbox_id if inbox_id is not None else ctx["inbox_id"]
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/inboxes/{inbox_id}"
    resp = requests.get(url, headers=ctx["headers"])
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

def get_conversations(
    ctx: dict,
    search: str | None = None,
    phone_numbers: list[str] | None = None,
) -> list[dict]:
    """
    List conversations: either POST filter by phone_number (when phone_numbers non-empty)
    or GET /conversations with optional q= and inbox_id=.
    """
    phones = phone_numbers if phone_numbers is not None else []
    print("PHONES")
    print(phones)
    print("PHONES")
    if phones:
        return _fetch_conversations_by_phone_filter(ctx, phones)

    base_url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations"
    query_parts: list[tuple[str, str]] = []
    if search:
        query_parts.append(("q", search))
    inbox_id = ctx.get("inbox_id")
    if inbox_id is not None and inbox_id != "":
        query_parts.append(("inbox_id", str(inbox_id).strip()))

    url = f"{base_url}?{urlencode(query_parts)}" if query_parts else base_url
    resp = requests.get(url, headers=ctx["headers"])
    resp.raise_for_status()
    return _parse_conversation_list_body(resp.json())