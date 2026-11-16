"""Source-derived date fixtures and negative ingestion/publication cases."""
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest
import shutil
import io
import zipfile
from unittest.mock import patch
from datetime import date

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sorcshortages.schema import parse_date, normalize_record, infer_report_type
from sorcshortages.pipeline import build_consolidated
from sorcshortages.api import ShortagesApiClient, ShortagesApiError, write_snapshot, _read_export_bytes, PublicExportClient
from automation import shortages_monthly as monthly
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'automation'))
import pages_gate

class SourceRules(unittest.TestCase):
    def build(self, rows, snapshot='2026-11-01', earlier=None):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); snapshots=root/'snapshots'
            if earlier: write_snapshot(snapshots/'2026-10-01.json',snapshot_date='2026-10-01',shortages=earlier,discontinuations=[],source='independent fixtures')
            write_snapshot(snapshots/(snapshot+'.json'),snapshot_date=snapshot,shortages=rows,discontinuations=[],source='independent fixtures')
            return build_consolidated(snapshots,root/'derived.json')

    def test_real_calendar_dates(self):
        self.assertIsNone(parse_date('2026-02-31'))
        self.assertIsNone(parse_date('2026-13-01'))
        self.assertEqual(parse_date('2024-02-29T12:00:00Z'),'2024-02-29')

    def test_future_start_has_no_observed_duration_or_overlap(self):
        # Official report293905's pre-snapshot history anticipates Nov2.
        row={'report_id':'293905','anticipated_start_date':'2026-11-02','actual_end_date':'2026-12-10','status':'anticipated_shortage'}
        result=self.build([row]);r=result['records'][0]
        self.assertIsNone(r['duration_days'],'future-start report has an observed duration')
        self.assertEqual(result['series']['active_shortages'][-1]['value'],0)

    def test_future_actual_end_is_retained_flagged_and_censored(self):
        # Published record217366 has a source actual end after Nov1.
        row={'report_id':'217366','actual_start_date':'2024-01-18','actual_end_date':'2026-12-30','status':'active_confirmed'}
        r=self.build([row])['records'][0]
        self.assertEqual(r['end_date'],'2026-12-30')
        self.assertTrue(r.get('end_after_snapshot'),'future actual end is unflagged')
        self.assertTrue(r['censored'])
        self.assertEqual(r['duration_days'],1018)

    def test_avoided_report_has_no_shortage_duration(self):
        # Official status means the anticipated shortage was avoided.
        r=self.build([{'report_id':'651','anticipated_start_date':'2017-04-03','actual_end_date':'2017-04-05','status':'avoided_shortage'}])['records'][0]
        self.assertIsNone(r['duration_days'],'avoided report treated as an observed shortage duration')

    def test_anticipated_status_is_not_promoted_to_actual(self):
        r=self.build([{'report_id':'x','anticipated_start_date':'2026-10-01','status':'anticipated_shortage'}])['records'][0]
        self.assertFalse(r['active_at_snapshot'],'anticipated report promoted to actual shortage')

    def test_disappeared_report_is_not_extended(self):
        result=self.build([],earlier=[{'report_id':'x','actual_start_date':'2026-09-01','status':'active_confirmed'}])
        r=result['records'][0]
        self.assertFalse(r['active_at_snapshot'],'absent report extended to later snapshot')
        self.assertEqual(r['last_seen_snapshot'],'2026-10-01')
        self.assertEqual(r['duration_days'],30)

    def test_market_status_and_source_status_survive(self):
        r=normalize_record({'Report ID':'4355','Shortage status':'Actual shortage','Market Status':'CANCELLED POST MARKET'},'shortage')
        self.assertEqual(r['status'],'active_confirmed')
        self.assertEqual(r['source_status'],'Actual shortage')
        self.assertEqual(r['market_status'],'CANCELLED POST MARKET')

    def test_unknown_report_type_is_not_guessed(self):
        with self.assertRaises(ValueError):infer_report_type({'report_type':'Unrecognized event'})

    def test_company_and_health_canada_notes_are_separate(self):
        r=normalize_record({'id':'x','status':'resolved','en_comments':'Company note','hc_en_comments':'Health Canada note','en_supply_disruption_mitigation':'Mitigation text'},'shortage')
        self.assertEqual((r['company_comments_en'],r['health_canada_comments_en'],r['mitigation_en']),('Company note','Health Canada note','Mitigation text'))

    def test_invalid_estimated_date_is_flagged_without_changing_start(self):
        r=normalize_record({'id':'x','status':'active_confirmed','actual_start_date':'2026-10-01','estimated_end_date':'2026-02-31'},'shortage')
        self.assertEqual(r['start_date'],'2026-10-01')
        self.assertTrue(r['date_plausibility_issue'])
        self.assertIn('estimated_end_date',r['invalid_date_fields'])

    def test_resolution_on_snapshot_is_completed_zero_day_episode(self):
        r=self.build([{'report_id':'x','actual_start_date':'2026-11-01','actual_end_date':'2026-11-01','status':'resolved'}])['records'][0]
        self.assertEqual(r['duration_days'],0)
        self.assertFalse(r['active_at_snapshot'])
        self.assertFalse(r['censored'])

    def test_withheld_durations_explain_the_specific_source_problem(self):
        suffix=' This report is excluded from duration statistics.'
        cases=[
            ({'report_id':'3746','actual_start_date':'2017-04-30','actual_end_date':'2017-03-20','status':'resolved'},'the reported end date precedes the start date.'),
            ({'report_id':'missing','status':'active_confirmed'},'the source start date is missing or invalid.'),
            ({'report_id':'bad','actual_start_date':'0008-08-31','status':'active_confirmed'},'the source start date is missing or invalid.'),
            ({'report_id':'future','actual_start_date':'2026-11-02','status':'active_confirmed'},'the reported start date is after the source snapshot.'),
            ({'report_id':'no-end','actual_start_date':'2026-10-01','status':'resolved'},'the resolved report has no usable actual end date.'),
            ({'report_id':'future-end','actual_start_date':'2026-10-01','actual_end_date':'2026-11-02','status':'resolved'},"the resolved report's actual end date is after the source snapshot."),
        ]
        for raw,reason in cases:
            with self.subTest(raw['report_id']):
                r=self.build([raw])['records'][0]
                self.assertIsNone(r['duration_days'])
                self.assertEqual(r['duration_exclusion_reason'],'Duration Withheld: '+reason+suffix)

    def test_non_applicable_durations_are_not_called_withheld(self):
        for status in ['anticipated_shortage','avoided_shortage']:
            r=self.build([{'report_id':'x','anticipated_start_date':'2026-10-01','status':status}])['records'][0]
            self.assertEqual(r['duration_exclusion_reason'],'Not applicable: '+status.replace('_',' ')+' report.')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            write_snapshot(root/'snapshots/2026-11-01.json',snapshot_date='2026-11-01',shortages=[],discontinuations=[{'report_id':'d','status':'discontinued','discontinuation_date':'2026-10-01'}],source='independent fixture')
            r=build_consolidated(root/'snapshots',root/'derived.json')['records'][0]
            self.assertEqual(r['duration_exclusion_reason'],'Not applicable: discontinuation report.')

    def test_unrelated_date_flag_does_not_withhold_valid_duration(self):
        r=self.build([{'report_id':'108101','actual_start_date':'2020-03-06','actual_end_date':'2020-06-22','estimated_end_date':'0001-01-01','status':'resolved'}])['records'][0]
        self.assertTrue(r['date_plausibility_issue'])
        self.assertEqual(r['duration_days'],108)
        self.assertIsNone(r['duration_exclusion_reason'])
        r=self.build([{'report_id':'open','actual_start_date':'2026-10-01','actual_end_date':'2026-12-30','status':'active_confirmed'}])['records'][0]
        self.assertEqual(r['duration_days'],31)
        self.assertIsNone(r['duration_exclusion_reason'])

    def test_independent_audit_rejects_missing_withholding_explanation(self):
        from sorcshortages.audit import audit_dataset
        raw={'report_id':'3746','actual_start_date':'2017-04-30','actual_end_date':'2017-03-20','status':'resolved'}
        result=self.build([raw]);result['records'][0]['duration_exclusion_reason']=None
        with self.assertRaisesRegex(ValueError,'duration_exclusion_reason'):
            audit_dataset({'snapshot_date':'2026-11-01','shortages':[raw],'discontinuations':[]},result)

class IngestionRules(unittest.TestCase):
    def test_requested_window_start_is_preserved(self):
        self.assertEqual(list(monthly.month_ranges(date(2026,5,20),date(2026,6,2))),[(date(2026,5,20),date(2026,5,31)),(date(2026,6,1),date(2026,6,2))])

    def test_conflicting_duplicate_source_id_is_rejected(self):
        rows={};monthly.add_source_row(rows,{'Report ID':'1','status':'resolved'},'shortage')
        with self.assertRaises(ShortagesApiError):monthly.add_source_row(rows,{'Report ID':'1','status':'active_confirmed'},'shortage')

    def test_incomplete_api_page_is_rejected(self):
        client=ShortagesApiClient('unused','unused');client.token='unused'
        with patch.object(client,'search_page',return_value={'data':[{'id':1}],'total':2,'remaining':0}):
            with self.assertRaises(ShortagesApiError):client.fetch_all()

    def test_empty_public_export_window_needs_no_export_form(self):
        client=PublicExportClient()
        with patch.object(client,'_open',return_value=io.BytesIO(b'<html>No results found</html>')):
            self.assertEqual(client.fetch_range('2016-01-01','2016-01-31'),(0,{'shortages':[],'discontinuations':[]}))

    def test_malformed_csv_row_is_rejected(self):
        packed=io.BytesIO()
        with zipfile.ZipFile(packed,'w') as archive:archive.writestr('shortages.csv','Export\nGenerated date\nReport ID,Shortage status\n1\n')
        with self.assertRaisesRegex(ShortagesApiError,'row width'):_read_export_bytes(packed.getvalue())

    def test_export_preserves_both_types_and_leading_zero_din(self):
        packed=io.BytesIO()
        with zipfile.ZipFile(packed,'w') as archive:
            archive.writestr('shortages.csv','Export\nGenerated date\nReport ID,DIN,Shortage status\n4355,00728179,Actual shortage\n')
            archive.writestr('discontinuations.csv','Export\nGenerated date\nReport ID,DIN,Discontinuation status\n7264,02218860,Discontinued\n')
        result=_read_export_bytes(packed.getvalue())
        self.assertEqual(result['shortages'][0]['DIN'],'00728179')
        self.assertEqual(result['discontinuations'][0]['Report ID'],'7264')

    def test_count_and_source_must_match_exactly(self):
        result={'records':[{'record_key':'shortage:1','type':'shortage'},{'record_key':'discontinuation:2','type':'discontinuation'}],'latest_snapshot':date.today().isoformat(),'summary':{'total_reports':2,'shortages':1,'discontinuations':1},'series':{'active_shortages':[1],'new_shortages':[1]}}
        with self.assertRaises(ShortagesApiError):monthly.validate_candidate(result,3,{},1,.75)

    def run_export(self, directory, fail=False):
        class Source:
            def fetch_range(self,start,end):
                return (2,{'shortages':[{'Report ID':'4355','Actual start date':'2008-03-19','Shortage status':'Actual shortage'}], 'discontinuations':[] if fail else [{'Report ID':'7264','Actual discontinuation date':'2003-03-03','Discontinuation status':'Discontinued'}]})
        out=Path(directory)/'data'/'consolidated.json';today=date.today().isoformat()
        args=['monthly','--source','public-export','--start-date',today,'--end-date',today,'--minimum-records','1','--output',str(out)]
        with patch.object(monthly,'PublicExportClient',Source),patch.object(sys,'argv',args):monthly.main()
        return out

    def test_fallback_and_retained_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=self.run_export(tmp);data=json.loads(out.read_text())
            self.assertEqual(len(data['records']),2)
            archive=out.parent/data['provenance']['raw_snapshot_path']
            self.assertTrue(archive.is_file())
            raw=json.loads(gzip.decompress(archive.read_bytes()))
            self.assertEqual(len(raw['shortages'])+len(raw['discontinuations']),2)
            self.assertTrue((out.parent/'records.csv').is_file())

    def test_rejected_source_cannot_replace_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';root.mkdir()
            before={name:('sentinel:'+name).encode() for name in ['consolidated.json','consolidated.js','records.csv']}
            for name,raw in before.items():(root/name).write_bytes(raw)
            with self.assertRaises(ShortagesApiError):self.run_export(tmp,True)
            for name,raw in before.items():self.assertEqual((root/name).read_bytes(),raw)

    def test_failed_audit_blocks_actual_pages_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'source';out=self.run_export(root)
            data=json.loads(out.read_text());data['source_audit']['passed']=False
            out.write_text(json.dumps(data))
            checkout=Path(__file__).resolve().parents[1]
            for name in ['sorctracks_live.html','sorctracks_tool.html','index.html','CNAME','vendor/chart.umd.js','vendor/sorc-charts-v1.js']:
                target=root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(checkout/name,target)
            artifact=Path(tmp)/'published-artifact';sentinel=Path(tmp)/'sentinel';sentinel.write_bytes(b'unchanged')
            with self.assertRaisesRegex(ValueError,'Shortages source audit is not accepted'):pages_gate.build(root,artifact)
            self.assertFalse(artifact.exists(),'Failed source audit created a downstream publication artifact')
            self.assertEqual(sentinel.read_bytes(),b'unchanged')

if __name__=='__main__':unittest.main()
