ALTER TABLE mart.model_run ADD COLUMN status text NOT NULL DEFAULT 'RUNNING';
CREATE OR REPLACE VIEW mart.v_forecast AS
 SELECT p.target_date,c.category_name,d.community_board,d.borough,p.model,p.yhat,p.forecast_origin,
 r.created_at,p.run_id::text FROM mart.prediction p JOIN core.category c USING(category_id)
 JOIN core.district d USING(district_id) JOIN mart.model_run r USING(run_id)
 WHERE p.run_id=(SELECT run_id FROM mart.model_run WHERE status='SUCCESS' ORDER BY created_at DESC LIMIT 1);
CREATE OR REPLACE VIEW mart.v_selected_forecast AS
 SELECT f.* FROM mart.v_forecast f JOIN mart.model_run r ON r.run_id::text=f.run_id WHERE f.model=r.selected_model;
CREATE OR REPLACE VIEW mart.v_actual_selected_forecast AS
 SELECT request_date AS date,category_name,community_board,borough,'Факт'::text AS series,
 request_count::double precision AS value FROM mart.v_daily_requests
 UNION ALL SELECT target_date,category_name,community_board,borough,'Прогноз ('||model||')',yhat FROM mart.v_selected_forecast;
CREATE OR REPLACE VIEW mart.v_model_metrics AS
 SELECT r.created_at,m.split,m.fold,m.model,m.level,m.mae,m.rmse,m.wape,m.n,m.train_end,m.target_start,m.target_end
 FROM mart.model_metric m JOIN mart.model_run r USING(run_id)
 WHERE m.run_id=(SELECT run_id FROM mart.model_run WHERE status='SUCCESS' ORDER BY created_at DESC LIMIT 1);
CREATE OR REPLACE VIEW ops.v_quality_latest AS
 SELECT * FROM ops.v_quality WHERE run_id=(SELECT run_id::text FROM ops.quality_result ORDER BY checked_at DESC,result_id DESC LIMIT 1);
CREATE OR REPLACE VIEW ops.v_schema_changes AS
 SELECT source_name,schema_hash,observed_at,fields::text FROM meta.source_schema;
