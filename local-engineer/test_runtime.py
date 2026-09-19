import importlib.util
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import local_engineer as m
from engineer_runtime import bounded_process, preflight, atomic_json, request_completion


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.token_patch=patch('engineer_runtime.measure_tokens',return_value=1000)
        self.token_patch.start()
        self.temp=tempfile.TemporaryDirectory()
        self.root=pathlib.Path(self.temp.name)
        subprocess.run(['git','init','-b','development',str(self.root)],check=True,capture_output=True)
        self.project=m.Project('fixture',{'root':str(self.root),'build':'python3 -m unittest discover -v','test':'python3 -m unittest discover -v'}, {})
        self.original_state=m.STATE
        m.STATE=self.root/'state'

    def tearDown(self):
        self.token_patch.stop()
        m.STATE=self.original_state
        self.temp.cleanup()

    def test_main_protected(self):
        subprocess.run(['git','symbolic-ref','HEAD','refs/heads/main'],cwd=self.root,check=True)
        with self.assertRaises(ValueError): self.project.write_file('x.py','unsafe')

    def test_dirty_overwrite_blocked_exact_edit_preserves_work(self):
        (self.root/'code.py').write_text('user addition\nreturn a - b\n')
        preflight(self.project)
        self.assertEqual(self.project.write_file('code.py','replacement')[0],126)
        self.assertEqual(self.project.replace_text('code.py','return a - b','return a + b')[0],0)
        self.assertEqual((self.root/'code.py').read_text(),'user addition\nreturn a + b\n')
        self.assertEqual((self.project.backup_root/'code.py').read_text(),'user addition\nreturn a - b\n')

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root/'escape').symlink_to(outside,target_is_directory=True)
            self.assertNotEqual(self.project.write_file('escape/x','bad')[0],0)
            self.assertFalse((pathlib.Path(outside)/'x').exists())

    def test_symlink_to_sensitive_file_inside_repo(self):
        (self.root/'.env').write_text('private value')
        (self.root/'ordinary.txt').symlink_to(self.root/'.env')
        self.assertNotEqual(self.project.read_file('ordinary.txt')[0],0)
        self.assertNotEqual(self.project.write_file('ordinary.txt','changed')[0],0)
        self.assertEqual((self.root/'.env').read_text(),'private value')

    def test_finish_tool_for_read_only_question(self):
        self.project.cfg['task_mode']='inspect'
        response={'choices':[{'message':{'tool_calls':[{'id':'done','type':'function','function':{'name':'finish_task','arguments':json.dumps({'report':'Branch development; no changes made.'})}}]}}]}
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), patch.object(m,'get_json',return_value=response):
            self.assertEqual(m.agent(self.project,'Explain Git state'),0)

    def test_resume_cli_uses_self_contained_checkpoint(self):
        checkpoint=self.root/'checkpoint.json'
        checkpoint.write_text(json.dumps({'project':'absent-from-registry','project_config':self.project.cfg,'objective':'continue'}))
        with patch.object(m,'CFG',self.root/'missing-registry.json'), patch.object(sys,'argv',['local-engineer','resume',str(checkpoint)]), patch.object(m,'agent',return_value=0) as agent:
            with self.assertRaises(SystemExit) as result: m.main()
            self.assertEqual(result.exception.code,0)
            self.assertEqual(agent.call_args.args[0].root,str(self.root))

    def test_resume_preserves_edits_made_by_user_during_interruption(self):
        (self.root/'code.py').write_text('agent change\nuser addition\n')
        (self.root/'test_ok.py').write_text('import unittest\nclass Test(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n')
        old_backup=self.root/'old-backup'
        old_backup.mkdir(); (old_backup/'code.py').write_text('original\n')
        checkpoint=self.root/'checkpoint.json'
        checkpoint.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture','objective':'resume',
            'files_modified':['code.py'],'file_hashes':{'code.py':hashlib.sha256(b'agent change\n').hexdigest()},'backup_root':str(old_backup)}))
        response={'choices':[{'message':{'tool_calls':[{'id':'bad','type':'function','function':{'name':'write_file','arguments':json.dumps({'path':'code.py','content':'erased'})}}]}}]}
        final={'choices':[{'message':{'content':'User additions preserved.'}}]}
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), patch.object(m,'get_json',side_effect=[response,final]):
            self.assertEqual(m.agent(self.project,'resume',str(checkpoint)),0)
        self.assertEqual((self.root/'code.py').read_text(),'agent change\nuser addition\n')
        self.assertEqual((old_backup/'code.py').read_text(),'original\n')
        self.assertEqual((self.project.backup_root/'code.py').read_text(),'agent change\nuser addition\n')

    def test_command_bypasses_rejected(self):
        for command in ('git branch -D work','python3 -c "print(1)"','sed -i s/a/b/ x','git reset --hard','bash bad.sh','git diff --output=x','git status; rm -rf .','cargo build --config bad'):
            with self.subTest(command=command): self.assertFalse(self.project.safe_command(command)[0])

    def test_read_search_no_rg(self):
        (self.root/'hello.py').write_text('needle here\n')
        self.assertIn('needle',self.project.read_file('hello.py')[1])
        self.assertIn('hello.py:1:',self.project.search('needle')[1])

    def test_large_file_exact_edit(self):
        text='a'*15000+'\nneedle\n'+'b'*16000
        (self.root/'large.txt').write_text(text)
        self.assertEqual(self.project.replace_text('large.txt','needle','changed')[0],0)
        self.assertEqual((self.root/'large.txt').read_text(),text.replace('needle','changed'))

    def test_source_read_paginates_without_hiding_middle(self):
        (self.root/'source.txt').write_text('\n'.join(('line%03d '%i)+'x'*70 for i in range(100)))
        rc,text=self.project.read_file('source.txt',1,100)
        self.assertEqual(rc,0)
        self.assertIn('start_line=',text)
        self.assertNotIn('line099',text)
        numbered=[line for line in text.splitlines() if line[:1].isdigit()]
        self.assertEqual([int(line.split(':')[0]) for line in numbered],list(range(1,len(numbered)+1)))

    def test_timeout(self):
        rc,out=bounded_process([sys.executable,'-c','import time; time.sleep(30)'],timeout=1)
        self.assertEqual(rc,124); self.assertIn('TIMEOUT',out)

    def test_transient_api_retry_and_nontransient_failure(self):
        with patch.object(m,'get_json',side_effect=[urllib.error.URLError('temporary'),{'ok':True}]), patch('engineer_runtime.time.sleep'):
            self.assertEqual(request_completion(m,{}),{'ok':True})
        error=urllib.error.HTTPError('http://localhost',400,'bad request',{},None)
        with patch.object(m,'get_json',side_effect=error) as request:
            with self.assertRaises(urllib.error.HTTPError): request_completion(m,{})
            self.assertEqual(request.call_count,1)

    def test_stalled_discovery_gets_one_evidence_plan_then_repairs(self):
        (self.root/'calc.py').write_text('value = 0\n')
        (self.root/'test_ok.py').write_text('import unittest\nclass Test(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n')
        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':'call','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}
        responses=[tool('read_file',{'path':'calc.py','start_line':a,'end_line':b}) for a,b in ((1,1),(2,2),(1,2))]
        responses += [{'choices':[{'message':{'content':'The supplied calc.py has value 0; change it to 2 and verify.'}}]},
                      tool('replace_text',{'path':'calc.py','old':'value = 0','new':'value = 2'}),
                      {'choices':[{'message':{'content':'Fixed and verified.'}}]}]
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), patch.object(m,'get_json',side_effect=responses):
            self.assertEqual(m.agent(self.project,'fix value'),0)
        state=json.loads(next((m.STATE/'sessions').glob('*/working_state.json')).read_text())
        self.assertEqual(state['reflection_calls'],1)
        self.assertEqual((self.root/'calc.py').read_text(),'value = 2\n')

    def test_edit_build_failure_repair_rebuild_checkpoint_resume(self):
        (self.root/'calc.py').write_text('value = 0\n')
        (self.root/'test_calc.py').write_text('import unittest\nclass TestCalc(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n')
        calls=[('replace_text',{'path':'calc.py','old':'value = 0','new':'value = 1'}),
               ('replace_text',{'path':'calc.py','old':'value = 1','new':'value = 2'})]
        responses=[{'choices':[{'message':{'tool_calls':[{'id':str(i),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]} for i,(name,args) in enumerate(calls)]
        responses.append({'choices':[{'message':{'content':'Result: fixed and verified'}}]})
        builds=iter([(1,'compiler error'),(0,'build ok')])
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), patch.object(m,'get_json',side_effect=responses), patch.object(self.project,'build',side_effect=lambda extra='':next(builds)):
            self.assertEqual(m.agent(self.project,'fix'),0)
        checkpoints=list((m.STATE/'sessions').glob('*/working_state.json'))
        state=json.loads(checkpoints[0].read_text())
        self.assertEqual(state['files_modified'],['calc.py'])
        self.assertEqual(state['build_status'],'exit=0\nbuild ok')
        self.assertTrue(state['failed_attempts'])
        self.assertEqual(state['status'],'completed')
        # Resume requires new verification; evidence is not silently treated as current.
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), patch.object(m,'get_json',side_effect=RuntimeError('network down')):
            self.assertEqual(m.agent(self.project,'ignored',str(checkpoints[0])),2)
        newest=max((m.STATE/'sessions').glob('*/working_state.json'),key=lambda p:p.stat().st_mtime)
        self.assertEqual(json.loads(newest.read_text())['objective'],'fix')

if __name__=='__main__': unittest.main()
