import json
import time
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app import OFFLINE_SECONDS, db_connect, queue_outbound_sms


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

SEND_SMS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

mcp = FastMCP(
    name="SMS Center",
    instructions=(
        "Access SMS Center devices, received SMS, missed calls, forwarding "
        "records, outbound status, and routing summaries. The send_sms tool "
        "queues a real billable SMS on a selected Air724UG device. Always use "
        "a stable unique request_id so retries do not create duplicate jobs."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    streamable_http_path="/",
)


def _bounded_limit(value, default=50, maximum=200):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, value))


def _is_online(last_seen, now_epoch=None):
    try:
        seen_epoch = datetime.fromisoformat(last_seen).timestamp()
    except (TypeError, ValueError):
        return False
    return (now_epoch or time.time()) - seen_epoch <= OFFLINE_SECONDS


def _validate_since(value):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("since must be an ISO-8601 timestamp") from exc
    return value


@mcp.tool(
    title="Get SMS Center status",
    description="Return service counts and current device online state.",
    annotations=READ_ONLY,
)
def get_system_status() -> dict:
    now_epoch = time.time()
    with db_connect() as db:
        devices = db.execute(
            "SELECT id, last_seen FROM devices ORDER BY last_seen DESC"
        ).fetchall()
        return {
            "devices": len(devices),
            "online_devices": sum(
                1 for row in devices if _is_online(row["last_seen"], now_epoch)
            ),
            "offline_seconds": OFFLINE_SECONDS,
            "messages": db.execute(
                "SELECT COUNT(*) FROM messages WHERE event_type='sms'"
            ).fetchone()[0],
            "missed_calls": db.execute(
                "SELECT COUNT(*) FROM messages WHERE event_type='missed_call'"
            ).fetchone()[0],
            "forwarding": {
                "pending": db.execute(
                    """
                    SELECT COUNT(*) FROM deliveries
                    WHERE status IN ('pending','retry','sending')
                    """
                ).fetchone()[0],
                "failed": db.execute(
                    "SELECT COUNT(*) FROM deliveries WHERE status='failed'"
                ).fetchone()[0],
            },
            "outbound": {
                "pending": db.execute(
                    """
                    SELECT COUNT(*) FROM outbound_sms
                    WHERE status IN ('pending','sending')
                    """
                ).fetchone()[0],
                "failed_or_unknown": db.execute(
                    """
                    SELECT COUNT(*) FROM outbound_sms
                    WHERE status IN ('failed','unknown')
                    """
                ).fetchone()[0],
            },
        }


@mcp.tool(
    title="List devices",
    description=(
        "List registered Air724UG devices and their latest heartbeat state. "
        "Set online_only to true to hide offline devices."
    ),
    annotations=READ_ONLY,
)
def list_devices(online_only: bool = False, limit: int = 100) -> dict:
    limit = _bounded_limit(limit, default=100)
    now_epoch = time.time()
    with db_connect() as db:
        rows = db.execute(
            """
            SELECT id, name, phone_number, firmware, app_version, network,
                   signal, queue_count, first_seen, last_seen, status_message
            FROM devices
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    devices = []
    for row in rows:
        item = dict(row)
        item["online"] = _is_online(item["last_seen"], now_epoch)
        if online_only and not item["online"]:
            continue
        devices.append(item)
    return {"count": len(devices), "devices": devices}


@mcp.tool(
    title="Search received SMS",
    description=(
        "Search archived inbound SMS by text, sender, receiving device, status, "
        "or an ISO-8601 lower time bound."
    ),
    annotations=READ_ONLY,
)
def search_messages(
    query: str = "",
    device_id: str = "",
    sender: str = "",
    status: str = "",
    since: str = "",
    limit: int = 50,
) -> dict:
    limit = _bounded_limit(limit)
    since = _validate_since(since)
    conditions = ["m.event_type='sms'"]
    params = []
    if query := str(query or "").strip():
        conditions.append(
            "(m.body LIKE ? OR m.sender LIKE ? OR d.name LIKE ? OR d.phone_number LIKE ?)"
        )
        pattern = f"%{query}%"
        params.extend([pattern, pattern, pattern, pattern])
    if device_id := str(device_id or "").strip():
        conditions.append("m.device_id=?")
        params.append(device_id)
    if sender := str(sender or "").strip():
        conditions.append("m.sender=?")
        params.append(sender)
    if status := str(status or "").strip():
        conditions.append("m.status=?")
        params.append(status)
    if since:
        conditions.append("m.received_at>=?")
        params.append(since)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)
    with db_connect() as db:
        rows = db.execute(
            f"""
            SELECT m.id, m.device_id, m.event_type, d.name device_name,
                   d.phone_number device_phone, m.sender, m.sms_time, m.body,
                   m.status, m.received_at
            FROM messages m
            JOIN devices d ON d.id=m.device_id
            {where}
            ORDER BY m.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return {"count": len(rows), "messages": [dict(row) for row in rows]}


@mcp.tool(
    title="List missed calls",
    description=(
        "List inbound calls that were automatically rejected by an Air724UG "
        "device, with caller, receiving SIM, ring time, and forwarding status."
    ),
    annotations=READ_ONLY,
)
def list_missed_calls(
    device_id: str = "",
    caller: str = "",
    status: str = "",
    since: str = "",
    limit: int = 50,
) -> dict:
    limit = _bounded_limit(limit)
    since = _validate_since(since)
    conditions = ["m.event_type='missed_call'"]
    params = []
    if device_id := str(device_id or "").strip():
        conditions.append("m.device_id=?")
        params.append(device_id)
    if caller := str(caller or "").strip():
        conditions.append("m.sender=?")
        params.append(caller)
    if status := str(status or "").strip():
        conditions.append("m.status=?")
        params.append(status)
    if since:
        conditions.append("m.received_at>=?")
        params.append(since)
    params.append(limit)
    with db_connect() as db:
        rows = db.execute(
            f"""
            SELECT m.id, m.device_id, d.name device_name,
                   d.phone_number device_phone, m.sender caller,
                   m.sms_time started_at, m.metadata_json, m.status,
                   m.received_at
            FROM messages m
            JOIN devices d ON d.id=m.device_id
            WHERE {" AND ".join(conditions)}
            ORDER BY m.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    calls = []
    for row in rows:
        item = dict(row)
        try:
            metadata = json.loads(item.pop("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        item["ended_at"] = metadata.get("ended_at", "")
        item["duration_seconds"] = metadata.get("duration_seconds", 0)
        calls.append(item)
    return {"count": len(calls), "missed_calls": calls}


@mcp.tool(
    title="List forwarding records",
    description="List inbound SMS and missed-call forwarding attempts and results.",
    annotations=READ_ONLY,
)
def list_forwarding_records(status: str = "", limit: int = 50) -> dict:
    limit = _bounded_limit(limit)
    status = str(status or "").strip()
    where = "WHERE dl.status=?" if status else ""
    params = [status, limit] if status else [limit]
    with db_connect() as db:
        rows = db.execute(
            f"""
            SELECT dl.id, dl.message_id, dl.status, dl.attempts,
                   dl.last_attempt_at, dl.delivered_at, dl.last_error,
                   dl.http_status, dest.name destination_name,
                   dest.kind destination_kind, m.device_id, m.event_type,
                   dev.name device_name, dev.phone_number device_phone,
                   m.sender, m.sms_time, m.body, m.received_at,
                   o.id outbound_sms_id, o.status outbound_status,
                   o.recipient forward_recipient,
                   o.device_id forward_device_id, o.sent_at forward_sent_at
            FROM deliveries dl
            JOIN destinations dest ON dest.id=dl.destination_id
            JOIN messages m ON m.id=dl.message_id
            JOIN devices dev ON dev.id=m.device_id
            LEFT JOIN outbound_sms o ON o.source_delivery_id=dl.id
            {where}
            ORDER BY dl.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return {"count": len(rows), "deliveries": [dict(row) for row in rows]}


@mcp.tool(
    title="List outbound SMS records",
    description=(
        "List SMS jobs previously queued from the console or MCP and their modem "
        "submission status. This tool cannot create or retry a job."
    ),
    annotations=READ_ONLY,
)
def list_outbound_sms(status: str = "", limit: int = 50) -> dict:
    limit = _bounded_limit(limit)
    status = str(status or "").strip()
    where = "WHERE o.status=?" if status else ""
    params = [status, limit] if status else [limit]
    with db_connect() as db:
        rows = db.execute(
            f"""
            SELECT o.id, o.device_id, o.mcp_request_id, d.name device_name,
                   d.phone_number device_phone, o.recipient, o.body, o.status,
                   o.attempts, o.created_at, o.dispatched_at, o.completed_at,
                   o.sent_at, o.last_error, o.source_delivery_id,
                   dest.name source_destination_name
            FROM outbound_sms o
            JOIN devices d ON d.id=o.device_id
            LEFT JOIN deliveries dl ON dl.id=o.source_delivery_id
            LEFT JOIN destinations dest ON dest.id=dl.destination_id
            {where}
            ORDER BY o.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return {"count": len(rows), "outbound_sms": [dict(row) for row in rows]}


@mcp.tool(
    title="Send SMS",
    description=(
        "Queue a real SMS for an Air724UG device. The device sends it "
        "asynchronously and carrier charges may apply. request_id is required "
        "for idempotency; reusing it with identical parameters returns the "
        "existing job, while different parameters are rejected."
    ),
    annotations=SEND_SMS,
)
def send_sms(
    device_id: str,
    recipient: str,
    body: str,
    request_id: str,
) -> dict:
    queued = queue_outbound_sms(
        device_id,
        recipient,
        body,
        mcp_request_id=request_id,
    )
    return {
        "queued": True,
        "outbound_sms_id": queued["id"],
        "status": queued["status"],
        "created_at": queued["created_at"],
        "duplicate": queued["duplicate"],
        "request_id": request_id,
    }


@mcp.tool(
    title="Get routing summary",
    description=(
        "Return destination metadata, channel groups, and legacy direct routes. "
        "Webhook URLs, headers, and application secrets are never returned."
    ),
    annotations=READ_ONLY,
)
def get_routing_summary() -> dict:
    with db_connect() as db:
        destinations = [
            dict(row)
            for row in db.execute(
                """
                SELECT id, name, kind, enabled, created_at, updated_at
                FROM destinations
                ORDER BY id
                """
            ).fetchall()
        ]
        routes = [
            dict(row)
            for row in db.execute(
                """
                SELECT r.id, r.device_id, r.enabled,
                       d.id destination_id, d.name destination_name
                FROM routes r
                JOIN destinations d ON d.id=r.destination_id
                ORDER BY r.id
                """
            ).fetchall()
        ]
        groups = [
            dict(row)
            for row in db.execute(
                """
                SELECT id, name, description, enabled, created_at, updated_at
                FROM channel_groups
                ORDER BY id
                """
            ).fetchall()
        ]
        group_devices = db.execute(
            """
            SELECT cgd.group_id, cgd.device_id, d.name device_name,
                   d.phone_number device_phone
            FROM channel_group_devices cgd
            JOIN devices d ON d.id=cgd.device_id
            ORDER BY cgd.group_id, cgd.device_id
            """
        ).fetchall()
        group_destinations = db.execute(
            """
            SELECT cgd.group_id, cgd.destination_id,
                   d.name destination_name, d.kind destination_kind
            FROM channel_group_destinations cgd
            JOIN destinations d ON d.id=cgd.destination_id
            ORDER BY cgd.group_id, cgd.destination_id
            """
        ).fetchall()
    groups_by_id = {group["id"]: group for group in groups}
    for group in groups:
        group["devices"] = []
        group["destinations"] = []
    for row in group_devices:
        group = groups_by_id.get(row["group_id"])
        if group:
            group["devices"].append(
                {
                    "device_id": row["device_id"],
                    "device_name": row["device_name"],
                    "device_phone": row["device_phone"],
                }
            )
    for row in group_destinations:
        group = groups_by_id.get(row["group_id"])
        if group:
            group["destinations"].append(
                {
                    "destination_id": row["destination_id"],
                    "destination_name": row["destination_name"],
                    "destination_kind": row["destination_kind"],
                }
            )
    for item in destinations + routes + groups:
        if "enabled" in item:
            item["enabled"] = bool(item["enabled"])
    return {
        "destinations": destinations,
        "channel_groups": groups,
        "direct_routes": routes,
    }


@mcp.resource(
    "sms-center://status",
    name="SMS Center status",
    description="Current read-only service and device status snapshot.",
    mime_type="application/json",
)
def status_resource() -> str:
    return json.dumps(get_system_status(), ensure_ascii=False)


@mcp.resource(
    "sms-center://devices",
    name="SMS Center devices",
    description="Registered devices and latest heartbeat state.",
    mime_type="application/json",
)
def devices_resource() -> str:
    return json.dumps(list_devices(limit=200), ensure_ascii=False)
