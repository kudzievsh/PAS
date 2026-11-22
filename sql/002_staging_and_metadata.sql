CREATE OR REPLACE VIEW staging.v_requests AS
 SELECT unique_key,created_at,created_at::date AS request_date,
 trim(payload->>'complaint_type') AS category_name,
 coalesce(nullif(upper(trim(payload->>'community_board')),''),'UNKNOWN') AS community_board,
 CASE WHEN upper(trim(payload->>'borough')) IN ('BRONX','BROOKLYN','MANHATTAN','QUEENS','STATEN ISLAND')
      THEN upper(trim(payload->>'borough')) ELSE 'UNKNOWN' END AS borough,
 coalesce(nullif(trim(payload->>'agency'),''),'UNKNOWN') AS agency,
 coalesce(nullif(trim(payload->>'status'),''),'Unknown') AS status,load_id
 FROM raw.service_request;
CREATE OR REPLACE VIEW staging.v_weather AS
 SELECT station_id,observation_date,
 nullif(payload->>'TMIN','')::double precision AS temperature_min,
 nullif(payload->>'TMAX','')::double precision AS temperature_max,
 nullif(payload->>'PRCP','')::double precision AS precipitation,
 nullif(payload->>'SNOW','')::double precision AS snow,load_id FROM raw.weather;
CREATE TABLE IF NOT EXISTS meta.source_schema (
 source_name text,schema_hash text,observed_at timestamptz DEFAULT now(),
 fields jsonb NOT NULL,load_id uuid REFERENCES ops.load_log,
 PRIMARY KEY(source_name,schema_hash)
);
INSERT INTO meta.lineage VALUES
 ('raw.service_request','["NYC Open Data erm2-nwe9 JSON"]','HTTP extraction, keyset pagination, SHA256, upsert','src/ingestion.py','Selected non-personal fields of service requests'),
 ('raw.weather','["NOAA daily-summaries CSV USW00094728"]','CSV extraction, metric units, station-date upsert','src/ingestion.py','Central Park daily weather observations'),
 ('staging.v_requests','["raw.service_request"]','Trim category, uppercase district and borough, map unknowns','sql/002_staging_and_metadata.sql','Normalize source values without altering the retained payload'),
 ('core.service_request','["staging.v_requests","core.category","core.district"]','Resolve natural keys, typed fields, FK constraints','src/processing.py','One current request per unique_key'),
 ('core.request_history','["raw.service_request","NYC Open Data erm2-nwe9 JSON"]','Close current version when source_hash changes and insert observed version','src/ingestion.py','SCD Type 2 for observed changes, not reconstruction of unobserved past'),
 ('mart.daily_requests','["core.service_request","core.weather","ops.watermark"]','Daily COUNT, complete date grid, left join weather on date','src/processing.py','Number of requests by local NYC day, category and community board'),
 ('mart.prediction','["mart.daily_requests","mart.model_run"]','Weekly seasonal baseline and pooled direct ridge regression, seven horizons','src/modeling.py','Nonnegative forecasts from features available at the forecast origin'),
 ('Предметный дашборд','["mart.v_daily_requests","mart.v_actual_forecast","mart.v_forecast","mart.v_model_metrics"]','Superset SQL aggregations and native filters','src/dashboard.py','Actual demand, workload forecast and validation errors'),
 ('Операционный дашборд','["ops.v_loads","ops.v_quality","ops.v_freshness"]','Superset SQL aggregations, statuses, source filters','src/dashboard.py','Pipeline condition, freshness, rejects and quality checks')
ON CONFLICT(output_asset) DO UPDATE SET input_assets=excluded.input_assets,transformation=excluded.transformation,source_file=excluded.source_file,description=excluded.description;
