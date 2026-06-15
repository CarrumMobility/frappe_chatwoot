import os
import random
import re
import sys
import mimetypes
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse, urlencode

from core.api import carrum_accounts
from core.services.apihit_service import api_hit_service
import frappe
import requests
from frappe import _

log = frappe.logger("frappe_chatwoot:api_whatsapp")

LEAD_DOCTYPE = "CRM Lead"
DEAL_DOCTYPE = "CRM Deal"
FRAPPE_CHATWOOT_MESSAGE_TYPE_MAPPING = {1: "Outgoing", 2: "Incoming"}


def _chatwoot_response_body_for_log(response: requests.Response | None):
    if response is None:
        return None
    try:
        return response.json()
    except (ValueError, TypeError):
        return response.text


def _chatwoot_request_headers_for_log(response: requests.Response | None) -> dict | None:
    """Request headers as sent (from requests prepared request)."""
    if response is None or not getattr(response, "request", None):
        return None
    try:
        return dict(response.request.headers)
    except (TypeError, ValueError):
        return None


def _chatwoot_created_by_user() -> str | None:
    user = None
    if getattr(frappe.local, "session", None):
        user = frappe.session.get("user")
    return user if user and user not in (None, "Guest") else None


def _chatwoot_request_payload_for_log(kwargs: dict, explicit_payload=None):
    if explicit_payload is not None:
        return explicit_payload
    payload = {}
    if "params" in kwargs:
        payload["params"] = kwargs.get("params")
    if "json" in kwargs:
        payload["json"] = kwargs.get("json")
    if "data" in kwargs:
        payload["data"] = kwargs.get("data")
    if "files" in kwargs:
        files = kwargs.get("files")
        payload["files"] = list(files.keys()) if isinstance(files, dict) else str(type(files))
    return payload or None


def _chatwoot_api_request(
    method: str,
    url: str,
    *,
    api_operation: str = "request",
    request_payload=None,
    **kwargs,
) -> requests.Response:
    t0 = time.perf_counter()
    response = None
    err_message = None
    try:
        response = requests.request(method=method.upper(), url=url, **kwargs)
        if not response.ok:
            err_message = "HTTP not OK"
        return response
    except Exception as ex:
        err_message = str(ex)
        raise
    finally:
        try:
            api_hit_service.enqueue_log_api_hit(
                f"Chatwoot:{api_operation}",
                str(url),
                _chatwoot_request_headers_for_log(response),
                _chatwoot_request_payload_for_log(kwargs, request_payload),
                _chatwoot_response_body_for_log(response),
                int(response.status_code) if response is not None else 0,
                err_message,
                round(time.perf_counter() - t0, 4),
                created_by=_chatwoot_created_by_user(),
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Chatwoot api_hit enqueue")


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
    failure_reason = msg.get("content_attributes")
    failure_reason = failure_reason and isinstance(failure_reason, dict) and failure_reason.get("external_error") or None
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
        "failure_reason": failure_reason
    }


def _empty_whatsapp_messages_payload(can_reply: bool = False) -> dict:
    return {
        "messages": [],
        "can_reply": bool(can_reply),
        "conversation_id": None,
    }


def _get_crm_ref_doc_for_whatsapp_thread(reference_doctype: str, reference_name: str):
    if reference_doctype == DEAL_DOCTYPE:
        return frappe.get_doc(DEAL_DOCTYPE, reference_name)
    return frappe.get_doc(LEAD_DOCTYPE, reference_name)


def _conversation_unread_count(conv: dict) -> int:
    """Chatwoot may expose unread as ``unread_count`` or (rarely) ``unread`` on the root or in ``meta``."""
    if not conv or not isinstance(conv, dict):
        return 0
    for key in ("unread_count", "unread"):
        v = conv.get(key)
        if v is not None:
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                pass
    meta = conv.get("meta")
    if isinstance(meta, dict):
        for key in ("unread_count", "unread"):
            v = meta.get(key)
            if v is not None:
                try:
                    return max(0, int(v))
                except (TypeError, ValueError):
                    pass
    return 0


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

    try:
        ref_doc = _get_crm_ref_doc_for_whatsapp_thread(
            str(reference_doctype or ""), str(reference_name or "")
        )
    except Exception as e:
        log.error(f"Error getting CRM reference document for WhatsApp thread: {e}")
        return _empty_whatsapp_messages_payload(False)

    lead_phone_number = (ref_doc.get("mobile_no") or "").strip() if ref_doc else ""

    if not lead_phone_number:
        return _empty_whatsapp_messages_payload(False)

    contactInfo = get_or_create_contact(lead_phone_number, ctx)
    conversations = get_conversation(contactInfo["contact_id"], ctx)

    if not conversations or len(conversations) == 0:
        return _empty_whatsapp_messages_payload(False)

    conversations = _conversations_for_inbox(conversations, ctx["inbox_id"])
    if not conversations:
        return _empty_whatsapp_messages_payload(False)

    primary_conv = conversations[0]
    can_reply = _conversation_can_reply(primary_conv)

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
    return {
        "messages": data,
        "can_reply": bool(can_reply),
        "conversation_id": conversation_id,
    }


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

    conversations = get_conversation(contact_info["contact_id"], ctx) or []
    conv = _find_conversation_by_id(conversations, conversation_id)
    can_reply = _conversation_can_reply(conv)
    if conv is not None and not can_reply:
        frappe.throw(_("Send a template message to resume the conversation"))

    msg_response = send_message(conversation_id, message, ctx, attach=(attach or "").strip() or None)

    return {
        "contact_id": contact_info["contact_id"],
        "inbox_id": contact_info["inbox_id"],
        "source_id": contact_info["source_id"],
        "conversation_id": conversation_id,
        "can_reply": can_reply,
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
    all_var_indices = template_info.get("all_variable_indices") or template_info.get(
        "body_variable_indices", []
    )
    t_raw = template_info.get("raw_template") or {}

    if body_params is None:
        body_params_dict = {str(i): DUMMY_TEMPLATE_VAL for i in all_var_indices}
    else:
        if not isinstance(body_params, dict):
            frappe.throw(_("body_params must be an object mapping variable index to text"))
        body_params_dict = {}
        for i in all_var_indices:
            key = str(i)
            raw_val = body_params.get(key)
            if raw_val is None and i in body_params:
                raw_val = body_params.get(i)
            if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
                frappe.throw(_("Please provide a value for template variable {0}").format(key))
            body_params_dict[key] = str(raw_val).strip()

    content = _fill_template_body(template_info.get("body_text", ""), body_params_dict)
    processed = _build_processed_params_for_chatwoot(t_raw, body_params_dict)
    if not processed and body_params_dict:
        processed = {"body": body_params_dict}
    if processed == {} and not all_var_indices:
        processed = None  # use legacy { body: body_params } in send_template_message

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
        processed_params_override=processed,
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
    unread = _conversation_unread_count(conv)
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
        row["reference_docname"] = lead_name

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

    base = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations/filter"
    merged: list[dict] = []
    seen_ids: set = set()
    max_pages = 50
    
    for page in range(1, max_pages + 1):
        url = f"{base}?{urlencode({'page': page})}"
        resp = _chatwoot_api_request(
            "POST",
            url,
            api_operation="conversations_filter",
            headers=ctx["headers"],
            json={"payload": payload_filters},
        )
        resp.raise_for_status()
        # batch = _parse_conversation_list_body(resp.json())
        batch = resp.json().get("payload") or []
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

def _normalize_chatwoot_whatsapp_source_id(value: str | int | None) -> str:
    """
    Chatwoot Channel::Whatsapp validates source_id with WHATSAPP_CHANNEL_REGEX (digits only, length 1–15).
    Strip +, spaces, dashes, and any non-ASCII-digit characters so API accepts the value.
    """
    if value is None:
        return ""
    s = re.sub(r"[^0-9]", "", str(value).strip().replace("+", ""))
    if len(s) > 15:
        frappe.throw(
            "WhatsApp source_id must be 1–15 digits after normalization (E.164 without +). "
            f"Got {len(s)} digit(s)."
        )
    return s


def _contact_whatsapp_source_id(contact: dict) -> str | None:
    """Phone / identifier Chatwoot uses as WhatsApp source_id (E.164 digits only, no +)."""
    for key in ("phone_number", "identifier"):
        val = contact.get(key)
        if val is not None and str(val).strip():
            normalized = _normalize_chatwoot_whatsapp_source_id(val)
            if normalized:
                return normalized
    return None


def _contact_phone_matches_whatsapp_digits(contact: dict, digits: str) -> bool:
    """True if Chatwoot contact phone_number or identifier normalizes to the same WhatsApp digits."""
    if not digits:
        return False
    pn = _normalize_chatwoot_whatsapp_source_id(contact.get("phone_number"))
    ident = _normalize_chatwoot_whatsapp_source_id(contact.get("identifier"))
    return pn == digits or ident == digits


def _chatwoot_get_contact(contact_id: int, ctx: dict) -> dict:
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/contacts/{contact_id}"
    resp = _chatwoot_api_request(
        "GET",
        url,
        api_operation="get_contact",
        headers=ctx["headers"],
    )
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload", data)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        frappe.throw("Unexpected Chatwoot response when loading contact.")
    return payload


def _chatwoot_patch_contact_phone(contact_id: int, ctx: dict, digits: str) -> None:
    """Set phone_number (+E.164) so Chatwoot's ContactInboxBuilder can derive WhatsApp source_id."""
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/contacts/{contact_id}"
    body = {"phone_number": f"+{digits}"}
    resp = _chatwoot_api_request(
        "PATCH",
        url,
        api_operation="patch_contact_phone",
        headers=ctx["headers"],
        json=body,
        request_payload=body,
    )
    resp.raise_for_status()


def assign_whatsapp_inbox_to_contact(contact_id: int, ctx: dict, source_id: str) -> dict:
    """
    POST /accounts/:account_id/contacts/:id/contact_inboxes — link the site WhatsApp inbox
    to an existing contact.

    Chatwoot accepts optional ``source_id``; when omitted, ``ContactInboxBuilder`` sets it from
    ``contact.phone_number`` (digits without ``+``). Sending ``source_id`` in the JSON body can
    still hit validation edge cases; we therefore align ``phone_number`` with the intended WhatsApp
    identifier, then POST only ``inbox_id`` so the server generates a valid ``source_id``.
    """
    inbox_id = ctx.get("inbox_id")
    if inbox_id is None or inbox_id == "":
        frappe.throw("WhatsApp inbox_id is missing in Chatwoot context.")
    try:
        inbox_id_int = int(inbox_id)
    except (TypeError, ValueError):
        frappe.throw("Invalid inbox_id in Chatwoot context.")

    source = _normalize_chatwoot_whatsapp_source_id(source_id)
    if not source:
        frappe.throw("source_id (contact number) is required to assign WhatsApp inbox.")

    cw_contact = _chatwoot_get_contact(contact_id, ctx)
    if not _contact_phone_matches_whatsapp_digits(cw_contact, source):
        _chatwoot_patch_contact_phone(contact_id, ctx, source)

    url = (
        f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/contacts/"
        f"{contact_id}/contact_inboxes"
    )
    payload = {"inbox_id": inbox_id_int}
    resp = _chatwoot_api_request(
        "POST",
        url,
        api_operation="assign_contact_inbox",
        headers=ctx["headers"],
        json=payload,
        request_payload=payload,
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
    resp = _chatwoot_api_request(
        "GET",
        search_url,
        api_operation="search_contact",
        headers={"api_access_token": ctx["api_access_token"]},
    )
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload") or []
    if len(payload) == 0:
        raise Exception(f"Contact not found for phone {phone_no}")
    # if len(payload) > 1:
    #     raise Exception("Multiple contacts found for this phone number")
    return _parse_contact_info(payload[0], ctx)


def get_or_create_contact(
    phone_no: str, ctx: dict, contact_name: str | None = None
) -> dict:
    """
    Search contact by phone; create if not found. Returns contact_id, inbox_id, source_id
    for the WhatsApp channel.

    ``contact_name`` is used as the Chatwoot contact's display name when a new contact is
    created. Existing contacts are not renamed. Falls back to ``phone_no`` when not
    provided so we never POST an empty name.
    """
    try:
        return find_contact(phone_no, ctx)
    except Exception as e:
        if "Contact not found" not in str(e):
            raise
    display_name = (str(contact_name).strip() if contact_name is not None else "") or phone_no
    create_url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/contacts"
    create_resp = _chatwoot_api_request(
        "POST",
        create_url,
        api_operation="create_contact",
        headers=ctx["headers"],
        json={
            "inbox_id": ctx["inbox_id"],
            "phone_number": f"+91{phone_no}",
            "name": display_name,
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
    resp = _chatwoot_api_request(
        "GET",
        url,
        api_operation="search_contact",
        headers=ctx["headers"],
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("payload", data)


def _sort_conversations_newest_first(conversations: list) -> list:
    return sorted(conversations or [], key=lambda x: x.get("id", 0), reverse=True)


def _conversations_for_inbox(conversations: list, inbox_id) -> list:
    """Filter by inbox and return newest conversation first (highest id)."""
    filtered = [c for c in (conversations or []) if c.get("inbox_id") == inbox_id]
    return _sort_conversations_newest_first(filtered)


def _conversation_can_reply(conversation: dict | None) -> bool:
    if not conversation:
        return True
    value = conversation.get("can_reply")
    if value is None:
        return True
    return bool(value)


def _find_conversation_by_id(conversations: list, conversation_id: int) -> dict | None:
    try:
        want = int(conversation_id)
    except (TypeError, ValueError):
        return None
    for conv in conversations or []:
        if conv.get("id") == want:
            return conv
    return None


def get_conversation(contact_id: int, ctx: dict) -> list | None:
    """
    List contact conversations; return the first open/pending one for this inbox.
    Returns conversations list if found, else None.
    """
    list_url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/contacts/{contact_id}/conversations"
    resp = _chatwoot_api_request(
        "GET",
        list_url,
        api_operation="list_contact_conversations",
        headers=ctx["headers"],
    )
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
    # pn = (phone_number or "").strip() or (source_id or "").strip()
    # if pn:
        # body["custom_attributes"] = {"phoneNumber": pn}
    if initial_message:
        body["message"] = {"content": initial_message}
    conv_resp = _chatwoot_api_request(
        "POST",
        create_url,
        api_operation="create_conversation",
        headers=ctx["headers"],
        json=body,
    )
    conv_resp.raise_for_status()
    conv_data = conv_resp.json()
    return conv_data["id"]


def find_conversation(contact_id: int, inbox_id: int, ctx: dict) -> dict:
    """
    Find an existing conversation for this contact in the given inbox.
    Returns the conversation dict (includes id, can_reply, etc.). Raises if no conversation found.
    """
    conversations = get_conversation(contact_id, ctx)
    by_inbox = _conversations_for_inbox(conversations, inbox_id)
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
    by_inbox = _conversations_for_inbox(conversations, inbox_id)

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
        resp = _chatwoot_api_request(
            "POST",
            url,
            api_operation="send_message",
            headers=headers,
            data=data,
            files=files,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()

    resp = _chatwoot_api_request(
        "POST",
        url,
        api_operation="send_message",
        headers=ctx["headers"],
        json={"content": text},
    )
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
    processed_params_override: dict | None = None,
) -> dict:
    """
    Send a WhatsApp template message in an existing conversation.
    body_params: map of variable index to value (legacy, body only).
    If ``processed_params_override`` is set (header, body, footer, buttons for Chatwoot
    `processed_params`), that structure is used instead of body-only.
    """
    if processed_params_override is not None:
        proc = processed_params_override
    else:
        proc = {"body": body_params or {}}
    payload = {
        "content": content,
        "template_params": {
            "name": template_name,
            "category": category,
            "language": language,
            "processed_params": proc,
        },
    }
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations/{conversation_id}/messages"
    resp = _chatwoot_api_request(
        "POST",
        url,
        api_operation="send_template_message",
        headers=ctx["headers"],
        json=payload,
    )
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
    resp = _chatwoot_api_request(
        "GET",
        url,
        api_operation="get_messages",
        headers=ctx["headers"],
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("payload") or []


DUMMY_TEMPLATE_VAL = "TEST_VAL"


def _parse_body_variable_indices(body_text: str) -> list[int]:
    """Extract WhatsApp variable indices from any component text/URL (e.g. {{1}}, {{2}}). Sorted unique."""
    if not body_text:
        return []
    matches = re.findall(r"\{\{(\d+)\}\}", str(body_text))
    return sorted(set(int(m) for m in matches))


def _iter_template_component_strings(t: dict) -> list[tuple[str, str, str | None]]:
    """
    (component_key, free_text, note_for_hints)
    component_key: "header" | "body" | "footer" | "button_url" | "button"
    For buttons: note is button label, text is the URL to scan for {{n}}
    """
    out: list[tuple[str, str, str | None]] = []
    for comp in t.get("components") or []:
        cty = (comp.get("type") or "").upper()
        cfmt = (comp.get("format") or "TEXT").upper()
        if cty == "HEADER" and cfmt in ("", "TEXT") and (comp.get("text") is not None):
            out.append(("header", str(comp.get("text") or ""), None))
        elif cty == "HEADER":
            continue
        elif cty == "BODY":
            out.append(("body", str(comp.get("text") or ""), None))
        elif cty == "FOOTER":
            out.append(("footer", str(comp.get("text") or ""), None))
        elif cty == "BUTTONS":
            for btn in comp.get("buttons") or []:
                btxt = str((btn or {}).get("text") or "Button")
                btype = str((btn or {}).get("type") or "").upper()
                url = (btn or {}).get("url") or ""
                if btype == "URL" and url:
                    out.append(
                        (
                            "button_url",
                            str(url),
                            f"Button “{btxt}” (link)" if btxt else "Button link",
                        )
                    )
    return out


def enrich_message_template(t: dict) -> dict:
    """
    One Chatwoot / Meta template → row for CRM “WhatsApp Templates” list and UI.
    Includes all positional {{n}} in header, body, footer, and button URLs.
    """
    from collections import defaultdict

    if not t or not isinstance(t, dict):
        return {
            "name": "",
            "template": "",
            "footer": "",
            "header": "",
            "all_variable_indices": [],
            "variable_hints": {},
            "button_preview": [],
        }

    header_text, body_text, footer_text = "", "", ""
    for comp in t.get("components") or []:
        cty = (comp.get("type") or "").upper()
        cfmt = (comp.get("format") or "TEXT").upper()
        if cty == "HEADER" and cfmt in ("", "TEXT") and (comp.get("text") is not None):
            header_text = str(comp.get("text") or "")
        elif cty == "BODY":
            body_text = str(comp.get("text") or "")
        elif cty == "FOOTER":
            footer_text = str(comp.get("text") or "")

    refs: defaultdict = defaultdict(list)
    for k, s, label in _iter_template_component_strings(t):
        for m in re.findall(r"\{\{(\d+)\}\}", s or ""):
            idx = int(m)
            if k == "header":
                refs[idx].append("Header")
            elif k == "body":
                refs[idx].append("Body")
            elif k == "footer":
                refs[idx].append("Footer")
            elif k == "button_url" and label:
                refs[idx].append(str(label))
            else:
                refs[idx].append("Button URL")

    all_s = f"{header_text} {body_text} {footer_text} "
    for k, s, _lab in _iter_template_component_strings(t):
        if k == "button_url":
            all_s += s
    if not refs:
        for m in re.findall(r"\{\{(\d+)\}\}", all_s):
            refs[int(m)].append("Template")

    all_idx = sorted(set(refs.keys())) if refs else _parse_body_variable_indices(all_s)

    hints: dict = {}
    for i in all_idx:
        hlist = list(dict.fromkeys(refs.get(i) or [f"Variable {i}"]))
        hints[str(i)] = ", ".join(hlist) if hlist else str(i)

    # Small UI list: URL buttons with a dynamic part
    button_preview = []
    for comp in t.get("components") or []:
        if (comp.get("type") or "").upper() == "BUTTONS":
            for btn in comp.get("buttons") or []:
                btxt = str((btn or {}).get("text") or "")
                btype = str((btn or {}).get("type") or "").upper()
                url = (btn or {}).get("url") or ""
                if btype == "URL" and url and re.search(r"\{\{(\d+)\}\}", str(url)):
                    button_preview.append(
                        {
                            "text": btxt,
                            "url": str(url)[:200],
                            "has_variables": bool(re.search(r"\{\{(\d+)\}\}", str(url))),
                        }
                    )
                elif btype == "URL" and url:
                    button_preview.append(
                        {
                            "text": btxt,
                            "url": str(url)[:200],
                            "has_variables": False,
                        }
                    )
                elif btype == "QUICK_REPLY":
                    button_preview.append(
                        {
                            "text": btxt,
                            "type": "QUICK_REPLY",
                        }
                    )

    lang = t.get("language")
    if isinstance(lang, dict):
        language = str(lang.get("code") or lang.get("id") or "en")
    elif lang:
        language = str(lang)
    else:
        language = "en"

    return {
        "name": t.get("name", ""),
        "id": t.get("id", ""),
        "status": t.get("status", ""),
        "template": body_text,
        "footer": footer_text,
        "header": header_text,
        "category": t.get("category", ""),
        "language": language,
        "all_variable_indices": all_idx,
        "variable_hints": hints,
        "button_preview": button_preview,
    }


def _build_processed_params_for_chatwoot(t: dict, user_values: dict) -> dict:
    """
    User fills one field per unique {{n}}. Map to Chatwoot `processed_params`:
    body / header / footer (dicts with string keys "1".."n") and buttons: [{ "type": "url", "parameter": "…" }].
    """
    if not t or not isinstance(t, dict):
        return {}
    out = {}

    header_text, body_text, footer_text = "", "", ""
    for comp in t.get("components") or []:
        cty = (comp.get("type") or "").upper()
        cfmt = (comp.get("format") or "TEXT").upper()
        if cty == "HEADER" and cfmt in ("", "TEXT") and (comp.get("text") is not None):
            header_text = str(comp.get("text") or "")
        elif cty == "BODY":
            body_text = str(comp.get("text") or "")
        elif cty == "FOOTER":
            footer_text = str(comp.get("text") or "")

    h_idx = _parse_body_variable_indices(header_text)
    if h_idx:
        out["header"] = {str(i): (user_values.get(str(i)) or "").strip() for i in sorted(h_idx)}

    b_idx = _parse_body_variable_indices(body_text)
    if b_idx:
        out["body"] = {str(i): (user_values.get(str(i)) or "").strip() for i in sorted(b_idx)}

    f_idx = _parse_body_variable_indices(footer_text)
    if f_idx:
        out["footer"] = {str(i): (user_values.get(str(i)) or "").strip() for i in sorted(f_idx)}

    button_rows: list[dict] = []
    for comp in t.get("components") or []:
        if (comp.get("type") or "").upper() != "BUTTONS":
            continue
        for btn in comp.get("buttons") or []:
            btype = str((btn or {}).get("type") or "").upper()
            url = (btn or {}).get("url") or ""
            if btype == "URL" and url and _parse_body_variable_indices(str(url)):
                u_idx = _parse_body_variable_indices(str(url))
                if not u_idx:
                    continue
                if len(u_idx) == 1:
                    p = (user_values.get(str(u_idx[0])) or "").strip()
                else:
                    p = " ".join(
                        (user_values.get(str(x)) or "").strip() for x in sorted(u_idx) if (user_values.get(str(x)) or "").strip()
                    )
                button_rows.append({"type": "url", "parameter": p or ""})
    if button_rows:
        out["buttons"] = button_rows
    return out


def _fill_template_body(body_text: str, body_params: dict) -> str:
    """Replace {{1}}, {{2}}, ... in body text with values from body_params."""
    result = body_text
    for key, value in body_params.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


def get_template_info(template_name: str, ctx: dict, inbox_id: int | None = None) -> dict:
    """
    Fetch template by name from Chatwoot inbox API. Validates template exists and
    returns all positional variable indices (header, body, footer, button URL {{n}}), plus raw template.
    """
    inbox_id = inbox_id if inbox_id is not None else ctx["inbox_id"]
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/inboxes/{inbox_id}"
    resp = _chatwoot_api_request(
        "GET",
        url,
        api_operation="get_inbox_templates",
        headers=ctx["headers"],
    )
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload")
    if isinstance(payload, dict):
        message_templates = payload.get("message_templates") or []
    else:
        message_templates = data.get("message_templates") or []

    for t in message_templates:
        if t.get("name") == template_name:
            enriched = enrich_message_template(t)
            body_text = enriched.get("template") or ""
            language = t.get("language", {}).get("code", "en") if isinstance(t.get("language"), dict) else t.get("language") or "en"
            category = t.get("category", "UTILITY")
            body_variable_indices = _parse_body_variable_indices(body_text)
            return {
                "name": template_name,
                "raw_template": t,
                "body_text": body_text,
                "body_variable_indices": body_variable_indices,
                "all_variable_indices": enriched.get("all_variable_indices") or body_variable_indices,
                "variable_hints": enriched.get("variable_hints") or {},
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
    resp = _chatwoot_api_request(
        "GET",
        url,
        api_operation="list_conversations",
        headers=ctx["headers"],
    )
    resp.raise_for_status()
    return _parse_conversation_list_body(resp.json())

def assignSelfToContactOnChatwootIfHaveAccount(
    phone_no,
    username: str | None = None,
    contact_name: str | None = None,
):
    """Assign a Chatwoot agent to the WhatsApp conversation for ``phone_no``.

    ``username`` selects whose Chatwoot credentials/agent_id are used (defaults to
    ``frappe.session.user``). ``contact_name`` is used as the Chatwoot contact display
    name when the contact is created (existing contacts are not renamed); falls back to
    ``phone_no`` when omitted.

    Best-effort: creates the Chatwoot contact and/or conversation when missing, and skips
    the assignment POST when the agent is already the assignee. Returns
    ``{"success": bool, "message"?: str}`` and never raises so callers (e.g. the call
    dispose flow / CRM Lead controller hook) can treat it as fire-and-forget. Errors
    from upstream Chatwoot calls are caught and logged to the Frappe Error Log.
    """
    if phone_no is None or str(phone_no).strip() == "":
        return {"success": False, "message": "phone_no is required"}
    phone = str(phone_no).strip()

    target_user = (username or "").strip() or None
    ctx = _get_chatwoot_ctx(target_user)
    if ctx is None:
        return {
            "success": False,
            "message": "Chatwoot is not configured for this user. Check Carrum chatwoot credentials.",
        }

    try:
        want_agent_id = int(ctx.get("agent_id"))
    except (TypeError, ValueError):
        return {
            "success": False,
            "message": "Agent ID is not configured for this user. Check Carrum chatwoot credentials.",
        }

    try:
        contact_info = get_or_create_contact(phone, ctx, contact_name=contact_name)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "chatwoot_get_or_create_contact")
        return {"success": False, "message": f"Failed to resolve Chatwoot contact: {e}"}

    contact_id = contact_info.get("contact_id")
    inbox_id = contact_info.get("inbox_id") or ctx.get("inbox_id")
    source_id = contact_info.get("source_id") or phone
    if not contact_id or not inbox_id:
        return {
            "success": False,
            "message": "Chatwoot contact resolution returned incomplete data",
        }

    try:
        conversation_id = get_or_create_conversation(
            contact_id=contact_id,
            inbox_id=inbox_id,
            source_id=source_id,
            ctx=ctx,
            phone_number=phone,
        )
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "chatwoot_get_or_create_conversation")
        return {
            "success": False,
            "message": f"Failed to resolve Chatwoot conversation: {e}",
        }

    # Skip assignment POST when the conversation is already owned by this agent.
    try:
        existing = get_conversation(contact_id, ctx) or []
        current = next(
            (c for c in existing if c.get("id") == conversation_id),
            None,
        )
    except Exception:
        current = None
    already = _conversation_assignee_user_id(current)
    if already is not None and already == want_agent_id:
        return {"success": True, "message": "Already assigned"}

    try:
        assign_agent_to_conversation(
            conversation_id=conversation_id,
            agent_id=want_agent_id,
            ctx=ctx,
        )
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "chatwoot_assign_agent_to_conversation")
        return {"success": False, "message": f"Failed to assign agent: {e}"}

    return {"success": True}


def assign_agent_to_conversation(conversation_id: int, agent_id: int, ctx: dict):
    # https://chatwoot-dev.carrum.co.in/api/v1/accounts/1/conversations/2/assignments
    url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations/{conversation_id}/assignments"
    resp = _chatwoot_api_request(
        "POST",
        url,
        api_operation="assign_conversation",
        headers=ctx["headers"],
        json={"assignee_id": agent_id},
    )
    resp.raise_for_status()
    return resp.json()


def assign_chatwoot_conversation_to_frappe_user(frappe_username: str, conversation_id: int) -> bool:
    """Assign a Chatwoot conversation to the agent linked to this Frappe user (Carrum chatwootCred).

    Uses that user's API token to POST ``assignments`` with their ``agentId``. Safe for guest
    webhooks (no session); failures are logged and do not raise.
    """
    if not frappe_username or not str(frappe_username).strip() or not conversation_id:
        return False
    ctx = _get_chatwoot_ctx(str(frappe_username).strip())
    if ctx is None:
        return False
    try:
        agent_id = int(ctx.get("agent_id"))
    except (TypeError, ValueError):
        return False
    try:
        assign_agent_to_conversation(conversation_id, agent_id, ctx)
        return True
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "assign_chatwoot_conversation_to_frappe_user",
        )
        return False


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
    return _get_my_conversations(searchKey, page)

def _get_my_conversations(searchKey: str | int | None = None, page: int | str | None = 1):
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
        resp = _chatwoot_api_request(
            "GET",
            url,
            api_operation="list_my_conversations",
            headers=ctx["headers"],
            params=params,
            timeout=60,
        )
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


@frappe.whitelist()
def update_last_seen_at(conversation_id: int | str | None = None):
    if conversation_id is None or str(conversation_id).strip() == "":
        return {"success": False, "message": _("Missing conversation_id")}
    try:
        conv_id = int(str(conversation_id).strip())
    except (TypeError, ValueError):
        return {"success": False, "message": _("Invalid conversation_id")}

    ctx = _get_chatwoot_ctx()
    if ctx is None:
        return {
            "success": False,
            "message": "Chatwoot is not configured for this user. Check Carrum chatwoot credentials.",
        }
    try:
        url = f"{ctx['base_url']}/api/v1/accounts/{ctx['account_id']}/conversations/{conv_id}/update_last_seen"
        resp = _chatwoot_api_request(
            "POST",
            url,
            api_operation="update_last_seen",
            headers=ctx["headers"],
            timeout=30,
        )
        resp.raise_for_status()
        payload = None
        if resp.text and str(resp.text).strip():
            try:
                payload = resp.json()
            except Exception:
                payload = None
        return {
            "success": True,
            "is_valid": True,
            "reason": None,
            "data": {},
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Failed to update last seen at: {e}",
        }