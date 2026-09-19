"""Run real Bonsai acceptance tasks in disposable repositories; no commits/pushes."""
import argparse
import json
import pathlib
import subprocess
import tempfile
import time
import local_engineer as m


def run(level):
    root=pathlib.Path(tempfile.mkdtemp(prefix='local-engineer-L%d-'%level))
    subprocess.run(['git','init','-b','benchmark',str(root)],check=True,capture_output=True)
    (root/'AGENTS.md').write_text('Preserve notes.txt. Use exact edits. Run the documented verification. Do not commit.\n')
    (root/'README.md').write_text('Build and test: python3 -m unittest discover -v\nImplementation: arithmetic.py\n')
    (root/'notes.txt').write_text('User notes must survive unchanged.\n')
    (root/'arithmetic.py').write_text('def add(a, b):\n    return a - b\n')
    (root/'test_arithmetic.py').write_text('import unittest\nfrom arithmetic import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 3), 5)\n')
    tasks={1:'このrepoのbuild方法を調べて。編集は不要。',
           2:'現在のbranchと変更内容を説明して。編集は不要。',
           3:'compiler error: broken.c:1: undeclared identifier missing。原因のファイルとシンボルを調べて。編集は不要。',
           4:'Fix the arithmetic bug and run the tests. Preserve existing files and do not commit.',
           5:'Fix both failing tests: add must add numbers and greet must strip whitespace and use Hello. Inspect the two implementation files, edit them, and build/test. Preserve notes.txt.'}
    if level==3: (root/'broken.c').write_text('int main(void) { return missing; }\n')
    if level==5:
        (root/'greeting.py').write_text('def greet(name):\n    return "Bye " + name\n')
        (root/'test_greeting.py').write_text('import unittest\nfrom greeting import greet\nclass TestGreet(unittest.TestCase):\n    def test_greet(self): self.assertEqual(greet(" Ada "), "Hello Ada")\n')
    immutable={p.name:p.read_bytes() for p in root.iterdir() if p.is_file() and (level<=3 or p.name not in ('arithmetic.py','greeting.py'))}
    cfg={'root':str(root),'build':'python3 -m unittest discover -v','test':'python3 -m unittest discover -v',
         'preferred_branch':'benchmark','protected_branches':['main','master']}
    if level<=3: cfg['task_mode']='inspect'
    p=m.Project('benchmark-L%d'%level,cfg,{})
    before=set((m.STATE/'sessions').glob('*')) if (m.STATE/'sessions').exists() else set()
    start=time.monotonic(); rc=m.agent(p,tasks[level]); elapsed=time.monotonic()-start
    new=set((m.STATE/'sessions').glob('*'))-before
    checkpoint=next(iter(new))/'working_state.json'
    state=json.loads(checkpoint.read_text())
    final=state.get('final_report','')
    tests=subprocess.run(['python3','-m','unittest','discover','-v'],cwd=root,capture_output=True,text=True)
    unchanged=all((root/name).is_file() and (root/name).read_bytes()==data for name,data in immutable.items())
    success=rc==0 and unchanged
    if level==1: success=success and 'python3 -m unittest discover -v' in final
    if level==2: success=success and 'benchmark' in final and ('untracked' in final.lower() or '未追跡' in final)
    if level==3: success=success and 'broken.c' in final and 'missing' in final
    if level>=4:
        oracle='from arithmetic import add; assert all(add(a,b)==a+b for a,b in [(2,3),(-2,8),(0,0),(5,-7)])'
        if level==5: oracle+='; from greeting import greet; assert all(greet(s)=="Hello "+s.strip() for s in [" Ada "," Bob","Eve "])'
        verified=subprocess.run(['python3','-c',oracle],cwd=root,capture_output=True,text=True)
        success=success and tests.returncode==0 and verified.returncode==0
    if level==5: success=success and set(state['files_modified'])>={'arithmetic.py','greeting.py'}
    result={'level':level,'success':success,'agent_exit':rc,'elapsed_s':elapsed,
            'tool_calls':state['tool_calls'],'rounds':state['rounds'],'cache_hits':state['cache_hits'],
            'prompt_tokens':state['prompt_tokens'],'completion_tokens':state['completion_tokens'],
            'checkpoint':str(checkpoint),'root':str(root),'test_exit':tests.returncode,
            'final_report':final}
    destination=m.STATE/'benchmarks'/('level-%d-%d.json'%(level,time.time()))
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return success

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--level',type=int,choices=range(1,6),required=True)
    args=parser.parse_args()
    raise SystemExit(0 if run(args.level) else 1)
