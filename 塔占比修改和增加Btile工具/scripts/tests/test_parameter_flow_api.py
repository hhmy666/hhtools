"""Parameters use server-owned verified template evidence and two-phase writes."""
import csv
import io
from dataclasses import replace
from pathlib import Path

from scripts.tests.test_api import ApiContractCase
from scripts.tests.test_rewrite import synthetic
from scripts.tower_reducer.template_reader import decode_template, encode_template
from scripts.tower_reducer.settings import load_settings, save_settings


class ParameterFlowApiTests(ApiContractCase):
    def config(self):
        root = self.root / 'ulinkconfig/csv'
        root.mkdir(parents=True)
        headers = ['LevelId','Des','TemplateId','RandomType'] + ['DupPara'+str(i) for i in range(1,11)] + ['StrategyGroupId','randomSeed_groupID','Extra']
        stream = io.StringIO(newline='')
        writer = csv.writer(stream, lineterminator='\r\n')
        writer.writerow(headers); writer.writerow(['string'] * len(headers)); writer.writerow(['说明'] * len(headers))
        writer.writerow(['level-source','原备注','sample','1'] + ['4|'+str(i)+'|100|1' for i in range(10)] + ['1003','7','保持,未知字段'])
        raw = b'\xef\xbb\xbf' + stream.getvalue().encode()
        (root / 'config_level.csv').write_bytes(raw)
        (root / 'config_level_list.csv').write_text(
            'LevelIndex,DDAType,LevelId,DDALevelId1,DDALevelId2,DDALevelId3\nint,int,array,array,array,array\n说明,说明,说明,说明,说明,说明\n334,0,level-source,,,\n',encoding='utf-8')
        response = self.request('PUT','/api/settings',json_body={'config_root':str(root)})
        self.assertEqual(response[0],200,response[3])
        return root,raw

    def verified_job(self):
        raw = synthetic(second_tower=True)[0]
        imported = self.import_files([('sample_m.bytes',raw)])
        job_id = self.start_job(imported['import_id'],defaults={'ratio':.25,'retained_counts':[3,0],
            'strategy':'top_first','max_tiles_per_layer':20,'max_added_layers':2,'naming_rule':'lessT{原模板ID}'})
        finished = self.wait_job(job_id)
        self.assertEqual(self.rows(finished)[0]['status'],'verified',finished)
        return job_id

    def call(self,action,payload):
        result = self.request('POST','/api/parameters/'+action,json_body=payload)
        self.assertEqual(result[0],200,result[3])
        return result[2]

    def test_context_publishes_source_paths_without_changing_any_config(self):
        self.start_server()
        root,raw = self.config()
        response = self.request('GET','/api/parameters/context')
        self.assertEqual(response[0],200,response[3])
        self.assertEqual(response[2]['config_path'],str(root/'config_level.csv'))
        self.assertTrue(response[2]['config_ready'])
        self.assertEqual((root/'config_level.csv').read_bytes(),raw)

    def test_verified_job_calculates_exports_previews_and_appends_without_overwriting_original(self):
        self.start_server()
        root,old = self.config()
        job_id = self.verified_job()
        selection = self.call('resolve',{'mode':'job','job_id':job_id})
        result = self.call('calculate',{'selection_id':selection['selection_id']})
        row = result['rows'][0]
        self.assertEqual(row['original_level_id'],'level-source')
        self.assertEqual(row['new_template_id'],'lessTsample')
        self.assertEqual(row['level_indexes'],[334])
        self.assertEqual(row['new_parameters'][0],'3|0|100|2')
        self.assertEqual(row['new_parameters'][9],'3|9|100|2')
        download = self.request('GET','/api/parameters/'+result['calculation_id']+'/download?format=xlsx')
        self.assertEqual(download[0],200,download[3][:200])
        self.assertEqual(download[3][:2],b'PK')
        plan = self.call('preview',{'calculation_id':result['calculation_id'],
            'selected_row_ids':[row['row_id']], 'level_id_rule':'new{原关卡ID}', 'description':'批量备注,中文'})
        self.assertIn('+newlevel-source',plan['diff'])
        self.assertEqual((root/'config_level.csv').read_bytes(),old)
        committed = self.call('commit',{'plan_id':plan['plan_id']})
        self.assertEqual(committed['status'],'committed',committed)
        updated = (root/'config_level.csv').read_bytes()
        self.assertTrue(updated.startswith(old))
        data = list(csv.reader(io.StringIO(updated.decode('utf-8-sig'))))
        self.assertEqual(data[-1][0:4],['newlevel-source','批量备注,中文','lessTsample','1'])
        self.assertEqual(data[-1][-3:],['1003','7','保持,未知字段'])
        again = self.call('commit',{'plan_id':plan['plan_id']})
        self.assertEqual(again['status'],'committed')
        self.assertEqual((root/'config_level.csv').read_bytes(),updated)

    def test_calculate_and_preview_reject_browser_supplied_counts_or_arbitrary_paths(self):
        self.start_server()
        self.assert_error(self.request('POST','/api/parameters/calculate',json_body={
            'selection_id':'made-up','source_tile_counts':{'A':69,'B':39}}),400,'invalid_request')
        self.assert_error(self.request('POST','/api/parameters/commit',json_body={
            'plan_id':'made-up','path':'/tmp/outside.csv'}),400,'invalid_request')
        self.assert_error(self.request('POST','/api/parameters/resolve',json_body={'mode':'job','job_id':'job-'+'1'*32},csrf=False),403,'csrf_failed')

    def test_previewed_config_is_not_overwritten_after_external_change(self):
        self.start_server()
        root,old = self.config()
        selection = self.call('resolve',{'mode':'job','job_id':self.verified_job()})
        result = self.call('calculate',{'selection_id':selection['selection_id']})
        plan = self.call('preview',{'calculation_id':result['calculation_id'],
            'selected_row_ids':[result['rows'][0]['row_id']]})
        newer = old + b'\r\n'
        (root/'config_level.csv').write_bytes(newer)
        response = self.request('POST','/api/parameters/commit',json_body={'plan_id':plan['plan_id']})
        self.assertIn(response[0],(200,409))
        if response[0] == 200:
            self.assertEqual(response[2]['status'],'error')
        self.assertEqual((root/'config_level.csv').read_bytes(),newer)

    def test_versioned_export_and_previous_output_id_use_their_actual_verified_bytes(self):
        app = self.start_server()
        self.config()
        job_id = self.verified_job()
        job = app.repository.get(job_id)
        state = job.snapshot()
        record = next(iter(state['artifacts'].values()))
        raw = job.read_artifact(record['id'])
        output = self.root / 'exports'
        (output/'lessTsample_m.bytes').write_bytes(raw)
        renamed = job.activate_renamed_output(record['id'],'lessTsamplev2','lessTsample')
        (output/'lessTsamplev2_m.bytes').write_bytes(job.read_artifact(renamed['id']))
        for identity in ('lessTsample','lessTsamplev2'):
            selection = self.call('resolve',{'mode':'ids','source':'output','template_ids':[identity]})
            self.assertEqual(selection['entries'][0]['status'],'verified')
            result = self.call('calculate',{'selection_id':selection['selection_id']})
            self.assertEqual(result['rows'][0]['new_template_id'],identity)
            self.assertEqual(result['rows'][0]['original_template_id'],'sample')

    def test_known_template_id_with_different_bytes_cannot_bypass_record_by_explicit_mapping(self):
        app = self.start_server()
        self.config()
        job = app.repository.get(self.verified_job())
        record = next(iter(job.snapshot()['artifacts'].values()))
        raw = job.read_artifact(record['id'])
        # Unknown protobuf fields are legal but change the recorded byte SHA.
        (self.export_root/'lessTsample_m.bytes').write_bytes(raw + b'\xf8\x01\x01')
        selection = self.call('resolve',{'mode':'ids','source':'output','template_ids':['lessTsample'],
            'source_map':{'lessTsample':'sample'}})
        self.assertEqual(selection['entries'][0]['status'],'failed')
        self.assertIn('摘要',selection['entries'][0]['message'])

    def test_selected_file_change_after_matching_artifact_stops_calculation(self):
        app = self.start_server()
        self.config()
        job = app.repository.get(self.verified_job())
        record = next(iter(job.snapshot()['artifacts'].values()))
        raw = job.read_artifact(record['id'])
        output = self.export_root/'lessTsample_m.bytes'
        output.write_bytes(raw)
        selection = self.call('resolve',{'mode':'ids','source':'output','template_ids':['lessTsample']})
        self.assertEqual(selection['entries'][0]['status'],'verified')
        output.write_bytes(raw + b'\xf8\x01\x01')
        response = self.request('POST','/api/parameters/calculate',json_body={'selection_id':selection['selection_id']})
        self.assert_error(response,409,'parameter_evidence_changed')

    def test_explicit_mapping_of_tower_removed_output_still_calculates_new_parameters(self):
        self.start_server()
        self.config()
        source = synthetic(second_tower=True)[0]
        tile_root = self.root/'tile/Tile/Assets/Resources/TileMatchLevelData'
        tile_root.mkdir(parents=True)
        (tile_root/'sample_m.bytes').write_bytes(source)
        settings = load_settings(self.settings_path)
        settings.tile_root, settings.tile_confirmed = str(tile_root), True
        save_settings(settings,self.settings_path)
        doc = decode_template(source)
        doc.template_id = 'manualZeroTower'
        doc.tiles = [replace(tile,x=index*3,y=0,z=0,tile_id=-10 if index<9 else -11)
                     for index,tile in enumerate(doc.tiles)]
        (self.export_root/'manualZeroTower_m.bytes').write_bytes(encode_template(doc))
        selection = self.call('resolve',{'mode':'ids','source':'output','template_ids':['manualZeroTower'],
            'source_map':{'manualZeroTower':'sample'}})
        self.assertEqual(selection['entries'][0]['status'],'verified')
        result = self.call('calculate',{'selection_id':selection['selection_id']})
        self.assertEqual(result['rows'][0]['new_template_id'],'manualZeroTower')
        self.assertEqual(result['rows'][0]['new_parameters'][0],'3|0|100|2')
