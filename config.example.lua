-- Copy this file to config.lua.
return {
    center_url = "https://bytegallop.com/sms",
    center_token = "token-abc",
    device_name = "GK21.5PTM",
    phone_number = "",
    device_id = "",

    queue_file = "/ldata/sms_center_outbox.json",
    command_result_file = "/ldata/sms_center_command_results.json",
    max_queue_size = 200,
    http_timeout_ms = 30000,
    startup_delay_ms = 12000,
    heartbeat_interval_ms = 60000,
    command_poll_interval_ms = 60000,
    long_connection_enabled = true,
    long_connection_url = "",
    long_connection_connect_timeout_ms = 30000,
    long_connection_status_interval_ms = 600000,
    long_connection_reconnect_delays_ms = {
        5000, 15000, 60000, 300000, 900000, 3600000
    },
    -- Used only while the WebSocket is unavailable. Keep this below the
    -- center's 180-second offline threshold.
    fallback_sync_interval_ms = 120000,
    command_send_timeout_ms = 600000,
    network_watchdog_ms = 900000,
    traffic_stats_enabled = true,
    traffic_stats_interval_seconds = 60,
    enable_missed_call_forwarding = true,
    ota_enabled = false,
    ota_product_key = "",
    ota_startup_delay_ms = 120000,
    ota_check_interval_ms = 21600000,
    ota_busy_retry_ms = 60000,
    ota_watchdog_ms = 1800000,
    retry_delays_ms = {5000, 15000, 60000, 300000, 900000}
}
