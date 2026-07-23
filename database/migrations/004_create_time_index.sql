CREATE INDEX IF NOT EXISTS {create_time_index}
    ON {table} (create_time, safety_code);
