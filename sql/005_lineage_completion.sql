INSERT INTO meta.lineage VALUES
 ('mart.v_daily_requests','["mart.daily_requests","core.category","core.district"]','Join surrogate dimension keys to their display names','sql/001_initial.sql','BI dataset containing the daily request_count measure'),
 ('core.category','["staging.v_requests"]','Distinct trimmed category names, unique natural key','src/processing.py','Category dictionary'),
 ('core.district','["staging.v_requests"]','Distinct normalized Community Board and Borough pair','src/processing.py','Territory resolution dictionary; unknown values remain visible'),
 ('staging.v_weather','["raw.weather"]','Typed metric weather values, missing values remain NULL','sql/002_staging_and_metadata.sql','Weather normalization'),
 ('core.weather','["staging.v_weather"]','Range validation, station-date uniqueness','src/processing.py','Daily weather retained independently of request counts'),
 ('mart.model_run','["mart.daily_requests"]','Expanding windows, six validation weeks, four test weeks, validation-only model selection','src/modeling.py','Experiment parameters and publication state'),
 ('mart.v_actual_selected_forecast','["mart.v_daily_requests","mart.v_selected_forecast"]','UNION of observed demand and eligible selected-model predictions','sql/003_publication_views.sql','Primary actual-versus-forecast chart'),
 ('mart.v_model_metrics','["mart.model_metric","mart.model_run"]','Latest fully successful experiment only','sql/003_publication_views.sql','Validation and final-test metric dataset')
ON CONFLICT(output_asset) DO UPDATE SET input_assets=excluded.input_assets,transformation=excluded.transformation,source_file=excluded.source_file,description=excluded.description;
