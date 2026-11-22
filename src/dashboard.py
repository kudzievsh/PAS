"""Idempotent Superset datasets, native filters, charts and dashboard layouts."""
import json
import os
from pathlib import Path
from src.common import ROOT,dump_json


def sql_metric(expr,label):
    return {'expressionType':'SQL','sqlExpression':expr,'label':label,'optionName':'metric_'+label,
            'hasCustomLabel':True,'isNew':False}


def create_dashboards():
    from superset import db
    from superset.models.core import Database
    from superset.models.slice import Slice
    from superset.models.dashboard import Dashboard
    from superset.connectors.sqla.models import SqlaTable
    database=db.session.query(Database).filter_by(database_name='PAS PostgreSQL').one_or_none()
    if database is None:
        database=Database(database_name='PAS PostgreSQL',sqlalchemy_uri=os.environ['READER_DATABASE_URL'],
            expose_in_sqllab=True,allow_dml=False,allow_ctas=False,allow_cvas=False,allow_file_upload=False)
        database.set_sqlalchemy_uri(os.environ['READER_DATABASE_URL'])
        db.session.add(database);db.session.commit()
    datasets={}
    definitions=[('daily','mart','v_daily_requests','request_date'),
       ('combined','mart','v_actual_selected_forecast','date'),
       ('forecast','mart','v_selected_forecast','target_date'),
       ('metrics','mart','v_model_metrics','created_at'),
       ('loads','ops','v_loads','started_at'),('quality','ops','v_quality_latest','checked_at'),
       ('freshness','ops','v_freshness','updated_at'),('lineage','meta','lineage',None)]
    definitions.append(('category_freshness','ops','v_category_freshness','last_request_date'))
    for key,schema,name,time_col in definitions:
        table=db.session.query(SqlaTable).filter_by(database_id=database.id,schema=schema,table_name=name).one_or_none()
        if table is None:
            table=SqlaTable(table_name=name,schema=schema,database=database,main_dttm_col=time_col)
            db.session.add(table);db.session.flush()
        table.fetch_metadata();table.main_dttm_col=time_col
        for col in table.columns:
            col.groupby=True;col.filterable=True
        datasets[key]=table
    db.session.commit()
    charts={}

    def chart(key,name,dataset,viz,**config):
        table=datasets[dataset]
        params={'datasource':f'{table.id}__table','viz_type':viz,
            'time_range':'No filter','granularity_sqla':table.main_dttm_col,
            'time_grain_sqla':'P1D','row_limit':10000,'adhoc_filters':[],
            'color_scheme':'supersetColors','show_legend':True,'rich_tooltip':True,
            'x_axis_time_format':'%d.%m.%Y','y_axis_format':',.0f','number_format':',.0f',
            **config}
        item=db.session.query(Slice).filter_by(slice_name=name,datasource_id=table.id).one_or_none()
        if item is None:
            item=Slice(slice_name=name,datasource_type='table',datasource_id=table.id)
            db.session.add(item)
        item.viz_type=viz;item.params=json.dumps(params,ensure_ascii=False);item.cache_timeout=60
        item.description=config.pop('description','') if 'description' in config else ''
        db.session.flush();params['slice_id']=item.id;item.params=json.dumps(params,ensure_ascii=False)
        charts[key]=item
        return item

    total=sql_metric('SUM(request_count)','Обращения')
    forecast=sql_metric('SUM(yhat)','Прогноз обращений')
    chart('actual_kpi','Обращения за выбранный период','daily','big_number_total',metric=total,subheader='Три категории NYC 311',time_range='Last 90 days')
    chart('forecast_kpi','Ожидается за следующие 7 суток','forecast','big_number_total',metric=forecast,subheader='Модель выбрана по валидации')
    chart('series','Факт и прогноз нагрузки','combined','echarts_timeseries_line',
          metrics=[sql_metric('SUM(value)','Обращения в сутки')],groupby=['series'],
          time_range='90 days ago : 10 days later',x_axis='date',forecastEnabled=False,show_marker=False,zoomable=True)
    chart('categories','Доля категорий обращений','daily','pie',metric=total,groupby=['category_name'],time_range='Last 90 days',show_labels=True,label_type='key_percent',donut=True)
    chart('boroughs','Нагрузка по округам','daily','pie',metric=total,groupby=['borough'],time_range='Last 90 days',show_labels=True,label_type='key_percent',donut=True)
    chart('districts','Районы с наибольшей нагрузкой','daily','table',metrics=[total],groupby=['borough','community_board'],time_range='Last 90 days',row_limit=15,page_length=15,order_desc=True,include_search=True,table_timestamp_format='%d.%m.%Y',show_cell_bars=True)
    chart('forecast_table','Прогноз по категориям и округам','forecast','table',metrics=[forecast],groupby=['category_name','borough'],row_limit=30,page_length=15,show_cell_bars=True)
    chart('model_metrics','Ошибка моделей на итоговом тесте','metrics','table',
          groupby=['model','level'],metrics=[sql_metric('AVG(mae)','MAE'),sql_metric('AVG(rmse)','RMSE'),sql_metric('AVG(wape)','WAPE, %')],
          adhoc_filters=[{'expressionType':'SIMPLE','clause':'WHERE','subject':'split','operator':'==','comparator':'test'}],page_length=10,number_format=',.2f')

    chart('loaded_kpi','Записей принято источниками','loads','big_number_total',metric=sql_metric('SUM(rows_received)','Принято'),subheader='Включая повторные загрузки')
    chart('failed_kpi','Неуспешные загрузки','loads','big_number_total',metric=sql_metric("SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END)",'Ошибки'),subheader='Причины приведены в журнале')
    chart('quality_kpi','Критические ошибки качества','quality','big_number_total',metric=sql_metric("SUM(CASE WHEN status='FAIL' THEN 1 ELSE 0 END)",'Критические ошибки'),subheader='Последний запуск проверок')
    chart('freshness','Актуальность данных по источникам','freshness','table',query_mode='raw',all_columns=['source_name','complete_through','age_days','updated_at'],page_length=10,table_timestamp_format='%d.%m.%Y %H:%M')
    chart('loads_table','Журнал загрузок источников','loads','table',query_mode='raw',
          all_columns=['source_name','started_at','load_mode','date_from','date_to','rows_received','rows_inserted','rows_updated','rows_rejected','status','duration_seconds','error_message'],row_limit=100,page_length=10,include_search=True,order_by_cols=['["started_at", false]'],table_timestamp_format='%d.%m.%Y %H:%M')
    chart('quality_table','Результаты последнего контроля качества','quality','table',query_mode='raw',
          all_columns=['check_name','check_type','severity','observed_value','expected_rule','status'],page_length=20,row_limit=100,include_search=True)
    chart('category_freshness','Актуальность категорий и допуск к прогнозу','category_freshness','table',query_mode='raw',
          all_columns=['category_name','last_request_date','age_days','status'],page_length=10,row_limit=100)
    chart('source_share','Объём загрузок по источникам','loads','pie',groupby=['source_name'],metric=sql_metric('SUM(rows_received)','Принято'),show_labels=True,label_type='key_value')
    chart('lineage','Происхождение данных и показателей','lineage','table',query_mode='raw',all_columns=['output_asset','transformation','source_file','description'],page_length=10,row_limit=100,include_search=True)
    db.session.commit()

    def create_dashboard(title,slug,rows,filters):
        dash=db.session.query(Dashboard).filter_by(slug=slug).one_or_none()
        if dash is None:dash=Dashboard(slug=slug);db.session.add(dash)
        dash.dashboard_title=title;dash.published=True
        dash.description='Учебный проект по дисциплине «Прогнозно-аналитические системы». NYC 311 и NOAA, три выбранные категории. Данные поступают из PostgreSQL.'
        position={'DASHBOARD_VERSION_KEY':'v2','ROOT_ID':{'type':'ROOT','id':'ROOT_ID','children':['GRID_ID']},
           'GRID_ID':{'type':'GRID','id':'GRID_ID','children':[],'parents':['ROOT_ID']}}
        selected=[]
        for i,(keys,height) in enumerate(rows):
            rowid=f'ROW-{i}';position['GRID_ID']['children'].append(rowid)
            position[rowid]={'type':'ROW','id':rowid,'children':[],'parents':['ROOT_ID','GRID_ID'],'meta':{'background':'BACKGROUND_TRANSPARENT'}}
            for key in keys:
                item=charts[key];selected.append(item);chartid=f'CHART-{item.id}'
                position[rowid]['children'].append(chartid)
                position[chartid]={'type':'CHART','id':chartid,'children':[],'parents':['ROOT_ID','GRID_ID',rowid],
                    'meta':{'chartId':item.id,'sliceName':item.slice_name,'width':12//len(keys),'height':height}}
        native=[]
        for fkey,label,column,dataset,excluded in filters:
            fid='NATIVE_FILTER-'+fkey
            native.append({'id':fid,'name':label,'filterType':'filter_select','targets':[{'datasetId':datasets[dataset].id,'column':{'name':column}}],
                'defaultDataMask':{'extraFormData':{},'filterState':{}},'cascadeParentIds':[],
                'scope':{'rootPath':['ROOT_ID'],'excluded':excluded},'type':'NATIVE_FILTER',
                'controlValues':{'enableEmptyFilter':False,'defaultToFirstItem':False,'multiSelect':True,'searchAllOptions':True,'inverseSelection':False}})
        dash.slices=selected;dash.position_json=json.dumps(position,ensure_ascii=False)
        dash.json_metadata=json.dumps({'native_filter_configuration':native,'color_scheme':'supersetColors','timed_refresh_immune_slices':[],
           'refresh_frequency':300,'expanded_slices':{},'label_colors':{'Факт':'#2463a6'},'chart_configuration':{}},ensure_ascii=False)
        db.session.commit()
        return {'title':title,'slug':slug,'id':dash.id,'charts':[s.id for s in selected],'url':f'http://127.0.0.1:8088/superset/dashboard/{slug}/'}

    exclude_forecast=[charts['forecast_kpi'].id,charts['forecast_table'].id,charts['model_metrics'].id]
    subject=create_dashboard('Обращения граждан — анализ и прогноз','pas-demand',
        [(['actual_kpi','forecast_kpi'],22),(['series'],48),(['categories','boroughs'],36),(['districts','forecast_table'],45),(['model_metrics'],32)],
        [('category','Категория','category_name','daily',[charts['model_metrics'].id]),
         ('borough','Округ','borough','daily',[charts['model_metrics'].id]),
         ('district','Район Community Board','community_board','daily',[charts['model_metrics'].id])])
    operational=create_dashboard('Обращения граждан — состояние данных','pas-operations',
        [(['loaded_kpi','failed_kpi','quality_kpi'],22),(['freshness','source_share'],32),(['category_freshness'],32),(['loads_table'],48),(['quality_table'],66),(['lineage'],48)],
        [('source','Источник','source_name','loads',[charts[k].id for k in ['quality_kpi','quality_table','lineage']])])
    # Time range filter is separate from categorical filtering. Forecasts retain their future dates.
    dash=db.session.query(Dashboard).filter_by(slug='pas-demand').one()
    meta=json.loads(dash.json_metadata)
    meta['native_filter_configuration'].insert(0,{'id':'NATIVE_FILTER-period','name':'Период фактических данных',
        'filterType':'filter_time','targets':[{}],
        'defaultDataMask':{'extraFormData':{'time_range':'Last 90 days'},'filterState':{'value':'Last 90 days'}},
        'cascadeParentIds':[],'scope':{'rootPath':['ROOT_ID'],'excluded':exclude_forecast+[charts['series'].id]},'type':'NATIVE_FILTER',
        'controlValues':{}})
    dash.json_metadata=json.dumps(meta,ensure_ascii=False);db.session.commit()
    dump_json(ROOT/'data/exports/dashboards.json',{'subject':subject,'operations':operational,'datasets':{k:v.id for k,v in datasets.items()}})
    print('Created two dashboards with',len(charts),'charts',flush=True)
