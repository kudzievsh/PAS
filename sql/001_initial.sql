CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS meta.schema_migrations (
  version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ops.load_log (
  load_id uuid PRIMARY KEY, source_name text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
  load_mode text NOT NULL, date_from date NOT NULL, date_to date NOT NULL,
  rows_received bigint NOT NULL DEFAULT 0, rows_inserted bigint NOT NULL DEFAULT 0,
  rows_updated bigint NOT NULL DEFAULT 0, rows_rejected bigint NOT NULL DEFAULT 0,
  status text NOT NULL CHECK(status IN ('STARTED','SUCCESS','FAILED')),
  error_message text, parameters jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS ops.watermark (
  source_name text PRIMARY KEY, complete_through date NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(), load_id uuid REFERENCES ops.load_log
);
CREATE TABLE IF NOT EXISTS ops.loaded_partition (
  source_name text, period_start date, period_end date,
  scope_hash text NOT NULL, load_id uuid REFERENCES ops.load_log,
  row_count bigint NOT NULL, completed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_name,period_start,period_end,scope_hash)
);
CREATE TABLE IF NOT EXISTS raw.service_request (
  unique_key text PRIMARY KEY, created_at timestamp NOT NULL,
  payload jsonb NOT NULL, source_hash text NOT NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
  load_id uuid NOT NULL REFERENCES ops.load_log
);
CREATE INDEX IF NOT EXISTS raw_request_date ON raw.service_request(created_at);
CREATE TABLE IF NOT EXISTS raw.weather (
  station_id text, observation_date date, payload jsonb NOT NULL, source_hash text NOT NULL,
  load_id uuid REFERENCES ops.load_log, fetched_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(station_id,observation_date)
);
CREATE TABLE IF NOT EXISTS ops.rejected_record (
  reject_id bigserial PRIMARY KEY, load_id uuid REFERENCES ops.load_log,
  record_key text, reason text NOT NULL, payload jsonb NOT NULL, rejected_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS core.request_history (
  history_id bigserial PRIMARY KEY, unique_key text NOT NULL,
  source_hash text NOT NULL, payload jsonb NOT NULL,
  valid_from timestamptz NOT NULL DEFAULT now(), valid_to timestamptz,
  load_id uuid REFERENCES ops.load_log
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_request_version
 ON core.request_history(unique_key) WHERE valid_to IS NULL;
CREATE TABLE IF NOT EXISTS core.category (
  category_id bigserial PRIMARY KEY, category_name text NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS core.district (
  district_id bigserial PRIMARY KEY, community_board text NOT NULL,
  borough text NOT NULL, UNIQUE(community_board,borough)
);
CREATE TABLE IF NOT EXISTS core.service_request (
  unique_key text PRIMARY KEY REFERENCES raw.service_request,
  created_at timestamp NOT NULL, request_date date NOT NULL,
  category_id bigint NOT NULL REFERENCES core.category,
  district_id bigint NOT NULL REFERENCES core.district,
  agency text NOT NULL, status text NOT NULL,
  source_load_id uuid REFERENCES ops.load_log
);
CREATE INDEX IF NOT EXISTS core_request_date ON core.service_request(request_date);
CREATE TABLE IF NOT EXISTS core.weather (
  observation_date date PRIMARY KEY, station_id text NOT NULL,
  temperature_min double precision, temperature_max double precision,
  precipitation double precision, snow double precision,
  source_load_id uuid REFERENCES ops.load_log,
  CHECK(temperature_min IS NULL OR temperature_max IS NULL OR temperature_min<=temperature_max),
  CHECK(precipitation IS NULL OR precipitation>=0),CHECK(snow IS NULL OR snow>=0)
);
CREATE TABLE IF NOT EXISTS mart.daily_requests (
  request_date date, category_id bigint REFERENCES core.category,
  district_id bigint REFERENCES core.district,
  request_count integer NOT NULL CHECK(request_count>=0),
  temperature_min double precision,temperature_max double precision,
  precipitation double precision,snow double precision,
  PRIMARY KEY(request_date,category_id,district_id)
);
CREATE TABLE IF NOT EXISTS ops.quality_result (
  result_id bigserial PRIMARY KEY, run_id uuid NOT NULL, checked_at timestamptz DEFAULT now(),
  check_name text NOT NULL, check_type text NOT NULL, severity text NOT NULL,
  observed_value double precision, expected_rule text NOT NULL,
  status text NOT NULL CHECK(status IN ('PASS','WARN','FAIL')), details jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta.lineage (
  output_asset text PRIMARY KEY, input_assets jsonb NOT NULL,
  transformation text NOT NULL, source_file text NOT NULL, description text NOT NULL
);
CREATE TABLE IF NOT EXISTS meta.pipeline_run (
  run_id uuid PRIMARY KEY, started_at timestamptz DEFAULT now(), finished_at timestamptz,
  status text NOT NULL, steps jsonb NOT NULL DEFAULT '{}', error_message text
);
CREATE TABLE IF NOT EXISTS mart.model_run (
  run_id uuid PRIMARY KEY, created_at timestamptz DEFAULT now(), cutoff_date date NOT NULL,
  horizon integer NOT NULL, parameters jsonb NOT NULL, selected_model text,
  selection_reason text, validation_start date, validation_end date, test_start date,test_end date
);
CREATE TABLE IF NOT EXISTS mart.model_metric (
  metric_id bigserial PRIMARY KEY,run_id uuid REFERENCES mart.model_run,
  split text NOT NULL,fold integer NOT NULL,model text NOT NULL,
  level text NOT NULL,mae double precision NOT NULL,rmse double precision NOT NULL,
  wape double precision, n integer NOT NULL, train_end date NOT NULL,
  target_start date NOT NULL,target_end date NOT NULL
);
CREATE TABLE IF NOT EXISTS mart.prediction (
  run_id uuid REFERENCES mart.model_run, forecast_origin date,
  target_date date,category_id bigint REFERENCES core.category,district_id bigint REFERENCES core.district,
  model text,yhat double precision NOT NULL CHECK(yhat>=0),
  PRIMARY KEY(run_id,target_date,category_id,district_id,model)
);
CREATE TABLE IF NOT EXISTS mart.backtest (
  run_id uuid REFERENCES mart.model_run,split text,fold integer,forecast_origin date,
  target_date date,category_id bigint,district_id bigint,model text,
  actual integer,yhat double precision,
  PRIMARY KEY(run_id,split,fold,target_date,category_id,district_id,model)
);

CREATE OR REPLACE VIEW mart.v_daily_requests AS
 SELECT d.request_date,c.category_name,t.community_board,t.borough,
 d.request_count,d.temperature_min,d.temperature_max,d.precipitation,d.snow
 FROM mart.daily_requests d JOIN core.category c USING(category_id) JOIN core.district t USING(district_id);
CREATE OR REPLACE VIEW mart.v_forecast AS
 SELECT p.target_date,c.category_name,d.community_board,d.borough,p.model,p.yhat,p.forecast_origin,
 r.created_at,p.run_id::text FROM mart.prediction p JOIN core.category c USING(category_id)
 JOIN core.district d USING(district_id) JOIN mart.model_run r USING(run_id)
 WHERE p.run_id=(SELECT run_id FROM mart.model_run ORDER BY created_at DESC LIMIT 1);
CREATE OR REPLACE VIEW mart.v_actual_forecast AS
 SELECT request_date AS date,category_name,community_board,borough,'Факт'::text AS series,
 request_count::double precision AS value FROM mart.v_daily_requests
 UNION ALL SELECT target_date,category_name,community_board,borough,model,yhat FROM mart.v_forecast;
CREATE OR REPLACE VIEW ops.v_loads AS
 SELECT load_id::text,source_name,started_at,finished_at,load_mode,date_from,date_to,
 rows_received,rows_inserted,rows_updated,rows_rejected,status,error_message,
 extract(epoch FROM coalesce(finished_at,now())-started_at)::double precision AS duration_seconds FROM ops.load_log;
CREATE OR REPLACE VIEW ops.v_quality AS
 SELECT result_id,run_id::text,checked_at,check_name,check_type,severity,observed_value,
 expected_rule,status,details::text FROM ops.quality_result;
CREATE OR REPLACE VIEW ops.v_freshness AS
 SELECT source_name,complete_through,updated_at,
 (current_timestamp AT TIME ZONE 'America/New_York')::date-complete_through AS age_days FROM ops.watermark;
CREATE OR REPLACE VIEW mart.v_model_metrics AS
 SELECT r.created_at,m.split,m.fold,m.model,m.level,m.mae,m.rmse,m.wape,m.n,m.train_end,m.target_start,m.target_end
 FROM mart.model_metric m JOIN mart.model_run r USING(run_id)
 WHERE m.run_id=(SELECT run_id FROM mart.model_run ORDER BY created_at DESC LIMIT 1);
