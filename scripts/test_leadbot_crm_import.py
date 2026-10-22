"""Focused CRM route/transport regressions using only temporary exports/databases.

Run: venv/bin/python scripts/test_leadbot_crm_import.py
Set ACTII_TEST_ROOT to an ACT II checkout to also exercise its real import endpoint.
"""
import ast
import csv
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(os.environ.get('LEADMELEADS_TEST_ROOT', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from agents import actii_integration_agent as bridge, lead_dashboard_agent as dashboard


class CrmImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        previous = Path.cwd()
        os.chdir(temporary.name)
        self.addCleanup(os.chdir, previous)
        Path('data').mkdir()
        from agents import auth_agent as auth
        self.auth = auth
        self.enterContext(patch.object(auth, 'AUTH_DB', Path('data/test-auth.sqlite')))
        auth.init_auth_db()
        auth.create_user('owner', 'synthetic-password', role='standard')
        session = auth.create_session(auth.get_user_by_username('owner'))
        csrf = auth.issue_csrf_token(session)
        binding_file = Path('data/test-crm-bindings.json').resolve()
        from datetime import datetime, timedelta, timezone
        binding_file.write_text(json.dumps({'version':1,'connectors':[{
            'connector_id':'synthetic-local','user_id':auth.get_user_by_username('owner')['id'],
            'username':'owner','binding_version':'v1','enabled':True,
            'token_sha256':hashlib.sha256(b'synthetic-local-binding-only').hexdigest(),
            'expires_at':(datetime.now(timezone.utc)+timedelta(days=30)).isoformat(),
        }]}))
        binding_file.chmod(0o600)
        self.enterContext(patch.dict(os.environ, {'ACTII_CONNECTOR_BINDINGS_FILE':str(binding_file)}))
        self.enterContext(patch.dict(os.environ, {'ACTII_CRM_MODE': 'local', 'ACTII_INTEGRATION_SECRET': 'synthetic-integration-secret', 'ACTII_INTEGRATION_URL': 'http://127.0.0.1:8842'}))
        self.enterContext(patch.object(dashboard, 'EXPORT_DIR', Path('exports')))
        Path('exports').mkdir()
        self.row = dict(business='Synthetic business', domain='business.example', website='https://business.example', phone='555-0100', email='one@business.example, two@business.example', address='17 Example Road', final_lead_score='82', keyword='plumbers', market='Albany, NY', page_title='Example title', meta_description='Example description', h1='Example heading', source='Google organic')
        self.file = 'synthetic.csv'
        with Path('exports', self.file).open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=self.row)
            writer.writeheader(); writer.writerow(self.row)
        Path('data/leadbot_export_owners.json').write_text(json.dumps({self.file: {'owner_username': 'owner'}}))
        app = FastAPI()
        ns = dict(app=app, AuthRequest=Request, Request=Request, JSONResponse=JSONResponse, AUTH_COOKIE_NAME='vast_session', Path=Path, safe_export_file=dashboard.safe_export_file, _leadbot_export_visible_to_user=dashboard._leadbot_export_visible_to_user)
        names = {'auth_current_user', '_csrf_token_valid', 'leadbot_user_can_access_export', 'leadbot_add_to_crm'}
        tree = ast.parse((ROOT/'app/main.py').read_text())
        functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        assert {n.name for n in functions} == names
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'crm-route-harness', 'exec'), ns)
        self.client = TestClient(app)
        self.client.cookies.set('vast_session', session)
        self.payload = dict(csrf_token=csrf, source_file=self.file, source_domain='business.example', owner_id='attacker', lead=dict(company_name='Synthetic business'))
        self.transport = self.enterContext(patch.object(bridge.requests.Session, 'post'))
        self.transport.return_value = Mock(status_code=200, json=Mock(return_value=dict(ok=True,status='created',contact_id=1,open_url='http://127.0.0.1:8842/crm/contacts/1')))

    def send(self):
        return self.client.post('/lead-bot/add-to-crm', json=self.payload)

    def test_all_fields_and_authentic_provenance_are_signed(self):
        self.payload['lead'].update(source_note='forged provenance',lead_score='100',search_metadata={'serp_position':1},owner_id='attacker',phone='555-0123')
        self.assertEqual(self.send().status_code, 200)
        call = self.transport.call_args
        raw = call.kwargs['data']; body = json.loads(raw)
        self.assertEqual(body['owner_id'], 'owner')
        self.assertEqual(body['source_ref'], 'leadmeleads:synthetic.csv:business.example')
        lead = body['lead']
        for key in ('domain','website','email','address'):
            self.assertEqual(lead[key], self.row[key])
        self.assertEqual(lead['phone'],'555-0123')
        self.assertEqual(lead['lead_score'],'82')
        self.assertNotIn('forged', lead['source_note'])
        for value in ('synthetic.csv','business.example','Example title','Example description','Example heading','Google organic','82'):
            self.assertIn(value,lead['source_note'])
        signature=hmac.new(b'synthetic-integration-secret',call.kwargs['headers']['X-ACTII-Timestamp'].encode()+b'.'+raw,hashlib.sha256).hexdigest()
        self.assertEqual(call.kwargs['headers']['X-ACTII-Signature'],signature)
        self.assertFalse(call.kwargs['allow_redirects'])

    def test_unauthenticated_rejected(self):
        self.client.cookies.clear(); self.assertEqual(self.send().status_code,401);self.transport.assert_not_called()

    def test_local_mode_also_requires_explicit_binding(self):
        Path(os.environ['ACTII_CONNECTOR_BINDINGS_FILE']).unlink()
        self.assertEqual(self.send().status_code,503)
        self.transport.assert_not_called()

    def test_csrf_rejected(self):
        self.payload['csrf_token']='invalid'; self.assertEqual(self.send().status_code,403);self.transport.assert_not_called()

    def test_other_owner_rejected(self):
        Path('data/leadbot_export_owners.json').write_text(json.dumps({self.file:{'owner_username':'someone-else'}}))
        self.assertEqual(self.send().status_code,403);self.transport.assert_not_called()

    def test_unknown_domain_rejected(self):
        self.payload['source_domain']='other.example';self.assertEqual(self.send().status_code,422);self.transport.assert_not_called()

    def test_path_traversal_rejected(self):
        self.payload['source_file']='../synthetic.csv';self.assertIn(self.send().status_code,(403,422));self.transport.assert_not_called()

    def test_no_config_reports_blocker_without_importing(self):
        with patch.dict(os.environ,{'ACTII_INTEGRATION_SECRET':''}):
            response=self.send()
        self.assertEqual(response.status_code,503);self.assertIn('secure ACT II connection',response.json()['error']);self.transport.assert_not_called()

    def test_public_and_credential_urls_rejected(self):
        for url in ('https://crm.example','http://user:pass@127.0.0.1:8842','http://127.0.0.1:8842?secret=bad'):
            with self.subTest(url=url),patch.dict(os.environ,{'ACTII_INTEGRATION_URL':url}):
                self.assertEqual(self.send().status_code,503)
        self.transport.assert_not_called()

    def test_invalid_success_and_profile_links_rejected(self):
        for body in ({},[],dict(ok=True,status='created',contact_id=True,open_url='http://127.0.0.1:8842/crm/contacts/True'),dict(ok=True,status='created',contact_id=1,open_url='https://evil.example')):
            self.transport.return_value=Mock(status_code=200,json=Mock(return_value=body))
            self.assertEqual(self.send().status_code,502)

    def test_timeout_recoverable(self):
        self.transport.side_effect=bridge.requests.Timeout()
        self.assertEqual(self.send().status_code,503)

    def test_conflict_preserved(self):
        self.transport.return_value=Mock(status_code=409,json=Mock(return_value=dict(ok=False,error='Review existing contact.')))
        self.assertEqual(self.send().status_code,409)

    @unittest.skipUnless(os.getenv('ACTII_TEST_ROOT'), 'ACT II checkout not supplied')
    def test_real_crm_repeat_import_and_workspace_isolation(self):
        sys.path.insert(0,os.environ['ACTII_TEST_ROOT'])
        with patch.dict(os.environ,{'ACTII_DATABASE':str(Path.cwd()/'bootstrap.sqlite'),'ACTII_LEADME_OWNER_MAP':json.dumps({'owner':'Workspace'})}):
            from actii.main import create_app
            crm=create_app(Path('crm.sqlite'),password='synthetic-password',session_secret='synthetic-session-secret')
            viewer=TestClient(crm,base_url='http://127.0.0.1:8842')
            self.transport.side_effect=lambda url,**kwargs:viewer.post(url,content=kwargs['data'],headers=kwargs['headers'])
            first=self.send();self.assertEqual(first.status_code,200)
            for _ in range(3):
                repeat=self.send();self.assertEqual(repeat.json()['status'],'existing');self.assertEqual(repeat.json()['contact_id'],first.json()['contact_id'])
            self.assertEqual(len(crm.state.store.contacts('Workspace')),1)
            self.assertEqual(crm.state.store.contacts('owner'),[])
            viewer.post('/login',data={'username':'Workspace','password_attempt':'synthetic-password'})
            profile=viewer.get(first.json()['open_url']);self.assertEqual(profile.status_code,200);self.assertIn('Example heading',profile.text)
            viewer.post('/login',data={'username':'Other','password_attempt':'synthetic-password'})
            self.assertEqual(viewer.get(first.json()['open_url']).status_code,404)


if __name__=='__main__':unittest.main()
