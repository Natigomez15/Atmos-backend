CREATE TABLE IF NOT EXISTS atmos_decision_events (
    id BIGSERIAL PRIMARY KEY,
    timestamp_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tipo TEXT NOT NULL,
    motivo TEXT,
    pabellon TEXT,
    aire TEXT,
    sala_id UUID,
    nodo_id UUID,
    decision_final TEXT,
    comando_ir TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_atmos_decision_events_timestamp
    ON atmos_decision_events (timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_atmos_decision_events_tipo
    ON atmos_decision_events (tipo);

CREATE INDEX IF NOT EXISTS idx_atmos_decision_events_espacio
    ON atmos_decision_events (pabellon, aire);
