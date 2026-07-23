CREATE TABLE IF NOT EXISTS {schema}."workorder_monitor_checkpoints" (
    target_table VARCHAR(300) NOT NULL,
    stream_name VARCHAR(200) NOT NULL,
    last_success_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (target_table, stream_name)
);

ALTER TABLE {schema}."workorder_feishu_notification_outbox"
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMP WITHOUT TIME ZONE
    NOT NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE {schema}."workorder_feishu_notification_outbox"
    ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(200);

ALTER TABLE {schema}."workorder_feishu_notification_outbox"
    ADD COLUMN IF NOT EXISTS lease_until TIMESTAMP WITHOUT TIME ZONE;

ALTER TABLE {schema}."workorder_feishu_notification_outbox"
    ADD COLUMN IF NOT EXISTS error_kind VARCHAR(100);

ALTER TABLE {schema}."workorder_feishu_notification_outbox"
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE
    NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE INDEX IF NOT EXISTS "idx_workorder_feishu_outbox_due"
    ON {schema}."workorder_feishu_notification_outbox"
        (target_table, next_attempt_at, created_at, safety_code)
    WHERE sent_at IS NULL;

CREATE INDEX IF NOT EXISTS "idx_workorder_monitor_checkpoints_updated"
    ON {schema}."workorder_monitor_checkpoints" (updated_at);
