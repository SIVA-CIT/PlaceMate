import json
import io
import os
import tempfile
import unittest
import urllib.error
import uuid
from unittest.mock import patch

import app as placemate


class PlaceMateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        placemate.DB_PATH = os.path.join(self.tmp.name, "test.db")
        placemate.init_db()
        self.client = placemate.app.test_client()
        created=self.client.post("/api/register",json={"name":"Test Student","email":f"{uuid.uuid4().hex}@example.com","password":"test-password-123"})
        self.assertEqual(created.status_code,201)
        with placemate.db() as c:
            c.execute("DELETE FROM profiles")
            c.execute("DELETE FROM diagnostics")
            c.execute("DELETE FROM interviews")

    def tearDown(self): self.tmp.cleanup()

    def test_home_and_bootstrap(self):
        response=self.client.get("/")
        self.assertEqual(response.status_code, 200); response.close()
        self.assertIn('window.PLACEMATE_API_BASE', self.client.get('/static/config.js').text)
        data = self.client.get("/api/bootstrap").get_json()
        self.assertEqual(len(data["curriculum"]["CSE"]), 8)
        self.assertEqual(data["profile"]["semester"], 5)

    def test_profile_validation_and_update(self):
        bad = self.client.put("/api/profile", json={"branch":"CSE","semester":9,"domain":"SDE"})
        self.assertEqual(bad.status_code, 400)
        good = self.client.put("/api/profile", json={"name":"Sam","branch":"IT","semester":3,"domain":"Backend Developer"})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.get_json()["profile"]["semester"], 3)

    def test_diagnostic_requires_answer_and_returns_strategy(self):
        self.assertEqual(self.client.post("/api/diagnostic", json={"answers":{}}).status_code, 400)
        result=self.client.post("/api/diagnostic", json={"answers":{"OS":"I am not sure"}})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.get_json()["strategy"]["refreshers"])

    def test_interview_flow_persists_and_scopes_user(self):
        start=self.client.post("/api/interviews",json={"role":"Backend Developer","difficulty":"Intermediate","areas":["DBMS"]})
        self.assertEqual(start.status_code,201); iid=start.get_json()["id"]
        self.assertEqual(start.get_json()["state"],"INTRO")
        self.assertEqual(self.client.post(f"/api/interviews/{iid}/begin",json={}).status_code,200)
        self.assertEqual(self.client.get(f"/api/interviews/{iid}").status_code,200)
        ans=self.client.post(f"/api/interviews/{iid}/answer",json={"answer":"A reasoned answer with an example and trade-offs that describes the approach clearly and considers edge cases for a real application."})
        self.assertEqual(ans.status_code,200)
        self.assertTrue(ans.get_json()["question"]["question"])
        item=self.client.get(f"/api/interviews/{iid}").get_json()
        self.assertEqual(item["turns"][0]["answer"].startswith("A reasoned"),True)
        report=self.client.post(f"/api/interviews/{iid}/finish",json={"answer":"I would test empty inputs and duplicates."})
        self.assertEqual(report.status_code,200)
        self.assertGreater(report.get_json()["report"]["evidence_count"],0)
        self.assertEqual(self.client.get("/api/mentor").status_code,200)

    def test_account_authentication_and_isolation(self):
        interview=self.client.post('/api/interviews',json={'role':'Developer','difficulty':'Beginner','areas':['SQL']})
        interview_id=interview.get_json()['id']
        other=placemate.app.test_client()
        self.assertEqual(other.get('/api/bootstrap').status_code,401)
        created=other.post('/api/register',json={'name':'Taylor','email':'taylor@example.com','password':'a-secure-password'})
        self.assertEqual(created.status_code,201)
        self.assertEqual(other.get('/api/bootstrap').get_json()['profile']['name'],'Taylor')
        self.assertEqual(other.get(f'/api/interviews/{interview_id}').status_code,404)
        self.assertEqual(other.post('/api/logout',json={}).status_code,200)
        self.assertEqual(other.get('/api/bootstrap').status_code,401)

    def test_cors_is_allowlisted_and_preflight_is_public(self):
        with patch.dict(os.environ,{"FRONTEND_ORIGIN":"https://placemate.vercel.app"}):
            allowed=self.client.options('/api/bootstrap',headers={'Origin':'https://placemate.vercel.app'})
            self.assertEqual(allowed.status_code,200)
            self.assertEqual(allowed.headers.get('Access-Control-Allow-Origin'),'https://placemate.vercel.app')
            denied=self.client.options('/api/bootstrap',headers={'Origin':'https://attacker.example'})
            self.assertIsNone(denied.headers.get('Access-Control-Allow-Origin'))

    def test_demo_workspace_gets_an_isolated_session(self):
        first=placemate.app.test_client(); second=placemate.app.test_client()
        first.post('/api/demo',json={}); second.post('/api/demo',json={})
        self.assertNotEqual(first.get('/api/bootstrap').get_json()['profile']['user_id'],second.get('/api/bootstrap').get_json()['profile']['user_id'])

    def test_gemini_request_uses_backend_header_and_parses_json(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_): return False
            def read(self): return json.dumps({'candidates':[{'content':{'parts':[{'text':'{"ok":true}'}]}}]}).encode()
        with patch.dict(os.environ,{'GEMINI_API_KEY':'test-only-secret','GEMINI_MODEL':'gemini-test'}):
            with patch('app.urllib.request.urlopen',return_value=Response()) as send:
                self.assertEqual(placemate.gemini_json('Return ok',{'ok':False}),{'ok':True})
                req=send.call_args.args[0]
                self.assertEqual(req.get_header('X-goog-api-key'),'test-only-secret')
                self.assertNotIn('test-only-secret',req.full_url)
                self.assertEqual(req.full_url,'https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent')

    def test_gemini_failure_and_invalid_json_use_fallback(self):
        fallback={'local':True}
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_): return False
            def read(self): return b'{"candidates":[{"content":{"parts":[{"text":"not-json"}]}}]}'
        with patch.dict(os.environ,{'GEMINI_API_KEY':'test-only-secret'}):
            with patch('app.urllib.request.urlopen',return_value=Response()):
                self.assertEqual(placemate.gemini_json('prompt',fallback),fallback)
            failure=urllib.error.HTTPError('https://example.test',429,'limited',{},io.BytesIO())
            with patch('app.urllib.request.urlopen',side_effect=failure):
                self.assertEqual(placemate.gemini_json('prompt',fallback),fallback)
    def test_coding_round_and_history(self):
        start=self.client.post("/api/interviews",json={"role":"Developer","difficulty":"Beginner","areas":["Python"]})
        iid=start.get_json()["id"]
        self.client.post(f"/api/interviews/{iid}/begin",json={})
        for n in range(4):
            response=self.client.post(f"/api/interviews/{iid}/answer",json={"answer":f"I would reason through the problem using a concrete example number {n}, check edge cases, and explain the tradeoffs."})
            if response.get_json()["state"]=="CODING": break
        item=self.client.get(f"/api/interviews/{iid}").get_json()
        self.assertEqual(item["state"],"CODING")
        saved=self.client.post(f"/api/interviews/{iid}/coding",json={"action":"save","code":"def solve(x):\n return x","language":"Python"})
        self.assertTrue(saved.get_json()["saved"])
        self.assertEqual(self.client.post(f"/api/interviews/{iid}/coding",json={"action":"hint"}).status_code,200)
        submitted=self.client.post(f"/api/interviews/{iid}/coding",json={"action":"submit","code":"def solve(x):\n return x","language":"Python"})
        self.assertTrue(submitted.get_json()["submitted"])
        self.assertEqual(self.client.get("/api/bootstrap").get_json()["interviews"][0]["state"],"CODING_FOLLOW_UP")

    def test_offline_report_withholds_scores_and_saves_evidence(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY":""}):
            started=self.client.post('/api/interviews',json={'role':'Developer','difficulty':'Beginner','areas':['Algorithms']}).get_json()
            iid=started['id']; self.client.post(f'/api/interviews/{iid}/begin',json={})
            self.client.post(f'/api/interviews/{iid}/answer',json={'answer':'I would build a hash map of seen values, check for the complement as I scan, and return the pair indices in expected linear time.'})
            report=self.client.post(f'/api/interviews/{iid}/finish',json={}).get_json()['report']
            self.assertIsNone(report['overall'])
            self.assertTrue(all(x['score'] is None for x in report['categories']))
            self.assertIn('withheld',report['summary'])

    def test_retest_avoids_prior_questions_and_refresh_data_is_persisted(self):
        first=self.client.post('/api/interviews',json={'role':'Developer','difficulty':'Beginner','areas':['DBMS']}).get_json(); iid=first['id']
        prior=self.client.get(f'/api/interviews/{iid}').get_json()['turns'][0]['question']['question']
        self.client.post(f'/api/interviews/{iid}/begin',json={})
        second=self.client.post('/api/interviews',json={'role':'Developer','difficulty':'Beginner','areas':['DBMS']}).get_json()
        self.assertNotEqual(second['question']['question'],prior)
        resumed=self.client.get(f"/api/interviews/{second['id']}").get_json()
        self.assertEqual(resumed['state'],'INTRO')
        self.assertEqual(resumed['turns'][0]['question']['question'],second['question']['question'])


if __name__ == "__main__": unittest.main()
