import pathlib, subprocess, sys
root=pathlib.Path.cwd().resolve()
windows_root=subprocess.check_output(['wslpath','-w',str(root)],text=True).strip()
script=pathlib.Path(__file__).with_suffix('.cjs')
windows_script=subprocess.check_output(['wslpath','-w',str(script)],text=True).strip()
result=subprocess.run(['/mnt/c/Program Files/nodejs/node.exe',windows_script,windows_root,sys.argv[1]])
raise SystemExit(result.returncode)
