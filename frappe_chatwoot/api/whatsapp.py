import os
import random
import re
import sys
import mimetypes
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse, urlencode

from core.api import carrum_accounts
import frappe
import requests
from frappe import _

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
    agentId = cfg.agentId
    return {
        "api_access_token": token,
        "inbox_id": inbox_id,
        "account_id": account_id,
        "base_url": base_url,
        "agent_id": agentId,
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


def _chatwoot_api_message_to_crm_whatsapp_row(
    msg: dict,
    contact_info: dict,
    reference_doctype: str,
    reference_name: str,
) -> dict | None:
    """Map one Chatwoot REST message to the shape expected by CRM WhatsAppArea.vue."""
    sender = msg.get("sender")
    if not sender:
        return None

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
            creation_str = (
                frappe.utils.format_datetime(dt, "yyyy-MM-dd HH:mm:ss") if dt else None
            )
        except Exception:
            creation_str = None

    raw_body = msg.get("processed_message_content")
    if raw_body is None:
        raw_body = msg.get("content")
    body = "" if raw_body is None else str(raw_body)

    attachments = msg.get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []
    if not attachments and isinstance(msg.get("attachment"), dict):
        attachments = [msg["attachment"]]

    attach_url = msg.get("attachment")
    if isinstance(attach_url, dict):
        attach_url = (
            attach_url.get("data_url")
            or attach_url.get("thumb_url")
            or attach_url.get("media_url")
        )
    elif isinstance(attach_url, str):
        attach_url = attach_url.strip() or None
    else:
        attach_url = None

    content_type = str(msg.get("content_type") or "text").lower()
    attach_file_name = ""

    if attachments:
        a0 = attachments[0] if isinstance(attachments[0], dict) else {}
        ft = str(a0.get("file_type") or "").lower()
        attach_file_name = str(a0.get("file_name") or "").strip()
        if ft in ("image", "gif", "sticker"):
            content_type = "image"
            attach_url = (
                a0.get("data_url")
                or a0.get("thumb_url")
                or a0.get("media_url")
                or attach_url
            )
        elif ft == "video":
            content_type = "video"
            attach_url = (
                a0.get("data_url")
                or a0.get("media_url")
                or a0.get("thumb_url")
                or attach_url
            )
        elif ft == "audio":
            content_type = "audio"
            attach_url = (
                a0.get("data_url")
                or a0.get("media_url")
                or attach_url
            )
        else:
            content_type = "document"
            attach_url = (
                a0.get("data_url")
                or a0.get("media_url")
                or attach_url
            )

    template_val = msg.get("template")
    message_type_ui = "Template" if template_val else "Manual"

    has_display = bool(body.strip()) or bool(attach_url) or bool(template_val)
    if not has_display:
        return None

    return {
        "name": str(msg.get("id")),
        "type": msg_type,
        "to": None,
        "from": contact_info.get("source_id"),
        "content_type": content_type,
        "message_type": message_type_ui,
        "attach": attach_url,
        "attach_file_name": attach_file_name,
        "template": template_val,
        "use_template": msg.get("use_template"),
        "message_id": msg.get("source_id") or f"msg_{random.randint(100000, 999999)}",
        "is_reply": msg.get("is_reply") or 0,
        "reply_to_message_id": msg.get("reply_to_message_id"),
        "creation": creation_str,
        "message": body,
        "status": msg.get("status"),
        "reference_doctype": reference_doctype,
        "reference_name": reference_name,
        "template_parameters": msg.get("template_parameters"),
        "template_header_parameters": msg.get("template_header_parameters"),
        "from_name": from_name or "Administrator",
    }


def _empty_whatsapp_messages_payload(can_reply: bool = False) -> dict:
    return {"messages": [], "can_reply": bool(can_reply)}


@frappe.whitelist()
def get_whatsapp_messages(reference_doctype: str, reference_name: str):
    """
    Return WhatsApp thread messages for a lead plus Chatwoot ``can_reply``.

    ``can_reply`` comes from the contact's conversation payload (see Chatwoot
    contact conversations API). When false, only template messages can reopen the
    session — mirror that in the desk composer.
    """
    ctx = _get_chatwoot_ctx()
    if ctx is None:
        return _empty_whatsapp_messages_payload(False)
    leadData = frappe.get_doc(LEAD_DOCTYPE, reference_name)
    
    leadPhoneNumber = leadData.get("mobile_no")
    if not leadPhoneNumber:
        return _empty_whatsapp_messages_payload(False)

    contactInfo = get_or_create_contact(leadPhoneNumber, ctx)
    conversations = get_conversation(contactInfo["contact_id"], ctx)
    if not conversations or len(conversations) == 0:
        return _empty_whatsapp_messages_payload(False)

    conversations = [c for c in conversations if c.get("inbox_id") == ctx["inbox_id"]]
    if not conversations:
        return _empty_whatsapp_messages_payload(False)

    primary_conv = conversations[0]
    can_reply = primary_conv.get("can_reply")
    if can_reply is None:
        can_reply = True

    all_messages = []
    lastMsgId = None
    conversation_id = primary_conv.get("id")
    if conversation_id:
        while True:
            raw_messages = get_messages(conversation_id, ctx, before_msg_id=lastMsgId)
            if len(raw_messages) == 0:
                break
            lastMsgId = raw_messages[0].get("id")
            all_messages.extend(raw_messages)

    data = []
    for msg in all_messages:
        row = _chatwoot_api_message_to_crm_whatsapp_row(
            msg, contactInfo, reference_doctype, reference_name
        )
        if row:
            data.append(row)

    data.sort(
        key=lambda r: (r.get("creation") or "", int(r["name"]) if str(r.get("name") or "").isdigit() else 0)
    )
    return {"messages": data, "can_reply": bool(can_reply)}


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

    msg_response = send_message(conversation_id, message, ctx, attach=(attach or "").strip() or None)

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
def send_whatsapp_template(
    reference_doctype: str,
    reference_name: str,
    template: str,
    to: str,
    body_params=None,
):
    """
    Validate template via inbox API, find or create contact and conversation,
    then send a WhatsApp template message.

    If ``body_params`` is omitted, body placeholders {{1}}, {{2}}, … are filled with
    TEST_VAL (legacy behaviour). If ``body_params`` is provided (dict), each required
    index must have a non-empty string value, e.g. ``{"1": "Ann", "2": "3pm"}``.
    """
    ctx = _get_chatwoot_ctx()
    if ctx is None:
        frappe.throw("Chatwoot is not configured for this user. Check Carrum chatwoot credentials.")

    if isinstance(body_params, str) and body_params.strip():
        body_params = frappe.parse_json(body_params)
    elif body_params == "":
        body_params = None

    template_info = get_template_info(template, ctx)
    body_variable_indices = template_info["body_variable_indices"]

    if body_params is None:
        body_params_dict = {str(i): DUMMY_TEMPLATE_VAL for i in body_variable_indices}
    else:
        if not isinstance(body_params, dict):
            frappe.throw(_("body_params must be an object mapping variable index to text"))
        body_params_dict = {}
        for i in body_variable_indices:
            key = str(i)
            raw_val = body_params.get(key)
            if raw_val is None and i in body_params:
                raw_val = body_params.get(i)
            if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
                frappe.throw(_("Please provide a value for template variable {0}").format(key))
            body_params_dict[key] = str(raw_val).strip()

    content = _fill_template_body(template_info.get("body_text", ""), body_params_dict)

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
        body_params=body_params_dict,
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

def _chatwoot_message_list_meta(msg: dict | None) -> dict:
    """
    Build chat-list preview text and optional media thumbnail from a Chatwoot message payload.

    Uses the same [image] / [video] / [document] / [audio] prefixes as WaChatListModal.vue.
    """
    out: dict = {"preview": "", "thumb_url": None}
    if not msg or not isinstance(msg, dict):
        return out

    from frappe.utils import strip_html

    raw_content = msg.get("processed_message_content")
    if raw_content is None:
        raw_content = msg.get("content")
    content = ""
    if raw_content is not None and str(raw_content).strip():
        content = (
            strip_html(str(raw_content).strip()) or str(raw_content).strip()
        ).strip()

    attachments = msg.get("attachments")
    if not isinstance(attachments, list):
        attachments = []
    if not attachments and isinstance(msg.get("attachment"), dict):
        attachments = [msg["attachment"]]

    if not attachments:
        out["preview"] = content
        return out

    att0 = attachments[0] if isinstance(attachments[0], dict) else {}
    ft = str(att0.get("file_type") or "").lower()
    fn = (str(att0.get("file_name") or "").strip()) or (
        str(att0.get("extension") or "").strip()
    )

    thumb = None
    if ft in ("image", "gif", "sticker"):
        thumb = (
            (att0.get("thumb_url") or att0.get("data_url") or "").strip() or None
        )
    elif ft == "video":
        thumb = (str(att0.get("thumb_url") or "").strip() or None)

    if ft in ("image", "gif", "sticker"):
        tag = "[image]"
    elif ft == "video":
        tag = "[video]"
    elif ft == "audio":
        tag = "[audio]"
    else:
        tag = "[document]"

    if tag == "[document]":
        rest_parts = [p for p in (fn, content) if p]
        rest = " ".join(rest_parts)
        preview = f"{tag} {rest}".strip() if rest else tag
    elif content:
        preview = f"{tag} {content}"
    else:
        preview = tag

    out["preview"] = preview
    out["thumb_url"] = thumb
    return out


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

    list_meta = _chatwoot_message_list_meta(last_msg if last_msg else None)
    last_text = list_meta.get("preview") or ""
    last_thumb = list_meta.get("thumb_url")
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
    row["last_message_thumb"] = last_thumb

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
def get_chat_list_by_phoneNumbers(searchKey=None, phoneNumbers=None):
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

def get_contact(phone_no: str, ctx: dict) -> dict:
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/contacts/search?page=1&q={phone_no}"
    resp = requests.get(url, headers=ctx["headers"])
    resp.raise_for_status()
    data = resp.json()
    return data.get("payload", data)


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


def _conversation_assignee_user_id(conversation: dict | None) -> int | None:
    """Best-effort Chatwoot agent (user) id from a conversation payload; shapes differ by endpoint."""
    if not conversation:
        return None
    raw = conversation.get("assignee_id")
    if raw is None:
        meta = conversation.get("meta") or {}
        assignee = meta.get("assignee")
        if isinstance(assignee, dict):
            raw = assignee.get("id")
        else:
            assignee = conversation.get("assignee")
            if isinstance(assignee, dict):
                raw = assignee.get("id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


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


MAX_WHATSAPP_ATTACHMENT_BYTES = 40 * 1024 * 1024


def _attachment_fs_path_from_url_path(path: str) -> str | None:
    """Map /files/... or /private/files/... URL path to an on-disk site path."""
    path = unquote((path or "").split("?", 1)[0])
    if "/private/files/" in path:
        rel = path.split("/private/files/", 1)[1].lstrip("/")
        base = frappe.get_site_path("private", "files")
    elif "/files/" in path:
        rel = path.split("/files/", 1)[1].lstrip("/")
        base = frappe.get_site_path("public", "files")
    else:
        return None
    rel_norm = rel.replace("\\", "/")
    if ".." in rel_norm.split("/"):
        frappe.throw(_("Invalid attachment path"))
    full = os.path.normpath(os.path.join(base, *rel_norm.split("/")))
    base_norm = os.path.normpath(base)
    if full != base_norm and not full.startswith(base_norm + os.sep):
        frappe.throw(_("Invalid attachment path"))
    return full


def _read_bytes_from_filesystem(fs_path: str) -> tuple[bytes, str, str]:
    fname = os.path.basename(fs_path)
    with open(fs_path, "rb") as f:
        data = f.read()
    mime, _ = mimetypes.guess_type(fname)
    mime = mime or "application/octet-stream"
    return data, fname, mime


def _resolve_attachment_file(attach: str) -> tuple[bytes, str, str]:
    """
    Load attachment bytes for Chatwoot multipart upload.
    Prefers local site files for /files and /private/files URLs; otherwise HTTP GET.
    """
    raw = (attach or "").strip()
    if not raw:
        frappe.throw(_("No attachment URL provided"))
    if not raw.startswith(("http://", "https://", "/")) and raw.startswith(
        ("private/files/", "files/")
    ):
        raw = "/" + raw

    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https"):
        site_url = frappe.utils.get_url()
        site_netloc = urlparse(site_url).netloc
        path_part = unquote((parsed.path or "").split("?", 1)[0])
        if parsed.netloc == site_netloc and path_part:
            fs_path = _attachment_fs_path_from_url_path(path_part)
            if fs_path and os.path.isfile(fs_path):
                return _read_bytes_from_filesystem(fs_path)
        resp = requests.get(raw, timeout=90)
        resp.raise_for_status()
        fname = os.path.basename(path_part) or "attachment"
        mime = (resp.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
        data = resp.content
    else:
        fs_path = _attachment_fs_path_from_url_path(raw)
        if not fs_path or not os.path.isfile(fs_path):
            frappe.throw(_("Attachment file not found"))
        data, fname, mime = _read_bytes_from_filesystem(fs_path)

    if len(data) > MAX_WHATSAPP_ATTACHMENT_BYTES:
        frappe.throw(_("Attachment is too large to send via WhatsApp"))
    return data, fname, mime


def send_message(
    conversation_id: int,
    content: str,
    ctx: dict,
    attach: str | None = None,
) -> dict:
    """Send a text message and/or attachment in an existing conversation. Returns the message response."""
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations/{conversation_id}/messages"
    text = (content or "").strip()

    if attach:
        file_bytes, filename, mime = _resolve_attachment_file(attach)
        headers = {
            k: v
            for k, v in ctx["headers"].items()
            if k.lower() != "content-type"
        }
        data = {
            "content": text if text else " ",
            "message_type": "outgoing",
            "private": "false",
        }
        files = {"attachments[]": (filename, file_bytes, mime)}
        resp = requests.post(url, headers=headers, data=data, files=files, timeout=120)
        resp.raise_for_status()
        return resp.json()

    resp = requests.post(url, headers=ctx["headers"], json={"content": text})
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

def assignSelfToContactOnChatwootIfHaveAccount(phone_no):
    ctx = _get_chatwoot_ctx()
    if ctx is None:
        return {"success": False, "message": "Chatwoot is not configured for this user. Check Carrum chatwoot credentials."}
    
    agentId = ctx.get("agent_id")

    if agentId is None:
        return {"success": False, "message": "Agent ID is not configured for this user. Check Carrum chatwoot credentials."}
        
    contacts_info = get_contact(phone_no, ctx)

    if not contacts_info:
        return {'success': False, 'message': 'Contact not found'}

    contact_id = None
    if len(contacts_info) > 0 and contacts_info[0] is not None:
        contact_id = contacts_info[0].get("id")

    if contact_id is None:
        return {'success': False, 'message': 'Contact not found'}

    conversation = find_conversation(contact_id=contact_id, inbox_id=ctx.get('inbox_id'), ctx=ctx)

    if not conversation:
        return {'success': False, 'message': 'Conversation not found'}
        
    conversation_id = conversation.get("id")

    try:
        want_id = int(agentId)
    except (TypeError, ValueError):
        return {
            "success": False,
            "message": "Agent ID is not configured for this user. Check Carrum chatwoot credentials.",
        }

    already = _conversation_assignee_user_id(conversation)
    if already is not None and already == want_id:
        return {"success": True}

    assign_agent_to_conversation(conversation_id=conversation_id, agent_id=want_id, ctx=ctx)
    return {"success": True}


def assign_agent_to_conversation(conversation_id: int, agent_id: int, ctx: dict):
    # https://chatwoot-dev.carrum.co.in/api/v1/accounts/1/conversations/2/assignments
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations/{conversation_id}/assignments"
    resp = requests.post(url, headers=ctx["headers"], json={"assignee_id": agent_id})
    resp.raise_for_status()
    return resp.json()


def _conversation_page_size(meta: dict, payload_len: int) -> int:
    """Infer page size from Chatwoot list meta or payload length."""
    for key in ("per_page", "page_limit", "limit"):
        v = meta.get(key)
        if v is not None:
            try:
                n = int(v)
                if n > 0:
                    return n
            except (TypeError, ValueError):
                pass
    if payload_len > 0:
        return max(payload_len, 15)
    return 15


@frappe.whitelist()
def get_my_conversations(searchKey: str | int | None = None, page: int | str | None = 1):
    """
    List conversations assigned to the current agent (Chatwoot token user).

    GET .../conversations?assignee_type=me&status=open&page=1&q=...

    Returns::
        {
          "status": True,
          "data": {
            "haveMore": <bool>,
            "page": <int>,
            "conversations": [<raw Chatwoot conversation objects>],
            "rows": [<CRM chat list rows for WaChatListModal>],
          },
        }
    """
    ctx = _get_chatwoot_ctx()
    if ctx is None:
        return {
            "success": False,
            "message": "Chatwoot is not configured for this user. Check Carrum chatwoot credentials.",
        }

    try:
        page_i = int(page) if page is not None else 1
    except (TypeError, ValueError):
        page_i = 1
    if page_i < 1:
        page_i = 1

    params: dict[str, str | int] = {
        "assignee_type": "me",
        "page": page_i,
        "status": "open",
    }

    sk = str(searchKey).strip() if searchKey is not None else ""
    if sk:
        params["q"] = sk

    inbox_id = ctx.get("inbox_id")
    if inbox_id is not None and str(inbox_id).strip() != "":
        try:
            params["inbox_id"] = int(str(inbox_id).strip())
        except (TypeError, ValueError):
            params["inbox_id"] = str(inbox_id).strip()

    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations"
    try:
        resp = requests.get(url, headers=ctx["headers"], params=params, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"success": False, "message": str(e)}

    body = resp.json()
    payload = _parse_conversation_list_body(body)

    data_block = body.get("data")
    meta = data_block.get("meta") if isinstance(data_block, dict) else {}
    if not isinstance(meta, dict):
        meta = {}

    per_page = _conversation_page_size(meta, len(payload))
    have_more = len(payload) >= per_page

    for key in ("total_pages", "page_count"):
        total_pages = meta.get(key)
        cur = meta.get("current_page")
        if total_pages is not None and cur is not None:
            try:
                have_more = int(cur) < int(total_pages)
            except (TypeError, ValueError):
                pass
            break

    rows: list[dict] = []
    for c in payload:
        if not c.get("id"):
            continue
        rows.append(_conversation_to_chat_list_row(c))

    return {
        "status": True,
        "data": {
            "haveMore": have_more,
            "page": page_i,
            "conversations": payload,
            "rows": rows,
        },
    }