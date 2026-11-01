CREATE TABLE worker_heartbeat (
    worker_name text PRIMARY KEY,
    beat_at timestamptz NOT NULL
);
