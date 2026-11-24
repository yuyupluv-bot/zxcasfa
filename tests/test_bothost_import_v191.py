"""Offline startup regressions; no external database or Docker daemon."""
import os
import ast
import json
import shutil
import signal
import sys
import time
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import bothost_main as launcher

class BotHostImportV191(unittest.TestCase):
    def test_migration_prefers_baked_project_over_host_common(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); baked=base/'baked'; mounted=base/'mounted'
            for root in (baked,mounted):
                (root/'common').mkdir(parents=True)
                (root/'common'/'__init__.py').write_text('')
            (baked/'common'/'db_migrate.py').write_text(
                "def ensure_schema():\n    print('PROJECT_MIGRATION_SELECTED')\n    return True\n")
            (mounted/'common'/'db_migrate.py').write_text(
                "def ensure_schema():\n    print('WRONG_MIGRATION_SELECTED')\n    return False\n")
            with patch.object(launcher,'ROOT',baked), \
                 patch.dict(os.environ,{'PYTHONPATH':str(mounted),'PYTHONSAFEPATH':'1'}):
                env=launcher.project_environment()
                self.assertEqual(env['PYTHONPATH'].split(os.pathsep)[0],str(baked))
                result=subprocess.run(launcher.migration_command(),cwd=mounted,env=env,
                                      capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('PROJECT_MIGRATION_SELECTED',result.stdout)
            self.assertNotIn('WRONG_MIGRATION_SELECTED',result.stdout)

    def test_missing_migration_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'common').mkdir()
            (root/'common'/'__init__.py').write_text('')
            with patch.object(launcher,'ROOT',root), \
                 patch.dict(os.environ,{'PYTHONPATH':'','PYTHONSAFEPATH':'1'}):
                result=subprocess.run(launcher.migration_command(),cwd=root,env=launcher.project_environment(),
                                      capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('common.db_migrate',result.stderr)

    def test_child_processes_prioritize_project_and_preserve_existing_path(self):
        with patch.dict(os.environ,{'PYTHONPATH':'/host/extra'}):
            for role in ('bot','admin'):
                env=launcher.child_environment(role)
                self.assertEqual(env['PYTHONPATH'].split(os.pathsep),[str(launcher.ROOT),'/host/extra'])
                self.assertEqual(env['AUTO_MIGRATE_ON_START'],'0')

    def test_launcher_starts_both_children_with_project_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); baked=base/'baked'; mounted=base/'mounted'
            baked.mkdir(); mounted.mkdir()
            shutil.copyfile(Path(launcher.__file__), baked/'bothost_main.py')
            for root in (baked,mounted):
                (root/'common').mkdir(); (root/'common'/'__init__.py').write_text('')
            (baked/'common'/'db_migrate.py').write_text("def ensure_schema():\n    print('BAKED_MIGRATION_OK',flush=True)\n    return True\n")
            (mounted/'common'/'db_migrate.py').write_text("def ensure_schema():\n    return False\n")
            for name in ('main.py','admin_main.py'):
                (baked/name).write_text('import time\ntime.sleep(30)\n')
            env=os.environ.copy()
            env.update(PYTHONPATH=str(mounted),PYTHONSAFEPATH='1',
                DATABASE_URL='postgresql://localtest:localtest@localhost/localtest',
                VK_TOKEN='localtest',VK_GROUP_ID='123',SECRET_KEY='a'*40,
                ADMIN_LOGIN='localtest',ADMIN_PASSWORD='localtest')
            proc=subprocess.Popen([sys.executable,str(baked/'bothost_main.py')],cwd=mounted,
                                  env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            try:
                time.sleep(2)
                self.assertIsNone(proc.poll(), 'launcher exited early')
            finally:
                if proc.poll() is None: proc.send_signal(signal.SIGTERM)
                output,_=proc.communicate(timeout=10)
            self.assertEqual(proc.returncode,0,output)
            for marker in ('STARTUP SOURCE OK:', 'BAKED_MIGRATION_OK', 'STARTUP BOTHOST DATABASE OK',
                           'bot started pid=', 'admin started pid='):
                self.assertIn(marker,output)
            self.assertNotIn('localtest',output)

    def test_apk_fallback_uses_baked_downloads(self):
        root=Path(__file__).resolve().parents[1]
        tree=ast.parse((root/'web'/'push_app.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_find_apk_file')
        scope={'Path':Path,'APK_FILE':Path('/nonexistent/custom.apk'),
               '__file__':str(root/'web'/'push_app.py')}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'push_app.py','exec'),scope)
        actual=scope['_find_apk_file']()
        # /app may be populated in another test environment and has priority.
        if not Path('/app/downloads/BEZTAKSY.apk').is_file() and not Path('/app/android-driver-app/app/downloads/BEZTAKSY.apk').is_file():
            self.assertEqual(actual,root/'downloads'/'BEZTAKSY.apk')
        self.assertTrue(actual.is_file())

    def test_actual_build_preflight_accepts_project_and_rejects_missing_file(self):
        docker=(Path(__file__).resolve().parents[1]/'Dockerfile').read_text()
        line=next(line[4:] for line in docker.splitlines() if line.startswith('RUN python -c ') and 'BUILD MIGRATION MODULE OK' in line)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'common').mkdir(); (root/'common'/'__init__.py').write_text('')
            module=root/'common'/'db_migrate.py'; module.write_text('')
            command=line.replace('/opt/vk-taxi',str(root))
            env=os.environ.copy(); env['PYTHONPATH']=str(root)
            result=subprocess.run(command,cwd=root,env=env,shell=True,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('BUILD MIGRATION MODULE OK',result.stdout)
            module.unlink()
            result=subprocess.run(command,cwd=root,env=env,shell=True,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)

    def test_docker_command_is_valid_json_and_python(self):
        docker=(Path(__file__).resolve().parents[1]/'Dockerfile').read_text()
        cmd=json.loads(next(line[4:] for line in docker.splitlines() if line.startswith('CMD ')))
        self.assertEqual(cmd[:2],['python','-c'])
        compile(cmd[2],'<docker-cmd>','exec')
        self.assertIn('/opt/vk-taxi/bothost_main.py',cmd[2])

    def test_docker_uses_baked_code_and_original_persistent_paths(self):
        docker=(Path(__file__).resolve().parents[1]/'Dockerfile').read_text()
        self.assertIn('WORKDIR /opt/vk-taxi',docker)
        self.assertIn('BUILD MIGRATION MODULE OK:',docker)
        self.assertIn('/opt/vk-taxi/bothost_main.py',docker)
        self.assertIn('/app/data/push_sounds',docker)
        self.assertIn('/app/logs',docker)
        self.assertIn("os.setuid(u.pw_uid)",docker)
