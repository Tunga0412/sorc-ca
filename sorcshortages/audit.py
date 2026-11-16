"""Independent source-to-output checks; no production normalizer is imported."""
from collections import Counter
from datetime import date, datetime
import calendar
import re
import statistics

TEXT_FIELDS = {
    'din': ['din','drug_identification_number','Drug Identification Number'],
    'brand_name': ['brand.name','brand_name','Brand name','en_drug_brand_name'],
    'company_name': ['company.name','company_name','Company Name'],
    'common_name': ['common.name','common_name','Common or Proper name','en_drug_common_name'],
    'active_ingredient': ['ingredients','active_ingredient','active_ingredients','Active Ingredient(s)','en_ingredients'],
    'strength': ['drug_strength','strength','Strength(s)'],
    'dosage_form': ['drug_dosage_form','dosage_form','Dosage form(s)'],
    'route': ['drug_route','route','Route of administration'],
    'packaging': ['drug_package_quantity','packaging','Packaging size'],
    'atc_code': ['atc.code','atc_code','ATC code','atc_number'],
    'atc_description': ['atc_description','ATC description'],
    'market_status': ['market_status','Market Status','drug.market_status','current_status'],
    'company_comments_en':['en_comments','en_discontinuation_comments','Company comments'],
    'health_canada_comments_en':['hc_en_comments','Health Canada comments'],
    'company_comments_fr':['fr_comments','fr_discontinuation_comments'],
    'health_canada_comments_fr':['hc_fr_comments'],
    'mitigation_en':['en_supply_disruption_mitigation'],
    'mitigation_fr':['fr_supply_disruption_mitigation'],
    'reason': ['reason','Reason for shortage','Reason for discontinuation','shortage_reason','discontinuance_reason'],
    'english_description': ['description_en','english_description','Description English','en_comments','hc_en_comments','en_discontinuation_comments'],
    'french_description': ['description_fr','french_description','Description French','fr_comments','hc_fr_comments','fr_discontinuation_comments'],
}
DATE_FIELDS = {
    'actual_start_date':['actual_start_date','Actual start date','actualStartDate'],
    'anticipated_start_date':['anticipated_start_date','Anticipated start date','anticipatedStartDate'],
    'actual_end_date':['actual_end_date','Actual end date','actualEndDate','resolved_date'],
    'estimated_end_date':['estimated_end_date','Estimated end date','estimatedEndDate'],
    'actual_discontinuation_date':['discontinuation.date','actual_discontinuation_date','Actual discontinuation date','discontinuation_date'],
    'anticipated_discontinuation_date':['anticipated_discontinuation_date','Anticipated discontinuation date'],
    'remaining_supply_date':['remaining_supply_date','Remaining supply date'],
    'updated_date':['last.updated','last_updated','updated_date','Updated date'],
    'created_date':['date.created','created_date','Date created'],
}

def take(row, names):
    for key in names:
        if row.get(key) not in (None,''): return row[key]
    normalized={re.sub('[^a-z0-9]','',str(k).lower()):v for k,v in row.items()}
    for key in names:
        value=normalized.get(re.sub('[^a-z0-9]','',key.lower()))
        if value not in (None,''): return value
    for parent in ['report','drug','company','shortage','discontinuation']:
        child=row.get(parent)
        if isinstance(child,dict):
            value=take(child,names)
            if value not in (None,''): return value
    return None

def text(value):
    if isinstance(value,dict):
        for key in ['en_name','en_reason','label','name','value']:
            if value.get(key) not in (None,''): return str(value[key]).strip()
        return ''
    return '' if value is None else str(value).strip()

def parsed_date(value):
    if value in (None,''): return None
    raw=str(value).strip()
    iso=re.search(r'\d{4}-\d{2}-\d{2}',raw)
    try:
        if iso: result=date.fromisoformat(iso.group())
        else:
            result=None
            for pattern in ['%Y/%m/%d','%m/%d/%Y','%d/%m/%Y']:
                try: result=datetime.strptime(raw,pattern).date();break
                except ValueError: pass
            if result is None:return None
        return result.isoformat() if 1900<=result.year<=2100 else None
    except ValueError:return None

def source_expected(row,kind):
    identifier=take(row,['report.id','report_id','id','Report ID'])
    if identifier is None:raise ValueError('Original source has no report identifier')
    expected={'report_id':str(identifier),'record_key':kind+':'+str(identifier),'type':kind}
    expected.update({field:text(take(row,names)) for field,names in TEXT_FIELDS.items()})
    expected.update({field:parsed_date(take(row,names)) for field,names in DATE_FIELDS.items()})
    status=text(take(row,['shortage_status','Shortage status','status'] if kind=='shortage' else ['discontinuation_status','Discontinuation status','status']))
    key=re.sub(r'[\s-]+','_',status.lower())
    expected['source_status']=status
    expected['status']={'actual_shortage':'active_confirmed','actual':'active_confirmed','anticipated':'anticipated_shortage','avoided':'avoided_shortage'}.get(key,key)
    tier=take(row,['tier_3','tier3','Tier 3 Status','tier_3_status'])
    expected['tier_3']=tier is True or str(tier or '').strip().lower() in ['1','true','yes','y','t','tier 3','actual tier 3']
    first,second=('actual_start_date','anticipated_start_date') if kind=='shortage' else ('actual_discontinuation_date','anticipated_discontinuation_date')
    expected['product_updated_date']=parsed_date((row.get('drug') or {}).get('updated_date')) if isinstance(row.get('drug'),dict) else None
    expected['start_date']=expected[first] or expected[second]
    expected['start_date_source']=first if expected[first] else (second if expected[second] else None)
    expected['end_date']=expected['actual_end_date'] if kind=='shortage' else expected['actual_discontinuation_date']
    return expected

def audit_dataset(original,derived):
    """Reject omitted/excess reports, field changes and incorrect aggregates."""
    snapshot=derived['latest_snapshot'];date.fromisoformat(snapshot)
    if original.get('snapshot_date') != snapshot:raise ValueError('Source and derived snapshot dates differ')
    expected={}
    raw_by_key={}
    for kind,collection in [('shortage','shortages'),('discontinuation','discontinuations')]:
        for raw in original.get(collection,[]):
            if raw.get('draft') is True: raise ValueError('Draft report is outside the public source census')
            label=text(raw.get('type')).lower()
            if label and ((kind=='discontinuation' and 'discontinu' not in label) or (kind=='shortage' and 'shortage' not in label)):
                raise ValueError('Original report type differs from its collection')
            row=source_expected(raw,kind)
            if row['record_key'] in expected:raise ValueError('Duplicate report in original source')
            expected[row['record_key']]=row
            raw_by_key[row['record_key']]=raw
    actual={r['record_key']:r for r in derived['records']}
    if len(actual)!=len(derived['records']) or actual.keys()!=expected.keys():
        raise ValueError('Source/derived report census differs')
    comparisons=0
    source_fields=len(next(iter(expected.values()))) if expected else 0
    for key,row in expected.items():
        for field,value in row.items():
            if actual[key].get(field)!=value:raise ValueError(f'Source field mismatch {key} {field}: source={value!r} derived={actual[key].get(field)!r}')
            comparisons+=1
        start,end=row['start_date'],row['end_date'];status=row['status']
        row['active_at_snapshot']=bool(row['type']=='shortage' and status=='active_confirmed' and start and start<=snapshot and (not end or end>snapshot))
        row['future_start']=bool(start and start>snapshot)
        row['end_after_snapshot']=bool(end and end>snapshot)
        eligible=row['type']=='shortage' and start and start<=snapshot and (status=='active_confirmed' or (status=='resolved' and end and end<=snapshot))
        finish=min(end,snapshot) if end else snapshot
        days=(date.fromisoformat(finish)-date.fromisoformat(start)).days if eligible else -1
        row['duration_days']=days if days>=0 else None
        row['censored']=bool(eligible and (not end or end>snapshot))
        reversed_order=bool(start and end and end<start)
        row['avoided_before_start']=reversed_order and status=='avoided_shortage'
        row['date_order_issue']=reversed_order and not row['avoided_before_start']
        raw=raw_by_key[key]
        invalid=[]
        for field in ['actual_start_date','anticipated_start_date','actual_end_date','estimated_end_date','actual_discontinuation_date','anticipated_discontinuation_date']:
            value=take(raw,DATE_FIELDS[field])
            if value and re.search(r'\d{4}-\d{2}-\d{2}',str(value)) and parsed_date(value) is None:invalid.append(field)
        row['invalid_date_fields']=invalid
        row['date_plausibility_issue']=bool(invalid)
        row['ongoing']=row['censored']
        for field in ['active_at_snapshot','future_start','end_after_snapshot','duration_days','censored','avoided_before_start','date_order_issue','invalid_date_fields','date_plausibility_issue','ongoing']:
            if actual[key].get(field)!=row[field]:raise ValueError(f'Derived rule mismatch {key} {field}')
            comparisons+=1
    rows=list(expected.values());short=[r for r in rows if r['type']=='shortage']
    durations=[r['duration_days'] for r in short if r['duration_days'] is not None]
    med=round(statistics.median(durations)) if durations else None
    summary={'total_reports':len(rows),'shortages':len(short),'discontinuations':len(rows)-len(short),'active_shortages':sum(r['active_at_snapshot'] for r in rows),'tier_3_shortages':sum(r['tier_3'] for r in short),'median_duration_days':med,'duration_records':len(durations),'date_order_issues':sum(r['date_order_issue'] for r in rows),'avoided_before_start_records':sum(r['avoided_before_start'] for r in rows),'future_start_records':sum(r['future_start'] for r in rows),'end_after_snapshot_records':sum(r['end_after_snapshot'] for r in rows),'right_censored_records':sum(r['censored'] for r in rows),'missing_latest_snapshot_records':0}
    for field,value in summary.items():
        if derived['summary'].get(field)!=value:raise ValueError('Summary mismatch: '+field)
    if derived['summary']['date_plausibility_issues']!=sum(r['date_plausibility_issue'] for r in rows):raise ValueError('Invalid source-date count differs')
    months=derived['series']['active_shortages'];new=[];overlap=[]
    valid_starts=[r['start_date'][:7] for r in short if r['start_date'] and r['start_date']<=snapshot]
    first=max(min(valid_starts,default=snapshot[:7]),'2017-03');cursor=date.fromisoformat(first+'-01');expected_months=[]
    while cursor.isoformat()[:7]<=snapshot[:7]:
        expected_months.append(cursor.isoformat()[:7]);cursor=date(cursor.year+(cursor.month==12),1 if cursor.month==12 else cursor.month+1,1)
    if [p['month'] for p in months]!=expected_months:raise ValueError('Monthly coverage differs from reporting window')
    for month in expected_months:
        year,number=map(int,month.split('-'));left=month+'-01';right=min(f'{month}-{calendar.monthrange(year,number)[1]:02}',snapshot)
        count=sum(bool(r['start_date'] and r['start_date']<=right and (not r['end_date'] or (r['end_date']>=left and r['end_date']>=r['start_date']))) for r in short)
        overlap.append({'month':month,'value':count})
        new.append({'month':month,'value':sum(bool(r['start_date'] and r['start_date']<=snapshot and r['start_date'][:7]==month) for r in short)})
    if months!=overlap or derived['series']['new_shortages']!=new:raise ValueError('Monthly interval or start series mismatch')
    bins=[sum(x<=30 for x in durations),sum(31<=x<=90 for x in durations),sum(91<=x<=180 for x in durations),sum(181<=x<=365 for x in durations),sum(x>=366 for x in durations)]
    if [p['value'] for p in derived['series']['duration_distribution']]!=bins:raise ValueError('Duration histogram mismatch')
    if {p['label']:p['value'] for p in derived['status_counts']}!=dict(Counter(r['status'] for r in rows)):raise ValueError('Status counts differ')
    return {'schema_version':1,'passed':True,'source_records':len(rows),'source_fields_checked':source_fields,'field_and_rule_comparisons':comparisons,'months_checked':len(months),'method':'Independent source-field projection, calendar arithmetic and full census/aggregate reconciliation; no production normalizer imported'}
