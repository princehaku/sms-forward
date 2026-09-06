import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class SmsCenterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = str(Path(cls.temp_dir.name) / "test.db")
        os.environ["DEVICE_TOKEN"] = "sms-sb"
        os.environ["ADMIN_TOKEN"] = "admin-test"
        os.environ["DISABLE_WORKER"] = "1"
        spec = importlib.util.spec_from_file_location(
            "sms_center_app", Path(__file__).parents[1] / "app.py"
        )
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules["sms_center_app"] = cls.module
        spec.loader.exec_module(cls.module)
        cls.client = cls.module.app.test_client()

        sys.modules["app"] = cls.module
        mcp_spec = importlib.util.spec_from_file_location(
            "sms_center_mcp", Path(__file__).parents[1] / "mcp_server.py"
        )
        cls.mcp_module = importlib.util.module_from_spec(mcp_spec)
        sys.modules["sms_center_mcp"] = cls.mcp_module
        mcp_spec.loader.exec_module(cls.mcp_module)

        sys.modules["mcp_server"] = cls.mcp_module
        asgi_spec = importlib.util.spec_from_file_location(
            "sms_center_asgi", Path(__file__).parents[1] / "asgi.py"
        )
        cls.asgi_module = importlib.util.module_from_spec(asgi_spec)
        sys.modules["sms_center_asgi"] = cls.asgi_module
        asgi_spec.loader.exec_module(cls.asgi_module)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_device_forward_switches_and_call_template(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-forward-switches"
        self.client.post("/api/device/register", json={"token": "sms-sb", "device_id": device_id, "name": "keep name"})
        destination = self.client.post("/api/admin/destinations", headers=headers, json={"name": "switch target", "kind": "feishu_webhook", "config": {"url": "https://example.com/test"}}).json["id"]
        self.client.post("/api/admin/routes", headers=headers, json={"device_id": device_id, "destination_id": destination})
        endpoint = "/api/admin/devices/" + device_id
        self.assertEqual(self.client.patch(endpoint, headers=headers, json={"sms_forward_enabled": "false"}).status_code, 400)
        self.assertEqual(self.client.patch(endpoint, headers=headers, json={"sms_forward_enabled": False}).status_code, 200)
        def queue(event, key):
            with self.module.db_connect() as db:
                cursor = db.execute("INSERT INTO messages(device_id, message_key, event_type, sender, body, received_at, status) VALUES (?, ?, ?, '12345', 'content', ?, 'stored')", (device_id, key, event, self.module.utc_now()))
                return self.module.queue_deliveries(db, cursor.lastrowid, device_id)
        self.assertEqual(queue("sms", "disabled-sms"), 0)
        self.assertEqual(queue("missed_call", "enabled-call"), 1)
        self.client.patch(endpoint, headers=headers, json={"sms_forward_enabled": True, "call_forward_enabled": False})
        self.module.init_db()
        self.assertEqual(queue("sms", "enabled-sms"), 1)
        self.assertEqual(queue("missed_call", "disabled-call"), 0)
        with self.module.db_connect() as db:
            self.assertEqual(db.execute("SELECT name FROM devices WHERE id=?", (device_id,)).fetchone()["name"], "keep name")
        template = "电话 {{ori}} / {{device}} / {{time}}"
        self.assertEqual(self.client.put("/api/admin/call-template", json={"body": template}).status_code, 401)
        self.assertEqual(self.client.put("/api/admin/call-template", headers=headers, json={"body": "{{invalid}}"}).status_code, 400)
        original = self.client.get("/api/admin/snapshot", headers=headers).json["call_template"]
        try:
            self.assertEqual(self.client.put("/api/admin/call-template", headers=headers, json={"body": template}).status_code, 200)
            self.module.init_db()
            self.assertEqual(self.client.get("/api/admin/snapshot", headers=headers).json["call_template"], template)
            message = {"event_type": "missed_call", "sender": "12345", "device_label": "board", "device_phone": "", "body": "未接来电", "sms_time": "", "received_at": "today"}
            with self.module.db_connect() as db:
                rendered = self.module.forwarded_sms_content(db, message)
                self.assertEqual(rendered, "电话 12345 / board / today")
                self.assertEqual(self.module.formatted_feishu_message(message, self.module.resolve_call_template_body(db)), rendered)
                delivery_id = db.execute("SELECT dl.id FROM deliveries dl JOIN messages m ON m.id=dl.message_id WHERE m.device_id=? AND m.event_type='missed_call'", (device_id,)).fetchone()["id"]
            with patch.object(self.module, "deliver", return_value=(True, 200, "", "{}")) as send:
                self.module.process_delivery(delivery_id)
                self.assertTrue(send.call_args.kwargs["formatted_message"].startswith("电话 12345 / keep name / "))
        finally:
            self.client.put("/api/admin/call-template", headers=headers, json={"body": original})

    def test_health_and_device_auth(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        denied = self.client.post("/api/device/register", json={"device_id": "dev-1"})
        self.assertEqual(denied.status_code, 401)
        accepted = self.client.post(
            "/api/device/register",
            json={"token": "sms-sb", "device_id": "dev-1", "signal": 18},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json["code"], 0)

    def test_device_traffic_accumulates_across_sessions_without_duplicates(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-traffic"

        def report(session_id, session_bytes):
            return self.client.post(
                "/api/device/heartbeat",
                json={
                    "token": "sms-sb",
                    "device_id": device_id,
                    "phone_number": "15300000088",
                    "traffic_session_id": session_id,
                    "traffic_session_bytes": session_bytes,
                },
            )

        self.assertEqual(report("1", 1024).status_code, 200)
        self.assertEqual(report("1", 3072).status_code, 200)
        self.assertEqual(report("1", 3072).status_code, 200)
        self.assertEqual(report("1", 2048).status_code, 200)
        self.assertEqual(report("2", 512).status_code, 200)
        legacy = self.client.post(
            "/api/device/heartbeat",
            json={"token": "sms-sb", "device_id": device_id},
        )
        self.assertEqual(legacy.status_code, 200)

        snapshot = self.client.get("/api/admin/snapshot", headers=headers).json
        device = next(item for item in snapshot["devices"] if item["id"] == device_id)
        self.assertEqual(device["traffic_total_bytes"], 3584)
        self.assertEqual(device["traffic_session_id"], "2")
        self.assertEqual(device["traffic_session_bytes"], 512)
        self.assertTrue(device["traffic_updated_at"])

    def test_idempotent_message_and_routing(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        destination = self.client.post(
            "/api/admin/destinations",
            headers=headers,
            json={
                "name": "test hook",
                "kind": "webhook",
                "config": {"url": "https://example.test/hook"},
            },
        )
        self.assertEqual(destination.status_code, 200)
        route = self.client.post(
            "/api/admin/routes",
            headers=headers,
            json={"device_id": "*", "destination_id": destination.json["id"]},
        )
        self.assertEqual(route.status_code, 200)
        payload = {
            "token": "sms-sb",
            "device_id": "dev-1",
            "message_id": "msg-1",
            "sender": "10086",
            "body": "测试短信",
        }
        first = self.client.post("/api/messages", json=payload)
        second = self.client.post("/api/messages", json=payload)
        self.assertFalse(first.json["duplicate"])
        self.assertEqual(first.json["delivery_count"], 1)
        self.assertTrue(second.json["duplicate"])

    def test_missed_call_is_separate_idempotent_and_forwardable(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-missed-call"
        self.client.post(
            "/api/device/register",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "name": "call board",
                "phone_number": "15300000019",
            },
        )
        destination = self.client.post(
            "/api/admin/destinations",
            headers=headers,
            json={
                "name": "missed call phone notice",
                "kind": "sms_forward",
                "config": {"recipient": "13800138019"},
            },
        )
        self.assertEqual(destination.status_code, 200)
        self.client.post(
            "/api/admin/routes",
            headers=headers,
            json={
                "device_id": device_id,
                "destination_id": destination.json["id"],
            },
        )
        payload = {
            "token": "sms-sb",
            "device_id": device_id,
            "call_id": "call-20260729-1",
            "caller": "13100000019",
            "started_at": "2026-07-29 09:01:02",
            "ended_at": "2026-07-29 09:01:05",
            "duration_seconds": 3,
        }
        first = self.client.post("/api/device/missed-call", json=payload)
        second = self.client.post("/api/device/missed-call", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json["duplicate"])
        self.assertTrue(second.json["duplicate"])
        self.assertEqual(first.json["message_id"], second.json["message_id"])

        snapshot = self.client.get("/api/admin/snapshot", headers=headers).json
        self.assertFalse(
            any(item["id"] == first.json["message_id"] for item in snapshot["messages"])
        )
        call = next(
            item
            for item in snapshot["missed_calls"]
            if item["id"] == first.json["message_id"]
        )
        self.assertEqual(call["sender"], payload["caller"])
        self.assertEqual(call["metadata"]["ended_at"], payload["ended_at"])
        self.assertEqual(call["metadata"]["duration_seconds"], 3)

        delivery = next(
            item
            for item in snapshot["deliveries"]
            if item["message_id"] == first.json["message_id"]
            and item["destination_id"] == destination.json["id"]
        )
        self.module.process_delivery(delivery["id"])
        snapshot = self.client.get("/api/admin/snapshot", headers=headers).json
        outbound = next(
            item
            for item in snapshot["outbound_sms"]
            if item["source_delivery_id"] == delivery["id"]
        )
        self.assertIn("未接来电", outbound["body"])
        self.assertIn("接收卡：15300000019", outbound["body"])
        self.assertIn("来电号码：13100000019", outbound["body"])
        self.assertIn("入库时间：", outbound["body"])
        self.assertNotIn(payload["started_at"], outbound["body"])
        self.assertIn("未接来电", self.module.formatted_feishu_message(call))
        self.assertIn(
            f"入库时间：{call['received_at']}",
            self.module.formatted_feishu_message(call),
        )

    def test_gb18030_encoded_device_message(self):
        payload = {
            "token": "sms-sb",
            "device_id": "dev-gbk",
            "phone_number": "18500000000",
            "message_id": "msg-gbk-1",
            "sender": "10010",
            "body": "\u8054\u901a\u77ed\u4fe1\u6d4b\u8bd5",
        }
        response = self.client.post(
            "/api/messages",
            data=json.dumps(payload, ensure_ascii=False).encode("gb18030"),
            content_type="application/json; charset=utf-8",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["code"], 0)

        snapshot = self.client.get(
            "/api/admin/snapshot",
            headers={"X-SMS-Admin-Token": "admin-test"},
        )
        stored = next(
            item for item in snapshot.json["messages"] if item["device_id"] == "dev-gbk"
        )
        self.assertEqual(stored["body"], payload["body"])
        self.assertEqual(stored["device_phone"], payload["phone_number"])

    def test_outbound_sms_queue_poll_and_result(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-outbound-success"
        registered = self.client.post(
            "/api/device/register",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "phone_number": "18500000003",
            },
        )
        self.assertEqual(registered.status_code, 200)

        denied = self.client.post(
            "/api/admin/outbound-sms",
            json={
                "device_id": device_id,
                "recipient": "13800138000",
                "body": "hello",
            },
        )
        self.assertEqual(denied.status_code, 401)

        queued = self.client.post(
            "/api/admin/outbound-sms",
            headers=headers,
            json={
                "device_id": device_id,
                "recipient": "138 0013 8000",
                "body": "控制台发送测试",
            },
        )
        self.assertEqual(queued.status_code, 200)
        command_id = queued.json["id"]

        poll = self.client.post(
            "/api/device/outbound/poll",
            json={"token": "sms-sb", "device_id": device_id},
        )
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(
            poll.json,
            {
                "code": 0,
                "id": command_id,
                "to": "13800138000",
                "text": "控制台发送测试",
            },
        )

        empty_poll = self.client.post(
            "/api/device/outbound/poll",
            json={"token": "sms-sb", "device_id": device_id},
        )
        self.assertEqual(empty_poll.json, {"code": 0})
        self.assertLessEqual(len(empty_poll.data), 12)

        result = self.client.post(
            "/api/device/outbound/result",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "id": command_id,
                "ok": True,
            },
        )
        self.assertEqual(result.status_code, 200)

        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        stored = next(
            item
            for item in snapshot.json["outbound_sms"]
            if item["id"] == command_id
        )
        self.assertEqual(stored["status"], "sent")
        self.assertIsNotNone(stored["sent_at"])
        self.assertEqual(stored["attempts"], 1)

    def test_device_sync_combines_presence_and_outbound_poll(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-combined-sync"
        first = self.client.post(
            "/api/device/sync",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "signal": 17,
            },
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["code"], 0)
        self.assertEqual(first.json["sync_seconds"], 120)
        self.assertNotIn("command", first.json)

        queued = self.client.post(
            "/api/admin/outbound-sms",
            headers=headers,
            json={
                "device_id": device_id,
                "recipient": "13800138001",
                "body": "combined sync",
            },
        )
        second = self.client.post(
            "/api/device/sync",
            json={"token": "sms-sb", "device_id": device_id},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.json["command"],
            {
                "id": queued.json["id"],
                "to": "13800138001",
                "text": "combined sync",
            },
        )
        legacy_poll = self.client.post(
            "/api/device/outbound/poll",
            json={"token": "sms-sb", "device_id": device_id},
        )
        self.assertEqual(legacy_poll.json, {"code": 0})

    def test_device_websocket_pushes_command_and_accepts_result(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-websocket"
        self.client.post(
            "/api/device/register",
            json={"token": "sms-sb", "device_id": device_id},
        )
        queued = self.client.post(
            "/api/admin/outbound-sms",
            headers=headers,
            json={
                "device_id": device_id,
                "recipient": "13800138002",
                "body": "websocket push",
            },
        )
        command_id = queued.json["id"]

        class FakeWebSocket:
            scope = {"type": "websocket", "headers": [], "client": ("127.0.0.1", 1)}

            def __init__(self):
                self.accepted = False
                self.sent = []
                self.received = [
                    {
                        "type": "hello",
                        "token": "sms-sb",
                        "device_id": device_id,
                        "signal": 16,
                        "queue_count": 0,
                    },
                    {"type": "result", "id": command_id, "ok": True},
                ]

            async def accept(self):
                self.accepted = True

            async def receive_json(self):
                if self.received:
                    return self.received.pop(0)
                raise self_outer.asgi_module.WebSocketDisconnect(1000)

            async def send_json(self, payload):
                self.sent.append(payload)

            async def close(self, code=1000):
                self.closed_code = code

        self_outer = self
        websocket = FakeWebSocket()
        asyncio.run(self.asgi_module.device_websocket(websocket))
        self.assertTrue(websocket.accepted)
        self.assertEqual(websocket.sent[0]["type"], "ready")
        self.assertEqual(websocket.sent[0]["status_seconds"], 21600)
        self.assertEqual(websocket.sent[0]["fallback_sync_seconds"], 120)
        self.assertLessEqual(
            len(json.dumps(websocket.sent[0], separators=(",", ":")).encode()),
            125,
        )
        self.assertEqual(websocket.sent[1]["type"], "command")
        self.assertEqual(websocket.sent[1]["id"], command_id)
        self.assertEqual(websocket.sent[2], {"type": "result_ack", "code": 0, "id": command_id})

        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        stored = next(
            item
            for item in snapshot.json["outbound_sms"]
            if item["id"] == command_id
        )
        self.assertEqual(stored["status"], "sent")

    def test_outbound_sms_failure_and_manual_retry(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-outbound-retry"
        self.client.post(
            "/api/device/register",
            json={"token": "sms-sb", "device_id": device_id},
        )
        queued = self.client.post(
            "/api/admin/outbound-sms",
            headers=headers,
            json={
                "device_id": device_id,
                "recipient": "+8613900139000",
                "body": "retry me",
            },
        )
        command_id = queued.json["id"]
        self.client.post(
            "/api/device/outbound/poll",
            json={"token": "sms-sb", "device_id": device_id},
        )
        failed = self.client.post(
            "/api/device/outbound/result",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "id": command_id,
                "ok": False,
                "error": "network rejected",
            },
        )
        self.assertEqual(failed.status_code, 200)

        retried = self.client.post(
            f"/api/admin/outbound-sms/{command_id}/retry",
            headers=headers,
            json={},
        )
        self.assertEqual(retried.status_code, 200)
        second_poll = self.client.post(
            "/api/device/outbound/poll",
            json={"token": "sms-sb", "device_id": device_id},
        )
        self.assertEqual(second_poll.json["id"], command_id)

        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        stored = next(
            item
            for item in snapshot.json["outbound_sms"]
            if item["id"] == command_id
        )
        self.assertEqual(stored["status"], "sending")
        self.assertEqual(stored["attempts"], 2)

    def test_pending_outbound_sms_can_be_deleted(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-outbound-delete"
        self.client.post(
            "/api/device/register",
            json={"token": "sms-sb", "device_id": device_id},
        )
        queued = self.client.post(
            "/api/admin/outbound-sms",
            headers=headers,
            json={
                "device_id": device_id,
                "recipient": "13800138002",
                "body": "delete before dispatch",
            },
        )
        command_id = queued.json["id"]

        denied = self.client.delete(f"/api/admin/outbound-sms/{command_id}")
        self.assertEqual(denied.status_code, 401)
        deleted = self.client.delete(
            f"/api/admin/outbound-sms/{command_id}",
            headers=headers,
        )
        self.assertEqual(deleted.status_code, 200)
        poll = self.client.post(
            "/api/device/outbound/poll",
            json={"token": "sms-sb", "device_id": device_id},
        )
        self.assertEqual(poll.json, {"code": 0})

        already_deleted = self.client.delete(
            f"/api/admin/outbound-sms/{command_id}",
            headers=headers,
        )
        self.assertEqual(already_deleted.status_code, 404)

    def test_dispatched_outbound_sms_cannot_be_deleted(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-outbound-delete-after-dispatch"
        self.client.post(
            "/api/device/register",
            json={"token": "sms-sb", "device_id": device_id},
        )
        queued = self.client.post(
            "/api/admin/outbound-sms",
            headers=headers,
            json={
                "device_id": device_id,
                "recipient": "13800138003",
                "body": "already dispatched",
            },
        )
        command_id = queued.json["id"]
        self.client.post(
            "/api/device/outbound/poll",
            json={"token": "sms-sb", "device_id": device_id},
        )
        refused = self.client.delete(
            f"/api/admin/outbound-sms/{command_id}",
            headers=headers,
        )
        self.assertEqual(refused.status_code, 409)

        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        stored = next(
            item
            for item in snapshot.json["outbound_sms"]
            if item["id"] == command_id
        )
        self.assertEqual(stored["status"], "sending")

    def test_outbound_sms_validation(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-outbound-validation"
        self.client.post(
            "/api/device/register",
            json={"token": "sms-sb", "device_id": device_id},
        )
        for payload in (
            {"device_id": device_id, "recipient": "abc", "body": "hello"},
            {"device_id": device_id, "recipient": "13800138000", "body": ""},
            {
                "device_id": device_id,
                "recipient": "13800138000",
                "body": "x" * 1001,
            },
            {
                "device_id": "missing-device",
                "recipient": "13800138000",
                "body": "hello",
            },
        ):
            response = self.client.post(
                "/api/admin/outbound-sms",
                headers=headers,
                json=payload,
            )
            self.assertIn(response.status_code, (400, 404))

    def test_outbound_sms_without_result_becomes_unknown(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-outbound-unknown"
        self.client.post(
            "/api/device/register",
            json={"token": "sms-sb", "device_id": device_id},
        )
        queued = self.client.post(
            "/api/admin/outbound-sms",
            headers=headers,
            json={
                "device_id": device_id,
                "recipient": "13800138001",
                "body": "unknown state test",
            },
        )
        command_id = queued.json["id"]
        self.client.post(
            "/api/device/outbound/poll",
            json={"token": "sms-sb", "device_id": device_id},
        )
        with self.module.db_connect() as db:
            db.execute(
                "UPDATE outbound_sms SET dispatched_epoch=? WHERE id=?",
                (
                    self.module.time.time()
                    - self.module.OUTBOUND_STALE_SECONDS
                    - 1,
                    command_id,
                ),
            )

        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        stored = next(
            item
            for item in snapshot.json["outbound_sms"]
            if item["id"] == command_id
        )
        self.assertEqual(stored["status"], "unknown")

        empty_poll = self.client.post(
            "/api/device/outbound/poll",
            json={"token": "sms-sb", "device_id": device_id},
        )
        self.assertEqual(empty_poll.json, {"code": 0})

    def test_sms_forward_destination_queues_once_and_tracks_success(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-sms-forward-source"
        self.client.post(
            "/api/device/register",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "name": "forward source",
                "phone_number": "15300000001",
            },
        )
        destination = self.client.post(
            "/api/admin/destinations",
            headers=headers,
            json={
                "name": "forward to on-call",
                "kind": "sms_forward",
                "config": {
                    "recipient": "138 0013 8999",
                    "sender_device_id": "",
                },
            },
        )
        self.assertEqual(destination.status_code, 200)
        self.client.post(
            "/api/admin/routes",
            headers=headers,
            json={
                "device_id": device_id,
                "destination_id": destination.json["id"],
            },
        )
        received = self.client.post(
            "/api/messages",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "message_id": "sms-forward-success",
                "sender": "10086",
                "sms_time": "2026-07-28 10:00:00",
                "body": "forward this message",
            },
        )
        self.assertEqual(received.status_code, 200)
        delivery_id = next(
            item["id"]
            for item in self.client.get(
                "/api/admin/snapshot",
                headers=headers,
            ).json["deliveries"]
            if item["message_id"] == received.json["message_id"]
            and item["destination_id"] == destination.json["id"]
        )

        self.module.process_delivery(delivery_id)
        self.module.process_delivery(delivery_id)
        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        queued = [
            item
            for item in snapshot.json["outbound_sms"]
            if item["source_delivery_id"] == delivery_id
        ]
        self.assertEqual(len(queued), 1)
        command = queued[0]
        self.assertEqual(command["device_id"], device_id)
        self.assertEqual(command["recipient"], "13800138999")
        self.assertIn("原号码：10086", command["body"])
        self.assertIn("forward this message", command["body"])

        poll = self.client.post(
            "/api/device/outbound/poll",
            json={"token": "sms-sb", "device_id": device_id},
        )
        self.assertEqual(poll.json["id"], command["id"])
        sent = self.client.post(
            "/api/device/outbound/result",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "id": command["id"],
                "ok": True,
            },
        )
        self.assertEqual(sent.status_code, 200)

        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        delivery = next(
            item for item in snapshot.json["deliveries"] if item["id"] == delivery_id
        )
        command = next(
            item
            for item in snapshot.json["outbound_sms"]
            if item["source_delivery_id"] == delivery_id
        )
        self.assertEqual(delivery["status"], "delivered")
        self.assertEqual(delivery["outbound_status"], "sent")
        self.assertEqual(command["status"], "sent")
        self.assertEqual(command["source_destination_name"], "forward to on-call")

    def test_sms_forward_can_use_another_device_and_manual_retry(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        source_device = "dev-sms-forward-receiver"
        sender_device = "dev-sms-forward-sender"
        for device_id, phone in (
            (source_device, "15300000002"),
            (sender_device, "15300000003"),
        ):
            self.client.post(
                "/api/device/register",
                json={
                    "token": "sms-sb",
                    "device_id": device_id,
                    "phone_number": phone,
                },
            )
        template = self.client.post(
            "/api/admin/sms-templates",
            headers=headers,
            json={
                "name": "current sender phone test",
                "body": "receiver={{receiver}}\nphone={{phone}}\n{{sms}}",
                "is_default": False,
            },
        )
        self.assertEqual(template.status_code, 200)
        destination = self.client.post(
            "/api/admin/destinations",
            headers=headers,
            json={
                "name": "forward through backup SIM",
                "kind": "sms_forward",
                "config": {
                    "recipient": "+8613800138998",
                    "sender_device_id": sender_device,
                    "template_id": template.json["id"],
                },
            },
        )
        self.client.post(
            "/api/admin/routes",
            headers=headers,
            json={
                "device_id": source_device,
                "destination_id": destination.json["id"],
            },
        )
        received = self.client.post(
            "/api/messages",
            json={
                "token": "sms-sb",
                "device_id": source_device,
                "message_id": "sms-forward-retry",
                "sender": "95588",
                "body": "retry forwarding",
            },
        )
        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        delivery_id = next(
            item["id"]
            for item in snapshot.json["deliveries"]
            if item["message_id"] == received.json["message_id"]
            and item["destination_id"] == destination.json["id"]
        )
        self.module.process_delivery(delivery_id)
        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        command = next(
            item
            for item in snapshot.json["outbound_sms"]
            if item["source_delivery_id"] == delivery_id
        )
        self.assertEqual(command["device_id"], sender_device)
        self.assertEqual(
            command["body"],
            (
                "receiver=15300000002\n"
                "phone=15300000003\n"
                "retry forwarding"
            ),
        )

        self.client.post(
            "/api/device/outbound/poll",
            json={"token": "sms-sb", "device_id": sender_device},
        )
        self.client.post(
            "/api/device/outbound/result",
            json={
                "token": "sms-sb",
                "device_id": sender_device,
                "id": command["id"],
                "ok": False,
                "error": "modem rejected",
            },
        )
        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        failed = next(
            item for item in snapshot.json["deliveries"] if item["id"] == delivery_id
        )
        self.assertEqual(failed["status"], "failed")
        self.assertIn("modem rejected", failed["last_error"])

        retry = self.client.post(
            f"/api/admin/deliveries/{delivery_id}/retry",
            headers=headers,
            json={},
        )
        self.assertEqual(retry.status_code, 200)
        self.module.process_delivery(delivery_id)
        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        retried = next(
            item
            for item in snapshot.json["outbound_sms"]
            if item["source_delivery_id"] == delivery_id
        )
        self.assertEqual(retried["id"], command["id"])
        self.assertEqual(retried["status"], "pending")

        deleted = self.client.delete(
            f"/api/admin/outbound-sms/{command['id']}",
            headers=headers,
        )
        self.assertEqual(deleted.status_code, 200)
        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        deleted_delivery = next(
            item for item in snapshot.json["deliveries"] if item["id"] == delivery_id
        )
        self.assertEqual(deleted_delivery["status"], "failed")
        self.assertIsNone(deleted_delivery["outbound_sms_id"])

    def test_sms_forward_rejects_registered_sim_and_unknown_sender_device(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        self.client.post(
            "/api/device/register",
            json={
                "token": "sms-sb",
                "device_id": "dev-sms-forward-loop",
                "phone_number": "13900139999",
            },
        )
        for config in (
            {"recipient": "+8613900139999", "sender_device_id": ""},
            {
                "recipient": "13800138997",
                "sender_device_id": "missing-forward-device",
            },
        ):
            response = self.client.post(
                "/api/admin/destinations",
                headers=headers,
                json={
                    "name": "invalid SMS forward",
                    "kind": "sms_forward",
                    "config": config,
                },
            )
            self.assertEqual(response.status_code, 400)

    def test_sms_template_management_and_forward_rendering(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        original_default = next(
            item for item in snapshot.json["sms_templates"] if item["is_default"]
        )
        self.assertIn("{{ori}}", original_default["body"])
        self.assertIn("{{sms}}", original_default["body"])

        invalid = self.client.post(
            "/api/admin/sms-templates",
            headers=headers,
            json={
                "name": "invalid placeholder",
                "body": "{{unknown}}",
            },
        )
        self.assertEqual(invalid.status_code, 400)

        created = self.client.post(
            "/api/admin/sms-templates",
            headers=headers,
            json={
                "name": "compact forwarding test",
                "body": (
                    "from={{ori}}\nbody={{sms}}\nsim={{receiver}}\n"
                    "phone={{phone}}\ntime={{time}}\ndevice={{device}}"
                ),
                "is_default": False,
            },
        )
        self.assertEqual(created.status_code, 200)
        template_id = created.json["id"]

        device_id = "dev-template-render"
        self.client.post(
            "/api/device/register",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "name": "模板设备",
                "phone_number": "15300000009",
            },
        )
        destination = self.client.post(
            "/api/admin/destinations",
            headers=headers,
            json={
                "name": "templated SMS forward",
                "kind": "sms_forward",
                "config": {
                    "recipient": "13800138996",
                    "sender_device_id": "",
                    "template_id": template_id,
                },
            },
        )
        self.assertEqual(destination.status_code, 200)
        self.client.post(
            "/api/admin/routes",
            headers=headers,
            json={
                "device_id": device_id,
                "destination_id": destination.json["id"],
            },
        )
        received = self.client.post(
            "/api/messages",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "message_id": "sms-template-render",
                "sender": "10690000",
                "sms_time": "2026-07-28 14:17:54",
                "body": "hello template",
            },
        )
        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        delivery_id = next(
            item["id"]
            for item in snapshot.json["deliveries"]
            if item["message_id"] == received.json["message_id"]
            and item["destination_id"] == destination.json["id"]
        )
        self.module.process_delivery(delivery_id)
        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        command = next(
            item
            for item in snapshot.json["outbound_sms"]
            if item["source_delivery_id"] == delivery_id
        )
        self.assertEqual(
            command["body"],
            (
                "from=10690000\nbody=hello template\nsim=15300000009\n"
                "phone=15300000009\ntime=2026-07-28 14:17:54\n"
                "device=模板设备"
            ),
        )

        in_use = self.client.delete(
            f"/api/admin/sms-templates/{template_id}",
            headers=headers,
        )
        self.assertEqual(in_use.status_code, 409)

        made_default = self.client.post(
            f"/api/admin/sms-templates/{template_id}/default",
            headers=headers,
            json={},
        )
        self.assertEqual(made_default.status_code, 200)
        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        self.assertTrue(
            next(
                item
                for item in snapshot.json["sms_templates"]
                if item["id"] == template_id
            )["is_default"]
        )

        restored_default = self.client.post(
            f"/api/admin/sms-templates/{original_default['id']}/default",
            headers=headers,
            json={},
        )
        self.assertEqual(restored_default.status_code, 200)
        self.client.delete(
            f"/api/admin/destinations/{destination.json['id']}",
            headers=headers,
        )
        deleted = self.client.delete(
            f"/api/admin/sms-templates/{template_id}",
            headers=headers,
        )
        self.assertEqual(deleted.status_code, 200)
        cannot_delete_default = self.client.delete(
            f"/api/admin/sms-templates/{original_default['id']}",
            headers=headers,
        )
        self.assertEqual(cannot_delete_default.status_code, 409)

    def test_outbound_schema_migrates_existing_database(self):
        database_path = Path(self.temp_dir.name) / "old-outbound-schema.db"
        connection = sqlite3.connect(database_path)
        connection.execute(
            """
            CREATE TABLE devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                phone_number TEXT NOT NULL DEFAULT '',
                firmware TEXT NOT NULL DEFAULT '',
                app_version TEXT NOT NULL DEFAULT '',
                network TEXT NOT NULL DEFAULT '',
                signal INTEGER,
                queue_count INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                last_ip TEXT NOT NULL DEFAULT '',
                status_message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_key TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL,
                sender TEXT NOT NULL DEFAULT '',
                sms_time TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                received_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'stored'
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE outbound_sms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
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
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE mcp_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                token_prefix TEXT NOT NULL,
                scopes TEXT NOT NULL DEFAULT 'read',
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                expires_at TEXT,
                revoked_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO mcp_tokens (
                name, token_hash, token_prefix, scopes, created_at
            ) VALUES ('legacy reader', 'legacy-hash', 'legacy-prefix',
                      'read', '2026-07-01T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            CREATE TABLE deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                destination_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                delivered_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                http_status INTEGER,
                response_excerpt TEXT NOT NULL DEFAULT '',
                UNIQUE(message_id, destination_id)
            )
            """
        )
        connection.commit()
        connection.close()

        original_path = self.module.DATABASE_PATH
        try:
            self.module.DATABASE_PATH = database_path
            self.module.init_db()
            with self.module.db_connect() as db:
                columns = {
                    row["name"]
                    for row in db.execute("PRAGMA table_info(outbound_sms)")
                }
                indexes = {
                    row["name"]
                    for row in db.execute("PRAGMA index_list(outbound_sms)")
                }
                mcp_token_columns = {
                    row["name"]
                    for row in db.execute("PRAGMA table_info(mcp_tokens)")
                }
                migrated_mcp_scope = db.execute(
                    "SELECT scopes FROM mcp_tokens WHERE name='legacy reader'"
                ).fetchone()["scopes"]
                delivery_columns = {
                    row["name"]
                    for row in db.execute("PRAGMA table_info(deliveries)")
                }
                message_columns = {
                    row["name"]
                    for row in db.execute("PRAGMA table_info(messages)")
                }
                device_columns = {
                    row["name"]
                    for row in db.execute("PRAGMA table_info(devices)")
                }
                default_template = db.execute(
                    "SELECT body FROM sms_templates WHERE is_default=1"
                ).fetchone()
            self.assertIn("source_delivery_id", columns)
            self.assertIn("mcp_request_id", columns)
            self.assertIn("idx_outbound_sms_source_delivery", indexes)
            self.assertIn("token_value", mcp_token_columns)
            self.assertEqual(migrated_mcp_scope, "read,send")
            self.assertIn("feishu_message_id", delivery_columns)
            self.assertIn("feishu_chat_id", delivery_columns)
            self.assertIn("event_type", message_columns)
            self.assertIn("metadata_json", message_columns)
            self.assertIn("traffic_total_bytes", device_columns)
            self.assertIn("traffic_session_id", device_columns)
            self.assertIn("traffic_session_bytes", device_columns)
            self.assertIn("traffic_updated_at", device_columns)
            self.assertIsNotNone(default_template)
            self.assertIn("{{ori}}", default_template["body"])
        finally:
            self.module.DATABASE_PATH = original_path

    def test_feishu_app_delivery(self):
        auth = Mock(status_code=200, text='{"code":0}')
        auth.json.return_value = {"code": 0, "tenant_access_token": "tenant-test"}
        send_payload = {
            "code": 0,
            "data": {
                "message_id": "om_test_delivery",
                "chat_id": "oc_test",
            },
        }
        send = Mock(status_code=200, text=json.dumps(send_payload))
        send.json.return_value = send_payload
        destination = {
            "kind": "feishu_app",
            "config_json": (
                '{"app_id":"cli_test","app_secret":"secret",'
                '"receive_id":"oc_test","receive_id_type":"chat_id"}'
            ),
        }
        message = {
            "id": 1,
            "device_id": "dev-1",
            "device_label": "测试设备",
            "sender": "10086",
            "sms_time": "2026-07-27 00:00:00",
            "body": "hello",
            "received_at": "2026-07-27T00:00:00+00:00",
        }
        with patch.object(self.module.requests, "post", side_effect=[auth, send]) as post:
            ok, status, error, _ = self.module.deliver(destination, message)
        self.assertTrue(ok)
        self.assertEqual(status, 200)
        self.assertEqual(error, "")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            self.module.feishu_response_reference(send.text),
            ("om_test_delivery", "oc_test"),
        )

    def test_feishu_app_delivery_uses_configured_default_template(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        with self.module.db_connect() as db:
            original_default_id = db.execute(
                "SELECT id FROM sms_templates WHERE is_default=1"
            ).fetchone()["id"]
        template = self.client.post(
            "/api/admin/sms-templates",
            headers=headers,
            json={
                "name": "Feishu default template regression",
                "body": (
                    "FEISHU|{{ori}}|{{sms}}|{{receiver}}|{{phone}}|"
                    "{{time}}|{{device}}"
                ),
                "is_default": True,
            },
        )
        self.assertEqual(template.status_code, 200)
        template_id = template.json["id"]
        selected_config = {
            "app_id": "cli_selected_template",
            "app_secret": "secret",
            "receive_id": "oc_selected_template",
            "template_id": str(template_id),
        }
        with self.module.db_connect() as db:
            self.module.validate_destination(
                "feishu_app",
                selected_config,
                db,
            )
        self.assertEqual(selected_config["template_id"], template_id)
        device_id = "dev-feishu-template"
        destination_id = None
        message_id = None
        try:
            registered = self.client.post(
                "/api/device/register",
                json={
                    "token": "sms-sb",
                    "device_id": device_id,
                    "name": "飞书模板设备",
                    "phone_number": "15300000071",
                },
            )
            self.assertEqual(registered.status_code, 200)
            destination = self.client.post(
                "/api/admin/destinations",
                headers=headers,
                json={
                    "name": "Feishu default template destination",
                    "kind": "feishu_app",
                    "config": {
                        "app_id": "cli_template",
                        "app_secret": "secret",
                        "receive_id": "oc_template",
                        "receive_id_type": "chat_id",
                    },
                },
            )
            self.assertEqual(destination.status_code, 200)
            destination_id = destination.json["id"]
            route = self.client.post(
                "/api/admin/routes",
                headers=headers,
                json={
                    "device_id": device_id,
                    "destination_id": destination_id,
                },
            )
            self.assertEqual(route.status_code, 200)
            received = self.client.post(
                "/api/messages",
                json={
                    "token": "sms-sb",
                    "device_id": device_id,
                    "message_id": "sms-feishu-template",
                    "sender": "10086",
                    "sms_time": "2026-08-25 16:30:00",
                    "body": "template body",
                },
            )
            self.assertEqual(received.status_code, 200)
            message_id = received.json["message_id"]
            snapshot = self.client.get(
                "/api/admin/snapshot", headers=headers
            ).json
            delivery_id = next(
                item["id"]
                for item in snapshot["deliveries"]
                if item["message_id"] == message_id
                and item["destination_id"] == destination_id
            )
            auth = Mock(status_code=200, text='{"code":0}')
            auth.json.return_value = {
                "code": 0,
                "tenant_access_token": "tenant-template",
            }
            send_payload = {
                "code": 0,
                "data": {
                    "message_id": "om_template_delivery",
                    "chat_id": "oc_template",
                },
            }
            send = Mock(status_code=200, text=json.dumps(send_payload))
            send.json.return_value = send_payload
            with patch.object(
                self.module.requests,
                "post",
                side_effect=[auth, send],
            ) as post:
                self.module.process_delivery(delivery_id)
            content = json.loads(
                post.call_args_list[1].kwargs["json"]["content"]
            )["text"]
            self.assertEqual(
                content,
                (
                    "FEISHU|10086|template body|15300000071|15300000071|"
                    "2026-08-25 16:30:00|飞书模板设备"
                ),
            )
        finally:
            with self.module.db_connect() as db:
                if destination_id is not None:
                    db.execute(
                        "DELETE FROM destinations WHERE id=?",
                        (destination_id,),
                    )
                if message_id is not None:
                    db.execute("DELETE FROM messages WHERE id=?", (message_id,))
                db.execute("DELETE FROM devices WHERE id=?", (device_id,))
                db.execute(
                    "UPDATE sms_templates SET is_default=0 WHERE id=?",
                    (template_id,),
                )
                db.execute(
                    "UPDATE sms_templates SET is_default=1 WHERE id=?",
                    (original_default_id,),
                )
                db.execute("DELETE FROM sms_templates WHERE id=?", (template_id,))

    def test_feishu_group_reply_queues_real_sms_once(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        device_id = "dev-feishu-reply"
        original_sender = "13988881234"
        registered = self.client.post(
            "/api/device/register",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "name": "飞书回复测试板",
                "phone_number": "18500000091",
            },
        )
        self.assertEqual(registered.status_code, 200)
        destination = self.client.post(
            "/api/admin/destinations",
            headers=headers,
            json={
                "name": "飞书双向测试群",
                "kind": "feishu_app",
                "config": {
                    "app_id": "cli_reply_test",
                    "app_secret": "reply-secret",
                    "receive_id": "oc_reply_test",
                    "receive_id_type": "chat_id",
                    "verification_token": "verify-reply-test",
                },
            },
        )
        self.assertEqual(destination.status_code, 200)
        destination_id = destination.json["id"]
        routed = self.client.post(
            "/api/admin/routes",
            headers=headers,
            json={
                "device_id": device_id,
                "destination_id": destination_id,
            },
        )
        self.assertEqual(routed.status_code, 200)
        received = self.client.post(
            "/api/messages",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "message_id": "msg-feishu-reply",
                "sender": original_sender,
                "body": "请回复这条短信",
            },
        )
        self.assertEqual(received.status_code, 200)
        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        delivery = next(
            item
            for item in snapshot.json["deliveries"]
            if item["message_id"] == received.json["message_id"]
            and item["destination_id"] == destination_id
        )

        auth = Mock(status_code=200, text='{"code":0}')
        auth.json.return_value = {
            "code": 0,
            "tenant_access_token": "tenant-reply-test",
        }
        send_payload = {
            "code": 0,
            "data": {
                "message_id": "om_forward_reply_test",
                "chat_id": "oc_reply_test",
            },
        }
        send = Mock(status_code=200, text=json.dumps(send_payload))
        send.json.return_value = send_payload
        with patch.object(
            self.module.requests,
            "post",
            side_effect=[auth, send],
        ):
            self.module.process_delivery(delivery["id"])

        bot_history_item = {
            "message_id": "om_forward_reply_test",
            "chat_id": "oc_reply_test",
            "create_time": "1785230000000",
            "msg_type": "text",
            "body": {
                "content": json.dumps(
                    {
                        "text": (
                            "短信转发\n设备：飞书回复测试板\n"
                            f"号码：{original_sender}\n"
                            "时间：2026-07-28T00:00:00+00:00\n"
                            "内容：请回复这条短信"
                        )
                    },
                    ensure_ascii=False,
                )
            },
            "sender": {
                "id": "cli_reply_test",
                "id_type": "app_id",
                "sender_type": "app",
            },
        }
        stale_reply = {
            "message_id": "om_stale_reply",
            "parent_id": "om_forward_reply_test",
            "root_id": "om_forward_reply_test",
            "chat_id": "oc_reply_test",
            "create_time": "1785230001000",
            "msg_type": "text",
            "body": {
                "content": json.dumps(
                    {"text": "旧回复不能追发"},
                    ensure_ascii=False,
                )
            },
            "sender": {
                "id": "ou_reply_operator",
                "id_type": "open_id",
                "sender_type": "user",
            },
        }
        future_reply = {
            **stale_reply,
            "message_id": "om_polled_reply",
            "create_time": "1785230002000",
            "body": {
                "content": json.dumps(
                    {"text": "轮询回复"},
                    ensure_ascii=False,
                )
            },
        }
        with self.module.db_connect() as db:
            destination_row = db.execute(
                "SELECT id, name, config_json FROM destinations WHERE id=?",
                (destination_id,),
            ).fetchone()
            initial_queued = self.module.handle_feishu_polled_items(
                db,
                destination_row,
                [bot_history_item, stale_reply],
                True,
            )
            polled_queued = self.module.handle_feishu_polled_items(
                db,
                destination_row,
                [bot_history_item, stale_reply, future_reply],
                False,
            )
            self.assertEqual(initial_queued, 0)
            self.assertEqual(polled_queued, 1)

        poll_ack_auth = Mock(status_code=200, text='{"code":0}')
        poll_ack_auth.json.return_value = {
            "code": 0,
            "tenant_access_token": "tenant-reply-test",
        }
        poll_ack_reply = Mock(status_code=200, text='{"code":0}')
        poll_ack_reply.json.return_value = {"code": 0}
        poll_ack_reaction = Mock(status_code=200, text='{"code":0}')
        poll_ack_reaction.json.return_value = {"code": 0}
        with patch.object(
            self.module.requests,
            "post",
            side_effect=[poll_ack_auth, poll_ack_reply, poll_ack_reaction],
        ):
            self.assertTrue(
                self.module.process_feishu_reply_acknowledgement()
            )

        with self.module.db_connect() as db:
            polled_outbound = db.execute(
                """
                SELECT o.id, o.body
                FROM outbound_sms o
                JOIN feishu_sms_replies fr ON fr.outbound_sms_id=o.id
                WHERE fr.inbound_message_id='om_polled_reply'
                """
            ).fetchone()
            self.assertEqual(polled_outbound["body"], "轮询回复")
            db.execute(
                "DELETE FROM outbound_sms WHERE id=?",
                (polled_outbound["id"],),
            )
            db.execute(
                "DELETE FROM feishu_reply_inbox WHERE inbound_message_id=?",
                ("om_polled_reply",),
            )
            db.execute(
                "DELETE FROM feishu_poll_seen WHERE destination_id=?",
                (destination_id,),
            )

        challenge = self.client.post(
            "/api/integrations/feishu/events",
            json={
                "type": "url_verification",
                "token": "verify-reply-test",
                "challenge": "challenge-test",
            },
        )
        self.assertEqual(challenge.status_code, 200)
        self.assertEqual(challenge.json["challenge"], "challenge-test")
        denied = self.client.post(
            "/api/integrations/feishu/events",
            json={
                "type": "url_verification",
                "token": "wrong-token",
                "challenge": "challenge-test",
            },
        )
        self.assertEqual(denied.status_code, 401)

        def event_payload(event_id, message_id, sender_type="user", parent_id=""):
            return {
                "schema": "2.0",
                "header": {
                    "event_id": event_id,
                    "event_type": "im.message.receive_v1",
                    "token": "verify-reply-test",
                    "app_id": "cli_reply_test",
                },
                "event": {
                    "sender": {
                        "sender_type": sender_type,
                        "sender_id": {"open_id": "ou_reply_operator"},
                    },
                    "message": {
                        "message_id": message_id,
                        "parent_id": parent_id,
                        "root_id": parent_id,
                        "chat_id": "oc_reply_test",
                        "chat_type": "group",
                        "message_type": "text",
                        "content": json.dumps(
                            {"text": "收到，我稍后联系你"},
                            ensure_ascii=False,
                        ),
                    },
                },
            }

        ordinary = self.client.post(
            "/api/integrations/feishu/events",
            json=event_payload("evt-ordinary", "om_ordinary"),
        )
        self.assertEqual(ordinary.status_code, 200)
        self.assertFalse(ordinary.json["queued"])

        reply_payload = event_payload(
            "evt-sms-reply",
            "om_human_reply",
            parent_id="om_forward_reply_test",
        )
        queued = self.client.post(
            "/api/integrations/feishu/events",
            json=reply_payload,
        )
        self.assertEqual(queued.status_code, 200)
        self.assertTrue(queued.json["queued"])
        self.assertFalse(queued.json["duplicate"])
        self.assertTrue(queued.json["acknowledgement_pending"])
        self.assertIsNone(queued.json["outbound_sms_id"])

        duplicate = self.client.post(
            "/api/integrations/feishu/events",
            json=reply_payload,
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json["duplicate"])
        self.assertIsNone(duplicate.json["outbound_sms_id"])
        bot_message = self.client.post(
            "/api/integrations/feishu/events",
            json=event_payload(
                "evt-bot-reply",
                "om_bot_reply",
                sender_type="app",
                parent_id="om_forward_reply_test",
            ),
        )
        self.assertFalse(bot_message.json["queued"])

        with self.module.db_connect() as db:
            outbound_before_ack = db.execute(
                """
                SELECT COUNT(*)
                FROM outbound_sms o
                JOIN feishu_sms_replies fr ON fr.outbound_sms_id=o.id
                WHERE fr.inbound_message_id='om_human_reply'
                """
            ).fetchone()[0]
        self.assertEqual(outbound_before_ack, 0)

        failed_ack_auth = Mock(status_code=200, text='{"code":0}')
        failed_ack_auth.json.return_value = {
            "code": 0,
            "tenant_access_token": "tenant-reply-test",
        }
        failed_ack_reply = Mock(status_code=200, text='{"code":0}')
        failed_ack_reply.json.return_value = {"code": 0}
        failed_ack_reaction = Mock(
            status_code=400,
            text='{"code":230001,"msg":"reaction failed"}',
        )
        failed_ack_reaction.json.return_value = {
            "code": 230001,
            "msg": "reaction failed",
        }
        with patch.object(
            self.module.requests,
            "post",
            side_effect=[
                failed_ack_auth,
                failed_ack_reply,
                failed_ack_reaction,
            ],
        ) as post:
            self.assertTrue(
                self.module.process_feishu_reply_acknowledgement()
            )
        self.assertIn(
            "/om_human_reply/reply",
            post.call_args_list[1].args[0],
        )
        self.assertIn(
            "已读，正在处理。",
            post.call_args_list[1].kwargs["json"]["content"],
        )
        self.assertIn(
            "/om_human_reply/reactions",
            post.call_args_list[2].args[0],
        )

        with self.module.db_connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM outbound_sms o "
                    "JOIN feishu_sms_replies fr ON fr.outbound_sms_id=o.id "
                    "WHERE fr.inbound_message_id='om_human_reply'"
                ).fetchone()[0],
                0,
            )
            db.execute(
                """
                UPDATE feishu_reply_inbox
                SET next_acknowledgement_at=0
                WHERE inbound_message_id='om_human_reply'
                """
            )

        ack_auth = Mock(status_code=200, text='{"code":0}')
        ack_auth.json.return_value = {
            "code": 0,
            "tenant_access_token": "tenant-reply-test",
        }
        ack_reply = Mock(status_code=200, text='{"code":0}')
        ack_reply.json.return_value = {"code": 0}
        ack_reaction = Mock(status_code=200, text='{"code":0}')
        ack_reaction.json.return_value = {"code": 0}
        with patch.object(
            self.module.requests,
            "post",
            side_effect=[ack_auth, ack_reply, ack_reaction],
        ) as post:
            self.assertTrue(
                self.module.process_feishu_reply_acknowledgement()
            )
        self.assertEqual(
            post.call_args_list[2].kwargs["json"]["reaction_type"]["emoji_type"],
            "OK",
        )

        with self.module.db_connect() as db:
            outbound_row = db.execute(
                """
                SELECT o.*
                FROM outbound_sms o
                JOIN feishu_sms_replies fr ON fr.outbound_sms_id=o.id
                WHERE fr.inbound_message_id='om_human_reply'
                """
            ).fetchone()
            outbound_id = outbound_row["id"]
            outbound = dict(
                outbound_row
            )
            reply_count = db.execute(
                """
                SELECT COUNT(*)
                FROM feishu_sms_replies
                WHERE outbound_sms_id=?
                """,
                (outbound_id,),
            ).fetchone()[0]
        self.assertEqual(outbound["device_id"], device_id)
        self.assertEqual(outbound["recipient"], original_sender)
        self.assertEqual(outbound["body"], "收到，我稍后联系你")
        self.assertEqual(reply_count, 1)
        self.assertFalse(self.module.process_feishu_notification())

        with self.module.db_connect() as db:
            db.execute(
                """
                UPDATE outbound_sms
                SET status='sent', sent_at=?, completed_at=?
                WHERE id=?
                """,
                ("2026-07-28T12:00:00+00:00", "2026-07-28T12:00:00+00:00", outbound_id),
            )
            db.execute(
                """
                UPDATE feishu_sms_replies
                SET next_notification_at=0
                WHERE outbound_sms_id=?
                """,
                (outbound_id,),
            )
        success_auth = Mock(status_code=200, text='{"code":0}')
        success_auth.json.return_value = {
            "code": 0,
            "tenant_access_token": "tenant-reply-test",
        }
        success_send = Mock(status_code=200, text='{"code":0}')
        success_send.json.return_value = {"code": 0}
        with patch.object(
            self.module.requests,
            "post",
            side_effect=[success_auth, success_send],
        ) as post:
            self.assertTrue(self.module.process_feishu_notification())
        self.assertIn(
            "发送成功",
            post.call_args_list[1].kwargs["json"]["content"],
        )
        self.assertFalse(self.module.process_feishu_notification())

        with self.module.db_connect() as db:
            db.execute("DELETE FROM outbound_sms WHERE id=?", (outbound_id,))
            db.execute("DELETE FROM destinations WHERE id=?", (destination_id,))

    def test_wecom_group_robot_delivery(self):
        response = Mock(status_code=200, text='{"errcode":0,"errmsg":"ok"}')
        response.json.return_value = {"errcode": 0, "errmsg": "ok"}
        destination = {
            "kind": "wecom_webhook",
            "config_json": '{"url":"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test"}',
        }
        message = {
            "id": 2,
            "device_id": "dev-wecom",
            "device_label": "企业微信测试设备",
            "sender": "10000",
            "sms_time": "2026-07-27 08:00:00",
            "body": "企微转发测试",
            "received_at": "2026-07-27T08:00:00+00:00",
        }
        with patch.object(self.module.requests, "post", return_value=response) as post:
            ok, status, error, _ = self.module.deliver(destination, message)
        self.assertTrue(ok)
        self.assertEqual(status, 200)
        self.assertEqual(error, "")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["msgtype"], "text")
        self.assertIn("企微转发测试", payload["text"]["content"])

    def test_channel_group_routes_multiple_devices(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        for device_id, phone_number in (
            ("dev-group-a", "18500000001"),
            ("dev-group-b", "18500000002"),
        ):
            response = self.client.post(
                "/api/device/register",
                json={
                    "token": "sms-sb",
                    "device_id": device_id,
                    "phone_number": phone_number,
                },
            )
            self.assertEqual(response.status_code, 200)

        destination = self.client.post(
            "/api/admin/destinations",
            headers=headers,
            json={
                "name": "group-wecom",
                "kind": "wecom_webhook",
                "config": {
                    "url": (
                        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
                        "?key=group-test"
                    )
                },
            },
        )
        self.assertEqual(destination.status_code, 200)
        group = self.client.post(
            "/api/admin/channel-groups",
            headers=headers,
            json={
                "name": "双卡通道组",
                "description": "两张卡使用同一企业微信群",
                "device_ids": ["dev-group-a", "dev-group-b"],
                "destination_ids": [destination.json["id"]],
            },
        )
        self.assertEqual(group.status_code, 200)

        # A legacy route pointing to the same target remains compatible and
        # must not create a duplicate delivery.
        legacy = self.client.post(
            "/api/admin/routes",
            headers=headers,
            json={
                "device_id": "dev-group-a",
                "destination_id": destination.json["id"],
            },
        )
        self.assertEqual(legacy.status_code, 200)

        for device_id in ("dev-group-a", "dev-group-b"):
            message = self.client.post(
                "/api/messages",
                json={
                    "token": "sms-sb",
                    "device_id": device_id,
                    "message_id": f"msg-{device_id}",
                    "sender": "10086",
                    "body": f"{device_id} message",
                },
            )
            self.assertEqual(message.status_code, 200)
            self.assertEqual(message.json["delivery_count"], 1)

        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        stored_group = next(
            item
            for item in snapshot.json["channel_groups"]
            if item["id"] == group.json["id"]
        )
        self.assertEqual(
            set(stored_group["device_ids"]),
            {"dev-group-a", "dev-group-b"},
        )
        self.assertEqual(
            stored_group["destination_ids"],
            [destination.json["id"]],
        )

    def test_successful_delivery_exposes_delivery_time(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        destination = self.client.post(
            "/api/admin/destinations",
            headers=headers,
            json={
                "name": "delivery-time-hook",
                "kind": "webhook",
                "config": {"url": "https://example.test/delivery-time"},
            },
        )
        self.assertEqual(destination.status_code, 200)
        route = self.client.post(
            "/api/admin/routes",
            headers=headers,
            json={
                "device_id": "dev-delivery-time",
                "destination_id": destination.json["id"],
            },
        )
        self.assertEqual(route.status_code, 200)
        message = self.client.post(
            "/api/messages",
            json={
                "token": "sms-sb",
                "device_id": "dev-delivery-time",
                "message_id": "msg-delivery-time",
                "sender": "10000",
                "body": "delivery time test",
            },
        )
        self.assertEqual(message.status_code, 200)

        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        delivery = next(
            item
            for item in snapshot.json["deliveries"]
            if item["device_id"] == "dev-delivery-time"
            and item["destination_id"] == destination.json["id"]
        )
        with patch.object(
            self.module,
            "deliver",
            return_value=(True, 200, "", '{"code":0}'),
        ):
            self.module.process_delivery(delivery["id"])

        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        delivered = next(
            item
            for item in snapshot.json["deliveries"]
            if item["id"] == delivery["id"]
        )
        self.assertEqual(delivered["status"], "delivered")
        self.assertIsNotNone(delivered["delivered_at"])

        console_response = self.client.get("/")
        console = console_response.get_data(as_text=True)
        console_response.close()
        self.assertIn("转发时间", console)
        self.assertIn("fmt(d.delivered_at||d.forward_sent_at)", console)
        self.assertIn("通道组", console)
        self.assertIn("企业微信群机器人", console)
        self.assertIn("发短信", console)
        self.assertIn("outbound-sms", console)
        self.assertIn("deleteOutbound", console)
        self.assertIn('data-page="calls"', console)
        self.assertIn('id="page-calls"', console)
        self.assertIn("来电记录", console)
        self.assertIn("开启电话通知转发后按通道组发送通知", console)
        self.assertNotIn("'呼入时间','挂断时间','响铃时长'", console)
        self.assertIn("脚本：${esc(d.app_version||'未上报')}", console)
        self.assertIn("固件：${esc(d.firmware||'未上报')}", console)
        self.assertIn("累计估算流量", console)
        self.assertIn("trafficKb(d.traffic_total_bytes)", console)

    def test_mcp_token_lifecycle_and_storage(self):
        headers = {"X-SMS-Admin-Token": "admin-test"}
        denied = self.client.post(
            "/api/admin/mcp-tokens",
            json={"name": "denied"},
        )
        self.assertEqual(denied.status_code, 401)

        created = self.client.post(
            "/api/admin/mcp-tokens",
            headers=headers,
            json={"name": "test reader", "expires_in_days": 30},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.headers["Cache-Control"], "no-store")
        raw_token = created.json["token"]
        token_id = created.json["id"]
        self.assertTrue(raw_token.startswith("smsmcp_"))
        self.assertEqual(created.json["scopes"], "read,send")

        with self.module.db_connect() as db:
            stored = dict(
                db.execute(
                    "SELECT * FROM mcp_tokens WHERE id=?",
                    (token_id,),
                ).fetchone()
            )
        self.assertEqual(
            stored["token_hash"],
            hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(stored["token_value"], raw_token)
        self.assertEqual(stored["scopes"], "read,send")

        snapshot = self.client.get("/api/admin/snapshot", headers=headers)
        self.assertEqual(snapshot.headers["Cache-Control"], "no-store")
        token_metadata = next(
            item
            for item in snapshot.json["mcp_tokens"]
            if item["id"] == token_id
        )
        self.assertEqual(token_metadata["status"], "active")
        self.assertEqual(token_metadata["token"], raw_token)
        self.assertNotIn("token_hash", token_metadata)
        self.assertNotIn("token_value", token_metadata)

        authenticated = self.module.authenticate_mcp_token(raw_token)
        self.assertEqual(authenticated["id"], token_id)
        self.assertIsNotNone(authenticated["last_used_at"])
        self.assertIsNone(self.module.authenticate_mcp_token("smsmcp_invalid"))

        revoked = self.client.delete(
            f"/api/admin/mcp-tokens/{token_id}",
            headers=headers,
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertIsNone(self.module.authenticate_mcp_token(raw_token))
        listed = self.client.get("/api/admin/mcp-tokens", headers=headers)
        self.assertEqual(listed.headers["Cache-Control"], "no-store")
        revoked_metadata = next(
            item for item in listed.json["tokens"] if item["id"] == token_id
        )
        self.assertEqual(revoked_metadata["status"], "revoked")
        self.assertEqual(revoked_metadata["token"], raw_token)

        denied_delete = self.client.delete(
            f"/api/admin/mcp-tokens/{token_id}/permanent"
        )
        self.assertEqual(denied_delete.status_code, 401)
        deleted = self.client.delete(
            f"/api/admin/mcp-tokens/{token_id}/permanent",
            headers=headers,
        )
        self.assertEqual(deleted.status_code, 200)
        listed = self.client.get("/api/admin/mcp-tokens", headers=headers)
        self.assertFalse(
            any(item["id"] == token_id for item in listed.json["tokens"])
        )
        missing = self.client.delete(
            f"/api/admin/mcp-tokens/{token_id}/permanent",
            headers=headers,
        )
        self.assertEqual(missing.status_code, 404)

    def test_mcp_exposes_queries_and_idempotent_sms_send(self):
        tools = asyncio.run(self.mcp_module.mcp.list_tools())
        self.assertEqual(
            {tool.name for tool in tools},
            {
                "get_system_status",
                "list_devices",
                "search_messages",
                "list_missed_calls",
                "list_forwarding_records",
                "list_outbound_sms",
                "get_routing_summary",
                "send_sms",
            },
        )
        for tool in tools:
            if tool.name == "send_sms":
                continue
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)

        send_tool = next(tool for tool in tools if tool.name == "send_sms")
        self.assertFalse(send_tool.annotations.readOnlyHint)
        self.assertFalse(send_tool.annotations.destructiveHint)
        self.assertTrue(send_tool.annotations.idempotentHint)
        self.assertTrue(send_tool.annotations.openWorldHint)

        status = self.mcp_module.get_system_status()
        self.assertIn("devices", status)
        routing = self.mcp_module.get_routing_summary()
        self.assertNotIn("config_json", json.dumps(routing))
        self.assertNotIn("app_secret", json.dumps(routing))

        device_id = "dev-mcp-send"
        registered = self.client.post(
            "/api/device/register",
            json={
                "token": "sms-sb",
                "device_id": device_id,
                "name": "MCP 发送测试板",
                "phone_number": "18500000888",
            },
        )
        self.assertEqual(registered.status_code, 200)

        first = self.mcp_module.send_sms(
            device_id=device_id,
            recipient="13900000123",
            body="MCP send test",
            request_id="mcp-send-idempotency-1",
        )
        duplicate = self.mcp_module.send_sms(
            device_id=device_id,
            recipient="13900000123",
            body="MCP send test",
            request_id="mcp-send-idempotency-1",
        )
        self.assertTrue(first["queued"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(
            duplicate["outbound_sms_id"],
            first["outbound_sms_id"],
        )
        with self.assertRaisesRegex(ValueError, "different SMS parameters"):
            self.mcp_module.send_sms(
                device_id=device_id,
                recipient="13900000123",
                body="changed body",
                request_id="mcp-send-idempotency-1",
            )

        with self.module.db_connect() as db:
            queued = db.execute(
                "SELECT * FROM outbound_sms WHERE id=?",
                (first["outbound_sms_id"],),
            ).fetchone()
            self.assertEqual(queued["status"], "pending")
            self.assertEqual(
                queued["mcp_request_id"],
                "mcp-send-idempotency-1",
            )
            db.execute(
                "DELETE FROM outbound_sms WHERE id=?",
                (first["outbound_sms_id"],),
            )
            db.execute("DELETE FROM devices WHERE id=?", (device_id,))

    def test_console_includes_mcp_token_management(self):
        response = self.client.get("/")
        console = response.get_data(as_text=True)
        response.close()
        self.assertIn("send_sms", console)
        self.assertIn("read,send", console)
        self.assertIn("创建 MCP Token", console)
        self.assertIn("mcpTokenForm", console)
        self.assertIn("revokeMcpToken", console)
        self.assertIn("deleteMcpToken", console)
        self.assertIn("完整 Token、哈希和使用记录都会删除", console)
        self.assertIn("Token 全文", console)
        self.assertIn("copyMcpStoredToken", console)
        self.assertIn("@media(max-width:640px)", console)
        self.assertIn(
            "grid-template-columns:repeat(3,minmax(0,1fr))",
            console,
        )
        self.assertIn("content:attr(data-label)", console)
        self.assertIn("applyTableLabels", console)
        self.assertIn("smsTemplateForm", console)
        self.assertIn("{{ori}} 原号码", console)
        self.assertIn("{{phone}} 当前设备号码", console)
        self.assertIn("destinationTemplate", console)
        self.assertIn("templatedDestinationKinds", console)
        self.assertIn("短信转发和飞书通道均可选择", console)
        self.assertIn('data-page="templates"', console)
        self.assertIn('id="page-templates"', console)
        self.assertNotIn("兼容旧版直连路由", console)
        self.assertNotIn('id="routeForm"', console)
        self.assertIn("new URL('mcp/',location.href)", console)


if __name__ == "__main__":
    unittest.main()
