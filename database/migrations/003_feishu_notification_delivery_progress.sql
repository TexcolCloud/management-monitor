ALTER TABLE {schema}."workorder_feishu_notification_outbox"
    ADD COLUMN IF NOT EXISTS card_sent_at TIMESTAMP WITHOUT TIME ZONE;

ALTER TABLE {schema}."workorder_feishu_notification_outbox"
    ADD COLUMN IF NOT EXISTS sent_image_paths JSONB NOT NULL DEFAULT '[]'::jsonb;
