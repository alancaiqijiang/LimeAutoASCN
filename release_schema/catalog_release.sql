PRAGMA foreign_keys = ON;

CREATE TABLE catalog_releases (
    release_id TEXT NOT NULL,
    release_no TEXT NOT NULL UNIQUE,
    source_system TEXT,
    source_snapshot TEXT,
    source_snapshot_fingerprint TEXT NOT NULL DEFAULT '',
    source_counts_json TEXT NOT NULL DEFAULT '{}',
    validation_summary_json TEXT NOT NULL DEFAULT '{}',
    schema_version TEXT NOT NULL DEFAULT 'n2a',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'failed', 'validated', 'published', 'retired')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    PRIMARY KEY (release_id)
);

CREATE TABLE release_models (
    release_id TEXT NOT NULL,
    series_code TEXT NOT NULL,
    model_code TEXT NOT NULL,
    series_name_source TEXT,
    model_name_source TEXT,
    model_name TEXT,
    publish_status TEXT NOT NULL DEFAULT 'published',
    PRIMARY KEY (release_id, series_code, model_code),
    FOREIGN KEY (release_id) REFERENCES catalog_releases (release_id)
);

CREATE TABLE system_nodes (
    release_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    series_code TEXT NOT NULL,
    model_code TEXT NOT NULL,
    source_obj_code TEXT,
    source_tree_key TEXT,
    path_key TEXT NOT NULL,
    parent_key TEXT,
    node_path_source TEXT,
    name_source TEXT,
    display_name TEXT,
    depth INTEGER NOT NULL DEFAULT 0,
    is_derived INTEGER NOT NULL DEFAULT 0 CHECK (is_derived IN (0, 1)),
    path_variant_count INTEGER NOT NULL DEFAULT 1 CHECK (path_variant_count >= 0),
    child_count INTEGER NOT NULL DEFAULT 0 CHECK (child_count >= 0),
    direct_part_count INTEGER NOT NULL DEFAULT 0 CHECK (direct_part_count >= 0),
    descendant_part_count INTEGER NOT NULL DEFAULT 0 CHECK (descendant_part_count >= 0),
    publish_status TEXT NOT NULL DEFAULT 'published',
    PRIMARY KEY (release_id, node_key),
    FOREIGN KEY (release_id, series_code, model_code)
        REFERENCES release_models (release_id, series_code, model_code)
);

CREATE TABLE catalog_parts (
    release_id TEXT NOT NULL,
    material_code TEXT NOT NULL,
    display_name_source TEXT,
    description TEXT,
    manual_code TEXT,
    source_detail_status TEXT NOT NULL DEFAULT 'unavailable',
    publish_status TEXT NOT NULL DEFAULT 'reference_only',
    status TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY (release_id, material_code),
    FOREIGN KEY (release_id) REFERENCES catalog_releases (release_id)
);

CREATE TABLE fitments (
    release_id TEXT NOT NULL,
    source_occurrence_key TEXT NOT NULL
        CHECK (length(trim(coalesce(source_occurrence_key, ''))) > 0),
    series_code TEXT NOT NULL,
    model_code TEXT NOT NULL,
    node_key TEXT NOT NULL,
    material_code TEXT NOT NULL,
    callout TEXT,
    quantity INTEGER,
    quantity_raw TEXT,
    manual_code TEXT,
    fitment_note TEXT,
    fitment_level TEXT,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    PRIMARY KEY (release_id, source_occurrence_key),
    FOREIGN KEY (release_id, series_code, model_code)
        REFERENCES release_models (release_id, series_code, model_code),
    FOREIGN KEY (release_id, node_key)
        REFERENCES system_nodes (release_id, node_key),
    FOREIGN KEY (release_id, material_code)
        REFERENCES catalog_parts (release_id, material_code)
);

CREATE TABLE catalog_assets (
    release_id TEXT NOT NULL,
    asset_key TEXT NOT NULL
        CHECK (length(trim(asset_key)) > 0),
    material_code TEXT,
    system_node_key TEXT,
    asset_type TEXT NOT NULL
        CHECK (asset_type IN ('material_image', 'epc_drawing', 'thumbnail', 'other')),
    object_key TEXT NOT NULL
        CHECK (
            length(trim(object_key)) > 0
            AND substr(object_key, 1, 1) <> '/'
            AND instr(object_key, char(92)) = 0
            AND instr(object_key, '..') = 0
            AND instr(object_key, ':') = 0
            AND instr(object_key, '?') = 0
            AND instr(object_key, '#') = 0
            AND instr(object_key, '=') = 0
            AND instr(object_key, '://') = 0
        ),
    source_sha256 TEXT
        CHECK (
            source_sha256 IS NULL
            OR (
                length(source_sha256) = 64
                AND source_sha256 NOT GLOB '*[^0-9A-Fa-f]*'
            )
        ),
    size_bytes INTEGER
        CHECK (
            size_bytes IS NULL
            OR (typeof(size_bytes) = 'integer' AND size_bytes >= 0)
        ),
    mime_type TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'ready', 'done', 'available', 'failed', 'missing')),
    CHECK (
        (length(trim(coalesce(material_code, ''))) > 0)
        + (length(trim(coalesce(system_node_key, ''))) > 0) = 1
    ),
    PRIMARY KEY (release_id, asset_key),
    FOREIGN KEY (release_id) REFERENCES catalog_releases (release_id),
    FOREIGN KEY (release_id, material_code)
        REFERENCES catalog_parts (release_id, material_code),
    FOREIGN KEY (release_id, system_node_key)
        REFERENCES system_nodes (release_id, node_key)
);

CREATE INDEX system_nodes_path_idx
    ON system_nodes (release_id, series_code, model_code, path_key);

CREATE INDEX system_nodes_parent_idx
    ON system_nodes (release_id, series_code, model_code, parent_key);

CREATE INDEX fitments_node_idx
    ON fitments (release_id, node_key);

CREATE INDEX fitments_material_idx
    ON fitments (release_id, material_code);

CREATE INDEX catalog_assets_hash_idx
    ON catalog_assets (release_id, source_sha256);

CREATE INDEX catalog_assets_material_idx
    ON catalog_assets (release_id, material_code, asset_type, status);

CREATE INDEX catalog_assets_node_type_idx
    ON catalog_assets (release_id, system_node_key, asset_type, status);
