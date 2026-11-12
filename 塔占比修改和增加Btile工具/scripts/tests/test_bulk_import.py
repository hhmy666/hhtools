"""One user batch may span several bounded HTTP import requests."""
import json
import shutil
import subprocess
from pathlib import Path
from scripts.tests.test_api import ApiContractCase
from scripts.tests.test_rewrite import synthetic


def template_bytes():
    return synthetic('A')[0]


class BulkImportTests(ApiContractCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')

    def merge(self, imports):
        return self.request('POST', '/api/imports/merge', json_body={
            'import_ids': [item['import_id'] for item in imports]})

    def test_twenty_one_files_merge_in_order_and_deduplicate_across_requests(self):
        app = self.start_server()
        first = self.import_files([(str(i) + '_m.bytes', template_bytes()) for i in range(20)])
        second = self.import_files([('last_m.bytes', template_bytes()), ('broken_m.bytes', b'broken')])
        status, _, result, raw = self.merge([first, second])
        self.assertEqual(status, 200, raw)
        self.assertEqual(len(result['files']), 22)
        self.assertEqual([r['original_name'] for r in result['files']],
                         [str(i) + '_m.bytes' for i in range(20)] + ['last_m.bytes', 'broken_m.bytes'])
        self.assertEqual(result['files'][20]['duplicate_of'], result['files'][0]['file_id'])
        self.assertEqual(result['files'][21]['status'], 'failed')
        self.assertEqual(result['files'][0]['file_id'], first['files'][0]['file_id'])
        job = self.start_job(result['import_id'])
        finished = self.wait_job(job)
        self.assertEqual(len(self.rows(finished)), 2)
        self.assertEqual(len(next(r for r in self.rows(finished) if r['source_template_id'])['original_names']), 21)
        self.assertEqual(app._load_import(first['import_id'])[1]['files'], first['files'])

    def test_merge_rejects_tampered_snapshot_instead_of_using_new_bytes(self):
        app = self.start_server()
        imported = self.import_files([('a_m.bytes', template_bytes())])
        path = app.import_root / imported['import_id'] / 'files' / (imported['files'][0]['file_id'] + '.bytes')
        path.chmod(0o600)
        path.write_bytes(b'tampered')
        self.assert_error(self.merge([imported]), 409, 'import_state_invalid')

    def test_merge_rejects_symbolic_link_snapshot(self):
        app = self.start_server()
        imported = self.import_files([('a_m.bytes', template_bytes())])
        path = app.import_root / imported['import_id'] / 'files' / (imported['files'][0]['file_id'] + '.bytes')
        other = self.root / 'other.bytes'
        other.write_bytes(template_bytes())
        path.unlink()
        path.symlink_to(other)
        self.assert_error(self.merge([imported]), 409, 'import_state_invalid')

    def test_merge_requires_csrf_and_valid_known_import_ids(self):
        self.start_server()
        self.assert_error(self.request('POST', '/api/imports/merge', json_body={'import_ids': []}, csrf=False), 403, 'csrf_failed')
        self.assert_error(self.request('POST', '/api/imports/merge', json_body={'import_ids': []}), 400, 'invalid_request')
        self.assert_error(self.request('POST', '/api/imports/merge', json_body={'import_ids': ['../elsewhere']}), 400, 'invalid_request')
        self.assert_error(self.request('POST', '/api/imports/merge', json_body={'import_ids': ['import-' + '1' * 32]}), 404, 'import_not_found')

    def test_retrying_merge_with_repeated_chunk_id_does_not_duplicate_rows(self):
        self.start_server()
        imported = self.import_files([('a_m.bytes', template_bytes())])
        status, _, result, _ = self.merge([imported, imported])
        self.assertEqual(status, 200)
        self.assertEqual(len(result['files']), 1)

    def test_large_file_metadata_is_a_failed_row_and_never_grants_file_read_authority(self):
        self.start_server()
        response = self.request('POST', '/api/import-rejections', json_body={
            'files': [{'name': 'large_m.bytes', 'size': 33 * 1024 * 1024}]})
        self.assertEqual(response[0], 200, response[3])
        result = response[2]
        self.assertEqual(result['files'][0]['error']['code'], 'file_too_large')
        self.assertEqual(result['files'][0]['status'], 'failed')
        valid = self.import_files([('ok_m.bytes', template_bytes())])
        combined = self.merge([valid, result])[2]
        job = self.wait_job(self.start_job(combined['import_id']))
        self.assertEqual(len(self.rows(job)), 2)
        self.assertEqual(sum(row['status'] == 'verified' for row in self.rows(job)), 1)
        self.assert_error(self.request('POST', '/api/import-rejections', json_body={
            'files': [{'name': 'ok_m.bytes', 'size': 1024}]}), 400, 'invalid_request')

    def test_shipped_javascript_upload_coordinator_uses_real_http_for_more_than_twenty_files(self):
        if not self.node:
            self.skipTest('Node runtime unavailable')
        self.start_server()
        module = str(Path(__file__).resolve().parents[2] / 'frontend/assets/batch-import.js')
        code = r'''
const Bulk = require(MODULE);
const files = Array.from({length: 45}, (_, i) => new File([Buffer.from(RAW, 'hex')], `sample${i}_m.bytes`));
files.push(new File(['broken'], 'bad_m.bytes'));
files.push({name:'large_m.bytes', size:33 * 1024 * 1024});
const session=Bulk.createSession({items:files, mode:'files'});
const calls=[];
Bulk.run(session,{request:async(path, options)=>{
  calls.push(path);
  const headers={'Origin':ORIGIN,'X-CSRF-Token':TOKEN};
  if(options.json) headers['Content-Type']='application/json';
  const response=await fetch(ORIGIN+path,{method:'POST',headers,
    body:options.formData || JSON.stringify(options.json)});
  const payload=await response.json();
  if(!response.ok) throw new Error(JSON.stringify(payload));
  return payload;
}}).then(result=>console.log(JSON.stringify({calls,completed:session.completed,result})))
  .catch(error=>{console.error(error);process.exitCode=1;});
'''
        code = code.replace('MODULE', json.dumps(module)).replace('RAW', json.dumps(template_bytes().hex()))
        code = code.replace('ORIGIN', json.dumps(self.origin)).replace('TOKEN', json.dumps(self.csrf))
        run = subprocess.run([self.node, '-'], input=code, text=True, capture_output=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result['completed'], 47)
        self.assertEqual(result['calls'], ['/api/import'] * 3 + ['/api/import-rejections', '/api/imports/merge'])
        rows = result['result']['files']
        self.assertEqual(len(rows), 47)
        self.assertEqual(sum(bool(row.get('duplicate_of')) for row in rows), 44)
        self.assertEqual([row['error']['code'] for row in rows[-2:]], ['invalid_template', 'file_too_large'])
