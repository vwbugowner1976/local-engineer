#!/usr/bin/env python3
import argparse, base64, datetime as dt, hashlib, json, os, pathlib, re, shlex, subprocess, sys, urllib.request, urllib.error
from zmk_support import (artifact_name, copy_uf2, discover_zmk_project,
                         detect_zmk_version, select_destination)

HOME = pathlib.Path.home()
CFG = pathlib.Path(os.environ.get('LOCAL_ENGINEER_CONFIG', HOME/'.config/local-engineer/projects.json'))
STATE = HOME/'.local/state/local-engineer'
API_BASE = os.environ.get('BONSAI_API_BASE', 'http://127.0.0.1:8080/v1')
MAX_ROUNDS = 20
MAX_OUTPUT = 2500
KEEP_TOOL_ROUNDS = 2
MAX_WORKING_MEMORY = 3200
MAX_REPEAT_CALLS = 2
DISCOVERY_ROUNDS = 6
FORCE_ACTION_ROUND = 10

BLOCKED = [
    r'(^|[;&| ])sudo([ ;&|]|$)', r'\brm\b', r'\bmv\b', r'\bgit\s+push\b', r'\bgit\s+reset\b',
    r'\bgit\s+clean\b', r'\bgit\s+checkout\s+--\b', r'\bgit\s+restore\s+\.\b',
    r'\bshutdown\b', r'\breboot\b', r'\bdiskutil\b', r'\bmkfs\b', r'\bdd\s+if=',
    r'\blaunchctl\b', r'\bscp\b', r'\brsync\b', r'\bcurl\b', r'\bwget\b',
    r'[;&|<>\x60\n]', r'\$\('
]
ALLOWED_PREFIX = (
    'git status', 'git diff', 'git log', 'git branch', 'git rev-parse', 'git show',
    'ls', 'find', 'rg', 'grep', 'sed', 'head', 'tail', 'wc', 'pwd', 'stat',
    'python ', 'python3 ', 'pytest', 'cargo ', 'west ', 'cmake ', 'ninja', 'ctest',
    'make', 'bash ', './', '.venv/', 'sh '
)
SENSITIVE = re.compile(r'(^|/)(\.env($|\.)|.*(secret|token|private[_-]?key|sign[_-]?seed|credentials?).*)', re.I)


def load_cfg():
    if not CFG.exists():
        raise SystemExit(f'Config not found: {CFG}\nRun local-engineer init first.')
    return json.loads(CFG.read_text())


def run_local(cmd, cwd=None, timeout=900):
    p = subprocess.run(cmd, cwd=cwd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout


def clip(s):
    s = s or ''
    if len(s) <= MAX_OUTPUT:
        return s
    return s[:1200] + '\n...<truncated>...\n' + s[-(MAX_OUTPUT-1250):]


def safe_rel(path):
    p = pathlib.PurePosixPath(path)
    if p.is_absolute() or '..' in p.parts:
        raise ValueError('path must be relative and stay inside the project')
    if SENSITIVE.search(path):
        raise ValueError('refusing to read/write a sensitive-looking path')
    return str(p)


class Project:
    def __init__(self, name, cfg, global_cfg):
        self.name = name
        self.cfg = cfg
        self.global_cfg = global_cfg
        self.transport = cfg.get('transport', 'local')
        self.root = os.path.expanduser(cfg['root'])
        self.host = cfg.get('ssh_host') or global_cfg.get('settings', {}).get('ssh_host', 'wsl')
        self.ssh_port = cfg.get('ssh_port') or global_cfg.get('settings', {}).get('ssh_port')
        self.ssh_control_path = cfg.get('ssh_control_path') or global_cfg.get('settings', {}).get('ssh_control_path')
        self.build_cmd = cfg.get('build', 'auto')
        self.backed_up = set()
        self.backup_root = STATE/'backups'/name/dt.datetime.now().strftime('%Y%m%d-%H%M%S')

    def _remote(self, shell_cmd, timeout=900):
        full = f"cd {shlex.quote(self.root)} && {shell_cmd}"
        ssh_cmd = ['ssh']
        if self.ssh_port:
            ssh_cmd += ['-p', str(self.ssh_port)]
        if self.ssh_control_path:
            ssh_cmd += ['-o', f'ControlPath={self.ssh_control_path}']
        ssh_cmd += [self.host, full]
        p = subprocess.run(ssh_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout

    def exec(self, shell_cmd, timeout=900):
        if self.transport == 'ssh':
            return self._remote(shell_cmd, timeout)
        return run_local(shell_cmd, cwd=self.root, timeout=timeout)

    def exists(self):
        if self.transport == 'ssh':
            rc, _ = self._remote('pwd', 20)
            return rc == 0
        return pathlib.Path(self.root).is_dir()

    def test_source_path(self):
        import re
        try:
            parts = shlex.split(str(self.cfg.get('test','')))
        except ValueError:
            return ''
        for i, part in enumerate(parts[:-1]):
            if re.fullmatch(r'python(?:3(?:\\.[0-9]+)?)?|pypy(?:3)?', part):
                candidate = parts[i + 1]
                if candidate.endswith('.py') and not candidate.startswith('-'):
                    return candidate
        return ''

    def read_test_source(self, start=1, end=260):
        path = self.test_source_path()
        if not path:
            return 2, 'registered test source not found'
        parts = pathlib.PurePosixPath(path).parts
        if any(
            part.lower().startswith('.env') or
            re.search(r'(secret|token|private[_-]?key|sign[_-]?seed|credentials?)', part, re.I)
            for part in parts
        ):
            return 126, 'registered test source rejected: sensitive-looking path'
        start = max(1, int(start))
        end = max(start, min(int(end), start + 399))
        if self.transport == 'ssh':
            py = "\n".join((
                "import pathlib, re",
                "p=pathlib.Path(%r)" % path,
                "source=p.read_text(errors='replace'); lines=source.splitlines()",
                "a=%d; b=%d" % (start, end),
                "print('Registered test source: '+str(p))",
                "print('\\n'.join(f'{i+1}: {lines[i]}' for i in range(a-1,min(b,len(lines)))))",
                "if \"with_suffix('.cjs')\" in source or 'with_suffix(\\\".cjs\\\")' in source:",
                "    q=p.with_suffix('.cjs'); resolved=q.resolve()",
                "    sensitive=re.compile(r'(^|/)(\\.env($|\\.)|.*(secret|token|private[_-]?key|sign[_-]?seed|credentials?).*)',re.I)",
                "    parts=resolved.as_posix().split('/')",
                "    print('\\nDelegated test source: '+str(q))",
                "    if any(sensitive.search('/'+part) for part in parts): print('registered sidecar rejected: sensitive-looking path')",
                "    elif resolved.parent!=p.resolve().parent: print('registered sidecar rejected: path escapes wrapper directory')",
                "    elif resolved.is_file(): print('\\n'.join(f'{i+1}: {line}' for i,line in enumerate(resolved.read_text(errors='replace').splitlines()) if a<=i+1<=b))",
                "    else: print('registered sidecar not found')",
            ))
            rc, out = self._remote('python3 -c ' + shlex.quote(py), 60)
        else:
            p = pathlib.Path(path)
            if not p.is_absolute():
                try:
                    relative = safe_rel(path)
                except ValueError as error:
                    return 126, 'registered test source rejected: ' + str(error)
                root = pathlib.Path(self.root).resolve()
                p = (root/relative).resolve()
                if root not in p.parents:
                    return 126, 'registered test source rejected: path escapes project root'
                resolved_parts = pathlib.PurePosixPath(p.as_posix()).parts
                if any(
                    part.lower().startswith('.env') or
                    re.search(r'(secret|token|private[_-]?key|sign[_-]?seed|credentials?)', part, re.I)
                    for part in resolved_parts
                ):
                    return 126, 'registered test source rejected: sensitive-looking path'
            if not p.exists():
                return 2, 'file not found'
            source = p.read_text(errors='replace')
            lines = source.splitlines()
            out = 'Registered test source: '+str(p)+'\\n'+'\\n'.join(
                f'{i+1}: {lines[i]}' for i in range(start-1, min(end, len(lines))))
            if "with_suffix('.cjs')" in source or 'with_suffix(".cjs")' in source:
                sidecar = p.with_suffix('.cjs')
                resolved = sidecar.resolve()
                resolved_parts = pathlib.PurePosixPath(resolved.as_posix()).parts
                sensitive = any(
                    part.lower().startswith('.env') or
                    re.search(r'(secret|token|private[_-]?key|sign[_-]?seed|credentials?)', part, re.I)
                    for part in resolved_parts
                )
                out += '\\nDelegated test source: '+str(sidecar)+'\\n'
                if sensitive:
                    out += 'registered sidecar rejected: sensitive-looking path'
                elif resolved.parent != p.resolve().parent:
                    out += 'registered sidecar rejected: path escapes wrapper directory'
                elif resolved.is_file():
                    sidecar_lines = resolved.read_text(errors='replace').splitlines()
                    out += '\\n'.join(
                        f'{i+1}: {sidecar_lines[i]}' for i in range(start-1, min(end, len(sidecar_lines))))
                else:
                    out += 'registered sidecar not found'
            rc = 0
        return rc, clip(out)

    def read_file(self, path, start=1, end=260):
        path = safe_rel(path)
        start = max(1, int(start)); end = max(start, min(int(end), start+399))
        if self.transport == 'ssh':
            py = (
                "import pathlib; p=pathlib.Path(%r); lines=p.read_text(errors='replace').splitlines(); "
                "a=%d; b=%d; print('\\n'.join(f'{i+1}: {lines[i]}' for i in range(a-1,min(b,len(lines)))))"
            ) % (path, start, end)
            rc, out = self._remote('python3 -c ' + shlex.quote(py), 60)
        else:
            p = pathlib.Path(self.root)/path
            if not p.exists(): return 2, 'file not found'
            lines = p.read_text(errors='replace').splitlines()
            out = '\n'.join(f'{i+1}: {lines[i]}' for i in range(start-1, min(end, len(lines))))
            rc = 0
        return rc, clip(out)

    def _raw_read(self, path):
        path = safe_rel(path)
        if self.transport == 'ssh':
            code = "import pathlib,sys; sys.stdout.write(pathlib.Path(%r).read_text(errors='replace'))" % path
            return self._remote('python3 -c ' + shlex.quote(code), 60)
        p = pathlib.Path(self.root)/path
        if not p.exists(): return 2, ''
        return 0, p.read_text(errors='replace')

    def zmk_version_info(self):
        manifests = {}
        for path in ('config/west.yml', 'west.yml'):
            rc, text = self._raw_read(path)
            if rc == 0:
                manifests[path] = text
        return detect_zmk_version(manifests)

    def zmk_project_info(self):
        files = {}
        for path in ('config/west.yml', 'west.yml', 'build.yaml'):
            rc, text = self._raw_read(path)
            if rc == 0:
                files[path] = text
        info = discover_zmk_project(files, self.root)
        info['project'] = self.cfg.get('artifact_project_name') or self.name
        return info

    def _build_zmk(self, info):
        if info['version'] not in ('v0.3', 'v0.4'):
            return 126, 'ZMK_VERSION_UNKNOWN: cannot select a ZMK build environment'
        if not info['is_zmk']:
            return 126, 'ZMK project requires a west manifest and build.yaml targets'
        try:
            destination = select_destination(info['version'], info['project'])
        except ValueError as error:
            return 126, 'unsafe ZMK project destination: ' + str(error)
        environment = ''
        workspace = ''
        env_script = ''
        if self.transport == 'ssh':
            settings = self.global_cfg.get('settings', {})
            env_roots = self.cfg.get('zmk_workspaces', settings.get('zmk_workspaces', {}))
            env_root = env_roots.get(info['version'],
                                     '/home/stc/zmk-dev/' + info['version']).rstrip('/')
            env_script = env_root + '/env.sh'
            env_rc, env_out = self.exec(
                'test -x %s && test -f %s && test -f %s/zmk/app/CMakeLists.txt' % (
                    shlex.quote(env_root+'/.venv/bin/west'), shlex.quote(env_script),
                    shlex.quote(env_root)), 30)
            if env_rc:
                return 126, 'no usable WSL ZMK %s environment at %s: %s' % (
                    info['version'], env_root, env_out.strip())
            environment = '/bin/bash -c '
            manifest_repo = 'config' if info['version_source'] == 'config/west.yml' else 'manifest'
            workspace = self.root.rstrip('/')
            manifest_path = str(pathlib.PurePosixPath(workspace) / manifest_repo)
            manifest_rc, manifest_text = self._raw_read(manifest_repo + '/west.yml')
            if manifest_rc:
                return manifest_rc, 'cannot read selected west manifest: ' + manifest_repo + '/west.yml'
            manifest_digest = hashlib.sha256(manifest_text.encode()).hexdigest()
            setup = ('cd %s && '
                     'if ! test -f .west/config; then west init -l %s || exit; fi && '
                     'test "$(west config manifest.path)" = %s && '
                     'test "$(readlink -f "$(west config manifest.path)/west.yml")" = '
                     '"$(readlink -f %s/west.yml)" && '
                     'if ! test "$(cat .west/local-engineer-manifest.sha256 2>/dev/null)" = %s; '
                     'then west update && printf %%s %s > .west/local-engineer-manifest.sha256; '
                     'else echo "reusing manifest-verified west modules"; fi') % (
                         shlex.quote(workspace), shlex.quote(manifest_repo),
                         shlex.quote(manifest_repo),
                         shlex.quote(manifest_path), shlex.quote(manifest_digest),
                         shlex.quote(manifest_digest))
            setup_script = 'source %s >/dev/null && %s' % (
                shlex.quote(env_script), setup)
            rc, out = self.exec(environment + shlex.quote(setup_script), 1800)
            if rc:
                return rc, 'west project workspace setup failed at %s using %s environment\n%s' % (
                    workspace, info['version'], out)
        app_rc, app_info = self._raw_read('app/CMakeLists.txt')
        if app_rc not in (0, 2):
            return app_rc, 'cannot safely inspect app/CMakeLists.txt: ' + app_info
        source = 'app' if app_rc == 0 else 'zmk/app'
        board_root = str(pathlib.PurePosixPath(self.root))
        if self.transport == 'local':
            has_project_boards = (pathlib.Path(self.root)/'boards').is_dir()
        else:
            board_rc, board_out = self.exec(
                'test -d ' + shlex.quote(board_root + '/boards'), 30)
            if board_rc not in (0, 1):
                return board_rc, 'cannot safely inspect project boards directory: ' + board_out
            has_project_boards = board_rc == 0
        results = []
        used_names = set()
        used_labels = set()
        for index, target in enumerate(info['targets']):
            label = target.get('artifact-name') or f"target-{index+1}"
            if not re.fullmatch(r'[A-Za-z0-9._-]+', label):
                return 126, 'unsafe build target label in build.yaml'
            if label in used_labels:
                return 126, 'duplicate build target label in build.yaml: ' + label
            used_labels.add(label)
            relative_build_dir = f'build/local-engineer/{label}'
            build_dir = (str(pathlib.PurePosixPath(self.root)/relative_build_dir)
                         if self.transport == 'ssh' else relative_build_dir)
            source_dir = 'zmk/app' if self.transport == 'ssh' else source
            cmd = 'west build -s %s -d %s -b %s' % (
                shlex.quote(source_dir), shlex.quote(build_dir), shlex.quote(target['board']))
            shield = target.get('shield', '').strip()
            snippet = target.get('snippet', '').strip()
            if snippet:
                cmd += ' -S ' + shlex.quote(snippet)
            cmake_options = ['-DSHIELD=' + shield] if shield else []
            if self._raw_read('config/west.yml')[0] == 0:
                cmake_options.append('-DZMK_CONFIG=' + str(pathlib.PurePosixPath(self.root)/'config'))
            if has_project_boards:
                cmake_options.append('-DBOARD_ROOT=' + board_root)
            cmake_args = target.get('cmake-args', '').strip()
            if cmake_args:
                cmake_options.extend(shlex.split(cmake_args))
            if cmake_options:
                cmd += ' -- ' + ' '.join(shlex.quote(arg) for arg in cmake_options)
            if self.transport == 'ssh':
                command = 'source %s >/dev/null && cd %s && %s' % (
                    shlex.quote(env_script), shlex.quote(workspace), cmd)
                cmd = environment + shlex.quote(command)
            rc, out = self.exec(cmd, 1800)
            results.append(f'[{label}] exit={rc}\n{out}')
            if rc:
                return rc, '\n'.join(results)
            config_path = f'{relative_build_dir}/zephyr/.config'
            config_rc, config = self._raw_read(config_path)
            if config_rc:
                return 1, '\n'.join(results) + f'\nmissing generated Zephyr config under {build_dir}'
            if re.search(r'^(?:# )?CONFIG_ZMK_SPLIT(?:=n| is not set)$', config, re.M):
                results.append('UF2 packaging skipped: target is a non-split utility build')
                continue
            if not re.search(r'^CONFIG_ZMK_SPLIT=y$', config, re.M):
                return 1, '\n'.join(results) + '\nCannot determine split role from generated .config'
            if re.search(r'^CONFIG_ZMK_SPLIT_ROLE_CENTRAL=y$', config, re.M):
                role = 'Central'
            elif re.search(r'^(?:# )?CONFIG_ZMK_SPLIT_ROLE_CENTRAL(?:=n| is not set)$', config, re.M):
                role = 'Peripheral'
            else:
                return 1, '\n'.join(results) + '\nCannot determine split role from generated .config'
            artifact_rc, artifact_list = self.exec(
                'find ' + shlex.quote(build_dir) + " -type f -name '*.uf2' -print", 30)
            artifacts = [line.strip() for line in artifact_list.splitlines() if line.strip()]
            if artifact_rc or len(artifacts) != 1:
                return 1, '\n'.join(results) + (
                    f'\nexpected one UF2 under {build_dir}; found {len(artifacts)} UF2 file(s)')
            artifact_path = artifacts[0]
            if pathlib.PurePosixPath(artifact_path).is_absolute():
                try:
                    artifact_path = str(pathlib.PurePosixPath(artifact_path).relative_to(
                        pathlib.PurePosixPath(self.root)))
                except ValueError:
                    return 1, '\n'.join(results) + '\nartifact path is outside the project root'
            try:
                safe_rel(artifact_path)
            except ValueError as error:
                return 1, '\n'.join(results) + '\nrejected artifact path: ' + str(error)
            try:
                stamp = dt.datetime.now()
                filename = artifact_name(info['keyboard'], role, stamp, info['version'])
                while filename in used_names:
                    stamp += dt.timedelta(seconds=1)
                    filename = artifact_name(info['keyboard'], role, stamp, info['version'])
                copied = self._copy_zmk_artifact(artifact_path, destination, filename)
            except (OSError, ValueError) as error:
                return 1, '\n'.join(results) + '\nUF2 copy/verification failed: ' + str(error)
            used_names.add(filename)
            results.append('UF2 verified: %s -> %s (%d bytes, sha256 %s)' % (
                copied['source'], copied['destination'], copied['size'], copied['sha256']))
        return 0, '\n'.join(results)

    def _copy_zmk_artifact(self, source, destination, filename):
        if self.transport == 'local':
            return copy_uf2(str(pathlib.Path(self.root)/source), destination, filename,
                            '/mnt/d/ZMK-Firmware/zmk-dev')
        # Keep the remote operation self-contained: the WSL runtime may predate this
        # source checkout, while copy and verification still run on the artifact host.
        code = '''import hashlib,json,pathlib,shutil
root=pathlib.Path(%r).resolve(strict=True)
src=(root/%r).resolve(strict=True)
destdir=pathlib.Path(%r)
name=%r
allowed=pathlib.Path('/mnt/d/ZMK-Firmware/zmk-dev').resolve()
if root not in src.parents or not src.is_file() or src.suffix.lower()!='.uf2': raise ValueError('invalid source UF2 path')
if pathlib.Path(name).name!=name or not name.endswith('.uf2'): raise ValueError('invalid UF2 filename')
destdir.mkdir(parents=True,exist_ok=True)
destdir=destdir.resolve(strict=True)
if allowed not in destdir.parents: raise ValueError('destination escapes ZMK-Firmware root')
dst=destdir/name
if dst.exists(): raise ValueError('destination file already exists; refusing to overwrite it')
shutil.copy2(src,dst)
size=src.stat().st_size
digest=lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
if size<=0 or dst.stat().st_size!=size: raise ValueError('source/destination UF2 size mismatch')
source_hash,destination_hash=digest(src),digest(dst)
if source_hash!=destination_hash: raise ValueError('source/destination UF2 content mismatch')
print(json.dumps({'source':str(src),'destination':str(dst),'size':size,'sha256':source_hash,'verified':True}))
''' % (self.root, source, destination, filename)
        rc, out = self.exec('python3 -c ' + shlex.quote(code), 120)
        if rc:
            raise OSError(out.strip() or 'remote UF2 copy failed')
        import json
        result = json.loads(out.splitlines()[-1])
        if not result.get('verified'):
            raise OSError('remote UF2 copy verification failed')
        return result

    def _backup(self, path, original):
        if path in self.backed_up: return
        dest = self.backup_root/path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(original)
        self.backed_up.add(path)

    def write_file(self, path, content):
        path = safe_rel(path)
        rc, old = self._raw_read(path)
        self._backup(path, old if rc == 0 else '')
        if self.transport == 'ssh':
            b64 = base64.b64encode(content.encode()).decode()
            code = "import pathlib,base64; p=pathlib.Path(%r); p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(base64.b64decode(%r))" % (path, b64)
            return self._remote('python3 -c ' + shlex.quote(code), 60)
        p = pathlib.Path(self.root)/path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return 0, f'wrote {path} ({len(content)} chars)'

    def replace_text(self, path, old, new, count=1):
        path = safe_rel(path)
        rc, text = self._raw_read(path)
        if rc != 0: return rc, 'file not found'
        n = text.count(old)
        if n == 0: return 3, 'old text not found exactly'
        if count and n < count: return 4, f'old text occurs only {n} time(s)'
        self._backup(path, text)
        updated = text.replace(old, new, count if count else -1)
        return self.write_file(path, updated)

    def search(self, pattern, glob=''):
        args = ['rg', '-n', '--hidden', '--glob', '!build/**', '--glob', '!.git/**']
        if glob: args += ['--glob', glob]
        args += ['--', pattern, '.']
        cmd = ' '.join(shlex.quote(x) for x in args)
        rc, out = self.exec(cmd, 60)
        return rc, clip(out)

    def list_files(self, depth=3):
        depth = max(1, min(int(depth), 6))
        rc, out = self.exec(f"find . -maxdepth {depth} -type f -not -path './.git/*' -not -path './build/*' | sort | head -400", 60)
        return rc, clip(out)

    def safe_command(self, cmd):
        c = cmd.strip()
        if any(re.search(p, c, re.I) for p in BLOCKED):
            return False, 'blocked destructive/network/admin command'
        if not c.startswith(ALLOWED_PREFIX):
            return False, 'command prefix not in Local Engineer allowlist'
        return True, ''

    def command(self, cmd, timeout=900):
        ok, why = self.safe_command(cmd)
        if not ok: return 126, why
        return self.exec(cmd, timeout)

    def build(self, extra=''):
        zmk = self.zmk_project_info()
        if zmk['is_zmk']:
            if extra:
                return 126, 'extra build arguments are not supported for discovered ZMK targets'
            return self._build_zmk(zmk)
        cmd = self.build_cmd
        if cmd == 'auto':
            probe = "if [ -x ./ai-build ]; then echo './ai-build'; elif [ -f ./build_local.sh ]; then echo 'bash build_local.sh'; elif [ -f ./build-local.sh ]; then echo 'bash build-local.sh'; elif [ -f ./Cargo.toml ]; then echo 'cargo build'; elif [ -f ./Makefile ]; then echo 'make'; else exit 2; fi"
            rc, out = self.exec(probe, 30)
            if rc != 0: return rc, 'could not auto-detect build command'
            cmd = out.strip().splitlines()[-1]
        if extra:
            cmd += ' ' + extra
        return self.command(cmd, 1800)


def get_json(url, payload=None, timeout=600):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ensure_bonsai():
    try:
        get_json(API_BASE + '/models', timeout=3)
        return
    except Exception:
        pass
    print('[local-engineer] Bonsai API not ready; switching to Bonsai mode...')
    p = subprocess.run([str(HOME/'bin/llm'), 'bonsai'])
    if p.returncode:
        raise SystemExit('failed to start Bonsai')


def model_id():
    data = get_json(API_BASE + '/models', timeout=10)
    return data['data'][0]['id']


def tool_defs(phase='discovery'):
    def f(name, desc, props, required):
        return {'type':'function','function':{'name':name,'description':desc,'parameters':{'type':'object','properties':props,'required':required}}}
    edit_verify = [
      f('git_status','Show repository status.',{},[]),
      f('git_diff','Show current uncommitted diff.',{},[]),
      f('replace_text','Replace exact text in a file. Prefer this for targeted edits.',{'path':{'type':'string'},'old':{'type':'string'},'new':{'type':'string'},'count':{'type':'integer','minimum':1}},['path','old','new']),
      f('write_file','Create or rewrite a project file. Use mainly for small/new files.',{'path':{'type':'string'},'content':{'type':'string'}},['path','content']),
      f('run_command','Run an allowlisted build/test or narrowly targeted inspection command in the project.',{'command':{'type':'string'},'timeout':{'type':'integer','minimum':1,'maximum':1800}},['command']),
      f('build_project','Run the configured build or build every discovered ZMK target, then discover, copy, and verify generated UF2 artifacts.',{'extra_args':{'type':'string'}},[]),
    ]
    if phase == 'force_action':
        return edit_verify
    common = list(edit_verify)
    common.insert(2, f('inspect_zmk_project','Discover ZMK manifests, version, keyboard name, and build.yaml targets. Unknown revisions remain ZMK_VERSION_UNKNOWN.',{},[]))
    common.insert(2, f('read_file','Read contiguous source lines. Use search match line numbers for start_line; do not always start at 1. Follow the continuation line if output is bounded.',{'path':{'type':'string'},'start_line':{'type':'integer'},'end_line':{'type':'integer'}},['path']))
    if phase == 'post_edit_repair':
        common.insert(3, f('read_test_source','Read the registered test script as verification evidence. Only the script directly registered by the project test command is available.',{'start_line':{'type':'integer'},'end_line':{'type':'integer'}},[]))
    if phase == 'discovery':
        common.insert(2, f('list_files','List project files to a bounded depth.',{'depth':{'type':'integer','minimum':1,'maximum':6}},[]))
        common.insert(3, f('search_text','Search text with ripgrep.',{'pattern':{'type':'string'},'glob':{'type':'string'}},['pattern']))
    return common


def dispatch(project, name, args):
    try:
        if name == 'git_status': rc,out = project.exec('git status --short --branch',60)
        elif name == 'git_diff': rc,out = project.exec('git diff -- .',60)
        elif name == 'inspect_zmk_project':
            info=project.zmk_project_info()
            rc=0
            target_text='\n'.join('target %d: board=%s shield=%s' % (
                index+1, target.get('board',''), target.get('shield',''))
                for index,target in enumerate(info['targets'])) or 'targets: none discovered'
            out=(f'ZMK_VERSION: {info["version"]}\n'
                 f'revision: {info["revision"] or "unknown"}\n'
                 f'source: {info["version_source"] or "unknown"}\n'
                 f'keyboard: {info["keyboard"]}\n{target_text}\n'
                 f'reason: {info["version_reason"] or "explicit supported ZMK revision"}')
        elif name == 'list_files': rc,out = project.list_files(args.get('depth',3))
        elif name == 'search_text': rc,out = project.search(args['pattern'], args.get('glob',''))
        elif name == 'read_file': rc,out = project.read_file(args['path'], args.get('start_line',1), args.get('end_line',260))
        elif name == 'read_test_source': rc,out = project.read_test_source(args.get('start_line',1), args.get('end_line',260))
        elif name == 'replace_text': rc,out = project.replace_text(args['path'], args['old'], args['new'], args.get('count',1))
        elif name == 'write_file': rc,out = project.write_file(args['path'], args['content'])
        elif name == 'run_command': rc,out = project.command(args['command'], args.get('timeout',900))
        elif name == 'build_project': rc,out = project.build(args.get('extra_args',''))
        else: return 'unknown tool'
        return f'exit={rc}\n{clip(out)}'
    except (TypeError, ValueError) as e:
        if name in ('write_file', 'replace_text', 'read_file'):
            return f'exit=126\\n{name} rejected: {e}'
        return 'tool error: ' + repr(e)
    except Exception as e:
        return 'tool error: ' + repr(e)


def agent(project, task, resume=None):
    from engineer_runtime import run_agent
    return run_agent(sys.modules[__name__], project, task, resume)


def doctor(cfg):
    print('=== Local Engineer doctor ===')
    checks = []

    # Report the exact runtime source/commit so stale installed copies are obvious.
    source_dir = pathlib.Path(__file__).resolve().parent
    runtime_path = source_dir / 'engineer_runtime.py'
    try:
        commit = subprocess.run(
            ['git','-C',str(source_dir),'rev-parse','--short','HEAD'],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3
        ).stdout.strip()
    except Exception:
        commit = 'unknown'
    print(f'Runtime source        : {source_dir}')
    print(f'Runtime module        : {runtime_path}')
    print(f'Runtime git commit    : {commit or "unknown"}')

    try:
        data = get_json(API_BASE + '/models', timeout=3)
        model = data.get('data',[{}])[0].get('id','unknown')
        checks.append(('Bonsai API', True, f'127.0.0.1:8080 ({model})'))
    except Exception as e:
        checks.append(('Bonsai API', False, '127.0.0.1:8080'))

    host = os.environ.get('LOCAL_ENGINEER_SSH_HOST', cfg.get('settings',{}).get('ssh_host','wsl'))
    tunnel_ok = False
    try:
        import socket
        with socket.create_connection(('127.0.0.1', 2222), timeout=2):
            tunnel_ok = True
    except Exception:
        tunnel_ok = False
    checks.append(('WSL SSH :2222', tunnel_ok, 'reverse tunnel'))

    try:
        p = subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=3',host,'echo','LOCAL_ENGINEER_SSH_OK'],
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5)
        checks.append((f'SSH host {host}', p.returncode == 0, 'ssh connectivity'))
    except Exception:
        checks.append((f'SSH host {host}', False, 'ssh connectivity'))

    for name, pcfg in cfg.get('projects',{}).items():
        try:
            p = Project(name, pcfg, cfg)
            ok = p.exists()
            checks.append((name, ok, p.root))
        except Exception:
            checks.append((name, False, pcfg.get('root','')))

    for name, ok, detail in checks:
        print(f"{name:20} : {'OK' if ok else 'FAIL'}  {detail}")
    required = checks
    ready = all(ok for _, ok, _ in required)
    print(f"\\nOverall              : {'READY' if ready else 'NOT READY'}")
    return 0 if ready else 1

def discover(cfg):
    host=os.environ.get('LOCAL_ENGINEER_SSH_HOST', cfg.get('settings',{}).get('ssh_host','wsl'))
    roots=['/home/stc/zmk-dev','/home/stc/zmk-sim','/home/stc/rmk-dev']
    expr=' '.join(shlex.quote(r) for r in roots)
    cmd=f"for r in {expr}; do [ -d \"$r\" ] && find \"$r\" -maxdepth 6 -type d -name .git -print; done"
    p=subprocess.run(['ssh',host,cmd],text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=60)
    if p.returncode:
        print(p.stdout); return p.returncode
    repos=[]
    for line in p.stdout.splitlines():
        if line.endswith('/.git'): repos.append(line[:-5])
    for r in sorted(set(repos)):
        print(r)
    return 0


def main():
    ap=argparse.ArgumentParser(prog='local-engineer')
    sub=ap.add_subparsers(dest='cmd',required=True)
    sub.add_parser('status'); sub.add_parser('projects'); sub.add_parser('discover'); sub.add_parser('doctor')
    b=sub.add_parser('build'); b.add_argument('project'); b.add_argument('extra',nargs='*')
    f=sub.add_parser('fix'); f.add_argument('project'); f.add_argument('task',nargs='+')
    i=sub.add_parser('inspect'); i.add_argument('project'); i.add_argument('task',nargs='+')
    sub.add_parser('technocore-bonsai')
    r=sub.add_parser('resume'); r.add_argument('checkpoint')
    a=sub.add_parser('ask'); a.add_argument('task',nargs='+')
    args=ap.parse_args()
    cfg=load_cfg() if CFG.exists() or args.cmd!='resume' else {'settings':{},'projects':{}}
    if args.cmd=='doctor': raise SystemExit(doctor(cfg))
    if args.cmd=='status':
        subprocess.run([str(HOME/'bin/llm'),'status']); return
    if args.cmd=='projects':
        for k,v in cfg['projects'].items(): print(f"{k:20} {v.get('transport','local'):5} {v['root']}")
        return
    if args.cmd=='discover': raise SystemExit(discover(cfg))
    checkpoint=None
    if args.cmd=='resume':
        checkpoint=json.loads(pathlib.Path(args.checkpoint).read_text())
        pname=checkpoint['project']
        cfg['projects'][pname]=checkpoint['project_config']
    elif args.cmd=='ask':
        task=' '.join(args.task)
        candidates=[name for name in cfg['projects'] if name.lower() in task.lower()]
        if not candidates:
            cwd=pathlib.Path.cwd().resolve()
            candidates=[name for name,p in cfg['projects'].items()
                        if p.get('transport','local')=='local' and
                        (cwd==pathlib.Path(p['root']).resolve() or pathlib.Path(p['root']).resolve() in cwd.parents)]
        if len(candidates)!=1: raise SystemExit('Specify exactly one project name or run inside its repo. Projects: '+', '.join(cfg['projects']))
        pname=candidates[0]
    else:
        pname='technocore' if args.cmd=='technocore-bonsai' else args.project
    if pname not in cfg['projects']: raise SystemExit(f'unknown project: {pname}')
    p=Project(pname,cfg['projects'][pname],cfg)
    if args.cmd=='inspect': p.cfg=dict(p.cfg,task_mode='inspect')
    if not p.exists(): raise SystemExit(f'project not reachable: {p.root} ({p.transport})')
    if checkpoint:
        raise SystemExit(agent(p,checkpoint['objective'],args.checkpoint))
    if args.cmd=='build':
        from engineer_runtime import preflight
        preflight(p)
        rc,out=p.build(' '.join(args.extra)); print(out); raise SystemExit(rc)
    if args.cmd=='technocore-bonsai':
        task='''Inspect the current local Technocore implementation and add a managed_bonsai LLM backend alongside the existing managed_mlx backend. Preserve managed_mlx behavior. managed_bonsai must call the local OpenAI-compatible Bonsai API at http://127.0.0.1:8080/v1 and must not spawn mlx_worker.py. Backend selection must honor environment variable TECHNOCORE_LLM_BACKEND, with values managed_bonsai or managed_mlx, while preserving the current default when the variable is absent. Reuse existing prompt, timeout, logging, draft/review/quality-gate behavior. Add or update deterministic tests and concise documentation. Do not touch secrets, signing identity, launch daemon plist files, or network posting safety rules. Run the existing unit tests and any focused new tests. Do not commit.'''
        raise SystemExit(agent(p,task))
    raise SystemExit(agent(p,' '.join(args.task)))

from engineer_runtime import install
install(sys.modules[__name__])

if __name__=='__main__': main()
