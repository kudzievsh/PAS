CREATE OR REPLACE VIEW ops.v_category_freshness AS
 SELECT 'nyc_311'::text AS source_name,c.category_name,max(r.request_date) AS last_request_date,
 (current_timestamp AT TIME ZONE 'America/New_York')::date-max(r.request_date) AS age_days,
 CASE WHEN (current_timestamp AT TIME ZONE 'America/New_York')::date-max(r.request_date)>14 THEN 'WARN' ELSE 'PASS' END AS status
 FROM core.service_request r JOIN core.category c USING(category_id) GROUP BY c.category_name;
CREATE OR REPLACE VIEW mart.v_selected_forecast AS
 SELECT f.* FROM mart.v_forecast f JOIN mart.model_run r ON r.run_id::text=f.run_id
 JOIN ops.v_category_freshness q USING(category_name)
 WHERE f.model=r.selected_model AND q.status='PASS';
INSERT INTO meta.lineage VALUES
 ('mart.v_selected_forecast','["mart.prediction","mart.model_run","ops.v_category_freshness"]',
  'Latest successful run, model chosen on validation, exclude stale source categories',
  'sql/004_category_freshness.sql','Categories without new requests for more than 14 days are withheld from the published forecast')
ON CONFLICT(output_asset) DO UPDATE SET input_assets=excluded.input_assets,transformation=excluded.transformation,description=excluded.description;
