from pathlib import Path
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

    def test_finish_tool_with_git_status_completes_from_same_response(self):
        finish_report='Finish report from the mixed tool-call response.'
        mixed={'choices':[{'message':{'tool_calls':[
            {'id':'finish','type':'function','function':{'name':'finish_task',
                'arguments':json.dumps({'report':finish_report})}},
            {'id':'status','type':'function','function':{'name':'git_status',
                'arguments':'{}'}}
        ]}}]}
        fallback={'choices':[{'message':{'content':'Fallback report after mixed calls.'}}]}
        calls=[]

        def api(url,payload,timeout=600):
            calls.append(payload)
            return mixed if len(calls)==1 else fallback

        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=api):
            self.assertEqual(m.agent(self.project,'Explain repository state'),0)

        self.assertEqual(len(calls),1)
        event_file=next((m.STATE/'sessions').glob('*/events.jsonl'))
        events=[json.loads(line) for line in event_file.read_text().splitlines()]
        finish_event=next(event for event in events if event.get('tool')=='finish_task')
        self.assertEqual(finish_event['result'],'final report requested')
        self.assertFalse(any(event.get('tool')=='git_status' for event in events))
        state=json.loads(next((m.STATE/'sessions').glob('*/working_state.json')).read_text())
        self.assertEqual(state['final_report'],finish_report)

    def test_multiple_edits_in_one_response_run_before_first_result_returns_to_model(self):
        (self.root/'.gitignore').write_text('code.py\n')
        (self.root/'code.py').write_text('initial\n')
        first={'id':'first-edit','type':'function','function':{
            'name':'write_file','arguments':json.dumps({
                'path':'code.py','content':'first edit\n'})}}
        second={'id':'second-edit','type':'function','function':{
            'name':'write_file','arguments':json.dumps({
                'path':'code.py','content':'second edit\n'})}}
        batch={'choices':[{'message':{'tool_calls':[first,second]}}]}
        second_batch={'choices':[{'message':{'tool_calls':[second]}}]}
        final={'choices':[{'message':{'content':'Both edits were verified.'}}]}
        sequence=[]

        def api(url,payload,timeout=600):
            carries_first_result=any(message.get('role')=='tool'
                and message.get('tool_call_id')=='first-edit'
                for message in payload['messages'])
            sequence.append(('model_request',carries_first_result))
            request_count=len([event for event in sequence if event[0]=='model_request'])
            if request_count==1:
                return batch
            if request_count==2:
                return second_batch
            return final

        original_dispatch=m.dispatch
        def observed_dispatch(project,name,args):
            if name=='write_file':
                sequence.append(('edit_dispatch',args['content']))
            return original_dispatch(project,name,args)

        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=api), \
             patch.object(m,'dispatch',side_effect=observed_dispatch), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',return_value=(0,'test ok')):
            self.assertEqual(m.agent(self.project,'edit code.py'),0)

        first_edit_index=next(i for i,event in enumerate(sequence)
                              if event==('edit_dispatch','first edit\n'))
        second_edit_index=next(i for i,event in enumerate(sequence)
                               if event==('edit_dispatch','second edit\n'))
        result_seen_index=next((i for i,event in enumerate(sequence)
                                if event==('model_request',True)
                                and i<second_edit_index),None)
        self.assertIsNotNone(result_seen_index,
            'second edit ran before the first edit result was returned to the model')
        self.assertLess(first_edit_index,result_seen_index)

    def test_finish_tool_rejects_non_string_report_and_resumes(self):
        invalid={'choices':[{'message':{'tool_calls':[{'id':'bad','type':'function',
            'function':{'name':'finish_task','arguments':'{"report":7}'}}]}}]}
        valid={'choices':[{'message':{'tool_calls':[{'id':'done','type':'function',
            'function':{'name':'finish_task','arguments':json.dumps({'report':'Retry completed.'})}}]}}]}
        calls=[]
        session_dir=None
        retry_tool_results=[]

        def api(url,payload,timeout=600):
            nonlocal session_dir
            calls.append(payload)
            if len(calls)==1:
                return invalid
            session_dir=next((m.STATE/'sessions').glob('*/working_state.json')).parent
            checkpoint=session_dir/'working_state.json'
            self.assertTrue(checkpoint.is_file())
            persisted=json.loads(checkpoint.read_text())
            self.assertEqual(persisted['status'],'running')
            self.assertIsNone(persisted.get('pending_tool'))
            retry_context='\n'.join(message.get('content','') or '' for message in payload['messages'])
            self.assertIn('finish_task: malformed tool arguments',retry_context)
            self.assertIn('report must be a string',retry_context)
            retry_tool_results.extend(message for message in payload['messages']
                                      if message.get('role')=='tool'
                                      and message.get('tool_call_id')=='bad')
            return valid

        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=api):
            self.assertEqual(m.agent(self.project,'Explain repository state'),0)

        self.assertEqual(len(calls),2)
        final_state=json.loads((session_dir/'working_state.json').read_text())
        self.assertEqual(final_state['status'],'completed')
        self.assertIsNone(final_state.get('pending_tool'))
        self.assertEqual(final_state['final_report'],'Retry completed.')
        self.assertEqual(len(retry_tool_results),1)
        self.assertIn('report must be a string',retry_tool_results[0]['content'])
        events=(session_dir/'events.jsonl').read_text()
        self.assertIn('Malformed finish_task arguments rejected:',events)

    def test_malformed_tool_arguments_are_rejected_and_retry_persists_state(self):
        malformed={'choices':[{'message':{'tool_calls':[{'id':'bad','type':'function',
            'function':{'name':'replace_text','arguments':'{"path":"code.py","old":"old","new":"unterminated'}}]}}]}
        retry={'choices':[{'message':{'tool_calls':[{'id':'done','type':'function',
            'function':{'name':'finish_task','arguments':json.dumps({'report':'Retry completed.'})}}]}}]}
        calls=[]
        session_dir=None

        def api(url,payload,timeout=600):
            nonlocal session_dir
            calls.append(payload)
            if len(calls)==1:
                return malformed
            session_dir=next((m.STATE/'sessions').glob('*/working_state.json')).parent
            persisted=json.loads((session_dir/'working_state.json').read_text())
            self.assertIsNone(persisted['pending_tool'])
            messages=calls[-1]['messages']
            tool_result=next(message['content'] for message in reversed(messages)
                             if message.get('role')=='tool')
            self.assertIn('Malformed tool arguments rejected:',tool_result)
            self.assertIn('Unterminated string',tool_result)
            return retry

        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=api), \
             patch.object(self.project,'replace_text') as execute_tool:
            self.assertEqual(m.agent(self.project,'repair code.py'),0)

        execute_tool.assert_not_called()
        self.assertEqual(len(calls),2)
        final_state=json.loads((session_dir/'working_state.json').read_text())
        self.assertIsNone(final_state['pending_tool'])
        self.assertEqual(final_state['status'],'completed')
        self.assertIn('replace_text: malformed tool arguments',final_state['failed_attempts'][-1])
        events=(session_dir/'events.jsonl').read_text()
        self.assertIn('Malformed tool arguments rejected:',events)

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

    def test_resume_reconciles_edit_interrupted_after_write_before_checkpoint(self):
        (self.root/'.gitignore').write_text('code.py\n')
        write={'choices':[{'message':{'tool_calls':[{'id':'write1','type':'function',
            'function':{'name':'write_file','arguments':json.dumps({
                'path':'code.py','content':'agent version\n'})}}]}}]}
        done={'choices':[{'message':{'tool_calls':[{'id':'done','type':'function',
            'function':{'name':'finish_task','arguments':json.dumps({'report':'Resumed.'})}}]}}]}
        original_dispatch=m.dispatch
        did_interrupt=False

        def write_then_interrupt(project,name,args):
            nonlocal did_interrupt
            result=original_dispatch(project,name,args)
            if name=='write_file' and result.startswith('exit=0') and not did_interrupt:
                did_interrupt=True
                raise RuntimeError('simulated interruption after filesystem write')
            return result

        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',return_value=write), \
             patch.object(m,'dispatch',side_effect=write_then_interrupt):
            self.assertEqual(m.agent(self.project,'write code.py'),2)

        checkpoint=next((m.STATE/'sessions').glob('*/working_state.json'))
        interrupted=json.loads(checkpoint.read_text())
        self.assertEqual(interrupted['status'],'interrupted')
        self.assertEqual(interrupted['pending_tool']['name'],'write_file')
        self.assertNotIn('code.py',interrupted['files_modified'])
        self.assertEqual((self.root/'code.py').read_text(),'agent version\n')

        with (self.root/'code.py').open('a') as user_file:
            user_file.write('user addition\n')

        resume_calls=[]
        def resume_api(url,payload,timeout=600):
            resume_calls.append(payload)
            if len(resume_calls)==1:
                return write
            latest=max((m.STATE/'sessions').glob('*/working_state.json'),
                       key=lambda path:path.stat().st_mtime)
            persisted=json.loads(latest.read_text())
            self.assertIn('interrupted edit outcome is uncertain',
                          '\n'.join(persisted['failed_attempts']))
            tool_error=next(message['content'] for message in reversed(payload['messages'])
                            if message.get('role')=='tool'
                            and message.get('tool_call_id')=='write1')
            self.assertIn('same-path edit was not replayed',tool_error)
            return done

        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=resume_api), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',return_value=(0,'test ok')), \
             patch.object(m,'dispatch',wraps=original_dispatch) as resumed_dispatch:
            self.assertEqual(m.agent(self.project,'ignored',str(checkpoint)),0)

        replayed=[call for call in resumed_dispatch.call_args_list
                  if call.args[1]=='write_file']
        self.assertEqual(len(replayed),0)
        self.assertEqual((self.root/'code.py').read_text(),
                         'agent version\nuser addition\n')

    def test_resume_uncertain_edit_with_changed_content_preserves_user_addition(self):
        (self.root/'.gitignore').write_text('code.py\n')
        first_write={'choices':[{'message':{'tool_calls':[{'id':'write1','type':'function',
            'function':{'name':'write_file','arguments':json.dumps({
                'path':'code.py','content':'agent version\n'})}}]}}]}
        changed_write={'choices':[{'message':{'tool_calls':[{'id':'write2','type':'function',
            'function':{'name':'write_file','arguments':json.dumps({
                'path':'code.py','content':'agent revised version\n'})}}]}}]}
        read={'choices':[{'message':{'tool_calls':[{'id':'read1','type':'function',
            'function':{'name':'read_file','arguments':json.dumps({'path':'code.py'})}}]}}]}
        safe_write={'choices':[{'message':{'tool_calls':[{'id':'write3','type':'function',
            'function':{'name':'write_file','arguments':json.dumps({
                'path':'code.py','content':'agent revised version\nuser addition\n'})}}]}}]}
        done={'choices':[{'message':{'tool_calls':[{'id':'done','type':'function',
            'function':{'name':'finish_task','arguments':json.dumps({'report':'Resumed.'})}}]}}]}
        original_dispatch=m.dispatch

        def write_then_interrupt(project,name,args):
            result=original_dispatch(project,name,args)
            if name=='write_file' and result.startswith('exit=0'):
                raise RuntimeError('simulated interruption after filesystem write')
            return result

        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',return_value=first_write), \
             patch.object(m,'dispatch',side_effect=write_then_interrupt):
            self.assertEqual(m.agent(self.project,'write code.py'),2)

        checkpoint=next((m.STATE/'sessions').glob('*/working_state.json'))
        interrupted=json.loads(checkpoint.read_text())
        self.assertEqual(interrupted['status'],'interrupted')
        self.assertEqual(interrupted['pending_tool']['name'],'write_file')
        self.assertNotIn('code.py',interrupted['files_modified'])
        with (self.root/'code.py').open('a') as user_file:
            user_file.write('user addition\n')

        resume_calls=[]
        def resume_api(url,payload,timeout=600):
            resume_calls.append(payload)
            if 'tools' not in payload:
                return {'choices':[{'message':{'content':json.dumps({
                    'hypothesis':'the inspected file already includes the user addition',
                    'target_file':'code.py','expected_effect':'preserve the user addition',
                    'smallest_edit':'write only the reviewed contents','missing_evidence':''})}}]}
            if len(resume_calls)==1:
                return changed_write
            if len(resume_calls)==2:
                error=next(message['content'] for message in reversed(payload['messages'])
                           if message.get('role')=='tool'
                           and message.get('tool_call_id')=='write2')
                self.assertIn('same-path edit was not replayed',error)
                return read
            if len(resume_calls)==4:
                return safe_write
            return done

        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=resume_api), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',return_value=(0,'test ok')), \
             patch.object(m,'dispatch',wraps=original_dispatch) as resumed_dispatch:
            self.assertEqual(m.agent(self.project,'ignored',str(checkpoint)),0)

        replayed=[call for call in resumed_dispatch.call_args_list
                  if call.args[1]=='write_file']
        self.assertEqual(len(replayed),1)
        self.assertEqual(replayed[0].args[2]['content'],
                         'agent revised version\nuser addition\n')
        self.assertEqual((self.root/'code.py').read_text(),
                         'agent revised version\nuser addition\n')


    def test_command_bypasses_rejected(self):
        for command in ('git branch -D work','python3 -c "print(1)"','sed -i s/a/b/ x','git reset --hard','bash bad.sh','git diff --output=x','git status; rm -rf .','cargo build --config bad'):
            with self.subTest(command=command): self.assertFalse(self.project.safe_command(command)[0])

    def test_read_search_no_rg(self):
        (self.root/'hello.py').write_text('needle here\n')
        self.assertIn('needle',self.project.read_file('hello.py')[1])
        self.assertIn('hello.py:1:',self.project.search('needle')[1])

    def test_search_no_match_is_successful_exploration(self):
        rc,out=self.project.search('definitely-not-present')
        self.assertEqual(rc,0)
        self.assertIn('No matches',out)

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

    def test_failed_verification_discovery_is_semantically_bounded_and_rereflects(self):
        (self.root/'calc.py').write_text('value = 0\nvalue = 1\nvalue = 2\nvalue = 3\n')
        self.project.cfg['initial_verify']=True
        self.project.cfg['test']='registered-failing-test'
        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':name+str(args),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}
        wrong={'choices':[{'message':{'content':"Actual is 'Saved', expected is 'Ready', but prefer Saved."}}]}
        corrected={'choices':[{'message':{'content':"Actual is 'Saved'; expected is 'Ready'. Change selection toward Ready."}}]}
        first=[tool('read_file',{'path':'calc.py','start_line':n,'end_line':n}) for n in (1,2,3)]
        middle=[tool('read_file',{'path':'calc.py','start_line':n,'end_line':n}) for n in (4,1,2,3,4,1)]
        last=[tool('search_text',{'pattern':'value%s'%n,'glob':'*.py'}) for n in range(5)]
        responses=first+[wrong]+middle+[corrected]+last
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=responses), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',return_value=(1,"assertion: actual 'Saved' != expected 'Ready'")):
            self.assertEqual(m.agent(self.project,'repair failing peer selection'),2)
        state=json.loads(next((m.STATE/'sessions').glob('*/working_state.json')).read_text())
        self.assertEqual(state['reflections_this_generation'],1)
        self.assertEqual(state['discovery_after_hypothesis'],3)
        self.assertEqual(state['status'],'blocked')

    def test_hypothesis_gate_prefers_an_edit_after_initial_discovery(self):
        (self.root/'calc.py').write_text('value = 0\nvalue = 1\nvalue = 2\nvalue = 3\n')
        (self.root/'test_ok.py').write_text('import unittest\nclass Test(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n')
        self.project.cfg['initial_verify']=True
        self.project.cfg['test']='registered-failing-test'
        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':name+str(args),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}
        reads=[tool('read_file',{'path':'calc.py','start_line':n,'end_line':n}) for n in (1,2,3,4,1,2,3,4,1)]
        wrong={'choices':[{'message':{'content':'The observed value is Saved, so preserve Saved.'}}]}
        edit=tool('replace_text',{'path':'calc.py','old':'value = 0','new':'value = 2'})
        final={'choices':[{'message':{'content':'Result: corrected after test-evidence review.'}}]}
        responses=reads[:3]+[wrong,edit,final]
        tests=iter([(1,"actual 'Saved' != expected 'Ready'"),(0,'test ok')])
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=responses), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',side_effect=lambda *args: next(tests)):
            self.assertEqual(m.agent(self.project,'repair'),0)
        state=json.loads(next((m.STATE/'sessions').glob('*/working_state.json')).read_text())
        self.assertEqual(state['reflection_calls'],1)
        self.assertEqual((self.root/'calc.py').read_text().splitlines()[0],'value = 2')

    def test_hypothesis_gate_warns_before_it_blocks_discovery(self):
        (self.root/'calc.py').write_text('value = 0\nvalue = 1\nvalue = 2\n')
        (self.root/'test_ok.py').write_text('import unittest\nclass Test(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n')
        self.project.cfg['initial_verify']=True
        self.project.cfg['test']='registered-failing-test'
        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':name+str(args),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}
        reads=[tool('read_file',{'path':'calc.py','start_line':n,'end_line':n}) for n in (1,2,3,1)]
        reflection={'choices':[{'message':{'content':'Actual is 0; expected is 2. Replace the arithmetic operator.'}}]}
        edit=tool('replace_text',{'path':'calc.py','old':'value = 0','new':'value = 2'})
        final={'choices':[{'message':{'content':'Result: experiment verified.'}}]}
        tests=iter([(1,'actual 0, expected 2'),(0,'test ok')])
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=reads[:3]+[reflection,reads[3],edit,final]), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',side_effect=lambda *args: next(tests)):
            self.assertEqual(m.agent(self.project,'repair'),0)
        session=next((m.STATE/'sessions').glob('*/working_state.json')).parent
        self.assertIn('EXPERIMENT REQUIRED', (session/'events.jsonl').read_text())

    def test_unknown_edit_target_blocks_safely_after_hypothesis_budget(self):
        (self.root/'source.py').write_text('unrelated = True\n')
        self.project.cfg['initial_verify']=True
        self.project.cfg['test']='registered-failing-test'
        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':name+str(args),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}
        first=[tool('read_file',{'path':'source.py','start_line':1,'end_line':1}) for _ in range(3)]
        searches=[tool('search_text',{'pattern':'missing%s'%n,'glob':'*.py'}) for n in range(3)]
        reflection={'choices':[{'message':{'content':'The test fails, but no safe edit target is identified yet.'}}]}
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=first+[reflection]+searches), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',return_value=(1,'expected behavior is unknown')):
            self.assertEqual(m.agent(self.project,'repair'),2)
        state=json.loads(next((m.STATE/'sessions').glob('*/working_state.json')).read_text())
        self.assertEqual(state['status'],'blocked')
        self.assertEqual(state['files_modified'],[])
        self.assertTrue(state['targeted_discovery_required'])

    def test_experiment_tool_schema_removes_discovery_and_keeps_edits(self):
        (self.root/'calc.py').write_text('value = 0\n')
        (self.root/'test_ok.py').write_text('import unittest\nclass Test(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n')
        checkpoint=self.root/'resume-gate.json'
        checkpoint.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture',
            'project_config':self.project.cfg,'objective':'repair','files_modified':[],
            'build_status':'exit=0\\nbuild ok','test_status':'exit=1\\nactual 0 expected 2',
            'cache':{'0:read_file:{"end_line": 1, "path": "calc.py", "start_line": 1}':'exit=0\\n1: value = 0'}}))
        seen=[]
        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':name+str(args),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}
        def api(url,payload,timeout=600):
            if 'tools' not in payload:
                return {'choices':[{'message':{'content':json.dumps({'hypothesis':'value is wrong','target_file':'calc.py','expected_effect':'value becomes 2','smallest_edit':'replace 0 with 2','missing_evidence':''})}}]}
            names={entry['function']['name'] for entry in payload['tools']}
            seen.append(names)
            if len(seen)==1:
                self.assertIn('read_file',names)
                return tool('read_file',{'path':'calc.py','start_line':1,'end_line':1})
            if len(seen)==2:
                self.assertIn('read_file',names)
                return tool('read_file',{'path':'calc.py','start_line':1,'end_line':1})
            self.assertNotIn('read_file',names)
            self.assertFalse({'search_text','list_files'} & names)
            self.assertNotIn('run_command',names)
            self.assertIn('replace_text',names)
            self.assertIn('test_project',names)
            return tool('replace_text',{'path':'calc.py','old':'value = 0','new':'value = 2'})
        final={'choices':[{'message':{'content':'Result: edit verified.'}}]}
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=lambda url,payload,timeout=600: final if len(seen)>=3 and 'tools' not in payload and payload['messages'][0]['content'].startswith('Report') else api(url,payload,timeout)):
            self.assertEqual(m.agent(self.project,'ignored',str(checkpoint)),0)
        self.assertEqual((self.root/'calc.py').read_text(),'value = 2\n')

    def test_experiment_state_updates_cannot_extend_the_gate_indefinitely(self):
        (self.root/'calc.py').write_text('value = 0\n')
        checkpoint=self.root/'resume-updates.json'
        checkpoint.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture',
            'project_config':self.project.cfg,'objective':'repair','files_modified':[],
            'build_status':'exit=0\\nbuild ok','test_status':'exit=1\\nactual 0 expected 2',
            'cache':{'0:read_file:{"end_line": 1, "path": "calc.py", "start_line": 1}':'exit=0\\n1: value = 0'}}))
        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':name+str(args),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}
        reflection={'choices':[{'message':{'content':json.dumps({'hypothesis':'value is wrong','target_file':'calc.py','expected_effect':'value becomes 2','smallest_edit':'replace 0 with 2','missing_evidence':''})}}]}
        update=tool('update_working_state',{'next_action':'still considering the same edit'})
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=[reflection,update,update,update]):
            self.assertEqual(m.agent(self.project,'ignored',str(checkpoint)),2)
        state=json.loads(next((m.STATE/'sessions').glob('*/working_state.json')).read_text())
        self.assertEqual(state['experiment_state_updates'],3)
        self.assertEqual(state['files_modified'],[])
        self.assertEqual(state['status'],'blocked')

    def test_resume_stale_hypothesis_is_reflected_again(self):
        (self.root/'calc.py').write_text('value = 0\n')
        checkpoint=self.root/'stale.json'
        checkpoint.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture',
            'project_config':self.project.cfg,'objective':'repair','files_modified':[],
            'build_status':'exit=0\\nbuild ok','test_status':"exit=1\\nactual 'Saved' != expected 'Ready'",
            'hypothesis':'Prefer the earlier Saved peer.','reflection_calls':1,
            'reflections_this_generation':1,'generation':0,'cache':{
                '0:read_file:{"end_line": 1, "path": "calc.py", "start_line": 1}':'exit=0\\n1: value = 0'}}))
        reflection={'choices':[{'message':{'content':"Actual is 'Saved'; expected is 'Ready'. Prefer an edit that selects Ready."}}]}
        done={'choices':[{'message':{'tool_calls':[{'id':'done','type':'function','function':{'name':'finish_task','arguments':json.dumps({'report':'Need edit after corrected hypothesis.'})}}]}}]}
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), patch.object(m,'get_json',side_effect=[reflection,done]):
            m.agent(self.project,'ignored',str(checkpoint))
        state=json.loads(next((m.STATE/'sessions').glob('*/working_state.json')).read_text())
        self.assertEqual(state['reflection_calls'],2)
        self.assertFalse(state['force_reflection'])
        self.assertTrue(state['hypothesis_ready'])
        self.assertTrue(state['experiment_required'])
        self.assertEqual(state['discovery_after_hypothesis'],0)
        self.assertIn('expected',state['hypothesis'])

    def test_evidence_supported_edit_resets_stall_budget(self):
        (self.root/'calc.py').write_text('value = 0\n')
        (self.root/'test_ok.py').write_text('import unittest\nclass Test(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n')
        checkpoint=self.root/'stale-edit.json'
        checkpoint.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture',
            'project_config':self.project.cfg,'objective':'repair','files_modified':[],
            'build_status':'exit=0\\nbuild ok','test_status':'exit=1\\nactual differs from expected',
            'hypothesis':'stale','reflection_calls':1,'reflections_this_generation':1,
            'failed_verification_discovery_calls':6,'generation':0,'cache':{
                '0:read_file:{"end_line": 1, "path": "calc.py", "start_line": 1}':'exit=0\\n1: value = 0'}}))
        reflection={'choices':[{'message':{'content':'Actual is 0; expected is 2. Replace 0 with 2, then test.'}}]}
        edit={'choices':[{'message':{'tool_calls':[{'id':'edit','type':'function','function':{'name':'replace_text','arguments':json.dumps({'path':'calc.py','old':'value = 0','new':'value = 2'})}}]}}]}
        final={'choices':[{'message':{'content':'Result: verified minimal edit.'}}]}
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), patch.object(m,'get_json',side_effect=[reflection,edit,final]):
            self.assertEqual(m.agent(self.project,'ignored',str(checkpoint)),0)
        state=json.loads(next((m.STATE/'sessions').glob('*/working_state.json')).read_text())
        self.assertEqual(state['reflections_this_generation'],0)
        self.assertEqual(state['failed_verification_discovery_calls'],0)
        self.assertFalse(state['experiment_required'])
        self.assertEqual(state['discovery_after_hypothesis'],0)
        self.assertEqual(state['experiment_state_updates'],0)
        self.assertEqual((self.root/'calc.py').read_text(),'value = 2\n')

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
        first_edit={'choices':[{'message':{'tool_calls':[{'id':'0','type':'function','function':{'name':calls[0][0],'arguments':json.dumps(calls[0][1])}}]}}]}
        repair_reflection={'choices':[{'message':{'content':json.dumps({
            'hypothesis':'first edit causes compiler error; use the follow-up value already identified by the task evidence',
            'target_file':'calc.py','expected_effect':'build succeeds','smallest_edit':'replace value = 1 with value = 2',
            'missing_evidence':'','targeted_reads':[]})}}]}
        second_edit={'choices':[{'message':{'tool_calls':[{'id':'1','type':'function','function':{'name':calls[1][0],'arguments':json.dumps(calls[1][1])}}]}}]}
        responses=[first_edit,repair_reflection,second_edit,{'choices':[{'message':{'content':'Result: fixed and verified'}}]}]
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


    def test_post_edit_failure_enters_closed_repair_and_reedits(self):
        self.project.cfg['test']='registered-failing-test'
        (self.root/'calc.py').write_text('value = 1\n')
        old_backup=self.root/'old-backup'; old_backup.mkdir(); (old_backup/'calc.py').write_text('value = 0\n')
        digest=hashlib.sha256((self.root/'calc.py').read_bytes()).hexdigest()
        checkpoint=self.root/'post-edit.json'
        checkpoint.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture',
            'project_config':self.project.cfg,'objective':'repair value','files_modified':['calc.py'],
            'file_hashes':{'calc.py':digest},'backup_root':str(old_backup),
            'hypothesis':'value 0 should become 1','build_status':'exit=0\nbuild ok',
            'test_status':'exit=1\nAssertionError: actual 1 expected 2','generation':1,'cache':{}}))
        seen=[]
        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':name+str(len(seen)),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}
        def api(url,payload,timeout=600):
            if 'tools' not in payload:
                system=payload['messages'][0]['content']
                if system.startswith('Report'):
                    return {'choices':[{'message':{'content':'Result: repaired and verified.'}}]}
                self.assertIn('refining a failed code edit',system)
                body=payload['messages'][1]['content']
                self.assertIn('previous_hypothesis',body)
                self.assertIn('current_diff',body)
                self.assertIn('actual 1 expected 2',body)
                return {'choices':[{'message':{'content':json.dumps({
                    'hypothesis':'the first edit changed the value but stopped at 1',
                    'target_file':'calc.py','expected_effect':'value becomes 2',
                    'smallest_edit':'replace value = 1 with value = 2','missing_evidence':'','targeted_reads':[]})}}]}
            names={entry['function']['name'] for entry in payload['tools']}; seen.append(names)
            self.assertFalse({'search_text','list_files','run_command'} & names)
            self.assertIn('replace_text',names); self.assertIn('test_project',names)
            return tool('replace_text',{'path':'calc.py','old':'value = 1','new':'value = 2'})
        tests=iter([(1,'AssertionError: actual 1 expected 2'),(0,'test ok')])
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=api), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',side_effect=lambda *args: next(tests)):
            self.assertEqual(m.agent(self.project,'ignored',str(checkpoint)),0)
        state=json.loads(next((m.STATE/'sessions').glob('*/working_state.json')).read_text())
        self.assertEqual((self.root/'calc.py').read_text(),'value = 2\n')
        self.assertEqual(state['repair_attempts'],1)
        self.assertEqual(state['verification_failure_class'],'')
        self.assertEqual(state['phase'],'normal')
        self.assertTrue(seen)

    def test_post_edit_repair_instructions_match_active_tool_schema(self):
        self.project.cfg['test']='registered-failing-test'
        (self.root/'calc.py').write_text('value = 1\n')
        digest=hashlib.sha256((self.root/'calc.py').read_bytes()).hexdigest()
        checkpoint=self.root/'post-edit-prompt.json'
        checkpoint.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture',
            'project_config':self.project.cfg,'objective':'repair value','files_modified':['calc.py'],
            'file_hashes':{'calc.py':digest},'backup_root':str(self.root/'backup'),
            'hypothesis':'value should become 2','build_status':'exit=0\nbuild ok',
            'test_status':'exit=1\nactual 1 expected 2','generation':1,'cache':{}}))
        repair_request={}

        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':name,'type':'function',
                'function':{'name':name,'arguments':json.dumps(args)}}]}}]}

        def api(url,payload,timeout=600):
            nonlocal repair_request
            if 'tools' not in payload:
                if payload['messages'][0]['content'].startswith('Report'):
                    return {'choices':[{'message':{'content':'Result: repaired.'}}]}
                return {'choices':[{'message':{'content':json.dumps({
                    'hypothesis':'value 1 must become 2','target_file':'calc.py',
                    'expected_effect':'the test expects value 2','smallest_edit':'replace 1 with 2',
                    'missing_evidence':'','targeted_reads':[]})}}]}
            repair_request=payload
            return tool('replace_text',{'path':'calc.py','old':'value = 1','new':'value = 2'})

        tests=iter([(1,'actual 1 expected 2'),(0,'test ok')])
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=api), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',side_effect=lambda *args: next(tests)):
            self.assertEqual(m.agent(self.project,'ignored',str(checkpoint)),0)

        active_names={entry['function']['name'] for entry in repair_request['tools']}
        self.assertNotIn('update_working_state',active_names)
        system=repair_request['messages'][0]['content']
        user=repair_request['messages'][1]['content']
        self.assertNotIn('Use update_working_state to save',system)
        self.assertNotIn('Use update_working_state to save',user)
        self.assertIn('update_working_state is also unavailable',user)
        self.assertIn('checkpoint persistence is automatic',user)

    def test_registered_test_source_is_available_without_project_root_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            source=pathlib.Path(outside)/'acceptance.py'
            source.write_text("EXPECTED = 'Keyboard-A'\\n")
            self.project.cfg['test']=f'python3 {source} test'
            rc,out=self.project.read_test_source()
            self.assertEqual(rc,0)
            self.assertIn("EXPECTED = 'Keyboard-A'",out)

    def test_registered_test_source_rejects_sensitive_path(self):
        with tempfile.TemporaryDirectory() as outside:
            source=pathlib.Path(outside)/'.env.py'
            source.write_text("SECRET = 'no'\\n")
            self.project.cfg['test']=f'python3 {source} test'
            rc,out=self.project.read_test_source()
            self.assertNotEqual(rc,0)

    def test_post_edit_repair_rejects_unrelated_read(self):
        self.project.cfg['test']='registered-failing-test'
        (self.root/'calc.py').write_text('value = 1\n')
        (self.root/'unrelated.py').write_text('secret_of_bug = False\n')
        old_backup=self.root/'old-backup'; old_backup.mkdir(); (old_backup/'calc.py').write_text('value = 0\n')
        digest=hashlib.sha256((self.root/'calc.py').read_bytes()).hexdigest()
        checkpoint=self.root/'repair-read.json'
        checkpoint.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture',
            'project_config':self.project.cfg,'objective':'repair','files_modified':['calc.py'],
            'file_hashes':{'calc.py':digest},'backup_root':str(old_backup),
            'hypothesis':'first edit','build_status':'exit=0\nbuild ok',
            'test_status':'exit=1\nactual 1 expected 2','generation':1,'cache':{},'files_inspected':['calc.py']}))
        calls=[]
        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':name+str(len(calls)),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}
        def api(url,payload,timeout=600):
            if 'tools' not in payload:
                if payload['messages'][0]['content'].startswith('Report'):
                    return {'choices':[{'message':{'content':'blocked'}}]}
                return {'choices':[{'message':{'content':json.dumps({
                    'hypothesis':'need one exact source fact','target_file':'','expected_effect':'','smallest_edit':'',
                    'missing_evidence':'inspect changed line','targeted_reads':['unrelated.py']})}}]}
            calls.append({entry['function']['name'] for entry in payload['tools']})
            if len(calls)==1:
                return tool('read_file',{'path':'unrelated.py','start_line':1,'end_line':1})
            return tool('finish_task',{'report':'blocked after rejected unrelated read'})
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=api), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',return_value=(1,'actual 1 expected 2')):
            self.assertEqual(m.agent(self.project,'ignored',str(checkpoint)),2)
        events=next((m.STATE/'sessions').glob('*/events.jsonl')).read_text()
        self.assertIn('POST_EDIT_REPAIR read rejected',events)
        self.assertNotIn('secret_of_bug',events)

    def test_post_edit_repair_targeted_read_budget_is_two(self):
        self.project.cfg['test']='registered-failing-test'
        (self.root/'calc.py').write_text('value = 1\nmore = 0\n')
        old_backup=self.root/'old-backup'; old_backup.mkdir(); (old_backup/'calc.py').write_text('value = 0\nmore = 0\n')
        digest=hashlib.sha256((self.root/'calc.py').read_bytes()).hexdigest()
        checkpoint=self.root/'repair-budget.json'
        checkpoint.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture',
            'project_config':self.project.cfg,'objective':'repair','files_modified':['calc.py'],
            'file_hashes':{'calc.py':digest},'backup_root':str(old_backup),
            'hypothesis':'first edit','build_status':'exit=0\nbuild ok','test_status':'exit=1\nactual 1 expected 2',
            'generation':1,'cache':{},'files_inspected':['calc.py']}))
        schemas=[]; reflections=0
        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':name+str(len(schemas)),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}
        def api(url,payload,timeout=600):
            nonlocal reflections
            if 'tools' not in payload:
                if payload['messages'][0]['content'].startswith('Report'):
                    return {'choices':[{'message':{'content':'Result: verified.'}}]}
                reflections+=1
                if reflections<3:
                    return {'choices':[{'message':{'content':json.dumps({
                        'hypothesis':'need changed file context','target_file':'','expected_effect':'','smallest_edit':'',
                        'missing_evidence':'calc line','targeted_reads':['calc.py']})}}]}
                return {'choices':[{'message':{'content':json.dumps({
                    'hypothesis':'two reads show value must be 2','target_file':'calc.py','expected_effect':'value becomes 2',
                    'smallest_edit':'replace value 1 with 2','missing_evidence':'','targeted_reads':[]})}}]}
            names={entry['function']['name'] for entry in payload['tools']}; schemas.append(names)
            if len(schemas)<=2:
                self.assertIn('read_file',names)
                return tool('read_file',{'path':'calc.py','start_line':len(schemas),'end_line':len(schemas)})
            self.assertNotIn('read_file',names)
            return tool('replace_text',{'path':'calc.py','old':'value = 1','new':'value = 2'})
        tests=iter([(1,'actual 1 expected 2'),(0,'ok')])
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=api), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',side_effect=lambda *args: next(tests)):
            self.assertEqual(m.agent(self.project,'ignored',str(checkpoint)),0)
        state=json.loads(next((m.STATE/'sessions').glob('*/working_state.json')).read_text())
        self.assertEqual(state['repair_targeted_reads_used'],2)
        self.assertEqual((self.root/'calc.py').read_text().splitlines()[0],'value = 2')

    def test_post_edit_repair_absolute_read_path_is_rejected_not_crashing(self):
        # Absolute paths are invalid in the repair phase; reject them as tool errors.
        # The agent must continue instead of aborting with ValueError.
        state_path = self.root/'absolute-read.json'
        self.project.cfg['test']='registered-failing-test'
        (self.root/'calc.py').write_text('value = 1\n')
        digest=hashlib.sha256((self.root/'calc.py').read_bytes()).hexdigest()
        state_path.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture',
            'project_config':self.project.cfg,'objective':'repair','files_modified':['calc.py'],
            'file_hashes':{'calc.py':digest},'backup_root':str(self.root/'backup'),
            'hypothesis':'first edit','build_status':'exit=0\nbuild ok',
            'test_status':'exit=1\nactual 1 expected 2','generation':1}))
        responses=[
            {'choices':[{'message':{'content':json.dumps({
                'hypothesis':'inspect changed file','target_file':'calc.py',
                'expected_effect':'understand failure','smallest_edit':'','missing_evidence':'read file',
                'targeted_reads':['calc.py']})}}]},
            {'choices':[{'message':{'tool_calls':[{'id':'read1','type':'function',
                'function':{'name':'read_file','arguments':json.dumps({'path':str(self.root/'calc.py')})}}]}}]},
            {'choices':[{'message':{'tool_calls':[{'id':'repair','type':'function',
                'function':{'name':'replace_text','arguments':json.dumps({
                    'path':'calc.py','old':'value = 1','new':'value = 2'})}}]}}]},
            {'choices':[{'message':{'content':'Result: repaired and verified.'}}]}
        ]
        def api(*args,**kwargs):
            return responses.pop(0)
        tests=iter([(1,'actual 1 expected 2'),(0,'test ok')])
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=api), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',side_effect=lambda *args: next(tests)):
            result=m.agent(self.project,'ignored',str(state_path))
        self.assertEqual(result,0)
        state=json.loads(next((m.STATE/'sessions').glob('*/working_state.json')).read_text())
        self.assertEqual(state['status'],'completed')
        self.assertNotEqual(state['status'],'interrupted')
        self.assertEqual((self.root/'calc.py').read_text(),'value = 2\n')
        events=next((m.STATE/'sessions').glob('*/events.jsonl')).read_text()
        self.assertIn('POST_EDIT_REPAIR read rejected: path must be project-relative and stay inside the project',events)

    def test_post_edit_repair_cached_read_cannot_bypass_budget_or_state_update_gate(self):
        self.project.cfg['test']='registered-failing-test'
        (self.root/'calc.py').write_text('value = 1\n')
        old_backup=self.root/'old-backup'; old_backup.mkdir(); (old_backup/'calc.py').write_text('value = 0\n')
        digest=hashlib.sha256((self.root/'calc.py').read_bytes()).hexdigest()
        checkpoint=self.root/'repair-cached-read.json'
        cached_key='1:read_file:'+json.dumps({'path':'calc.py','start_line':1,'end_line':20},sort_keys=True)
        checkpoint.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture',
            'project_config':self.project.cfg,'objective':'repair','files_modified':['calc.py'],
            'file_hashes':{'calc.py':digest},'backup_root':str(old_backup),
            'hypothesis':'first edit','build_status':'exit=0\nbuild ok',
            'test_status':'exit=1\nactual 1 expected 2','generation':1,
            'cache':{cached_key:'exit=0\nvalue = 1\n'},'files_inspected':['calc.py']}))
        schemas=[]; events=[]

        def tool(name,args):
            return {'choices':[{'message':{'tool_calls':[{'id':name+str(len(schemas)),'type':'function',
                'function':{'name':name,'arguments':json.dumps(args)}}]}}]}

        def api(url,payload,timeout=600):
            if 'tools' not in payload:
                if payload['messages'][0]['content'].startswith('Report'):
                    return {'choices':[{'message':{'content':'blocked'}}]}
                # Reflection must receive the cached targeted evidence.
                user_json=payload['messages'][1]['content']
                self.assertIn('cached_targeted_evidence',user_json)
                self.assertIn('value = 1',user_json)
                return {'choices':[{'message':{'content':json.dumps({
                    'hypothesis':'need one exact changed-file fact','target_file':'','expected_effect':'',
                    'smallest_edit':'','missing_evidence':'inspect changed line','targeted_reads':['calc.py']})}}]}
            schemas.append({entry['function']['name'] for entry in payload['tools']})
            if len(schemas)==1:
                self.assertNotIn('update_working_state',schemas[-1])
                return tool('read_file',{'path':'calc.py','start_line':1,'end_line':20})
            return tool('finish_task',{'report':'blocked after cached read rejection'})

        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=api), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',return_value=(1,'actual 1 expected 2')):
            result=m.agent(self.project,'ignored',str(checkpoint))
            self.assertEqual(result,2)

        events_text=next((m.STATE/'sessions').glob('*/events.jsonl')).read_text()
        self.assertIn('cached read already supplied',events_text)

    def test_post_edit_failure_classifies_unchanged_target(self):
        self.project.cfg['test']='registered-failing-test'
        (self.root/'calc.py').write_text('value = 1\n')
        old_backup=self.root/'old-backup'; old_backup.mkdir(); (old_backup/'calc.py').write_text('value = 0\n')
        digest=hashlib.sha256((self.root/'calc.py').read_bytes()).hexdigest()
        checkpoint=self.root/'unchanged.json'
        failure='exit=1\nAssertionError: actual Saved expected Ready'
        checkpoint.write_text(json.dumps({'root':str(self.root),'branch':'development','project':'fixture',
            'project_config':self.project.cfg,'objective':'repair','files_modified':['calc.py'],
            'file_hashes':{'calc.py':digest},'backup_root':str(old_backup),
            'hypothesis':'prior','build_status':'exit=0\nbuild ok','test_status':failure,'generation':1,'cache':{}}))
        captured=[]
        def api(url,payload,timeout=600):
            if 'tools' not in payload:
                captured.append(payload['messages'][1]['content'])
                return {'choices':[{'message':{'content':json.dumps({
                    'hypothesis':'still wrong','target_file':'','expected_effect':'','smallest_edit':'',
                    'missing_evidence':'none','targeted_reads':[]})}}]}
            return {'choices':[{'message':{'tool_calls':[{'id':'done','type':'function','function':{'name':'finish_task','arguments':json.dumps({'report':'blocked'})}}]}}]}
        with patch.object(m,'ensure_bonsai'), patch.object(m,'model_id',return_value='Bonsai'), \
             patch.object(m,'get_json',side_effect=api), \
             patch.object(self.project,'build',return_value=(0,'build ok')), \
             patch.object(self.project,'command',return_value=(1,'AssertionError: actual Saved expected Ready')):
            self.assertEqual(m.agent(self.project,'ignored',str(checkpoint)),2)
        self.assertTrue(any('UNCHANGED_TARGET_FAILURE' in body for body in captured))

    def test_post_edit_repair_git_diff_cannot_repeat_before_followup_edit(self):
        runtime = Path(__file__).with_name("engineer_runtime.py").read_text()
        self.assertIn("POST_EDIT_REPAIR git_diff already supplied as repair evidence", runtime)
        self.assertIn("state['repair_git_diff_used']=0", runtime)
        self.assertIn("state['repair_git_diff_used']=state.get('repair_git_diff_used',0)+1", runtime)

    def test_post_edit_repair_rejected_edit_locks_git_diff_until_successful_edit(self):
        runtime = Path(__file__).with_name("engineer_runtime.py").read_text()
        self.assertIn("state.get('repair_git_diff_used',0)>=1 or state.get('repair_edit_failures',0)>0", runtime)
        self.assertIn("state['repair_edit_failures']=state.get('repair_edit_failures',0)+1", runtime)
        self.assertIn("state['repair_force_reflection']=True", runtime)
        self.assertIn("state['repair_failed_edit']", runtime)
        self.assertIn("state['repair_current_diff']", runtime)
        self.assertIn("Two post-edit repair edits were rejected without a successful change", runtime)
        self.assertIn("names.discard('git_diff')", runtime)
        self.assertIn("failed_edit':state.get('repair_failed_edit',{})", runtime)

if __name__=='__main__': unittest.main()
