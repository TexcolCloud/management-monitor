CREATE TABLE IF NOT EXISTS {schema}."workorder_feishu_notification_outbox" (
    target_table VARCHAR(300) NOT NULL,
    safety_code VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP WITHOUT TIME ZONE,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (target_table, safety_code)
);

CREATE INDEX IF NOT EXISTS "idx_workorder_feishu_outbox_pending"
    ON {schema}."workorder_feishu_notification_outbox" (target_table, created_at)
    WHERE sent_at IS NULL;
