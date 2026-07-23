CREATE TABLE IF NOT EXISTS {table} (
    id BIGSERIAL PRIMARY KEY,
    source_id VARCHAR(100),
    safety_code VARCHAR(100) NOT NULL,
    company_name VARCHAR(500),
    belong_company VARCHAR(300),
    safety_type VARCHAR(200),
    category VARCHAR(200),
    risk_level VARCHAR(50),
    theme TEXT,
    create_by VARCHAR(200),
    create_time TIMESTAMP WITHOUT TIME ZONE,
    collect_time TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_data JSONB,
    CONSTRAINT {unique_constraint} UNIQUE (safety_code)
);

COMMENT ON TABLE {table} IS '工单日常管理网页抓取及自动归类数据';
COMMENT ON COLUMN {table}.safety_code IS '单据编号';
