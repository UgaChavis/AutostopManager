-- Automotive Repair Knowledge Base schema
-- Generated: 2026-05-03
-- Purpose: CRM/agent-ready schema for authoritative automotive repair data.
-- Notes:
--   1) Do not ingest copyrighted OEM/book/database text without a license.
--   2) Store source_id and source_url for every derived fact.
--   3) For safety-critical procedures, prefer licensed OEM service information.

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    publisher TEXT,
    category TEXT,
    source_type TEXT,
    url TEXT,
    access TEXT,
    legal_ingestion_status TEXT,
    priority_score INTEGER CHECK (priority_score BETWEEN 1 AND 5),
    recommended_ingestion_route TEXT,
    notes TEXT,
    last_verified DATE
);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    vin TEXT UNIQUE,
    wmi TEXT,
    year INTEGER,
    make TEXT NOT NULL,
    model TEXT NOT NULL,
    trim TEXT,
    body_class TEXT,
    engine_code TEXT,
    engine_displacement_l REAL,
    fuel_type TEXT,
    transmission TEXT,
    drivetrain TEXT,
    market_region TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vehicles_ymme
ON vehicles (year, make, model, engine_code);

CREATE TABLE IF NOT EXISTS documents (
    document_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    external_document_id TEXT,
    title TEXT NOT NULL,
    document_type TEXT NOT NULL, -- recall, TSB, repair_manual, wiring_diagram, DTC, standard, book, training, parts_catalog
    make TEXT,
    model TEXT,
    year_from INTEGER,
    year_to INTEGER,
    engine_code TEXT,
    system_area TEXT, -- engine, transmission, brake, steering, suspension, HVAC, electrical, ADAS, SRS, body, HV battery, etc.
    language TEXT DEFAULT 'en',
    source_url TEXT,
    publication_date DATE,
    revision_date DATE,
    license_status TEXT,
    checksum_sha256 TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_documents_vehicle_scope
ON documents (make, model, year_from, year_to, engine_code, system_area, document_type);

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    document_id INTEGER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    heading TEXT,
    text_content TEXT NOT NULL,
    token_count INTEGER,
    embedding VECTOR(1536),
    safety_level TEXT DEFAULT 'normal', -- normal, caution, critical
    contains_copyrighted_text BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(document_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS recalls (
    recall_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    external_recall_id TEXT,
    nhtsa_campaign_number TEXT,
    manufacturer_campaign_number TEXT,
    make TEXT,
    model TEXT,
    model_year INTEGER,
    component TEXT,
    summary TEXT,
    consequence TEXT,
    remedy TEXT,
    report_received_date DATE,
    source_url TEXT,
    raw_payload JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_recalls_vehicle
ON recalls (make, model, model_year, component);

CREATE TABLE IF NOT EXISTS technical_service_bulletins (
    tsb_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    external_tsb_id TEXT,
    manufacturer TEXT,
    make TEXT,
    model TEXT,
    year_from INTEGER,
    year_to INTEGER,
    component TEXT,
    title TEXT,
    summary TEXT,
    bulletin_date DATE,
    source_url TEXT,
    raw_payload JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tsb_vehicle
ON technical_service_bulletins (make, model, year_from, year_to, component);

CREATE TABLE IF NOT EXISTS diagnostic_trouble_codes (
    dtc_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    code TEXT NOT NULL, -- e.g. P0301
    code_system TEXT, -- OBD-II, UDS, OEM-specific
    make TEXT,
    model TEXT,
    year_from INTEGER,
    year_to INTEGER,
    engine_code TEXT,
    description TEXT,
    diagnostic_steps_document_id INTEGER REFERENCES documents(document_id),
    source_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dtc_code_vehicle
ON diagnostic_trouble_codes (code, make, model, year_from, year_to);

CREATE TABLE IF NOT EXISTS repair_procedures (
    procedure_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    document_id INTEGER REFERENCES documents(document_id),
    make TEXT,
    model TEXT,
    year_from INTEGER,
    year_to INTEGER,
    engine_code TEXT,
    system_area TEXT,
    operation_name TEXT NOT NULL,
    labor_time_hours REAL,
    torque_specs JSONB,
    fluids JSONB,
    required_tools JSONB,
    safety_warnings JSONB,
    source_url TEXT,
    license_status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wiring_references (
    wiring_reference_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    document_id INTEGER REFERENCES documents(document_id),
    make TEXT,
    model TEXT,
    year_from INTEGER,
    year_to INTEGER,
    system_area TEXT,
    connector_id TEXT,
    pin_number TEXT,
    wire_color TEXT,
    signal_name TEXT,
    source_url TEXT,
    license_status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS parts (
    part_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    make TEXT,
    model TEXT,
    year_from INTEGER,
    year_to INTEGER,
    oem_part_number TEXT,
    superseded_by_part_number TEXT,
    part_name TEXT,
    system_area TEXT,
    fitment_notes TEXT,
    source_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_parts_oem
ON parts (oem_part_number, make, model);

CREATE TABLE IF NOT EXISTS labor_times (
    labor_time_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    make TEXT,
    model TEXT,
    year_from INTEGER,
    year_to INTEGER,
    engine_code TEXT,
    operation_code TEXT,
    operation_name TEXT,
    labor_time_hours REAL,
    source_url TEXT,
    license_status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS customer_cases (
    case_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    crm_external_id TEXT UNIQUE,
    vehicle_id INTEGER REFERENCES vehicles(vehicle_id),
    customer_complaint TEXT,
    symptoms JSONB,
    scan_results JSONB,
    odometer_km INTEGER,
    diagnosis_summary TEXT,
    recommended_work TEXT,
    estimate_status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS case_recommendations (
    recommendation_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    case_id INTEGER NOT NULL REFERENCES customer_cases(case_id) ON DELETE CASCADE,
    recommendation_type TEXT, -- diagnostic_plan, repair_plan, estimate, follow_up
    recommendation_text TEXT NOT NULL,
    confidence_level TEXT, -- low, medium, high
    missing_information JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS citations (
    citation_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    entity_type TEXT NOT NULL, -- recommendation, document, recall, tsb, dtc, procedure, part
    entity_id INTEGER NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    document_id INTEGER REFERENCES documents(document_id),
    source_url TEXT,
    quoted_text TEXT,
    page_or_section TEXT,
    license_status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
