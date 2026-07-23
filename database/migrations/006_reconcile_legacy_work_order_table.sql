ALTER TABLE {table} ADD COLUMN IF NOT EXISTS id BIGINT;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS source_id VARCHAR(100);
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS safety_code VARCHAR(100);
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS company_name VARCHAR(500);
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS belong_company VARCHAR(300);
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS safety_type VARCHAR(200);
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS category VARCHAR(200);
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS risk_level VARCHAR(50);
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS theme TEXT;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS create_by VARCHAR(200);
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS create_time TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS collect_time TIMESTAMP WITHOUT TIME ZONE
    NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS update_time TIMESTAMP WITHOUT TIME ZONE
    NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS raw_data JSONB;

DO $$
DECLARE
    actual_type TEXT;
    invalid_codes BIGINT;
    duplicate_groups BIGINT;
BEGIN
    SELECT format_type(attribute.atttypid, attribute.atttypmod)
    INTO actual_type
    FROM pg_attribute AS attribute
    WHERE attribute.attrelid = '{table}'::regclass
      AND attribute.attname = 'safety_code'
      AND NOT attribute.attisdropped;
    IF actual_type IS DISTINCT FROM 'character varying(100)' THEN
        RAISE EXCEPTION
            'Cannot reconcile work-order table: safety_code type must be character varying(100), found %',
            COALESCE(actual_type, '<missing>');
    END IF;

    SELECT format_type(attribute.atttypid, attribute.atttypmod)
    INTO actual_type
    FROM pg_attribute AS attribute
    WHERE attribute.attrelid = '{table}'::regclass
      AND attribute.attname = 'create_time'
      AND NOT attribute.attisdropped;
    IF actual_type IS DISTINCT FROM 'timestamp without time zone' THEN
        RAISE EXCEPTION
            'Cannot reconcile work-order table: create_time type must be timestamp without time zone, found %',
            COALESCE(actual_type, '<missing>');
    END IF;

    SELECT format_type(attribute.atttypid, attribute.atttypmod)
    INTO actual_type
    FROM pg_attribute AS attribute
    WHERE attribute.attrelid = '{table}'::regclass
      AND attribute.attname = 'raw_data'
      AND NOT attribute.attisdropped;
    IF actual_type IS DISTINCT FROM 'jsonb' THEN
        RAISE EXCEPTION
            'Cannot reconcile work-order table: raw_data type must be jsonb, found %',
            COALESCE(actual_type, '<missing>');
    END IF;

    SELECT COUNT(*)
    INTO invalid_codes
    FROM {table}
    WHERE safety_code IS NULL OR BTRIM(safety_code) = '';
    IF invalid_codes > 0 THEN
        RAISE EXCEPTION
            'Cannot enforce safety_code constraints: % rows have a null or blank safety_code',
            invalid_codes
            USING HINT = 'Repair those rows explicitly, then rerun the migration; no row was changed.';
    END IF;

    SELECT COUNT(*)
    INTO duplicate_groups
    FROM (
        SELECT safety_code
        FROM {table}
        GROUP BY safety_code
        HAVING COUNT(*) > 1
    ) AS duplicates;
    IF duplicate_groups > 0 THEN
        RAISE EXCEPTION
            'Cannot enforce safety_code uniqueness: % duplicate safety_code groups exist',
            duplicate_groups
            USING HINT = 'Resolve duplicates explicitly, then rerun the migration; no row was deleted.';
    END IF;
END
$$;

ALTER TABLE {table} ALTER COLUMN safety_code SET NOT NULL;

DO $$
DECLARE
    safety_code_attnum SMALLINT;
BEGIN
    SELECT attribute.attnum
    INTO safety_code_attnum
    FROM pg_attribute AS attribute
    WHERE attribute.attrelid = '{table}'::regclass
      AND attribute.attname = 'safety_code'
      AND NOT attribute.attisdropped;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint AS constraint_record
        WHERE constraint_record.conrelid = '{table}'::regclass
          AND constraint_record.contype IN ('p', 'u')
          AND constraint_record.conkey = ARRAY[safety_code_attnum]::SMALLINT[]
    ) THEN
        ALTER TABLE {table}
            ADD CONSTRAINT {unique_constraint} UNIQUE (safety_code);
    END IF;
END
$$;

COMMENT ON TABLE {table} IS '工单日常管理网页抓取及自动归类数据';
COMMENT ON COLUMN {table}.safety_code IS '单据编号';

CREATE INDEX IF NOT EXISTS {create_time_index}
    ON {table} (create_time, safety_code);
