#!/usr/bin/env python3
"""
Generic IMAP/SMTP MCP Server for Claude.

Connects to any IMAP-compatible email provider (IONOS, Gmail, Outlook,
self-hosted, etc.) and exposes tools for reading, searching, sending,
and managing emails.

Environment variables:
  IMAP_HOST      — IMAP server hostname (e.g. imap.ionos.de)
  IMAP_PORT      — IMAP server port (default: 993)
  SMTP_HOST      — SMTP server hostname (e.g. smtp.ionos.de)
  SMTP_PORT      — SMTP server port (default: 587)
  TLS_CA_FILE    — PEM file with extra trusted certificate(s), e.g. a self-signed server cert
  TLS_VERIFY     — "true" (default); "false" disables certificate checks (insecure)
  EMAIL_ADDRESS  — Full email address for login
  EMAIL_PASSWORD — Password or app-specific password
  IMAP_READ_ONLY — "true" (default) hides send/move/delete; "false" enables them
"""

import base64
import os
import sys
import json
import email
import imaplib
import smtplib
import re
import ssl
from datetime import datetime, timedelta
from email import policy
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, parseaddr

# ---------------------------------------------------------------------------
# MCP Protocol helpers (minimal stdio implementation)
# ---------------------------------------------------------------------------

def read_message():
    """Read a JSON-RPC message from stdin (Content-Length framing)."""
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:  # EOF: client closed stdin
            return None
        if line == b"\r\n" or line == b"\n":
            break
        if b":" in line:
            key, value = line.decode().split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", 0))
    if length == 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode())


def send_message(msg):
    """Write a JSON-RPC message to stdout (Content-Length framing)."""
    body = json.dumps(msg)
    header = f"Content-Length: {len(body.encode())}\r\n\r\n"
    sys.stdout.buffer.write(header.encode() + body.encode())
    sys.stdout.buffer.flush()


def make_response(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------

def make_ssl_context():
    """TLS context for IMAP and SMTP, honouring TLS_CA_FILE and TLS_VERIFY."""
    ctx = ssl.create_default_context()
    ca_file = os.environ.get("TLS_CA_FILE", "")
    if ca_file:
        ctx.load_verify_locations(cafile=ca_file)
    if os.environ.get("TLS_VERIFY", "true").strip().lower() in ("false", "0", "no"):
        print("WARNING: TLS_VERIFY is off; server certificates are not checked.", file=sys.stderr)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get_imap_connection():
    host = os.environ.get("IMAP_HOST", "")
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ.get("EMAIL_ADDRESS", "")
    password = os.environ.get("EMAIL_PASSWORD", "")
    if not host or not user or not password:
        raise ValueError("Missing IMAP configuration. Set IMAP_HOST, EMAIL_ADDRESS, and EMAIL_PASSWORD.")
    conn = imaplib.IMAP4_SSL(host, port, ssl_context=make_ssl_context())
    conn.login(user, password)
    caps = get_capabilities(conn)
    if "UTF8=ACCEPT" in caps and "ENABLE" in caps:
        # imaplib checks the pre-login capabilities in enable(); refresh them first
        conn.capabilities = tuple(caps)
        conn.enable("UTF8=ACCEPT")
    return conn


def get_capabilities(conn):
    """Return the post-login capability set (it can differ from the pre-login one)."""
    status, data = conn.capability()
    if status != "OK" or not data or data[0] is None:
        return set()
    return set(data[0].decode().upper().split())


def imap_quote(value):
    """Return value as an IMAP quoted string, escaping backslashes and quotes."""
    value = str(value)
    if "\r" in value or "\n" in value:
        raise ValueError("IMAP strings must not contain line breaks.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_utf7_encode(name):
    """Encode a folder name as IMAP modified UTF-7 (RFC 3501, 5.1.3)."""
    out, pending = [], []

    def flush():
        if pending:
            b64 = base64.b64encode("".join(pending).encode("utf-16-be")).decode("ascii")
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(name):
    """Decode an IMAP modified UTF-7 folder name."""
    def repl(m):
        b64 = m.group(1).replace(",", "/")
        if not b64:
            return "&"
        return base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-16-be")
    return re.sub(r"&([A-Za-z0-9+,]*)-", repl, name)


def quote_mailbox(conn, name):
    """Quote a folder name for a command, in modified UTF-7 unless UTF8=ACCEPT is on."""
    if "\r" in name or "\n" in name:
        raise ValueError("IMAP strings must not contain line breaks.")
    return imap_quote(name if conn.utf8_enabled else imap_utf7_encode(name))


LIST_LINE = re.compile(r'^\((?P<flags>[^)]*)\) (?P<delim>NIL|"(?:[^"\\]|\\.)*") (?P<name>.*)$')


def parse_list_entry(conn, entry):
    """Parse one LIST response item into (flags, delimiter, name), or None."""
    if isinstance(entry, tuple):  # name sent as a literal
        line, name = entry[0].decode("utf-8", "replace"), entry[1].decode("utf-8", "replace")
        m = LIST_LINE.match(line)
    elif isinstance(entry, bytes):
        m = LIST_LINE.match(entry.decode("utf-8", "replace"))
        name = m.group("name") if m else None
    else:
        return None
    if not m:
        return None
    if name.startswith('"') and name.endswith('"'):
        name = re.sub(r"\\(.)", r"\1", name[1:-1])
    if not conn.utf8_enabled:
        name = imap_utf7_decode(name)
    delim = m.group("delim")
    delim = None if delim == "NIL" else re.sub(r"\\(.)", r"\1", delim[1:-1])
    return set(m.group("flags").split()), delim, name


def list_mailboxes(conn):
    """Return [(flags, delimiter, name)] for all folders."""
    status, data = conn.list()
    if status != "OK":
        raise RuntimeError(f"LIST failed: {data}")
    return [p for p in (parse_list_entry(conn, e) for e in data) if p]


TRASH_NAMES = ["trash", "deleted items", "deleted messages", "deleted", "papierkorb",
               "gelöschte elemente", "gelöschte objekte"]


def find_trash_folder(mailboxes):
    """Pick the trash folder: the SPECIAL-USE \\Trash flag, else a well-known name."""
    for flags, _, name in mailboxes:
        if "\\trash" in {f.lower() for f in flags}:
            return name
    # Compare whole names (last path segment), not substrings: "Trash-Archive" is no trash
    by_leaf = {}
    for _, delim, name in mailboxes:
        leaf = name.rsplit(delim, 1)[-1] if delim else name
        by_leaf.setdefault(leaf.lower(), name)
    for trash_name in TRASH_NAMES:
        if trash_name in by_leaf:
            return by_leaf[trash_name]
    return None


SEARCH_KEYS = {"subject": "SUBJECT", "from": "FROM", "to": "TO", "since": "SINCE", "before": "BEFORE"}


def parse_search_query(query):
    """Split a query like 'from:max subject:"weekly report" budget' into (KEY, value) terms.

    Free text (outside key:value pairs) becomes one ("TEXT", phrase) term,
    matched against subject or body.
    """
    terms = []
    free = []
    for m in re.finditer(r'(\w+):(?:"([^"]*)"|(\S+))|"([^"]*)"|(\S+)', query):
        key, quoted_val, val, quoted_free, _ = m.groups()
        if key and key.lower() in SEARCH_KEYS:
            terms.append((SEARCH_KEYS[key.lower()], quoted_val if quoted_val is not None else val))
        else:
            free.append(quoted_free if quoted_free is not None else m.group(0))
    if free:
        terms.append(("TEXT", " ".join(free)))
    return terms


def search_criterion(key, value):
    """Render one (KEY, value) term as an IMAP SEARCH criterion."""
    if key in ("SINCE", "BEFORE"):
        d = datetime.strptime(value, "%Y-%m-%d")
        return f'{key} {d.strftime("%d-%b-%Y")}'
    if key == "TEXT":
        return f"OR SUBJECT {imap_quote(value)} BODY {imap_quote(value)}"
    return f"{key} {imap_quote(value)}"


def uid_search(conn, criteria, literal=None):
    """Run UID SEARCH; with literal, it is sent as the value of the last criterion."""
    if literal is None:
        status, data = conn.uid("SEARCH", f'({" ".join(criteria) or "ALL"})')
    else:
        conn.literal = literal.encode("utf-8")
        status, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *criteria)
    if status != "OK" or not data or data[0] is None:
        raise RuntimeError(f"Search failed: {data}")
    return set(data[0].split())


def search_uids(conn, terms):
    """Return the UIDs matching all terms, newest first."""
    if conn.utf8_enabled or all(value.isascii() for _, value in terms):
        uids = uid_search(conn, [search_criterion(key, value) for key, value in terms])
    else:
        # Without UTF8=ACCEPT, non-ASCII values must go as literals, and imaplib
        # sends only one (trailing) literal per command. Search once per
        # non-ASCII value, repeating the ASCII criteria, and intersect.
        base = [search_criterion(key, value) for key, value in terms if value.isascii()]
        uids = None
        for key, value in terms:
            if value.isascii():
                continue
            if key == "TEXT":
                found = uid_search(conn, base + ["SUBJECT"], value) | uid_search(conn, base + ["BODY"], value)
            else:
                found = uid_search(conn, base + [key], value)
            uids = found if uids is None else uids & found
    return sorted(uids, key=int, reverse=True)


def fetch_headers(conn, uids):
    """Fetch From/Subject/Date for all uids in one command; keeps the order of uids."""
    if not uids:
        return []
    uids = [u.decode() if isinstance(u, bytes) else str(u) for u in uids]
    status, data = conn.uid("FETCH", ",".join(uids), "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
    if status != "OK":
        raise RuntimeError(f"FETCH failed: {data}")
    headers = {}
    for item in data:
        if not isinstance(item, tuple):
            continue
        m = re.search(rb"UID (\d+)", item[0])
        if m:
            headers[m.group(1).decode()] = email.message_from_bytes(item[1], policy=policy.default)
    emails = []
    for uid in uids:
        msg = headers.get(uid)
        if msg is not None:
            emails.append({
                "id": uid,
                "from": decode_mime_header(msg.get("From", "")),
                "subject": decode_mime_header(msg.get("Subject", "(no subject)")),
                "date": decode_mime_header(msg.get("Date", "")),
            })
    return emails


def decode_mime_header(raw):
    """Decode a MIME-encoded header value to a plain string."""
    if raw is None:
        return ""
    parts = decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def get_text_from_message(msg):
    """Extract plain-text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        # Fallback to HTML if no plain text
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/html" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace")
                # Minimal HTML strip
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text).strip()
                return text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def list_attachments(msg):
    """Return a list of attachment filenames."""
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                fname = part.get_filename()
                if fname:
                    attachments.append(decode_mime_header(fname))
    return attachments


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_list_folders():
    """List all mailbox folders."""
    conn = get_imap_connection()
    try:
        return {"folders": [name for _, _, name in list_mailboxes(conn)]}
    finally:
        conn.logout()


def tool_list_emails(folder="INBOX", limit=20, offset=0):
    """List emails in a folder, most recent first."""
    conn = get_imap_connection()
    try:
        status, _ = conn.select(quote_mailbox(conn, folder), readonly=True)
        if status != "OK":
            return {"error": f"Could not open folder: {folder}"}

        status, data = conn.uid("SEARCH", "ALL")
        if status != "OK":
            return {"emails": [], "total": 0}

        ids = data[0].split()
        total = len(ids)
        # Most recent first
        ids = list(reversed(ids))
        page = ids[offset : offset + limit]

        emails = fetch_headers(conn, page)

        return {"emails": emails, "total": total, "folder": folder, "offset": offset, "limit": limit}
    finally:
        conn.logout()


def tool_read_email(email_id, folder="INBOX"):
    """Read a single email by its ID."""
    conn = get_imap_connection()
    try:
        conn.select(quote_mailbox(conn, folder), readonly=True)
        status, msg_data = conn.uid("FETCH", str(email_id), "(RFC822)")
        if status != "OK" or msg_data[0] is None:
            return {"error": f"Email {email_id} not found in {folder}"}

        raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
        msg = email.message_from_bytes(raw, policy=policy.default)

        return {
            "id": str(email_id),
            "folder": folder,
            "from": decode_mime_header(msg.get("From", "")),
            "to": decode_mime_header(msg.get("To", "")),
            "cc": decode_mime_header(msg.get("Cc", "")),
            "subject": decode_mime_header(msg.get("Subject", "")),
            "date": decode_mime_header(msg.get("Date", "")),
            "body": get_text_from_message(msg),
            "attachments": list_attachments(msg),
        }
    finally:
        conn.logout()


def tool_search_emails(query, folder="INBOX", limit=20):
    """Search emails using IMAP search criteria.

    Supports: subject, from, to, since, before, body text.
    """
    conn = get_imap_connection()
    try:
        conn.select(quote_mailbox(conn, folder), readonly=True)

        criteria = [search_criterion(key, value) for key, value in parse_search_query(query)]
        search_str = " ".join(criteria) if criteria else "ALL"
        ids = search_uids(conn, parse_search_query(query))
        total = len(ids)
        ids = ids[:limit]

        emails = fetch_headers(conn, ids)

        return {"emails": emails, "total": total, "query": search_str}
    finally:
        conn.logout()


def tool_send_email(to, subject, body, cc="", bcc="", reply_to_id="", reply_to_folder="INBOX"):
    """Send an email via SMTP. Optionally reply to an existing message."""
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("EMAIL_ADDRESS", "")
    password = os.environ.get("EMAIL_PASSWORD", "")

    if not smtp_host or not user or not password:
        raise ValueError("Missing SMTP configuration. Set SMTP_HOST, EMAIL_ADDRESS, and EMAIL_PASSWORD.")

    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc

    # If replying, set In-Reply-To and References headers
    if reply_to_id:
        try:
            conn = get_imap_connection()
            conn.select(quote_mailbox(conn, reply_to_folder), readonly=True)
            status, msg_data = conn.uid("FETCH", str(reply_to_id), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES)])")
            if status == "OK" and msg_data[0] is not None:
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                orig = email.message_from_bytes(raw, policy=policy.default)
                message_id = orig.get("Message-ID", "")
                references = orig.get("References", "")
                if message_id:
                    msg["In-Reply-To"] = message_id
                    msg["References"] = f"{references} {message_id}".strip()
            conn.logout()
        except Exception:
            pass  # Send without reply headers if lookup fails

    msg.attach(MIMEText(body, "plain", "utf-8"))

    recipients = [addr.strip() for addr in to.split(",")]
    if cc:
        recipients += [addr.strip() for addr in cc.split(",")]
    if bcc:
        recipients += [addr.strip() for addr in bcc.split(",")]

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls(context=make_ssl_context())
        server.login(user, password)
        server.sendmail(user, recipients, msg.as_string())

    return {"status": "sent", "to": to, "subject": subject}


def tool_move_email(email_id, source_folder="INBOX", target_folder="Archive"):
    """Move an email to a different folder."""
    conn = get_imap_connection()
    try:
        status, _ = conn.select(quote_mailbox(conn, source_folder))
        if status != "OK":
            return {"error": f"Could not open folder: {source_folder}"}
        uid = str(email_id)
        caps = get_capabilities(conn)
        if "MOVE" in caps:
            status, _ = conn.uid("MOVE", uid, quote_mailbox(conn, target_folder))
            if status != "OK":
                return {"error": f"Failed to move email to {target_folder}"}
            return {"status": "moved", "email_id": uid, "from": source_folder, "to": target_folder}
        status, _ = conn.uid("COPY", uid, quote_mailbox(conn, target_folder))
        if status != "OK":
            return {"error": f"Failed to copy email to {target_folder}"}
        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        if "UIDPLUS" in caps:
            conn.uid("EXPUNGE", uid)
            return {"status": "moved", "email_id": uid, "from": source_folder, "to": target_folder}
        # No UIDPLUS: a plain EXPUNGE would also purge other \Deleted messages, so leave it flagged
        return {"status": "copied", "email_id": uid, "from": source_folder, "to": target_folder,
                "note": "Original flagged \\Deleted but not expunged (server lacks UIDPLUS)."}
    finally:
        conn.logout()


def tool_delete_email(email_id, folder="INBOX"):
    """Move an email to Trash."""
    conn = get_imap_connection()
    try:
        trash_folder = find_trash_folder(list_mailboxes(conn))
        conn.logout()

        if trash_folder and trash_folder != folder:
            return tool_move_email(email_id, folder, trash_folder)
        else:
            # Fallback: flag as deleted
            conn = get_imap_connection()
            try:
                status, _ = conn.select(quote_mailbox(conn, folder))
                if status != "OK":
                    return {"error": f"Could not open folder: {folder}"}
                uid = str(email_id)
                status, _ = conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                if status != "OK":
                    return {"error": f"Failed to flag email {uid}"}
                if "UIDPLUS" in get_capabilities(conn):
                    conn.uid("EXPUNGE", uid)
                    return {"status": "deleted", "email_id": uid, "folder": folder}
                return {"status": "flagged_deleted", "email_id": uid, "folder": folder,
                        "note": "Flagged \\Deleted but not expunged (server lacks UIDPLUS)."}
            finally:
                conn.logout()
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# MCP Tool dispatch
# ---------------------------------------------------------------------------

WRITE_TOOLS = {"send_email", "move_email", "delete_email"}


def is_read_only():
    return os.environ.get("IMAP_READ_ONLY", "true").strip().lower() not in ("false", "0", "no")


TOOLS = {
    "list_folders": {
        "description": "List all mailbox folders (INBOX, Sent, Drafts, etc.)",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    "list_emails": {
        "description": "List emails in a folder, most recent first. Use offset for pagination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Mailbox folder (default: INBOX)", "default": "INBOX"},
                "limit": {"type": "integer", "description": "Max emails to return (default: 20)", "default": 20},
                "offset": {"type": "integer", "description": "Skip this many emails (default: 0)", "default": 0},
            },
            "required": [],
        },
    },
    "read_email": {
        "description": "Read a single email's full content by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "The email UID from list_emails"},
                "folder": {"type": "string", "description": "Mailbox folder (default: INBOX)", "default": "INBOX"},
            },
            "required": ["email_id"],
        },
    },
    "search_emails": {
        "description": "Search emails. Use structured queries like 'from:name@example.com', 'subject:meeting', 'subject:\"weekly report\"', 'since:2025-01-01', 'before:2025-12-31', or plain text to search subject and body. Quote multi-word values; terms are combined with AND.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (supports from:, subject:, to:, since:, before:, or plain text)"},
                "folder": {"type": "string", "description": "Mailbox folder (default: INBOX)", "default": "INBOX"},
                "limit": {"type": "integer", "description": "Max results (default: 20)", "default": 20},
            },
            "required": ["query"],
        },
    },
    "send_email": {
        "description": "Send an email via SMTP. Can also reply to an existing email by providing reply_to_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address(es), comma-separated"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body (plain text)"},
                "cc": {"type": "string", "description": "CC recipients, comma-separated", "default": ""},
                "bcc": {"type": "string", "description": "BCC recipients, comma-separated", "default": ""},
                "reply_to_id": {"type": "string", "description": "Email ID to reply to (sets threading headers)", "default": ""},
                "reply_to_folder": {"type": "string", "description": "Folder of the email being replied to", "default": "INBOX"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    "move_email": {
        "description": "Move an email to a different folder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "The email ID"},
                "source_folder": {"type": "string", "description": "Current folder (default: INBOX)", "default": "INBOX"},
                "target_folder": {"type": "string", "description": "Destination folder"},
            },
            "required": ["email_id", "target_folder"],
        },
    },
    "delete_email": {
        "description": "Move an email to the Trash folder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "The email ID"},
                "folder": {"type": "string", "description": "Current folder (default: INBOX)", "default": "INBOX"},
            },
            "required": ["email_id"],
        },
    },
}


def dispatch_tool(name, arguments):
    """Call the appropriate tool function."""
    if name in WRITE_TOOLS and is_read_only():
        return {"error": f"{name} is disabled (IMAP_READ_ONLY is on)."}
    try:
        if name == "list_folders":
            return tool_list_folders()
        elif name == "list_emails":
            return tool_list_emails(
                folder=arguments.get("folder", "INBOX"),
                limit=arguments.get("limit", 20),
                offset=arguments.get("offset", 0),
            )
        elif name == "read_email":
            return tool_read_email(
                email_id=arguments["email_id"],
                folder=arguments.get("folder", "INBOX"),
            )
        elif name == "search_emails":
            return tool_search_emails(
                query=arguments["query"],
                folder=arguments.get("folder", "INBOX"),
                limit=arguments.get("limit", 20),
            )
        elif name == "send_email":
            return tool_send_email(
                to=arguments["to"],
                subject=arguments["subject"],
                body=arguments["body"],
                cc=arguments.get("cc", ""),
                bcc=arguments.get("bcc", ""),
                reply_to_id=arguments.get("reply_to_id", ""),
                reply_to_folder=arguments.get("reply_to_folder", "INBOX"),
            )
        elif name == "move_email":
            return tool_move_email(
                email_id=arguments["email_id"],
                source_folder=arguments.get("source_folder", "INBOX"),
                target_folder=arguments["target_folder"],
            )
        elif name == "delete_email":
            return tool_delete_email(
                email_id=arguments["email_id"],
                folder=arguments.get("folder", "INBOX"),
            )
        else:
            return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Main MCP server loop
# ---------------------------------------------------------------------------

def main():
    while True:
        msg = read_message()
        if msg is None:
            break

        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            send_message(make_response(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "imap-connector",
                    "version": "0.1.0",
                },
            }))

        elif method == "notifications/initialized":
            pass  # No response needed

        elif method == "tools/list":
            tool_list = [
                {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
                for name, spec in TOOLS.items()
                if not (name in WRITE_TOOLS and is_read_only())
            ]
            send_message(make_response(req_id, {"tools": tool_list}))

        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = dispatch_tool(tool_name, arguments)
            send_message(make_response(req_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
            }))

        elif method == "ping":
            send_message(make_response(req_id, {}))

        else:
            if req_id is not None:
                send_message(make_error(req_id, -32601, f"Method not found: {method}"))


if __name__ == "__main__":
    main()
