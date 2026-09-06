import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request, send_from_directory

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # Keep health/admin access available if deployment is misconfigured.
    WebPushException = None
    webpush = None


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "data" / "sms-center.db"))
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "sms-sb")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", DEVICE_TOKEN)
HEARTBEAT_SECONDS = max(15, int(os.getenv("HEARTBEAT_SECONDS", "60")))
OFFLINE_SECONDS = max(HEARTBEAT_SECONDS * 2, int(os.getenv("OFFLINE_SECONDS", "180")))
DEVICE_SYNC_SECONDS = max(60, int(os.getenv("DEVICE_SYNC_SECONDS", "120")))
MAX_SMS_LENGTH = 12000
MAX_OUTBOUND_SMS_LENGTH = 1000
MAX_SMS_TEMPLATE_LENGTH = 4000
OUTBOUND_STALE_SECONDS = max(
    300, int(os.getenv("OUTBOUND_STALE_SECONDS", "900"))
)
RETRY_DELAYS = (5, 15, 60, 300, 900)
MCP_TOKEN_PREFIX = "smsmcp_"
MCP_DEFAULT_EXPIRY_DAYS = 90
FEISHU_POLL_SECONDS = 10
WEB_PUSH_MAX_ATTEMPTS = 5
WEB_PUSH_RETRY_DELAYS = (5, 30, 120, 600, 1800)
WEB_PUSH_TTL_SECONDS = 3600
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY_PATH = Path(
    os.getenv(
        "VAPID_PRIVATE_KEY_PATH",
        str(DATABASE_PATH.parent / "vapid_private.pem"),
    )
)
VAPID_SUBJECT = os.getenv(
    "VAPID_SUBJECT", "mailto:admin@bytegallop.com"
).strip()
DEFAULT_SMS_TEMPLATE_NAME = "默认短信转发模板"
DEFAULT_SMS_TEMPLATE_BODY = (
    "短信转发\n"
    "接收卡：{{receiver}}\n"
    "原号码：{{ori}}\n"
    "时间：{{time}}\n"
    "内容：{{sms}}"
)
DEFAULT_CALL_TEMPLATE_BODY = "未接来电\n设备：{{device}}\n接收卡：{{receiver}}\n来电号码：{{ori}}\n入库时间：{{time}}"
SMS_TEMPLATE_PLACEHOLDERS = frozenset(
    {"ori", "sms", "receiver", "phone", "time", "device"}
)
SMS_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")
TEMPLATED_DESTINATION_KINDS = frozenset(
    {"sms_forward", "feishu_app", "feishu_webhook"}
)

app = Flask(__name__, static_folder="static")
app.json.ensure_ascii = False
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("sms-center")
_vapid_lock = threading.Lock()
_vapid_config = None


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db_connect():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    with db_connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                phone_number TEXT NOT NULL DEFAULT '',
                firmware TEXT NOT NULL DEFAULT '',
                app_version TEXT NOT NULL DEFAULT '',
                network TEXT NOT NULL DEFAULT '',
                signal INTEGER,
                queue_count INTEGER NOT NULL DEFAULT 0,
                traffic_total_bytes INTEGER NOT NULL DEFAULT 0,
                traffic_session_id TEXT NOT NULL DEFAULT '',
                traffic_session_bytes INTEGER NOT NULL DEFAULT 0,
                traffic_updated_at TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                last_ip TEXT NOT NULL DEFAULT '',
                status_message TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_key TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL DEFAULT 'sms',
                sender TEXT NOT NULL DEFAULT '',
                sms_time TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                received_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'stored'
            );

            CREATE TABLE IF NOT EXISTS destinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                config_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL DEFAULT '*',
                destination_id INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(device_id, destination_id)
            );

            CREATE TABLE IF NOT EXISTS channel_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channel_group_devices (
                group_id INTEGER NOT NULL REFERENCES channel_groups(id) ON DELETE CASCADE,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                PRIMARY KEY (group_id, device_id)
            );

            CREATE TABLE IF NOT EXISTS channel_group_destinations (
                group_id INTEGER NOT NULL REFERENCES channel_groups(id) ON DELETE CASCADE,
                destination_id INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
                PRIMARY KEY (group_id, destination_id)
            );

            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                destination_id INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                delivered_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                http_status INTEGER,
                response_excerpt TEXT NOT NULL DEFAULT '',
                feishu_message_id TEXT,
                feishu_chat_id TEXT,
                UNIQUE(message_id, destination_id)
            );

            CREATE TABLE IF NOT EXISTS outbound_sms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                source_delivery_id INTEGER REFERENCES deliveries(id) ON DELETE SET NULL,
                mcp_request_id TEXT UNIQUE,
                recipient TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                dispatched_at TEXT,
                dispatched_epoch REAL,
                completed_at TEXT,
                sent_at TEXT,
                last_error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS mcp_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                token_prefix TEXT NOT NULL,
                token_value TEXT,
                scopes TEXT NOT NULL DEFAULT 'read,send',
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                expires_at TEXT,
                revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sms_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feishu_sms_replies (
                event_id TEXT PRIMARY KEY,
                destination_id INTEGER NOT NULL
                    REFERENCES destinations(id) ON DELETE CASCADE,
                source_delivery_id INTEGER NOT NULL
                    REFERENCES deliveries(id) ON DELETE CASCADE,
                inbound_message_id TEXT NOT NULL UNIQUE,
                sender_open_id TEXT NOT NULL DEFAULT '',
                outbound_sms_id INTEGER NOT NULL UNIQUE
                    REFERENCES outbound_sms(id) ON DELETE CASCADE,
                notified_status TEXT NOT NULL DEFAULT '',
                notification_attempts INTEGER NOT NULL DEFAULT 0,
                next_notification_at REAL NOT NULL DEFAULT 0,
                last_notification_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feishu_reply_inbox (
                inbound_message_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                destination_id INTEGER NOT NULL
                    REFERENCES destinations(id) ON DELETE CASCADE,
                source_delivery_id INTEGER NOT NULL
                    REFERENCES deliveries(id) ON DELETE CASCADE,
                sender_open_id TEXT NOT NULL DEFAULT '',
                reply_body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                acknowledgement_attempts INTEGER NOT NULL DEFAULT 0,
                next_acknowledgement_at REAL NOT NULL DEFAULT 0,
                last_acknowledgement_error TEXT NOT NULL DEFAULT '',
                acknowledged_at TEXT,
                outbound_sms_id INTEGER UNIQUE
                    REFERENCES outbound_sms(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feishu_poll_seen (
                message_id TEXT PRIMARY KEY,
                destination_id INTEGER NOT NULL
                    REFERENCES destinations(id) ON DELETE CASCADE,
                create_time TEXT NOT NULL DEFAULT '',
                seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT NOT NULL DEFAULT '',
                notify_sms INTEGER NOT NULL DEFAULT 1,
                notify_missed_call INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_success_at TEXT,
                last_error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS web_push_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL
                    REFERENCES web_push_subscriptions(id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL
                    REFERENCES messages(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                sent_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                UNIQUE(subscription_id, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices(last_seen);
            CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at);
            CREATE INDEX IF NOT EXISTS idx_deliveries_due
                ON deliveries(status, next_attempt_at);
            CREATE INDEX IF NOT EXISTS idx_channel_group_devices_device
                ON channel_group_devices(device_id);
            CREATE INDEX IF NOT EXISTS idx_channel_group_destinations_destination
                ON channel_group_destinations(destination_id);
            CREATE INDEX IF NOT EXISTS idx_outbound_sms_device_status
                ON outbound_sms(device_id, status, id);
            CREATE INDEX IF NOT EXISTS idx_outbound_sms_status_dispatch
                ON outbound_sms(status, dispatched_epoch);
            CREATE INDEX IF NOT EXISTS idx_mcp_tokens_active
                ON mcp_tokens(token_hash, revoked_at, expires_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sms_templates_one_default
                ON sms_templates(is_default)
                WHERE is_default=1;
            CREATE INDEX IF NOT EXISTS idx_feishu_sms_replies_notification
                ON feishu_sms_replies(next_notification_at, notified_status);
            CREATE INDEX IF NOT EXISTS idx_feishu_reply_inbox_due
                ON feishu_reply_inbox(status, next_acknowledgement_at);
            CREATE INDEX IF NOT EXISTS idx_feishu_poll_seen_destination
                ON feishu_poll_seen(destination_id, create_time);
            CREATE INDEX IF NOT EXISTS idx_web_push_deliveries_due
                ON web_push_deliveries(status, next_attempt_at);
            """
        )
        db.execute(
            """
            UPDATE web_push_deliveries
            SET status='retry', next_attempt_at=0
            WHERE status='sending'
            """
        )
        outbound_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(outbound_sms)")
        }
        if "source_delivery_id" not in outbound_columns:
            db.execute(
                """
                ALTER TABLE outbound_sms
                ADD COLUMN source_delivery_id INTEGER
                    REFERENCES deliveries(id) ON DELETE SET NULL
                """
            )
        if "mcp_request_id" not in outbound_columns:
            db.execute(
                "ALTER TABLE outbound_sms ADD COLUMN mcp_request_id TEXT"
            )
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_sms_mcp_request
            ON outbound_sms(mcp_request_id)
            WHERE mcp_request_id IS NOT NULL
            """
        )
        device_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(devices)")
        }
        for field in ("sms_forward_enabled", "call_forward_enabled"):
            if field not in device_columns:
                db.execute(f"ALTER TABLE devices ADD COLUMN {field} INTEGER NOT NULL DEFAULT 1")
        db.execute("CREATE TABLE IF NOT EXISTS call_template (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO call_template(id, body) VALUES (1, ?)", (DEFAULT_CALL_TEMPLATE_BODY,))
        if "traffic_total_bytes" not in device_columns:
            db.execute(
                "ALTER TABLE devices ADD COLUMN traffic_total_bytes "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "traffic_session_id" not in device_columns:
            db.execute(
                "ALTER TABLE devices ADD COLUMN traffic_session_id "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "traffic_session_bytes" not in device_columns:
            db.execute(
                "ALTER TABLE devices ADD COLUMN traffic_session_bytes "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "traffic_updated_at" not in device_columns:
            db.execute(
                "ALTER TABLE devices ADD COLUMN traffic_updated_at "
                "TEXT NOT NULL DEFAULT ''"
            )
        mcp_token_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(mcp_tokens)")
        }
        if "token_value" not in mcp_token_columns:
            db.execute("ALTER TABLE mcp_tokens ADD COLUMN token_value TEXT")
        db.execute(
            "UPDATE mcp_tokens SET scopes='read,send' WHERE scopes<>'read,send'"
        )
        message_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(messages)")
        }
        if "event_type" not in message_columns:
            db.execute(
                """
                ALTER TABLE messages
                ADD COLUMN event_type TEXT NOT NULL DEFAULT 'sms'
                """
            )
        if "metadata_json" not in message_columns:
            db.execute(
                """
                ALTER TABLE messages
                ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'
                """
            )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_event_type
            ON messages(event_type, received_at)
            """
        )
        delivery_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(deliveries)")
        }
        if "feishu_message_id" not in delivery_columns:
            db.execute("ALTER TABLE deliveries ADD COLUMN feishu_message_id TEXT")
        if "feishu_chat_id" not in delivery_columns:
            db.execute("ALTER TABLE deliveries ADD COLUMN feishu_chat_id TEXT")
        for row in db.execute(
            """
            SELECT dl.id, dl.response_excerpt, d.config_json
            FROM deliveries dl
            JOIN destinations d ON d.id=dl.destination_id
            WHERE d.kind='feishu_app'
              AND dl.feishu_message_id IS NULL
              AND dl.response_excerpt<>''
            """
        ).fetchall():
            message_id, chat_id = feishu_response_reference(
                row["response_excerpt"]
            )
            if not chat_id:
                config = json.loads(row["config_json"] or "{}")
                if config.get("receive_id_type", "chat_id") == "chat_id":
                    chat_id = text_value(config.get("receive_id"), 160) or None
            if message_id:
                db.execute(
                    """
                    UPDATE deliveries
                    SET feishu_message_id=?, feishu_chat_id=?
                    WHERE id=?
                    """,
                    (message_id, chat_id, row["id"]),
                )
        now = utc_now()
        db.execute(
            """
            INSERT INTO sms_templates (
                name, body, is_default, created_at, updated_at
            )
            SELECT ?, ?, 1, ?, ?
            WHERE NOT EXISTS (SELECT 1 FROM sms_templates)
            """,
            (
                DEFAULT_SMS_TEMPLATE_NAME,
                DEFAULT_SMS_TEMPLATE_BODY,
                now,
                now,
            ),
        )
        if not db.execute(
            "SELECT 1 FROM sms_templates WHERE is_default=1"
        ).fetchone():
            db.execute(
                """
                UPDATE sms_templates
                SET is_default=1, updated_at=?
                WHERE id=(SELECT MIN(id) FROM sms_templates)
                """,
                (now,),
            )
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_sms_source_delivery
            ON outbound_sms(source_delivery_id)
            WHERE source_delivery_id IS NOT NULL
            """
        )
        db.execute(
            """
            UPDATE deliveries
            SET status='retry', next_attempt_at=0
            WHERE status='sending'
              AND NOT EXISTS (
                  SELECT 1
                  FROM outbound_sms o
                  WHERE o.source_delivery_id=deliveries.id
                    AND o.status IN ('pending','sending')
              )
            """
        )


def secure_equal(received, expected):
    if not isinstance(received, str):
        return False
    return hmac.compare_digest(received.encode(), expected.encode())


def hash_mcp_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mcp_token_metadata(row, now=None):
    item = dict(row)
    now = now or datetime.now(timezone.utc)
    expires_at = item.get("expires_at")
    expired = False
    if expires_at:
        try:
            expired = datetime.fromisoformat(expires_at) <= now
        except ValueError:
            expired = True
    if item.get("revoked_at"):
        status = "revoked"
    elif expired:
        status = "expired"
    else:
        status = "active"
    item["status"] = status
    return item


def list_mcp_tokens(db):
    rows = db.execute(
        """
        SELECT id, name, token_prefix, token_value, scopes, created_at,
               last_used_at, expires_at, revoked_at
        FROM mcp_tokens
        ORDER BY id DESC
        """
    ).fetchall()
    now = datetime.now(timezone.utc)
    items = []
    for row in rows:
        item = mcp_token_metadata(row, now)
        item["token"] = item.pop("token_value")
        items.append(item)
    return items


def create_mcp_token(name, expires_in_days=MCP_DEFAULT_EXPIRY_DAYS):
    name = text_value(name, 120)
    if not name:
        raise ValueError("name is required")
    if expires_in_days in ("", None):
        expires_at = None
    else:
        try:
            expires_in_days = int(expires_in_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("expires_in_days must be an integer") from exc
        if not 1 <= expires_in_days <= 3650:
            raise ValueError("expires_in_days must be between 1 and 3650")
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=expires_in_days)
        ).isoformat(timespec="seconds")

    raw_token = MCP_TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = utc_now()
    with db_connect() as db:
        cursor = db.execute(
            """
            INSERT INTO mcp_tokens
                (
                    name, token_hash, token_prefix, token_value, scopes,
                    created_at, expires_at
                )
            VALUES (?, ?, ?, ?, 'read,send', ?, ?)
            """,
            (
                name,
                hash_mcp_token(raw_token),
                raw_token[:16],
                raw_token,
                now,
                expires_at,
            ),
        )
        token_id = cursor.lastrowid
    return {
        "id": token_id,
        "name": name,
        "token": raw_token,
        "token_prefix": raw_token[:16],
        "scopes": "read,send",
        "created_at": now,
        "expires_at": expires_at,
    }


def authenticate_mcp_token(token):
    if not isinstance(token, str) or not token.startswith(MCP_TOKEN_PREFIX):
        return None
    now = utc_now()
    with db_connect() as db:
        row = db.execute(
            """
            SELECT id, name, token_prefix, scopes, created_at, last_used_at,
                   expires_at, revoked_at
            FROM mcp_tokens
            WHERE token_hash=?
              AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at>?)
            """,
            (hash_mcp_token(token), now),
        ).fetchone()
        if not row:
            return None
        db.execute(
            "UPDATE mcp_tokens SET last_used_at=? WHERE id=?",
            (now, row["id"]),
        )
    item = dict(row)
    item["last_used_at"] = now
    return item


def request_json():
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data

    # Some LuatOS SMS firmwares expose decoded UCS2 text as GBK/GB18030
    # bytes. Their JSON encoder preserves those bytes even when the HTTP
    # content type says UTF-8, so Flask's strict JSON parser rejects the
    # otherwise valid payload. Accept that device-specific wire format and
    # normalize it to Python Unicode before validation/storage.
    raw = request.get_data(cache=True)
    parse_errors = []
    for encoding in ("utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            parse_errors.append(f"{encoding}:decode@{exc.start}")
            continue
        for strict in (True, False):
            try:
                decoded = json.loads(text, strict=strict)
            except json.JSONDecodeError as exc:
                parse_errors.append(
                    f"{encoding}:{'strict' if strict else 'relaxed'}"
                    f"@{exc.pos}:{exc.msg}"
                )
                continue
            if isinstance(decoded, dict):
                return decoded
            parse_errors.append(f"{encoding}:root={type(decoded).__name__}")
            break
    if raw:
        log.warning(
            "device JSON parse failed bytes=%d errors=%s",
            len(raw),
            "; ".join(parse_errors),
        )
    return {}


def device_authorized(data):
    token = (
        request.headers.get("X-SMS-Token")
        or request.args.get("token")
        or data.get("token")
    )
    return device_token_valid(token)


def device_token_valid(token):
    return secure_equal(token, DEVICE_TOKEN)


def admin_authorized():
    token = request.headers.get("X-SMS-Admin-Token") or request.args.get("admin_token")
    return secure_equal(token, ADMIN_TOKEN)


def require_admin():
    if admin_authorized():
        return None
    return jsonify({"code": 401, "message": "invalid admin token"}), 401


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",", 1)[0].strip() or request.remote_addr or "")[:80]


def text_value(value, limit=255):
    return str(value or "").strip()[:limit]


def decode_base64url(value):
    value = str(value or "").strip()
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def validate_vapid_public_key(value):
    try:
        raw = decode_base64url(value)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("VAPID_PUBLIC_KEY is invalid") from exc
    if len(raw) != 65 or raw[0] != 4:
        raise RuntimeError("VAPID_PUBLIC_KEY must be an uncompressed P-256 key")
    return str(value).rstrip("=")


def web_push_vapid_config():
    global _vapid_config
    if webpush is None:
        raise RuntimeError("pywebpush is not installed")
    with _vapid_lock:
        if _vapid_config is not None:
            return _vapid_config
        if bool(VAPID_PRIVATE_KEY) != bool(VAPID_PUBLIC_KEY):
            raise RuntimeError(
                "VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY must be configured together"
            )
        if VAPID_PRIVATE_KEY:
            _vapid_config = (
                VAPID_PRIVATE_KEY,
                validate_vapid_public_key(VAPID_PUBLIC_KEY),
            )
            return _vapid_config

        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError as exc:
            raise RuntimeError("cryptography is not installed") from exc

        VAPID_PRIVATE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        if VAPID_PRIVATE_KEY_PATH.exists():
            private_key = serialization.load_pem_private_key(
                VAPID_PRIVATE_KEY_PATH.read_bytes(),
                password=None,
            )
        else:
            private_key = ec.generate_private_key(ec.SECP256R1())
            private_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            descriptor = os.open(
                VAPID_PRIVATE_KEY_PATH,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as private_file:
                private_file.write(private_pem)
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        public_key = base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")
        _vapid_config = (str(VAPID_PRIVATE_KEY_PATH), public_key)
        return _vapid_config


def validate_web_push_subscription(data):
    subscription = data.get("subscription")
    if not isinstance(subscription, dict):
        raise ValueError("subscription is required")
    endpoint = str(subscription.get("endpoint") or "").strip()
    parsed = urlparse(endpoint)
    if (
        not endpoint
        or len(endpoint) > 4096
        or parsed.scheme != "https"
        or not parsed.netloc
    ):
        raise ValueError("subscription endpoint must be a valid HTTPS URL")
    keys = subscription.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("subscription keys are required")
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    try:
        p256dh_bytes = decode_base64url(p256dh)
        auth_bytes = decode_base64url(auth)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("subscription keys are invalid") from exc
    if len(p256dh_bytes) != 65 or p256dh_bytes[0] != 4 or len(auth_bytes) < 16:
        raise ValueError("subscription keys are invalid")
    preferences = data.get("preferences") or {}
    if not isinstance(preferences, dict):
        raise ValueError("preferences must be an object")
    notify_sms = preferences.get("sms", True)
    notify_missed_call = preferences.get("missed_call", True)
    if not isinstance(notify_sms, bool) or not isinstance(
        notify_missed_call, bool
    ):
        raise ValueError("notification preferences must be booleans")
    if not notify_sms and not notify_missed_call:
        raise ValueError("select at least one notification type")
    return endpoint, p256dh, auth, notify_sms, notify_missed_call


def validate_sms_template_body(value):
    body = str(value or "").strip()
    if not body:
        raise ValueError("template body is required")
    if len(body) > MAX_SMS_TEMPLATE_LENGTH:
        raise ValueError(
            f"template body exceeds {MAX_SMS_TEMPLATE_LENGTH} characters"
        )
    placeholders = set(SMS_TEMPLATE_PATTERN.findall(body))
    unknown = sorted(placeholders - SMS_TEMPLATE_PLACEHOLDERS)
    if unknown:
        raise ValueError("unsupported placeholders: " + ", ".join(unknown))
    return body


def list_sms_templates(db):
    return [
        {**dict(row), "is_default": bool(row["is_default"])}
        for row in db.execute(
            """
            SELECT id, name, body, is_default, created_at, updated_at
            FROM sms_templates
            ORDER BY is_default DESC, id
            """
        ).fetchall()
    ]


def render_sms_template(template_body, message, current_device_phone=None):
    values = {
        "ori": message["sender"] or "未知",
        "sms": message["body"],
        "receiver": message["device_phone"] or message["device_label"],
        "phone": current_device_phone or "未录入",
        "time": message["received_at"] if record_value(message, "event_type", "sms") == "missed_call" else message["sms_time"] or message["received_at"],
        "device": message["device_label"],
    }
    return SMS_TEMPLATE_PATTERN.sub(
        lambda match: str(values[match.group(1)]),
        template_body,
    )


def resolve_sms_template_body(db, template_id=None):
    template = None
    if template_id:
        template = db.execute(
            "SELECT body FROM sms_templates WHERE id=?",
            (template_id,),
        ).fetchone()
    if not template:
        template = db.execute(
            "SELECT body FROM sms_templates WHERE is_default=1"
        ).fetchone()
    return template["body"] if template else DEFAULT_SMS_TEMPLATE_BODY


def normalize_recipient(value):
    recipient = re.sub(r"[\s\-()]", "", str(value or "").strip())
    if not re.fullmatch(r"\+?\d{5,20}", recipient):
        raise ValueError("recipient must contain 5-20 digits with an optional leading +")
    return recipient


def comparable_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("0086") and len(digits) > 11:
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) > 11:
        digits = digits[2:]
    return digits


def ensure_sms_forward_target_is_external(db, recipient):
    target = comparable_phone(recipient)
    for row in db.execute(
        "SELECT id, phone_number FROM devices WHERE phone_number<>''"
    ).fetchall():
        if comparable_phone(row["phone_number"]) == target:
            raise ValueError(
                "recipient matches a registered device SIM and could create a forwarding loop"
            )


def record_value(record, key, default=None):
    try:
        value = record[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def formatted_feishu_message(message, template_body=None):
    if record_value(message, "event_type", "sms") == "missed_call" and template_body is None:
        template_body = DEFAULT_CALL_TEMPLATE_BODY
    if template_body is not None:
        return render_sms_template(
            template_body,
            message,
            current_device_phone=record_value(message, "device_phone", ""),
        )
    return (
        "短信转发\n"
        f"设备：{message['device_label']}\n"
        f"号码：{message['sender'] or '未知'}\n"
        f"时间：{message['sms_time'] or message['received_at']}\n"
        f"内容：{message['body']}"
    )


def feishu_response_reference(response_excerpt):
    try:
        payload = json.loads(response_excerpt or "{}")
    except (TypeError, ValueError):
        return None, None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, None
    return (
        text_value(data.get("message_id"), 160) or None,
        text_value(data.get("chat_id"), 160) or None,
    )


def matching_feishu_destinations(db, verification_token, app_id=""):
    if not verification_token:
        return []
    matches = []
    for row in db.execute(
        """
        SELECT id, name, config_json
        FROM destinations
        WHERE kind='feishu_app' AND enabled=1
        ORDER BY id
        """
    ).fetchall():
        config = json.loads(row["config_json"] or "{}")
        expected_token = config.get("verification_token")
        if not expected_token or not secure_equal(
            verification_token,
            str(expected_token),
        ):
            continue
        if app_id and not secure_equal(str(config.get("app_id") or ""), app_id):
            continue
        matches.append({**dict(row), "config": config})
    return matches


def feishu_event_text(message):
    if message.get("message_type") != "text":
        return ""
    try:
        content = json.loads(message.get("content") or "{}")
    except (TypeError, ValueError):
        return ""
    text = str(content.get("text") or "").strip()
    for mention in message.get("mentions") or []:
        if isinstance(mention, dict) and mention.get("key"):
            text = text.replace(str(mention["key"]), "").strip()
    return text


def enqueue_feishu_sms_reply(db, payload, destinations):
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = (
        sender.get("sender_id")
        if isinstance(sender.get("sender_id"), dict)
        else {}
    )
    event_id = text_value(header.get("event_id"), 160)
    inbound_message_id = text_value(message.get("message_id"), 160)
    chat_id = text_value(message.get("chat_id"), 160)
    if not event_id or not inbound_message_id:
        return {"queued": False, "reason": "event identity missing"}
    if sender.get("sender_type") == "app":
        return {"queued": False, "reason": "bot message ignored"}
    if message.get("chat_type") != "group":
        return {"queued": False, "reason": "only group replies are accepted"}

    reference_ids = []
    for field in ("parent_id", "root_id"):
        value = text_value(message.get(field), 160)
        if value and value not in reference_ids:
            reference_ids.append(value)
    if not reference_ids:
        return {"queued": False, "reason": "ordinary group message ignored"}

    body = feishu_event_text(message)
    if not body:
        return {"queued": False, "reason": "only text replies are accepted"}
    if len(body) > MAX_OUTBOUND_SMS_LENGTH:
        return {"queued": False, "reason": "reply body is too long"}

    existing = db.execute(
        """
        SELECT outbound_sms_id
        FROM feishu_sms_replies
        WHERE event_id=? OR inbound_message_id=?
        """,
        (event_id, inbound_message_id),
    ).fetchone()
    if existing:
        return {
            "queued": True,
            "duplicate": True,
            "outbound_sms_id": existing["outbound_sms_id"],
        }

    pending = db.execute(
        """
        SELECT status, outbound_sms_id
        FROM feishu_reply_inbox
        WHERE event_id=? OR inbound_message_id=?
        """,
        (event_id, inbound_message_id),
    ).fetchone()
    if pending:
        return {
            "queued": True,
            "duplicate": True,
            "acknowledgement_pending": pending["status"] == "pending",
            "outbound_sms_id": pending["outbound_sms_id"],
        }

    destination_ids = [item["id"] for item in destinations]
    destination_placeholders = ",".join("?" for _ in destination_ids)
    reference_placeholders = ",".join("?" for _ in reference_ids)
    delivery = db.execute(
        f"""
        SELECT
            dl.id source_delivery_id,
            dl.destination_id,
            dl.feishu_chat_id,
            m.device_id,
            m.sender
        FROM deliveries dl
        JOIN messages m ON m.id=dl.message_id
        WHERE dl.destination_id IN ({destination_placeholders})
          AND dl.feishu_message_id IN ({reference_placeholders})
          AND dl.status='delivered'
          AND m.event_type='sms'
          AND (
              ?=''
              OR dl.feishu_chat_id IS NULL
              OR dl.feishu_chat_id=''
              OR dl.feishu_chat_id=?
          )
        ORDER BY dl.id DESC
        LIMIT 1
        """,
        (*destination_ids, *reference_ids, chat_id, chat_id),
    ).fetchone()
    if not delivery:
        return {"queued": False, "reason": "reply target is not an SMS delivery"}

    recipient = normalize_recipient(delivery["sender"])
    ensure_sms_forward_target_is_external(db, recipient)
    if not db.execute(
        "SELECT 1 FROM devices WHERE id=?",
        (delivery["device_id"],),
    ).fetchone():
        return {"queued": False, "reason": "source device is unavailable"}

    now = utc_now()
    db.execute(
        """
        INSERT INTO feishu_reply_inbox (
            inbound_message_id, event_id, destination_id,
            source_delivery_id, sender_open_id, reply_body, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            inbound_message_id,
            event_id,
            delivery["destination_id"],
            delivery["source_delivery_id"],
            text_value(
                sender_id.get("open_id")
                or sender_id.get("union_id")
                or sender_id.get("user_id"),
                160,
            ),
            body,
            now,
        ),
    )
    return {
        "queued": True,
        "duplicate": False,
        "acknowledgement_pending": True,
        "outbound_sms_id": None,
    }


def fetch_feishu_history(destination):
    config = json.loads(destination["config_json"] or "{}")
    if config.get("receive_id_type", "chat_id") != "chat_id":
        return False, "receive_id_type is not chat_id", []
    timeout = max(5, min(60, int(config.get("timeout_seconds", 30))))
    auth = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": config["app_id"], "app_secret": config["app_secret"]},
        timeout=timeout,
    )
    ok, error = feishu_success(auth)
    if not ok:
        return False, error, []
    tenant_token = auth.json().get("tenant_access_token")
    if not tenant_token:
        return False, "tenant token missing", []
    response = requests.get(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        params={
            "container_id_type": "chat",
            "container_id": config["receive_id"],
            "sort_type": "ByCreateTimeDesc",
            "page_size": 50,
        },
        headers={"Authorization": f"Bearer {tenant_token}"},
        timeout=timeout,
    )
    ok, error = feishu_success(response)
    if not ok:
        return False, error, []
    data = response.json().get("data")
    items = data.get("items") if isinstance(data, dict) else []
    return True, "", items if isinstance(items, list) else []


def backfill_feishu_delivery_references(db, destination, items):
    app_messages = {}
    for item in items:
        if (item.get("sender") or {}).get("sender_type") != "app":
            continue
        if item.get("msg_type") != "text" or not item.get("message_id"):
            continue
        try:
            content = json.loads(
                (item.get("body") or {}).get("content") or "{}"
            )
        except (TypeError, ValueError):
            continue
        text = content.get("text")
        if isinstance(text, str):
            app_messages[text] = item
    if not app_messages:
        return 0

    config = json.loads(destination["config_json"] or "{}")
    template_body = resolve_sms_template_body(db, config.get("template_id"))

    updated = 0
    for row in db.execute(
        """
        SELECT
            dl.id,
            m.sender,
            m.sms_time,
            m.body,
            m.event_type,
            m.received_at,
            dev.phone_number device_phone,
            COALESCE(NULLIF(dev.name,''), NULLIF(dev.phone_number,''), dev.id)
                device_label
        FROM deliveries dl
        JOIN messages m ON m.id=dl.message_id
        JOIN devices dev ON dev.id=m.device_id
        WHERE dl.destination_id=?
          AND dl.status='delivered'
          AND dl.feishu_message_id IS NULL
        ORDER BY dl.id DESC
        """,
        (destination["id"],),
    ).fetchall():
        item = app_messages.get(formatted_feishu_message(row, template_body))
        if not item:
            item = app_messages.get(formatted_feishu_message(row))
        if not item:
            continue
        db.execute(
            """
            UPDATE deliveries
            SET feishu_message_id=?, feishu_chat_id=?
            WHERE id=? AND feishu_message_id IS NULL
            """,
            (
                text_value(item.get("message_id"), 160),
                text_value(item.get("chat_id"), 160),
                row["id"],
            ),
        )
        updated += 1
    return updated


def feishu_poll_payload(destination, item):
    sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
    return {
        "schema": "2.0",
        "header": {
            "event_id": "poll:" + text_value(item.get("message_id"), 150),
            "event_type": "im.message.receive_v1",
            "app_id": json.loads(destination["config_json"] or "{}").get(
                "app_id",
                "",
            ),
        },
        "event": {
            "sender": {
                "sender_type": sender.get("sender_type"),
                "sender_id": {
                    "open_id": sender.get("id")
                    if sender.get("id_type") == "open_id"
                    else ""
                },
            },
            "message": {
                "message_id": item.get("message_id"),
                "parent_id": item.get("parent_id"),
                "root_id": item.get("root_id"),
                "chat_id": item.get("chat_id"),
                "chat_type": "group",
                "message_type": item.get("msg_type"),
                "content": (item.get("body") or {}).get("content"),
                "mentions": item.get("mentions") or [],
            },
        },
    }


def handle_feishu_polled_items(db, destination, items, bootstrap):
    backfill_feishu_delivery_references(db, destination, items)
    queued = 0
    ordered_items = sorted(
        items,
        key=lambda item: (
            str(item.get("create_time") or ""),
            str(item.get("message_id") or ""),
        ),
    )
    for item in ordered_items:
        message_id = text_value(item.get("message_id"), 160)
        if not message_id:
            continue
        inserted = db.execute(
            """
            INSERT OR IGNORE INTO feishu_poll_seen (
                message_id, destination_id, create_time, seen_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                message_id,
                destination["id"],
                text_value(item.get("create_time"), 40),
                utc_now(),
            ),
        )
        if inserted.rowcount == 0 or bootstrap:
            continue
        try:
            result = enqueue_feishu_sms_reply(
                db,
                feishu_poll_payload(destination, item),
                [
                    {
                        **dict(destination),
                        "config": json.loads(
                            destination["config_json"] or "{}"
                        ),
                    }
                ],
            )
        except (ValueError, sqlite3.IntegrityError):
            continue
        if result.get("queued") and not result.get("duplicate"):
            queued += 1
    return queued


def poll_feishu_messages():
    with db_connect() as db:
        destinations = db.execute(
            """
            SELECT id, name, config_json
            FROM destinations
            WHERE kind='feishu_app' AND enabled=1
            ORDER BY id
            """
        ).fetchall()
    queued = 0
    for destination in destinations:
        try:
            ok, error, items = fetch_feishu_history(destination)
            if not ok:
                log.warning(
                    "Feishu history poll destination %s failed: %s",
                    destination["id"],
                    error,
                )
                continue
            with db_connect() as db:
                db.execute("BEGIN IMMEDIATE")
                bootstrap = not db.execute(
                    """
                    SELECT 1
                    FROM feishu_poll_seen
                    WHERE destination_id=?
                    LIMIT 1
                    """,
                    (destination["id"],),
                ).fetchone()
                queued += handle_feishu_polled_items(
                    db,
                    destination,
                    items,
                    bootstrap,
                )
        except Exception:
            log.exception(
                "Feishu history poll destination %s failed",
                destination["id"],
            )
    return queued


def expire_stale_outbound(db, device_id=None):
    cutoff = time.time() - OUTBOUND_STALE_SECONDS
    where = "status='sending' AND dispatched_epoch<?"
    params = [cutoff]
    if device_id:
        where += " AND device_id=?"
        params.append(device_id)
    linked = db.execute(
        f"""
        SELECT id, source_delivery_id
        FROM outbound_sms
        WHERE {where} AND source_delivery_id IS NOT NULL
        """,
        params,
    ).fetchall()
    feishu_linked = db.execute(
        f"""
        SELECT id
        FROM outbound_sms
        WHERE {where}
          AND EXISTS (
              SELECT 1
              FROM feishu_sms_replies fr
              WHERE fr.outbound_sms_id=outbound_sms.id
          )
        """,
        params,
    ).fetchall()
    query = """
        UPDATE outbound_sms
        SET status='unknown', completed_at=?,
            last_error='device did not confirm the send result in time'
        WHERE status='sending' AND dispatched_epoch<?
    """
    params = [utc_now(), cutoff]
    if device_id:
        query += " AND device_id=?"
        params.append(device_id)
    db.execute(query, params)
    for row in feishu_linked:
        db.execute(
            """
            UPDATE feishu_sms_replies
            SET next_notification_at=0, notification_attempts=0
            WHERE outbound_sms_id=?
            """,
            (row["id"],),
        )
    for row in linked:
        db.execute(
            """
            UPDATE deliveries
            SET status='failed',
                last_error='SMS forwarding result is unknown; confirm before retrying'
            WHERE id=? AND status<>'delivered'
            """,
            (row["source_delivery_id"],),
        )
        message = db.execute(
            "SELECT message_id FROM deliveries WHERE id=?",
            (row["source_delivery_id"],),
        ).fetchone()
        if message:
            refresh_message_status(db, message["message_id"])


def claim_outbound_sms(db, device_id):
    """Atomically claim the next command for one registered device."""
    if not db.execute(
        "SELECT 1 FROM devices WHERE id=?",
        (device_id,),
    ).fetchone():
        raise LookupError("device not found")
    expire_stale_outbound(db, device_id)
    command = db.execute(
        """
        SELECT id, recipient, body
        FROM outbound_sms
        WHERE device_id=? AND status='pending'
        ORDER BY id
        LIMIT 1
        """,
        (device_id,),
    ).fetchone()
    if not command:
        return None
    now = utc_now()
    updated = db.execute(
        """
        UPDATE outbound_sms
        SET status='sending', attempts=attempts+1,
            dispatched_at=?, dispatched_epoch=?, last_error=''
        WHERE id=? AND status='pending'
        """,
        (now, time.time(), command["id"]),
    )
    if updated.rowcount == 0:
        return None
    return {
        "id": command["id"],
        "to": command["recipient"],
        "text": command["body"],
    }


def upsert_device(db, data, remote_ip=None):
    device_id = text_value(data.get("device_id"), 80)
    if not device_id:
        raise ValueError("device_id is required")
    now = utc_now()
    traffic_session_id = text_value(data.get("traffic_session_id"), 80)
    traffic_session_bytes = 0
    if traffic_session_id:
        if isinstance(data.get("traffic_session_bytes"), bool):
            raise ValueError("traffic_session_bytes must be an integer")
        try:
            traffic_session_bytes = int(data.get("traffic_session_bytes") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("traffic_session_bytes must be an integer") from exc
        if traffic_session_bytes < 0:
            raise ValueError("traffic_session_bytes must be non-negative")
    db.execute(
        """
        INSERT INTO devices (
            id, name, phone_number, firmware, app_version, network, signal,
            queue_count, traffic_total_bytes, traffic_session_id,
            traffic_session_bytes, traffic_updated_at,
            first_seen, last_seen, last_ip, status_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            firmware=CASE WHEN excluded.firmware='' THEN devices.firmware ELSE excluded.firmware END,
            app_version=CASE WHEN excluded.app_version='' THEN devices.app_version ELSE excluded.app_version END,
            network=CASE WHEN excluded.network='' THEN devices.network ELSE excluded.network END,
            signal=COALESCE(excluded.signal, devices.signal),
            queue_count=excluded.queue_count,
            last_seen=excluded.last_seen,
            last_ip=excluded.last_ip,
            status_message=excluded.status_message,
            traffic_total_bytes=CASE
                WHEN excluded.traffic_session_id='' THEN devices.traffic_total_bytes
                WHEN excluded.traffic_session_id=devices.traffic_session_id THEN
                    devices.traffic_total_bytes + MAX(
                        0,
                        excluded.traffic_session_bytes-devices.traffic_session_bytes
                    )
                ELSE devices.traffic_total_bytes+excluded.traffic_session_bytes
            END,
            traffic_session_id=CASE
                WHEN excluded.traffic_session_id='' THEN devices.traffic_session_id
                ELSE excluded.traffic_session_id
            END,
            traffic_session_bytes=CASE
                WHEN excluded.traffic_session_id='' THEN devices.traffic_session_bytes
                WHEN excluded.traffic_session_id=devices.traffic_session_id THEN
                    MAX(devices.traffic_session_bytes, excluded.traffic_session_bytes)
                ELSE excluded.traffic_session_bytes
            END,
            traffic_updated_at=CASE
                WHEN excluded.traffic_session_id='' THEN devices.traffic_updated_at
                ELSE excluded.traffic_updated_at
            END,
            phone_number=CASE
                WHEN devices.phone_number='' THEN excluded.phone_number
                ELSE devices.phone_number
            END
        """,
        (
            device_id,
            text_value(data.get("name"), 120),
            text_value(data.get("phone_number"), 40),
            text_value(data.get("firmware"), 160),
            text_value(data.get("app_version"), 80),
            text_value(data.get("network"), 80),
            int(data["signal"]) if isinstance(data.get("signal"), (int, float)) else None,
            max(0, int(data.get("queue_count") or 0)),
            traffic_session_bytes,
            traffic_session_id,
            traffic_session_bytes,
            now if traffic_session_id else "",
            now,
            now,
            text_value(remote_ip, 80) if remote_ip is not None else client_ip(),
            text_value(data.get("status_message"), 255),
        ),
    )
    return device_id, now


def queue_deliveries(db, message_id, device_id):
    event = db.execute("SELECT event_type FROM messages WHERE id=?", (message_id,)).fetchone()
    device = db.execute("SELECT sms_forward_enabled, call_forward_enabled FROM devices WHERE id=?", (device_id,)).fetchone()
    field = "call_forward_enabled" if event and event["event_type"] == "missed_call" else "sms_forward_enabled"
    if device and not device[field]:
        db.execute("UPDATE messages SET status='stored' WHERE id=?", (message_id,))
        return 0
    rows = db.execute(
        """
        SELECT DISTINCT d.id
        FROM destinations d
        WHERE d.enabled=1 AND (
            EXISTS (
                SELECT 1
                FROM routes r
                WHERE r.destination_id=d.id
                  AND r.enabled=1
                  AND r.device_id IN ('*', ?)
            )
            OR EXISTS (
                SELECT 1
                FROM channel_group_destinations cgd
                JOIN channel_groups cg ON cg.id=cgd.group_id
                JOIN channel_group_devices cgdev ON cgdev.group_id=cg.id
                WHERE cgd.destination_id=d.id
                  AND cg.enabled=1
                  AND cgdev.device_id=?
            )
        )
        """,
        (device_id, device_id),
    ).fetchall()
    for row in rows:
        db.execute(
            """
            INSERT OR IGNORE INTO deliveries
                (message_id, destination_id, status, attempts, next_attempt_at)
            VALUES (?, ?, 'pending', 0, 0)
            """,
            (message_id, row["id"]),
        )
    status = "queued" if rows else "stored"
    db.execute("UPDATE messages SET status=? WHERE id=?", (status, message_id))
    return len(rows)


def queue_web_push_deliveries(db, message_id, event_type):
    preference_column = {
        "sms": "notify_sms",
        "missed_call": "notify_missed_call",
    }.get(event_type)
    if not preference_column:
        return 0
    cursor = db.execute(
        f"""
        INSERT OR IGNORE INTO web_push_deliveries (
            subscription_id, message_id, status, attempts, next_attempt_at
        )
        SELECT id, ?, 'pending', 0, 0
        FROM web_push_subscriptions
        WHERE {preference_column}=1
        """,
        (message_id,),
    )
    return cursor.rowcount


@app.get("/")
def index():
    response = send_from_directory(app.static_folder, "index.html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/service-worker.js")
def service_worker():
    response = send_from_directory(app.static_folder, "service-worker.js")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "./"
    return response


@app.get("/api/health")
def health():
    try:
        with db_connect() as db:
            db.execute("SELECT 1").fetchone()
        return jsonify({"status": "healthy", "time": utc_now()})
    except Exception as exc:
        log.exception("health check failed")
        return jsonify({"status": "unhealthy", "error": str(exc)}), 503


@app.post("/api/integrations/feishu/events")
def receive_feishu_event():
    payload = request_json()
    if not isinstance(payload, dict):
        return jsonify({"code": 400, "message": "invalid event payload"}), 400
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    verification_token = str(header.get("token") or payload.get("token") or "")
    app_id = text_value(header.get("app_id"), 160)
    with db_connect() as db:
        destinations = matching_feishu_destinations(
            db,
            verification_token,
            app_id,
        )
        if not destinations:
            return jsonify({"code": 401, "message": "invalid verification token"}), 401
        if payload.get("type") == "url_verification":
            challenge = text_value(payload.get("challenge"), 500)
            if not challenge:
                return jsonify({"code": 400, "message": "challenge is required"}), 400
            return jsonify({"challenge": challenge})
        if header.get("event_type") != "im.message.receive_v1":
            return jsonify({"code": 0, "ignored": True})
        try:
            db.execute("BEGIN IMMEDIATE")
            result = enqueue_feishu_sms_reply(db, payload, destinations)
        except (ValueError, sqlite3.IntegrityError) as exc:
            result = {"queued": False, "reason": text_value(exc, 160)}
    return jsonify({"code": 0, **result})


@app.post("/api/device/register")
def register_device():
    data = request_json()
    if not device_authorized(data):
        return jsonify({"code": 401, "message": "invalid token"}), 401
    try:
        with db_connect() as db:
            device_id, now = upsert_device(db, data)
        return jsonify(
            {
                "code": 0,
                "device_id": device_id,
                "registered_at": now,
                "heartbeat_seconds": HEARTBEAT_SECONDS,
            }
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400


@app.post("/api/device/heartbeat")
def device_heartbeat():
    data = request_json()
    if not device_authorized(data):
        return jsonify({"code": 401, "message": "invalid token"}), 401
    try:
        with db_connect() as db:
            device_id, now = upsert_device(db, data)
        return jsonify(
            {
                "code": 0,
                "device_id": device_id,
                "server_time": now,
                "heartbeat_seconds": HEARTBEAT_SECONDS,
            }
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400


@app.post("/api/device/sync")
def sync_device():
    """Combine registration, heartbeat and outbound polling for HTTP fallback."""
    data = request_json()
    if not device_authorized(data):
        return jsonify({"code": 401, "message": "invalid token"}), 401
    try:
        with db_connect() as db:
            db.execute("BEGIN IMMEDIATE")
            device_id, now = upsert_device(db, data)
            command = claim_outbound_sms(db, device_id)
        response = {
            "code": 0,
            "device_id": device_id,
            "server_time": now,
            "sync_seconds": DEVICE_SYNC_SECONDS,
        }
        if command:
            response["command"] = command
        return jsonify(response)
    except (ValueError, TypeError) as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400


@app.post("/api/device/outbound/poll")
def poll_outbound_sms():
    data = request_json()
    if not device_authorized(data):
        return jsonify({"code": 401}), 401
    device_id = text_value(data.get("device_id"), 80)
    if not device_id:
        return jsonify({"code": 400}), 400

    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            command = claim_outbound_sms(db, device_id)
        except LookupError:
            return jsonify({"code": 404}), 404
        if not command:
            return jsonify({"code": 0})
    return jsonify({"code": 0, **command})


def apply_outbound_sms_result(db, data):
    """Persist a device result. The caller owns the surrounding transaction."""
    device_id = text_value(data.get("device_id"), 80)
    try:
        command_id = int(data.get("id"))
    except (TypeError, ValueError):
        raise ValueError("invalid command id")
    success = data.get("ok")
    if not device_id or not isinstance(success, bool):
        raise ValueError("device_id and boolean ok are required")

    now = utc_now()
    row = db.execute(
        """
        SELECT status, source_delivery_id
        FROM outbound_sms
        WHERE id=? AND device_id=?
        """,
        (command_id, device_id),
    ).fetchone()
    if not row:
        raise LookupError("outbound SMS not found")
    if success:
        db.execute(
            """
            UPDATE outbound_sms
            SET status='sent', completed_at=?, sent_at=?, last_error=''
            WHERE id=? AND device_id=?
            """,
            (now, now, command_id, device_id),
        )
        if row["source_delivery_id"]:
            db.execute(
                """
                UPDATE deliveries
                SET status='delivered', delivered_at=?, next_attempt_at=0,
                    last_error='', http_status=NULL, response_excerpt=''
                WHERE id=?
                """,
                (now, row["source_delivery_id"]),
            )
    elif row["status"] != "sent":
        error = text_value(data.get("error") or "device send failed", 500)
        db.execute(
            """
            UPDATE outbound_sms
            SET status='failed', completed_at=?, last_error=?
            WHERE id=? AND device_id=?
            """,
            (
                now,
                error,
                command_id,
                device_id,
            ),
        )
        if row["source_delivery_id"]:
            db.execute(
                """
                UPDATE deliveries
                SET status='failed', next_attempt_at=0, last_error=?
                WHERE id=? AND status<>'delivered'
                """,
                (f"SMS forwarding failed: {error}", row["source_delivery_id"]),
            )
    if row["source_delivery_id"]:
        message = db.execute(
            "SELECT message_id FROM deliveries WHERE id=?",
            (row["source_delivery_id"],),
        ).fetchone()
        if message:
            refresh_message_status(db, message["message_id"])
    db.execute(
        """
        UPDATE feishu_sms_replies
        SET next_notification_at=0, notification_attempts=0
        WHERE outbound_sms_id=?
        """,
        (command_id,),
    )
    return command_id


@app.post("/api/device/outbound/result")
def report_outbound_sms_result():
    data = request_json()
    if not device_authorized(data):
        return jsonify({"code": 401}), 401
    try:
        with db_connect() as db:
            apply_outbound_sms_result(db, data)
    except ValueError:
        return jsonify({"code": 400}), 400
    except LookupError:
        return jsonify({"code": 404}), 404
    return jsonify({"code": 0})


@app.post("/api/messages")
def receive_message():
    data = request_json()
    if not device_authorized(data):
        return jsonify({"code": 401, "message": "invalid token"}), 401
    body = str(data.get("body") or data.get("text") or "")
    if not body:
        log.warning(
            "message rejected: body required bytes=%d keys=%s",
            len(request.get_data(cache=True)),
            ",".join(sorted(str(key) for key in data)),
        )
        return jsonify({"code": 400, "message": "body is required"}), 400
    if len(body) > MAX_SMS_LENGTH:
        log.warning("message rejected: body too long chars=%d", len(body))
        return jsonify({"code": 400, "message": "body is too long"}), 400
    try:
        with db_connect() as db:
            device_id, _ = upsert_device(db, data)
            sender = text_value(data.get("sender") or data.get("phone"), 80)
            sms_time = text_value(data.get("sms_time") or data.get("datetime"), 80)
            supplied_key = text_value(data.get("message_id"), 160)
            message_key = supplied_key or hashlib.sha256(
                f"{device_id}\0{sender}\0{sms_time}\0{body}".encode("utf-8")
            ).hexdigest()
            received_at = utc_now()
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO messages
                    (message_key, device_id, sender, sms_time, body, received_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'stored')
                """,
                (message_key, device_id, sender, sms_time, body, received_at),
            )
            duplicate = cursor.rowcount == 0
            row = db.execute(
                "SELECT id, status FROM messages WHERE message_key=?", (message_key,)
            ).fetchone()
            delivery_count = 0
            push_count = 0
            if not duplicate:
                delivery_count = queue_deliveries(db, row["id"], device_id)
                push_count = queue_web_push_deliveries(db, row["id"], "sms")
        return jsonify(
            {
                "code": 0,
                "message_id": row["id"],
                "duplicate": duplicate,
                "delivery_count": delivery_count,
                "push_count": push_count,
            }
        )
    except (ValueError, TypeError) as exc:
        log.warning("message rejected: %s", exc)
        return jsonify({"code": 400, "message": str(exc)}), 400


@app.post("/api/device/missed-call")
def receive_missed_call():
    data = request_json()
    if not device_authorized(data):
        return jsonify({"code": 401, "message": "invalid token"}), 401
    call_id = text_value(data.get("call_id"), 160)
    if not call_id:
        return jsonify({"code": 400, "message": "call_id is required"}), 400
    caller = text_value(data.get("caller") or data.get("phone"), 80)
    started_at = text_value(
        data.get("started_at") or data.get("datetime"),
        80,
    ) or utc_now()
    ended_at = text_value(data.get("ended_at"), 80)
    try:
        duration_seconds = max(
            0,
            min(86400, int(data.get("duration_seconds") or 0)),
        )
    except (TypeError, ValueError):
        return jsonify(
            {"code": 400, "message": "duration_seconds must be an integer"}
        ), 400
    try:
        with db_connect() as db:
            device_id, _ = upsert_device(db, data)
            message_key = f"missed-call:{device_id}:{call_id}"
            received_at = utc_now()
            metadata_json = json.dumps(
                {
                    "call_id": call_id,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "duration_seconds": duration_seconds,
                },
                ensure_ascii=False,
            )
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO messages (
                    message_key, device_id, event_type, sender, sms_time,
                    body, metadata_json, received_at, status
                ) VALUES (?, ?, 'missed_call', ?, ?, '未接来电', ?, ?, 'stored')
                """,
                (
                    message_key,
                    device_id,
                    caller,
                    started_at,
                    metadata_json,
                    received_at,
                ),
            )
            duplicate = cursor.rowcount == 0
            row = db.execute(
                "SELECT id, status FROM messages WHERE message_key=?",
                (message_key,),
            ).fetchone()
            delivery_count = 0
            push_count = 0
            if not duplicate:
                delivery_count = queue_deliveries(db, row["id"], device_id)
                push_count = queue_web_push_deliveries(
                    db, row["id"], "missed_call"
                )
        return jsonify(
            {
                "code": 0,
                "message_id": row["id"],
                "duplicate": duplicate,
                "delivery_count": delivery_count,
                "push_count": push_count,
            }
        )
    except (ValueError, TypeError) as exc:
        log.warning("missed call rejected: %s", exc)
        return jsonify({"code": 400, "message": str(exc)}), 400


def masked_destination(row):
    item = dict(row)
    config = json.loads(item.pop("config_json") or "{}")
    for secret_field in ("app_secret", "verification_token", "headers"):
        if config.get(secret_field):
            config[secret_field] = "••••••••"
    item["config"] = config
    item["enabled"] = bool(item["enabled"])
    return item


@app.post("/api/admin/login")
def admin_login():
    data = request_json()
    if not secure_equal(data.get("token"), ADMIN_TOKEN):
        return jsonify({"code": 401, "message": "invalid token"}), 401
    return jsonify({"code": 0})


@app.get("/api/admin/web-push/config")
def get_web_push_config():
    denied = require_admin()
    if denied:
        return denied
    try:
        _, public_key = web_push_vapid_config()
    except RuntimeError as exc:
        return jsonify({"code": 503, "message": str(exc), "enabled": False}), 503
    with db_connect() as db:
        subscription_count = db.execute(
            "SELECT COUNT(*) FROM web_push_subscriptions"
        ).fetchone()[0]
    response = jsonify(
        {
            "code": 0,
            "enabled": True,
            "public_key": public_key,
            "subscription_count": subscription_count,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/admin/web-push/subscriptions")
def save_web_push_subscription():
    denied = require_admin()
    if denied:
        return denied
    try:
        web_push_vapid_config()
        endpoint, p256dh, auth, notify_sms, notify_missed_call = (
            validate_web_push_subscription(request_json())
        )
    except (RuntimeError, ValueError) as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400
    now = utc_now()
    with db_connect() as db:
        db.execute(
            """
            INSERT INTO web_push_subscriptions (
                endpoint, p256dh, auth, user_agent, notify_sms,
                notify_missed_call, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh=excluded.p256dh,
                auth=excluded.auth,
                user_agent=excluded.user_agent,
                notify_sms=excluded.notify_sms,
                notify_missed_call=excluded.notify_missed_call,
                updated_at=excluded.updated_at,
                last_error=''
            """,
            (
                endpoint,
                p256dh,
                auth,
                text_value(request.headers.get("User-Agent"), 300),
                int(notify_sms),
                int(notify_missed_call),
                now,
                now,
            ),
        )
        row = db.execute(
            "SELECT id FROM web_push_subscriptions WHERE endpoint=?",
            (endpoint,),
        ).fetchone()
    return jsonify(
        {
            "code": 0,
            "id": row["id"],
            "preferences": {
                "sms": notify_sms,
                "missed_call": notify_missed_call,
            },
        }
    )


@app.get("/api/admin/web-push/subscriptions")
def get_web_push_subscription():
    denied = require_admin()
    if denied:
        return denied
    endpoint = str(request.args.get("endpoint") or "").strip()
    if not endpoint:
        return jsonify({"code": 400, "message": "endpoint is required"}), 400
    with db_connect() as db:
        row = db.execute(
            """
            SELECT notify_sms, notify_missed_call, last_success_at, last_error
            FROM web_push_subscriptions
            WHERE endpoint=?
            """,
            (endpoint,),
        ).fetchone()
    if not row:
        return jsonify({"code": 0, "subscribed": False})
    response = jsonify(
        {
            "code": 0,
            "subscribed": True,
            "preferences": {
                "sms": bool(row["notify_sms"]),
                "missed_call": bool(row["notify_missed_call"]),
            },
            "last_success_at": row["last_success_at"],
            "last_error": row["last_error"],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.delete("/api/admin/web-push/subscriptions")
def delete_web_push_subscription():
    denied = require_admin()
    if denied:
        return denied
    endpoint = str(request_json().get("endpoint") or "").strip()
    if not endpoint:
        return jsonify({"code": 400, "message": "endpoint is required"}), 400
    with db_connect() as db:
        db.execute(
            "DELETE FROM web_push_subscriptions WHERE endpoint=?",
            (endpoint,),
        )
    return jsonify({"code": 0})


@app.post("/api/admin/web-push/test")
def test_web_push_subscription():
    denied = require_admin()
    if denied:
        return denied
    endpoint = str(request_json().get("endpoint") or "").strip()
    with db_connect() as db:
        row = db.execute(
            """
            SELECT id, endpoint, p256dh, auth
            FROM web_push_subscriptions
            WHERE endpoint=?
            """,
            (endpoint,),
        ).fetchone()
    if not row:
        return jsonify({"code": 404, "message": "subscription not found"}), 404
    try:
        send_web_push_subscription(
            row,
            {
                "title": "SMS Center 通知已启用",
                "body": "Chrome 可以接收新短信和未接来电通知。",
                "tag": "sms-center-test",
                "url": "./#overview",
            },
        )
    except Exception as exc:
        status_code = web_push_exception_status(exc)
        with db_connect() as db:
            if status_code in (404, 410):
                db.execute(
                    "DELETE FROM web_push_subscriptions WHERE id=?", (row["id"],)
                )
            else:
                db.execute(
                    "UPDATE web_push_subscriptions SET last_error=? WHERE id=?",
                    (text_value(exc, 500), row["id"]),
                )
        return jsonify({"code": 502, "message": "test push failed"}), 502
    with db_connect() as db:
        db.execute(
            """
            UPDATE web_push_subscriptions
            SET last_success_at=?, last_error=''
            WHERE id=?
            """,
            (utc_now(), row["id"]),
        )
    return jsonify({"code": 0})


@app.get("/api/admin/mcp-tokens")
def get_mcp_tokens():
    denied = require_admin()
    if denied:
        return denied
    with db_connect() as db:
        tokens = list_mcp_tokens(db)
    response = jsonify({"code": 0, "tokens": tokens})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/admin/mcp-tokens")
def add_mcp_token():
    denied = require_admin()
    if denied:
        return denied
    data = request_json()
    try:
        created = create_mcp_token(
            data.get("name"),
            data.get("expires_in_days", MCP_DEFAULT_EXPIRY_DAYS),
        )
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400
    response = jsonify({"code": 0, **created})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.delete("/api/admin/mcp-tokens/<int:token_id>")
def revoke_mcp_token(token_id):
    denied = require_admin()
    if denied:
        return denied
    with db_connect() as db:
        row = db.execute(
            "SELECT revoked_at FROM mcp_tokens WHERE id=?",
            (token_id,),
        ).fetchone()
        if not row:
            return jsonify({"code": 404, "message": "MCP token not found"}), 404
        if not row["revoked_at"]:
            db.execute(
                "UPDATE mcp_tokens SET revoked_at=? WHERE id=?",
                (utc_now(), token_id),
            )
    return jsonify({"code": 0})


@app.delete("/api/admin/mcp-tokens/<int:token_id>/permanent")
def delete_mcp_token(token_id):
    denied = require_admin()
    if denied:
        return denied
    with db_connect() as db:
        cursor = db.execute("DELETE FROM mcp_tokens WHERE id=?", (token_id,))
        if cursor.rowcount == 0:
            return jsonify({"code": 404, "message": "MCP token not found"}), 404
    return jsonify({"code": 0})


@app.get("/api/admin/snapshot")
def admin_snapshot():
    denied = require_admin()
    if denied:
        return denied
    now_epoch = time.time()
    with db_connect() as db:
        expire_stale_outbound(db)
        devices = [dict(row) for row in db.execute(
            "SELECT * FROM devices ORDER BY last_seen DESC"
        ).fetchall()]
        for item in devices:
            try:
                seen_epoch = datetime.fromisoformat(item["last_seen"]).timestamp()
            except Exception:
                seen_epoch = 0
            item["online"] = now_epoch - seen_epoch <= OFFLINE_SECONDS
        events = [dict(row) for row in db.execute(
            """
            SELECT
                m.*,
                COALESCE(NULLIF(d.name,''), NULLIF(d.phone_number,''), d.id) device_label,
                d.name device_name,
                d.phone_number device_phone
            FROM messages m JOIN devices d ON d.id=m.device_id
            ORDER BY m.id DESC LIMIT 300
            """
        ).fetchall()]
        for item in events:
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except (TypeError, ValueError):
                item["metadata"] = {}
        messages = [item for item in events if item["event_type"] == "sms"]
        missed_calls = [
            item for item in events if item["event_type"] == "missed_call"
        ]
        destinations = [masked_destination(row) for row in db.execute(
            "SELECT * FROM destinations ORDER BY id"
        ).fetchall()]
        routes = [dict(row) for row in db.execute(
            """
            SELECT r.*, d.name destination_name
            FROM routes r JOIN destinations d ON d.id=r.destination_id
            ORDER BY r.id
            """
        ).fetchall()]
        channel_groups = [dict(row) for row in db.execute(
            "SELECT * FROM channel_groups ORDER BY id"
        ).fetchall()]
        group_devices = db.execute(
            "SELECT group_id, device_id FROM channel_group_devices ORDER BY device_id"
        ).fetchall()
        group_destinations = db.execute(
            """
            SELECT group_id, destination_id
            FROM channel_group_destinations
            ORDER BY destination_id
            """
        ).fetchall()
        devices_by_group = {}
        for row in group_devices:
            devices_by_group.setdefault(row["group_id"], []).append(row["device_id"])
        destinations_by_group = {}
        for row in group_destinations:
            destinations_by_group.setdefault(row["group_id"], []).append(
                row["destination_id"]
            )
        for item in channel_groups:
            item["enabled"] = bool(item["enabled"])
            item["device_ids"] = devices_by_group.get(item["id"], [])
            item["destination_ids"] = destinations_by_group.get(item["id"], [])
        deliveries = [dict(row) for row in db.execute(
            """
            SELECT
                dl.*,
                dest.name destination_name,
                m.device_id,
                m.event_type,
                m.sender,
                m.sms_time,
                COALESCE(NULLIF(dev.name,''), NULLIF(dev.phone_number,''), dev.id) device_label,
                dev.phone_number device_phone,
                o.id outbound_sms_id,
                o.status outbound_status,
                o.recipient forward_recipient,
                o.device_id forward_device_id,
                o.sent_at forward_sent_at
            FROM deliveries dl
            JOIN destinations dest ON dest.id=dl.destination_id
            JOIN messages m ON m.id=dl.message_id
            JOIN devices dev ON dev.id=m.device_id
            LEFT JOIN outbound_sms o ON o.source_delivery_id=dl.id
            ORDER BY dl.id DESC LIMIT 400
            """
        ).fetchall()]
        outbound_sms = [dict(row) for row in db.execute(
            """
            SELECT
                o.*,
                COALESCE(NULLIF(d.name,''), NULLIF(d.phone_number,''), d.id)
                    device_label,
                d.phone_number device_phone,
                dest.name source_destination_name,
                fr.event_id feishu_reply_event_id,
                fr.inbound_message_id feishu_inbound_message_id,
                reply_dest.name feishu_reply_destination_name,
                fr.notified_status feishu_notified_status,
                fr.last_notification_error feishu_notification_error
            FROM outbound_sms o
            JOIN devices d ON d.id=o.device_id
            LEFT JOIN deliveries dl ON dl.id=o.source_delivery_id
            LEFT JOIN destinations dest ON dest.id=dl.destination_id
            LEFT JOIN feishu_sms_replies fr ON fr.outbound_sms_id=o.id
            LEFT JOIN destinations reply_dest ON reply_dest.id=fr.destination_id
            ORDER BY o.id DESC
            LIMIT 300
            """
        ).fetchall()]
        sms_templates = list_sms_templates(db)
        call_template = resolve_call_template_body(db)
        mcp_tokens = list_mcp_tokens(db)
        stats = {
            "devices": len(devices),
            "online": sum(1 for item in devices if item["online"]),
            "messages": db.execute(
                "SELECT COUNT(*) FROM messages WHERE event_type='sms'"
            ).fetchone()[0],
            "missed_calls": db.execute(
                "SELECT COUNT(*) FROM messages WHERE event_type='missed_call'"
            ).fetchone()[0],
            "pending": db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE status IN ('pending','retry','sending')"
            ).fetchone()[0],
            "failed": db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE status='failed'"
            ).fetchone()[0],
            "outbound_pending": db.execute(
                """
                SELECT COUNT(*) FROM outbound_sms
                WHERE status IN ('pending','sending')
                """
            ).fetchone()[0],
        }
    response = jsonify(
        {
            "code": 0,
            "stats": stats,
            "devices": devices,
            "messages": messages,
            "missed_calls": missed_calls,
            "destinations": destinations,
            "routes": routes,
            "channel_groups": channel_groups,
            "deliveries": deliveries,
            "outbound_sms": outbound_sms,
            "sms_templates": sms_templates,
            "call_template": call_template,
            "mcp_tokens": mcp_tokens,
            "offline_seconds": OFFLINE_SECONDS,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def queue_outbound_sms(
    device_id,
    recipient_value,
    body_value,
    mcp_request_id=None,
):
    device_id = text_value(device_id, 80)
    body = str(body_value or "").strip()
    if not device_id:
        raise ValueError("device_id is required")
    if not body:
        raise ValueError("body is required")
    if len(body) > MAX_OUTBOUND_SMS_LENGTH:
        raise ValueError("body is too long")
    recipient = normalize_recipient(recipient_value)
    request_id = None
    if mcp_request_id is not None:
        request_id = str(mcp_request_id or "").strip()
        if not request_id:
            raise ValueError("request_id is required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", request_id):
            raise ValueError(
                "request_id must contain 1-160 letters, digits, dots, "
                "underscores, colons, or hyphens"
            )

    now = utc_now()
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if request_id:
            existing = db.execute(
                """
                SELECT id, device_id, recipient, body, status, created_at
                FROM outbound_sms
                WHERE mcp_request_id=?
                """,
                (request_id,),
            ).fetchone()
            if existing:
                if (
                    existing["device_id"] != device_id
                    or existing["recipient"] != recipient
                    or existing["body"] != body
                ):
                    raise ValueError(
                        "request_id was already used with different SMS parameters"
                    )
                return {
                    "id": existing["id"],
                    "status": existing["status"],
                    "created_at": existing["created_at"],
                    "duplicate": True,
                }
        if not db.execute(
            "SELECT 1 FROM devices WHERE id=?",
            (device_id,),
        ).fetchone():
            raise LookupError("device not found")
        cursor = db.execute(
            """
            INSERT INTO outbound_sms
                (
                    device_id, mcp_request_id, recipient, body,
                    status, created_at
                )
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (device_id, request_id, recipient, body, now),
        )
    return {
        "id": cursor.lastrowid,
        "status": "pending",
        "created_at": now,
        "duplicate": False,
    }


@app.post("/api/admin/outbound-sms")
def create_outbound_sms():
    denied = require_admin()
    if denied:
        return denied
    data = request_json()
    try:
        queued = queue_outbound_sms(
            data.get("device_id"),
            data.get("recipient"),
            data.get("body"),
        )
        return jsonify({"code": 0, "id": queued["id"]})
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400
    except LookupError as exc:
        return jsonify({"code": 404, "message": str(exc)}), 404


@app.post("/api/admin/outbound-sms/<int:command_id>/retry")
def retry_outbound_sms(command_id):
    denied = require_admin()
    if denied:
        return denied
    with db_connect() as db:
        expire_stale_outbound(db)
        row = db.execute(
            "SELECT status, source_delivery_id FROM outbound_sms WHERE id=?",
            (command_id,),
        ).fetchone()
        if not row:
            return jsonify({"code": 404, "message": "outbound SMS not found"}), 404
        if row["status"] not in {"failed", "unknown"}:
            return jsonify(
                {
                    "code": 409,
                    "message": f"cannot retry an outbound SMS in {row['status']} state",
                }
            ), 409
        db.execute(
            """
            UPDATE outbound_sms
            SET status='pending', dispatched_at=NULL, dispatched_epoch=NULL,
                completed_at=NULL, sent_at=NULL, last_error=''
            WHERE id=?
            """,
            (command_id,),
        )
        if row["source_delivery_id"]:
            now = utc_now()
            db.execute(
                """
                UPDATE deliveries
                SET status='sending', attempts=attempts+1,
                    last_attempt_at=?, next_attempt_at=0, last_error=''
                WHERE id=?
                """,
                (now, row["source_delivery_id"]),
            )
            message = db.execute(
                "SELECT message_id FROM deliveries WHERE id=?",
                (row["source_delivery_id"],),
            ).fetchone()
            if message:
                refresh_message_status(db, message["message_id"])
        db.execute(
            """
            UPDATE feishu_sms_replies
            SET next_notification_at=0, notification_attempts=0
            WHERE outbound_sms_id=?
            """,
            (command_id,),
        )
    return jsonify({"code": 0})


@app.delete("/api/admin/outbound-sms/<int:command_id>")
def delete_outbound_sms(command_id):
    denied = require_admin()
    if denied:
        return denied
    with db_connect() as db:
        linked = db.execute(
            """
            SELECT source_delivery_id
            FROM outbound_sms
            WHERE id=? AND status='pending'
            """,
            (command_id,),
        ).fetchone()
        cursor = db.execute(
            "DELETE FROM outbound_sms WHERE id=? AND status='pending'",
            (command_id,),
        )
        if cursor.rowcount:
            if linked and linked["source_delivery_id"]:
                db.execute(
                    """
                    UPDATE deliveries
                    SET status='failed',
                        last_error='SMS forwarding job was deleted before dispatch'
                    WHERE id=?
                    """,
                    (linked["source_delivery_id"],),
                )
                message = db.execute(
                    "SELECT message_id FROM deliveries WHERE id=?",
                    (linked["source_delivery_id"],),
                ).fetchone()
                if message:
                    refresh_message_status(db, message["message_id"])
            return jsonify({"code": 0})
        row = db.execute(
            "SELECT status FROM outbound_sms WHERE id=?",
            (command_id,),
        ).fetchone()
    if not row:
        return jsonify({"code": 404, "message": "outbound SMS not found"}), 404
    return jsonify(
        {
            "code": 409,
            "message": f"cannot delete an outbound SMS in {row['status']} state",
        }
    ), 409


@app.patch("/api/admin/devices/<device_id>")
def update_device(device_id):
    denied = require_admin()
    if denied:
        return denied
    data = request_json()
    fields = {}
    for key, limit in (("name", 120), ("phone_number", 40)):
        if key in data:
            fields[key] = text_value(data[key], limit)
    for key in ("sms_forward_enabled", "call_forward_enabled"):
        if key in data:
            if type(data[key]) is not bool:
                return jsonify({"code": 400, "message": key + " must be boolean"}), 400
            fields[key] = int(data[key])
    with db_connect() as db:
        if not db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
            return jsonify({"code": 404, "message": "device not found"}), 404
        if fields:
            db.execute("UPDATE devices SET " + ", ".join(key + "=?" for key in fields) + " WHERE id=?", (*fields.values(), device_id))
    return jsonify({"code": 0})


def resolve_call_template_body(db):
    row = db.execute("SELECT body FROM call_template WHERE id=1").fetchone()
    return row["body"] if row else DEFAULT_CALL_TEMPLATE_BODY


@app.put("/api/admin/call-template")
def save_call_template():
    denied = require_admin()
    if denied:
        return denied
    try:
        body = validate_sms_template_body(request_json().get("body"))
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400
    with db_connect() as db:
        db.execute("UPDATE call_template SET body=? WHERE id=1", (body,))
    return jsonify({"code": 0})


DESTINATION_FIELDS = {
    "feishu_app": ("app_id", "app_secret", "receive_id"),
    "feishu_webhook": ("url",),
    "wecom_webhook": ("url",),
    "sms_forward": ("recipient",),
    "webhook": ("url",),
}


def validate_destination(kind, config, db=None):
    if kind not in DESTINATION_FIELDS:
        raise ValueError("unsupported destination kind")
    missing = [name for name in DESTINATION_FIELDS[kind] if not text_value(config.get(name))]
    if missing:
        raise ValueError("missing: " + ", ".join(missing))
    template_id = None
    if kind in TEMPLATED_DESTINATION_KINDS:
        template_id = config.get("template_id")
        if template_id in (None, ""):
            config.pop("template_id", None)
            template_id = None
        else:
            try:
                template_id = int(template_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("template_id must be an integer") from exc
            if template_id < 1:
                raise ValueError("template_id must be a positive integer")
            config["template_id"] = template_id
        if db is not None and template_id and not db.execute(
            "SELECT 1 FROM sms_templates WHERE id=?",
            (template_id,),
        ).fetchone():
            raise ValueError("template_id does not match an SMS template")
    if kind == "sms_forward":
        config["recipient"] = normalize_recipient(config["recipient"])
        sender_device_id = text_value(config.get("sender_device_id"), 80)
        config["sender_device_id"] = sender_device_id
        if db is not None:
            if sender_device_id and not db.execute(
                "SELECT 1 FROM devices WHERE id=?",
                (sender_device_id,),
            ).fetchone():
                raise ValueError("sender_device_id does not match a registered device")
            ensure_sms_forward_target_is_external(db, config["recipient"])


@app.post("/api/admin/sms-templates")
def save_sms_template():
    denied = require_admin()
    if denied:
        return denied
    data = request_json()
    template_id = data.get("id")
    name = text_value(data.get("name"), 120)
    if not name:
        return jsonify({"code": 400, "message": "name is required"}), 400
    try:
        body = validate_sms_template_body(data.get("body"))
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400
    is_default = bool(data.get("is_default"))
    now = utc_now()
    try:
        with db_connect() as db:
            if template_id:
                try:
                    template_id = int(template_id)
                except (TypeError, ValueError):
                    return jsonify(
                        {"code": 400, "message": "id must be an integer"}
                    ), 400
                if not db.execute(
                    "SELECT 1 FROM sms_templates WHERE id=?",
                    (template_id,),
                ).fetchone():
                    return jsonify(
                        {"code": 404, "message": "SMS template not found"}
                    ), 404
                if is_default:
                    db.execute(
                        "UPDATE sms_templates SET is_default=0 WHERE is_default=1"
                    )
                db.execute(
                    """
                    UPDATE sms_templates
                    SET name=?, body=?, is_default=?, updated_at=?
                    WHERE id=?
                    """,
                    (name, body, 1 if is_default else 0, now, template_id),
                )
            else:
                if is_default:
                    db.execute(
                        "UPDATE sms_templates SET is_default=0 WHERE is_default=1"
                    )
                cursor = db.execute(
                    """
                    INSERT INTO sms_templates (
                        name, body, is_default, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, body, 1 if is_default else 0, now, now),
                )
                template_id = cursor.lastrowid
            if not db.execute(
                "SELECT 1 FROM sms_templates WHERE is_default=1"
            ).fetchone():
                db.execute(
                    """
                    UPDATE sms_templates
                    SET is_default=1, updated_at=?
                    WHERE id=(SELECT MIN(id) FROM sms_templates)
                    """,
                    (now,),
                )
        return jsonify({"code": 0, "id": template_id})
    except sqlite3.IntegrityError:
        return jsonify(
            {"code": 409, "message": "SMS template name already exists"}
        ), 409


@app.post("/api/admin/sms-templates/<int:template_id>/default")
def set_default_sms_template(template_id):
    denied = require_admin()
    if denied:
        return denied
    with db_connect() as db:
        if not db.execute(
            "SELECT 1 FROM sms_templates WHERE id=?",
            (template_id,),
        ).fetchone():
            return jsonify(
                {"code": 404, "message": "SMS template not found"}
            ), 404
        db.execute("UPDATE sms_templates SET is_default=0 WHERE is_default=1")
        db.execute(
            "UPDATE sms_templates SET is_default=1, updated_at=? WHERE id=?",
            (utc_now(), template_id),
        )
    return jsonify({"code": 0})


@app.delete("/api/admin/sms-templates/<int:template_id>")
def delete_sms_template(template_id):
    denied = require_admin()
    if denied:
        return denied
    with db_connect() as db:
        template = db.execute(
            "SELECT is_default FROM sms_templates WHERE id=?",
            (template_id,),
        ).fetchone()
        if not template:
            return jsonify(
                {"code": 404, "message": "SMS template not found"}
            ), 404
        if template["is_default"]:
            return jsonify(
                {
                    "code": 409,
                    "message": "set another default template before deleting this one",
                }
            ), 409
        for row in db.execute(
            """
            SELECT id, config_json
            FROM destinations
            WHERE kind IN ('sms_forward', 'feishu_app', 'feishu_webhook')
            """
        ).fetchall():
            config = json.loads(row["config_json"] or "{}")
            if config.get("template_id") == template_id:
                return jsonify(
                    {
                        "code": 409,
                            "message": "SMS template is used by a destination",
                    }
                ), 409
        db.execute("DELETE FROM sms_templates WHERE id=?", (template_id,))
    return jsonify({"code": 0})


@app.post("/api/admin/destinations")
def save_destination():
    denied = require_admin()
    if denied:
        return denied
    data = request_json()
    destination_id = data.get("id")
    name = text_value(data.get("name"), 120)
    kind = text_value(data.get("kind"), 40)
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    if not name:
        return jsonify({"code": 400, "message": "name is required"}), 400
    now = utc_now()
    try:
        with db_connect() as db:
            if destination_id:
                existing = db.execute(
                    "SELECT config_json FROM destinations WHERE id=?", (destination_id,)
                ).fetchone()
                if not existing:
                    return jsonify({"code": 404, "message": "destination not found"}), 404
                old_config = json.loads(existing["config_json"] or "{}")
                for secret_field in (
                    "app_secret",
                    "verification_token",
                    "headers",
                ):
                    if config.get(secret_field) in (None, "", "••••••••"):
                        if secret_field in old_config:
                            config[secret_field] = old_config[secret_field]
                validate_destination(kind, config, db)
                db.execute(
                    """
                    UPDATE destinations
                    SET name=?, kind=?, config_json=?, enabled=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        name,
                        kind,
                        json.dumps(config, ensure_ascii=False),
                        1 if data.get("enabled", True) else 0,
                        now,
                        destination_id,
                    ),
                )
            else:
                validate_destination(kind, config, db)
                cursor = db.execute(
                    """
                    INSERT INTO destinations
                        (name, kind, config_json, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        kind,
                        json.dumps(config, ensure_ascii=False),
                        1 if data.get("enabled", True) else 0,
                        now,
                        now,
                    ),
                )
                destination_id = cursor.lastrowid
        return jsonify({"code": 0, "id": destination_id})
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400


@app.delete("/api/admin/destinations/<int:destination_id>")
def delete_destination(destination_id):
    denied = require_admin()
    if denied:
        return denied
    with db_connect() as db:
        cursor = db.execute("DELETE FROM destinations WHERE id=?", (destination_id,))
    if cursor.rowcount == 0:
        return jsonify({"code": 404, "message": "destination not found"}), 404
    return jsonify({"code": 0})


@app.post("/api/admin/routes")
def save_route():
    denied = require_admin()
    if denied:
        return denied
    data = request_json()
    device_id = text_value(data.get("device_id") or "*", 80)
    try:
        destination_id = int(data.get("destination_id"))
    except (TypeError, ValueError):
        return jsonify({"code": 400, "message": "destination_id is required"}), 400
    with db_connect() as db:
        try:
            cursor = db.execute(
                """
                INSERT INTO routes (device_id, destination_id, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id, destination_id)
                DO UPDATE SET enabled=excluded.enabled
                """,
                (device_id, destination_id, 1 if data.get("enabled", True) else 0),
            )
        except sqlite3.IntegrityError:
            return jsonify({"code": 404, "message": "destination not found"}), 404
    return jsonify({"code": 0, "id": cursor.lastrowid})


@app.delete("/api/admin/routes/<int:route_id>")
def delete_route(route_id):
    denied = require_admin()
    if denied:
        return denied
    with db_connect() as db:
        cursor = db.execute("DELETE FROM routes WHERE id=?", (route_id,))
    if cursor.rowcount == 0:
        return jsonify({"code": 404, "message": "route not found"}), 404
    return jsonify({"code": 0})


def unique_values(values, converter):
    if not isinstance(values, list):
        return []
    result = []
    seen = set()
    for value in values:
        converted = converter(value)
        if converted in seen:
            continue
        seen.add(converted)
        result.append(converted)
    return result


def require_existing_values(db, table, column, values, label):
    if not values:
        return
    placeholders = ",".join("?" for _ in values)
    rows = db.execute(
        f"SELECT {column} FROM {table} WHERE {column} IN ({placeholders})",
        values,
    ).fetchall()
    found = {row[column] for row in rows}
    missing = [str(value) for value in values if value not in found]
    if missing:
        raise ValueError(f"unknown {label}: {', '.join(missing)}")


@app.post("/api/admin/channel-groups")
def save_channel_group():
    denied = require_admin()
    if denied:
        return denied
    data = request_json()
    name = text_value(data.get("name"), 120)
    if not name:
        return jsonify({"code": 400, "message": "name is required"}), 400
    description = text_value(data.get("description"), 500)
    try:
        group_id = int(data["id"]) if data.get("id") else None
        device_ids = unique_values(
            data.get("device_ids"),
            lambda value: text_value(value, 80),
        )
        device_ids = [value for value in device_ids if value]
        destination_ids = unique_values(data.get("destination_ids"), int)
        now = utc_now()
        with db_connect() as db:
            require_existing_values(db, "devices", "id", device_ids, "device")
            require_existing_values(
                db,
                "destinations",
                "id",
                destination_ids,
                "destination",
            )
            if group_id:
                cursor = db.execute(
                    """
                    UPDATE channel_groups
                    SET name=?, description=?, enabled=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        name,
                        description,
                        1 if data.get("enabled", True) else 0,
                        now,
                        group_id,
                    ),
                )
                if cursor.rowcount == 0:
                    return jsonify(
                        {"code": 404, "message": "channel group not found"}
                    ), 404
            else:
                cursor = db.execute(
                    """
                    INSERT INTO channel_groups
                        (name, description, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        description,
                        1 if data.get("enabled", True) else 0,
                        now,
                        now,
                    ),
                )
                group_id = cursor.lastrowid

            db.execute(
                "DELETE FROM channel_group_devices WHERE group_id=?",
                (group_id,),
            )
            db.execute(
                "DELETE FROM channel_group_destinations WHERE group_id=?",
                (group_id,),
            )
            db.executemany(
                """
                INSERT INTO channel_group_devices (group_id, device_id)
                VALUES (?, ?)
                """,
                [(group_id, device_id) for device_id in device_ids],
            )
            db.executemany(
                """
                INSERT INTO channel_group_destinations (group_id, destination_id)
                VALUES (?, ?)
                """,
                [
                    (group_id, destination_id)
                    for destination_id in destination_ids
                ],
            )
        return jsonify({"code": 0, "id": group_id})
    except (TypeError, ValueError) as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400
    except sqlite3.IntegrityError as exc:
        if "channel_groups.name" in str(exc):
            return jsonify(
                {"code": 409, "message": "channel group name already exists"}
            ), 409
        log.warning("channel group save failed: %s", exc)
        return jsonify({"code": 409, "message": "channel group conflict"}), 409


@app.delete("/api/admin/channel-groups/<int:group_id>")
def delete_channel_group(group_id):
    denied = require_admin()
    if denied:
        return denied
    with db_connect() as db:
        cursor = db.execute("DELETE FROM channel_groups WHERE id=?", (group_id,))
    if cursor.rowcount == 0:
        return jsonify({"code": 404, "message": "channel group not found"}), 404
    return jsonify({"code": 0})


@app.post("/api/admin/deliveries/<int:delivery_id>/retry")
def retry_delivery(delivery_id):
    denied = require_admin()
    if denied:
        return denied
    with db_connect() as db:
        cursor = db.execute(
            """
            UPDATE deliveries
            SET status='retry', next_attempt_at=0, last_error=''
            WHERE id=?
            """,
            (delivery_id,),
        )
    if cursor.rowcount == 0:
        return jsonify({"code": 404, "message": "delivery not found"}), 404
    return jsonify({"code": 0})


def feishu_success(response):
    if not 200 <= response.status_code < 300:
        return False, f"http={response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return False, "invalid JSON response"
    if payload.get("code") == 0:
        return True, ""
    return False, f"feishu={payload.get('code')} {payload.get('msg', '')}".strip()


def feishu_tenant_token(config):
    timeout = max(5, min(60, int(config.get("timeout_seconds", 30))))
    auth = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": config["app_id"], "app_secret": config["app_secret"]},
        timeout=timeout,
    )
    ok, error = feishu_success(auth)
    if not ok:
        return None, auth.status_code, error, auth.text[:500]
    tenant_token = auth.json().get("tenant_access_token")
    if not tenant_token:
        return None, auth.status_code, "tenant token missing", auth.text[:500]
    return tenant_token, auth.status_code, "", auth.text[:500]


def send_feishu_reply_with_token(
    config, tenant_token, message_id, text, request_uuid
):
    timeout = max(5, min(60, int(config.get("timeout_seconds", 30))))
    response = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages/"
        f"{message_id}/reply",
        headers={"Authorization": f"Bearer {tenant_token}"},
        json={
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
            "uuid": request_uuid[:50],
        },
        timeout=timeout,
    )
    ok, error = feishu_success(response)
    return ok, response.status_code, error, response.text[:500]


def add_feishu_reaction_with_token(
    config, tenant_token, message_id, emoji_type="OK"
):
    timeout = max(5, min(60, int(config.get("timeout_seconds", 30))))
    response = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages/"
        f"{message_id}/reactions",
        headers={"Authorization": f"Bearer {tenant_token}"},
        json={"reaction_type": {"emoji_type": emoji_type}},
        timeout=timeout,
    )
    ok, error = feishu_success(response)
    return ok, response.status_code, error, response.text[:500]


def send_feishu_reply(config, message_id, text, request_uuid):
    tenant_token, status, error, excerpt = feishu_tenant_token(config)
    if not tenant_token:
        return False, status, error, excerpt
    return send_feishu_reply_with_token(
        config,
        tenant_token,
        message_id,
        text,
        request_uuid,
    )


def add_feishu_reaction(config, message_id, emoji_type="OK"):
    tenant_token, status, error, excerpt = feishu_tenant_token(config)
    if not tenant_token:
        return False, status, error, excerpt
    return add_feishu_reaction_with_token(
        config,
        tenant_token,
        message_id,
        emoji_type,
    )


def acknowledge_feishu_reply(config, message_id):
    tenant_token, status, error, excerpt = feishu_tenant_token(config)
    if not tenant_token:
        return False, status, error, excerpt
    request_uuid = "sms-ack-" + hashlib.sha256(
        message_id.encode("utf-8")
    ).hexdigest()[:32]
    ok, status, error, excerpt = send_feishu_reply_with_token(
        config,
        tenant_token,
        message_id,
        "已读，正在处理。",
        request_uuid,
    )
    if not ok:
        return False, status, error, excerpt
    return add_feishu_reaction_with_token(
        config,
        tenant_token,
        message_id,
        "OK",
    )


def wecom_success(response):
    if not 200 <= response.status_code < 300:
        return False, f"http={response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return False, "invalid JSON response"
    if payload.get("errcode") == 0:
        return True, ""
    return False, (
        f"wecom={payload.get('errcode')} {payload.get('errmsg', '')}".strip()
    )


def deliver(destination, message, formatted_message=None):
    config = json.loads(destination["config_json"] or "{}")
    kind = destination["kind"]
    formatted = (
        formatted_message
        if formatted_message is not None
        else formatted_feishu_message(message)
    )
    timeout = max(5, min(60, int(config.get("timeout_seconds", 30))))

    if kind == "feishu_app":
        auth = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": config["app_id"], "app_secret": config["app_secret"]},
            timeout=timeout,
        )
        ok, error = feishu_success(auth)
        if not ok:
            return False, auth.status_code, error, auth.text[:500]
        tenant_token = auth.json().get("tenant_access_token")
        if not tenant_token:
            return False, auth.status_code, "tenant token missing", auth.text[:500]
        receive_type = config.get("receive_id_type") or "chat_id"
        response = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": receive_type},
            headers={"Authorization": f"Bearer {tenant_token}"},
            json={
                "receive_id": config["receive_id"],
                "msg_type": "text",
                "content": json.dumps({"text": formatted}, ensure_ascii=False),
            },
            timeout=timeout,
        )
        ok, error = feishu_success(response)
        return ok, response.status_code, error, response.text[:500]

    if kind == "feishu_webhook":
        response = requests.post(
            config["url"],
            json={"msg_type": "text", "content": {"text": formatted}},
            timeout=timeout,
        )
        ok, error = feishu_success(response)
        return ok, response.status_code, error, response.text[:500]

    if kind == "wecom_webhook":
        response = requests.post(
            config["url"],
            json={"msgtype": "text", "text": {"content": formatted}},
            timeout=timeout,
        )
        ok, error = wecom_success(response)
        return ok, response.status_code, error, response.text[:500]

    headers = config.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    event_type = record_value(message, "event_type", "sms")
    if event_type == "missed_call":
        try:
            metadata = json.loads(record_value(message, "metadata_json", "{}") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        event_name = "call.missed"
        event_payload = {
            "id": message["id"],
            "device_id": message["device_id"],
            "device_name": message["device_label"],
            "device_phone": record_value(message, "device_phone", ""),
            "caller": message["sender"],
            "started_at": message["sms_time"],
            "ended_at": metadata.get("ended_at", ""),
            "duration_seconds": metadata.get("duration_seconds", 0),
            "received_at": message["received_at"],
        }
    else:
        event_name = "sms.received"
        event_payload = {
            "id": message["id"],
            "device_id": message["device_id"],
            "device_name": message["device_label"],
            "sender": message["sender"],
            "sms_time": message["sms_time"],
            "body": message["body"],
            "received_at": message["received_at"],
        }
    response = requests.post(
        config["url"],
        headers=headers,
        json={
            "event": event_name,
            "message": event_payload,
        },
        timeout=timeout,
    )
    ok = 200 <= response.status_code < 300
    return ok, response.status_code, "" if ok else f"http={response.status_code}", response.text[:500]


def feishu_notification_text(row, status):
    if status == "queued":
        return (
            f"SMS #{row['outbound_sms_id']} 已加入发送队列\n"
            f"收件号码：{row['recipient']}\n"
            f"发送设备：{row['device_label']}"
        )
    if status == "sent":
        return (
            f"SMS #{row['outbound_sms_id']} 发送成功\n"
            f"发送时间：{row['sent_at'] or row['completed_at'] or utc_now()}"
        )
    if status == "unknown":
        return (
            f"SMS #{row['outbound_sms_id']} 状态未知\n"
            "请先向收件人核实，再决定是否手动重试。"
        )
    return (
        f"SMS #{row['outbound_sms_id']} 发送失败\n"
        f"原因：{text_value(row['last_error'] or 'device send failed', 180)}"
    )


def process_feishu_reply_acknowledgement():
    with db_connect() as db:
        row = db.execute(
            """
            SELECT
                inbox.*,
                destination.config_json,
                message.device_id,
                message.sender
            FROM feishu_reply_inbox inbox
            JOIN destinations destination ON destination.id=inbox.destination_id
            JOIN deliveries delivery ON delivery.id=inbox.source_delivery_id
            JOIN messages message ON message.id=delivery.message_id
            WHERE inbox.status='pending'
              AND inbox.next_acknowledgement_at<=?
            ORDER BY inbox.created_at
            LIMIT 1
            """,
            (time.time(),),
        ).fetchone()
        if not row:
            return False
        try:
            recipient = normalize_recipient(row["sender"])
            ensure_sms_forward_target_is_external(db, recipient)
            if not db.execute(
                "SELECT 1 FROM devices WHERE id=?",
                (row["device_id"],),
            ).fetchone():
                raise ValueError("source device is unavailable")
            preparation_error = ""
        except (ValueError, sqlite3.IntegrityError) as exc:
            preparation_error = str(exc)

    if preparation_error:
        ok, error = False, preparation_error
    else:
        config = json.loads(row["config_json"] or "{}")
        try:
            ok, _, error, _ = acknowledge_feishu_reply(
                config,
                row["inbound_message_id"],
            )
        except Exception as exc:
            ok, error = False, str(exc)

    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            """
            SELECT * FROM feishu_reply_inbox
            WHERE inbound_message_id=? AND status='pending'
            """,
            (row["inbound_message_id"],),
        ).fetchone()
        if not current:
            return True
        if not ok:
            attempts = current["acknowledgement_attempts"] + 1
            delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
            db.execute(
                """
                UPDATE feishu_reply_inbox
                SET acknowledgement_attempts=?, next_acknowledgement_at=?,
                    last_acknowledgement_error=?
                WHERE inbound_message_id=?
                """,
                (
                    attempts,
                    time.time() + delay,
                    text_value(error, 500),
                    current["inbound_message_id"],
                ),
            )
            return True

        existing = db.execute(
            """
            SELECT outbound_sms_id
            FROM feishu_sms_replies
            WHERE event_id=? OR inbound_message_id=?
            """,
            (current["event_id"], current["inbound_message_id"]),
        ).fetchone()
        if existing:
            outbound_sms_id = existing["outbound_sms_id"]
        else:
            cursor = db.execute(
                """
                INSERT INTO outbound_sms (
                    device_id, recipient, body, status, created_at
                ) VALUES (?, ?, ?, 'pending', ?)
                """,
                (
                    row["device_id"],
                    recipient,
                    current["reply_body"],
                    utc_now(),
                ),
            )
            outbound_sms_id = cursor.lastrowid
            db.execute(
                """
                INSERT INTO feishu_sms_replies (
                    event_id, destination_id, source_delivery_id,
                    inbound_message_id, sender_open_id, outbound_sms_id,
                    notified_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    current["event_id"],
                    current["destination_id"],
                    current["source_delivery_id"],
                    current["inbound_message_id"],
                    current["sender_open_id"],
                    outbound_sms_id,
                    utc_now(),
                ),
            )
        db.execute(
            """
            UPDATE feishu_reply_inbox
            SET status='queued', acknowledgement_attempts=0,
                next_acknowledgement_at=0,
                last_acknowledgement_error='', acknowledged_at=?,
                outbound_sms_id=?
            WHERE inbound_message_id=?
            """,
            (utc_now(), outbound_sms_id, current["inbound_message_id"]),
        )
    return True


def process_feishu_notification():
    with db_connect() as db:
        row = db.execute(
            """
            SELECT
                fr.event_id,
                fr.inbound_message_id,
                fr.outbound_sms_id,
                fr.notification_attempts,
                fr.notified_status,
                o.status outbound_status,
                o.recipient,
                o.sent_at,
                o.completed_at,
                o.last_error,
                d.config_json,
                COALESCE(NULLIF(dev.name,''), NULLIF(dev.phone_number,''), dev.id)
                    device_label
            FROM feishu_sms_replies fr
            JOIN outbound_sms o ON o.id=fr.outbound_sms_id
            JOIN destinations d ON d.id=fr.destination_id
            JOIN devices dev ON dev.id=o.device_id
            WHERE fr.next_notification_at<=?
              AND (
                  (
                      o.status IN ('pending','sending')
                      AND fr.notified_status<>'queued'
                  )
                  OR (
                      o.status IN ('sent','failed','unknown')
                      AND fr.notified_status<>o.status
                  )
              )
            ORDER BY fr.created_at
            LIMIT 1
            """,
            (time.time(),),
        ).fetchone()
    if not row:
        return False

    desired_status = (
        "queued"
        if row["outbound_status"] in {"pending", "sending"}
        else row["outbound_status"]
    )
    config = json.loads(row["config_json"] or "{}")
    try:
        if desired_status == "queued":
            ok, _, error, _ = add_feishu_reaction(
                config,
                row["inbound_message_id"],
                "OK",
            )
        else:
            ok, _, error, _ = send_feishu_reply(
                config,
                row["inbound_message_id"],
                feishu_notification_text(row, desired_status),
                f"sms-{row['outbound_sms_id']}-{desired_status}",
            )
    except Exception as exc:
        ok, error = False, str(exc)

    with db_connect() as db:
        current = db.execute(
            """
            SELECT notification_attempts
            FROM feishu_sms_replies
            WHERE event_id=?
            """,
            (row["event_id"],),
        ).fetchone()
        if not current:
            return True
        if ok:
            db.execute(
                """
                UPDATE feishu_sms_replies
                SET notified_status=?, notification_attempts=0,
                    next_notification_at=0, last_notification_error=''
                WHERE event_id=?
                """,
                (desired_status, row["event_id"]),
            )
        else:
            attempts = current["notification_attempts"] + 1
            delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
            db.execute(
                """
                UPDATE feishu_sms_replies
                SET notification_attempts=?, next_notification_at=?,
                    last_notification_error=?
                WHERE event_id=?
                """,
                (
                    attempts,
                    time.time() + delay,
                    text_value(error, 500),
                    row["event_id"],
                ),
            )
    return True


def refresh_message_status(db, message_id):
    rows = db.execute(
        "SELECT status FROM deliveries WHERE message_id=?", (message_id,)
    ).fetchall()
    statuses = {row["status"] for row in rows}
    if not statuses:
        status = "stored"
    elif statuses == {"delivered"}:
        status = "delivered"
    elif "failed" in statuses and not statuses.intersection({"pending", "retry", "sending"}):
        status = "failed"
    elif "delivered" in statuses:
        status = "partial"
    else:
        status = "queued"
    db.execute("UPDATE messages SET status=? WHERE id=?", (status, message_id))


def forwarded_sms_content(
    db, message, template_id=None, current_device_phone=None
):
    if record_value(message, "event_type", "sms") == "missed_call":
        content = render_sms_template(resolve_call_template_body(db), message, current_device_phone)
        if len(content) > MAX_OUTBOUND_SMS_LENGTH:
            raise ValueError(f"forwarded SMS exceeds {MAX_OUTBOUND_SMS_LENGTH} characters")
        return content
    content = render_sms_template(
        resolve_sms_template_body(db, template_id),
        message,
        current_device_phone=current_device_phone,
    )
    if len(content) > MAX_OUTBOUND_SMS_LENGTH:
        raise ValueError(
            f"forwarded SMS exceeds {MAX_OUTBOUND_SMS_LENGTH} characters"
        )
    return content


def enqueue_sms_forward(db, delivery):
    config = json.loads(delivery["config_json"] or "{}")
    recipient = normalize_recipient(config.get("recipient"))
    ensure_sms_forward_target_is_external(db, recipient)
    sender_device_id = (
        text_value(config.get("sender_device_id"), 80) or delivery["device_id"]
    )
    sender_device = db.execute(
        "SELECT phone_number FROM devices WHERE id=?",
        (sender_device_id,),
    ).fetchone()
    if not sender_device:
        raise ValueError("SMS forwarding sender device is not registered")

    now = utc_now()
    existing = db.execute(
        """
        SELECT id, status, last_error, sent_at, completed_at
        FROM outbound_sms
        WHERE source_delivery_id=?
        """,
        (delivery["id"],),
    ).fetchone()
    if existing:
        if existing["status"] == "sent":
            db.execute(
                """
                UPDATE deliveries
                SET status='delivered',
                    delivered_at=COALESCE(delivered_at, ?, ?),
                    next_attempt_at=0, last_error=''
                WHERE id=?
                """,
                (
                    existing["sent_at"],
                    existing["completed_at"],
                    delivery["id"],
                ),
            )
        elif existing["status"] in {"failed", "unknown"}:
            if delivery["status"] == "retry":
                db.execute(
                    """
                    UPDATE outbound_sms
                    SET status='pending', dispatched_at=NULL,
                        dispatched_epoch=NULL, completed_at=NULL,
                        sent_at=NULL, last_error=''
                    WHERE id=?
                    """,
                    (existing["id"],),
                )
                db.execute(
                    """
                    UPDATE deliveries
                    SET status='sending', attempts=attempts+1,
                        last_attempt_at=?, next_attempt_at=0, last_error=''
                    WHERE id=?
                    """,
                    (now, delivery["id"]),
                )
            else:
                db.execute(
                    """
                    UPDATE deliveries
                    SET status='failed', next_attempt_at=0, last_error=?
                    WHERE id=?
                    """,
                    (
                        f"SMS forwarding {existing['status']}: "
                        f"{existing['last_error']}",
                        delivery["id"],
                    ),
                )
        else:
            db.execute(
                """
                UPDATE deliveries
                SET status='sending', next_attempt_at=0, last_error=''
                WHERE id=?
                """,
                (delivery["id"],),
            )
        return existing["id"]

    cursor = db.execute(
        """
        INSERT INTO outbound_sms (
            device_id, source_delivery_id, recipient, body, status, created_at
        ) VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (
            sender_device_id,
            delivery["id"],
            recipient,
            forwarded_sms_content(
                db,
                delivery,
                config.get("template_id"),
                sender_device["phone_number"],
            ),
            now,
        ),
    )
    db.execute(
        """
        UPDATE deliveries
        SET status='sending', attempts=attempts+1, last_attempt_at=?,
            next_attempt_at=0, last_error=''
        WHERE id=?
        """,
        (now, delivery["id"]),
    )
    return cursor.lastrowid


def process_delivery(delivery_id):
    with db_connect() as db:
        row = db.execute(
            """
            SELECT dl.*, d.kind, d.config_json, d.enabled,
                   m.device_id, m.event_type, m.sender, m.sms_time, m.body,
                   m.metadata_json, m.received_at,
                   dev.phone_number device_phone,
                   COALESCE(NULLIF(dev.name,''), NULLIF(dev.phone_number,''), dev.id)
                       device_label
            FROM deliveries dl
            JOIN destinations d ON d.id=dl.destination_id
            JOIN messages m ON m.id=dl.message_id
            JOIN devices dev ON dev.id=m.device_id
            WHERE dl.id=?
            """,
            (delivery_id,),
        ).fetchone()
        if not row:
            return
        formatted_message = None
        if not row["enabled"]:
            db.execute(
                "UPDATE deliveries SET status='failed', last_error='destination disabled' WHERE id=?",
                (delivery_id,),
            )
            refresh_message_status(db, row["message_id"])
            return
        if row["kind"] == "sms_forward":
            try:
                enqueue_sms_forward(db, row)
            except (ValueError, sqlite3.IntegrityError) as exc:
                db.execute(
                    """
                    UPDATE deliveries
                    SET status='failed', attempts=attempts+1,
                        last_attempt_at=?, next_attempt_at=0, last_error=?
                    WHERE id=?
                    """,
                    (utc_now(), text_value(exc, 500), delivery_id),
                )
            refresh_message_status(db, row["message_id"])
            return
        if row["kind"] in {"feishu_app", "feishu_webhook"} or row["event_type"] == "missed_call":
            config = json.loads(row["config_json"] or "{}")
            formatted_message = formatted_feishu_message(
                row,
                resolve_call_template_body(db) if row["event_type"] == "missed_call" else resolve_sms_template_body(db, config.get("template_id")),
            )
        db.execute(
            "UPDATE deliveries SET status='sending', last_attempt_at=? WHERE id=?",
            (utc_now(), delivery_id),
        )
    try:
        ok, http_status, error, excerpt = deliver(
            row,
            row,
            formatted_message=formatted_message,
        )
    except Exception as exc:
        ok, http_status, error, excerpt = False, None, str(exc), ""
        log.warning("delivery %s failed: %s", delivery_id, exc)

    with db_connect() as db:
        current = db.execute(
            "SELECT attempts, message_id FROM deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
        if not current:
            return
        attempts = current["attempts"] + 1
        if ok:
            feishu_message_id = None
            feishu_chat_id = None
            if row["kind"] == "feishu_app":
                feishu_message_id, feishu_chat_id = feishu_response_reference(
                    excerpt
                )
                config = json.loads(row["config_json"] or "{}")
                if (
                    not feishu_chat_id
                    and config.get("receive_id_type", "chat_id") == "chat_id"
                ):
                    feishu_chat_id = (
                        text_value(config.get("receive_id"), 160) or None
                    )
            db.execute(
                """
                UPDATE deliveries SET status='delivered', attempts=?, delivered_at=?,
                    next_attempt_at=0, last_error='', http_status=?,
                    response_excerpt=?, feishu_message_id=?, feishu_chat_id=?
                WHERE id=?
                """,
                (
                    attempts,
                    utc_now(),
                    http_status,
                    excerpt,
                    feishu_message_id,
                    feishu_chat_id,
                    delivery_id,
                ),
            )
        else:
            delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
            db.execute(
                """
                UPDATE deliveries SET status='retry', attempts=?, next_attempt_at=?,
                    last_error=?, http_status=?, response_excerpt=?
                WHERE id=?
                """,
                (
                    attempts,
                    time.time() + delay,
                    text_value(error, 500),
                    http_status,
                    text_value(excerpt, 500),
                    delivery_id,
                ),
            )
        refresh_message_status(db, current["message_id"])


def web_push_payload(row):
    event_type = row["event_type"]
    device_label = row["device_label"] or row["device_id"]
    sender = row["sender"] or "未知号码"
    if event_type == "missed_call":
        title = f"未接来电 · {device_label}"
        body = f"来电号码：{sender}"
        page = "calls"
    else:
        title = f"收到短信 · {device_label}"
        message_body = str(row["body"] or "").strip()
        body = f"{sender}\n{message_body}" if message_body else sender
        page = "messages"
    return {
        "title": title[:120],
        "body": body[:800],
        "tag": f"sms-center-{event_type}-{row['message_id']}",
        "url": f"./#{page}",
        "event_type": event_type,
        "message_id": row["message_id"],
    }


def send_web_push_subscription(subscription, payload):
    private_key, _ = web_push_vapid_config()
    return webpush(
        subscription_info={
            "endpoint": subscription["endpoint"],
            "keys": {
                "p256dh": subscription["p256dh"],
                "auth": subscription["auth"],
            },
        },
        data=json.dumps(payload, ensure_ascii=False),
        vapid_private_key=private_key,
        vapid_claims={"sub": VAPID_SUBJECT},
        ttl=WEB_PUSH_TTL_SECONDS,
        timeout=10,
    )


def web_push_exception_status(exc):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def process_web_push_delivery(delivery_id):
    with db_connect() as db:
        row = db.execute(
            """
            SELECT
                push.id,
                push.subscription_id,
                push.message_id,
                push.status,
                push.attempts,
                subscription.endpoint,
                subscription.p256dh,
                subscription.auth,
                subscription.notify_sms,
                subscription.notify_missed_call,
                message.device_id,
                message.event_type,
                message.sender,
                message.body,
                COALESCE(
                    NULLIF(device.name,''),
                    NULLIF(device.phone_number,''),
                    device.id
                ) device_label
            FROM web_push_deliveries push
            JOIN web_push_subscriptions subscription
                ON subscription.id=push.subscription_id
            JOIN messages message ON message.id=push.message_id
            JOIN devices device ON device.id=message.device_id
            WHERE push.id=?
            """,
            (delivery_id,),
        ).fetchone()
        if not row or row["status"] not in ("pending", "retry"):
            return False
        enabled = (
            row["notify_sms"]
            if row["event_type"] == "sms"
            else row["notify_missed_call"]
        )
        if not enabled:
            db.execute("DELETE FROM web_push_deliveries WHERE id=?", (delivery_id,))
            return True
        db.execute(
            "UPDATE web_push_deliveries SET status='sending' WHERE id=?",
            (delivery_id,),
        )

    try:
        send_web_push_subscription(row, web_push_payload(row))
    except Exception as exc:
        status_code = web_push_exception_status(exc)
        with db_connect() as db:
            if status_code in (404, 410):
                db.execute(
                    "DELETE FROM web_push_subscriptions WHERE id=?",
                    (row["subscription_id"],),
                )
                return True
            current = db.execute(
                "SELECT attempts FROM web_push_deliveries WHERE id=?",
                (delivery_id,),
            ).fetchone()
            if not current:
                return True
            attempts = current["attempts"] + 1
            failed = attempts >= WEB_PUSH_MAX_ATTEMPTS
            delay = WEB_PUSH_RETRY_DELAYS[
                min(attempts - 1, len(WEB_PUSH_RETRY_DELAYS) - 1)
            ]
            db.execute(
                """
                UPDATE web_push_deliveries
                SET status=?, attempts=?, next_attempt_at=?, last_error=?
                WHERE id=?
                """,
                (
                    "failed" if failed else "retry",
                    attempts,
                    0 if failed else time.time() + delay,
                    text_value(exc, 500),
                    delivery_id,
                ),
            )
            db.execute(
                "UPDATE web_push_subscriptions SET last_error=? WHERE id=?",
                (text_value(exc, 500), row["subscription_id"]),
            )
        log.warning("web push delivery %s failed: %s", delivery_id, exc)
        return True

    with db_connect() as db:
        now = utc_now()
        db.execute(
            """
            UPDATE web_push_deliveries
            SET status='sent', attempts=attempts+1, next_attempt_at=0,
                sent_at=?, last_error=''
            WHERE id=?
            """,
            (now, delivery_id),
        )
        db.execute(
            """
            UPDATE web_push_subscriptions
            SET last_success_at=?, last_error=''
            WHERE id=?
            """,
            (now, row["subscription_id"]),
        )
    return True


def process_next_web_push_delivery():
    with db_connect() as db:
        row = db.execute(
            """
            SELECT id
            FROM web_push_deliveries
            WHERE status IN ('pending','retry') AND next_attempt_at<=?
            ORDER BY id
            LIMIT 1
            """,
            (time.time(),),
        ).fetchone()
    if not row:
        return False
    return process_web_push_delivery(row["id"])


def delivery_worker():
    next_feishu_poll_at = 0
    while True:
        did_work = False
        try:
            if process_feishu_reply_acknowledgement():
                did_work = True
        except Exception:
            log.exception("Feishu reply acknowledgement worker iteration failed")
        try:
            if process_next_web_push_delivery():
                did_work = True
        except Exception:
            log.exception("web push worker iteration failed")
        try:
            with db_connect() as db:
                row = db.execute(
                    """
                    SELECT id FROM deliveries
                    WHERE status IN ('pending','retry') AND next_attempt_at<=?
                    ORDER BY id LIMIT 1
                    """,
                    (time.time(),),
                ).fetchone()
            if row:
                process_delivery(row["id"])
                did_work = True
        except Exception:
            log.exception("delivery worker iteration failed")
        try:
            if process_feishu_notification():
                did_work = True
        except Exception:
            log.exception("Feishu notification worker iteration failed")
        if time.time() >= next_feishu_poll_at:
            try:
                if poll_feishu_messages():
                    did_work = True
            except Exception:
                log.exception("Feishu history polling failed")
            next_feishu_poll_at = time.time() + FEISHU_POLL_SECONDS
        if not did_work:
            time.sleep(1)


init_db()
if os.getenv("DISABLE_WORKER") != "1":
    threading.Thread(target=delivery_worker, name="delivery-worker", daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8787, threaded=True)
