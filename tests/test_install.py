import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from common import ROOT, fx, manager, module, FIXTURE


class FakeLaunch:
    def __init__(self):
        self.is_loaded=False
        self.is_disabled=False
        self.fail_bootstrap=0
        self.fail_stop=False
        self.calls=[]

    def __call__(self,*args,check=True):
        self.calls.append(args)
        rc,out=0,''
        if args[0]=='print': rc=0 if self.is_loaded else 1
        elif args[0]=='print-disabled':out='"'+manager.LABEL+'" => '+str(self.is_disabled).lower()
        elif args[0]=='bootout':
            if self.fail_stop: rc=1
            else:self.is_loaded=False
        elif args[0]=='bootstrap':
            if self.fail_bootstrap:self.fail_bootstrap-=1;rc=1
            else:self.is_loaded=True
        elif args[0]=='enable':self.is_disabled=False
        elif args[0]=='disable':self.is_disabled=True
        if rc and check:raise manager.SetupError('simulated launch failure')
        return subprocess.CompletedProcess([],rc,out,'')


def fake_run_producer(cfg,script,*flags,env=None):
    env=cfg.environment() if env is None else env
    loaded=module('fx_installed_synthetic',script)
    stdout,stderr=io.StringIO(),io.StringIO()
    payload={**FIXTURE['payload'],'timestamp':int(time.time())//86400*86400,'last_quote_at':int(time.time())}
    with patch.dict(os.environ,env,clear=True),patch.object(loaded,'request_payload',return_value=payload), \
         contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
        rc=loaded.main(list(flags))
    if rc:raise manager.SetupError('synthetic producer failed')
    return subprocess.CompletedProcess([],rc,stdout.getvalue(),stderr.getvalue())


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="fx install space ' 한글 ")
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        env={k:v for k,v in os.environ.items() if not k.startswith(('RUNCAT_','TWELVEDATA_','PYTHON'))}
        env['HOME']=str(self.home)
        self.stack.enter_context(patch.dict(os.environ,env,clear=True))
        self.launch=FakeLaunch()
        self.stack.enter_context(patch.object(manager,'launch',self.launch))
        self.stack.enter_context(patch.object(manager,'run_producer',side_effect=fake_run_producer))
        self.old=b'# synthetic legacy producer\n'
        self.stack.enter_context(patch.object(manager,'LEGACY_SOURCE_SHA256',manager.digest(self.old)))
        self.output=io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))

    def key(self,cfg):
        cfg.key.parent.mkdir(parents=True,exist_ok=True)
        cfg.key.write_text('SYNTHETIC_TEST_KEY\n');cfg.key.chmod(0o600)

    def old_install(self,cfg):
        self.key(cfg)
        cfg.script.write_bytes(self.old);cfg.script.chmod(0o755)
        cfg.plist.parent.mkdir(parents=True,exist_ok=True)
        original={k:v for k,v in cfg.payload().items() if k!='EnvironmentVariables'}
        cfg.plist.write_bytes(plistlib.dumps(original))
        cfg.out.parent.mkdir(parents=True,exist_ok=True)
        cfg.out.write_text('{"previous":"snapshot"}')
        self.launch.is_loaded=True
        return {p:p.read_bytes() for p in cfg.targets()}

    def test_legacy_install_migrates_and_preserves_key_and_paths(self):
        initial=manager.Config();self.old_install(initial)
        cfg=manager.Config();old_key=cfg.key.read_bytes()
        manager.install(cfg)
        self.assertEqual(cfg.key.read_bytes(),old_key)
        self.assertEqual(cfg.out,initial.out)
        self.assertEqual(cfg.python,initial.python)
        self.assertEqual(cfg.script.read_bytes(),(ROOT/'update-fx.py').read_bytes())
        self.assertEqual(manager.read_plist(cfg.plist),cfg.payload())
        self.assertTrue(self.launch.is_loaded)
        self.assertEqual(json.loads(cfg.out.read_text())['metricsBarValue'],'₩1,344 ▲0.28%')
        self.assertIn('ALL LOCAL CHECKS PASSED',self.output.getvalue())

    def test_key_permissions_tightened_without_copying_key_to_backup(self):
        cfg=manager.Config();self.old_install(cfg);cfg.key.chmod(0o644)
        manager.install(manager.Config())
        self.assertEqual(cfg.key.stat().st_mode&0o777,0o600)
        for path in (cfg.home/'backups').rglob('*'):
            if path.is_file():self.assertNotIn('SYNTHETIC_TEST_KEY',path.read_text(errors='replace'))

    def test_preflight_failure_keeps_running_legacy_and_snapshot(self):
        initial=manager.Config();before=self.old_install(initial);cfg=manager.Config()
        with patch.object(manager,'probe',side_effect=manager.SetupError('no permission')),self.assertRaises(manager.SetupError):
            manager.install(cfg)
        self.assertTrue(self.launch.is_loaded)
        self.assertEqual(self.launch.calls,[])
        self.assertEqual({p:p.read_bytes() for p in cfg.targets()},before)
        self.assertFalse((cfg.home/'backups').exists())

    def test_custom_saved_settings_and_python_alias_survive_reinstall(self):
        alias=self.home/'custom python';alias.symlink_to(sys.executable)
        cfg=manager.Config(python=str(alias),data_dir=str(self.home/'custom & home'),
                           output=str(self.home/'custom output/fx.json'),key_file=str(self.home/'secret/fx-api-key'))
        self.key(cfg);manager.install(cfg)
        with patch.dict(os.environ,RUNCAT_OUT_FILE='/unrelated/codex.json',RUNCAT_HOME='/unrelated',TWELVEDATA_API_KEY='IGNORED'):
            saved=manager.Config()
            self.assertEqual(saved.payload(),cfg.payload())
            manager.install(saved)
        self.assertIn('ALREADY INSTALLED',self.output.getvalue())
        self.assertEqual(len(list((cfg.home/'backups').iterdir())),1)

    def test_missing_runtime_does_not_block_uninstall_or_readonly_config(self):
        cfg=manager.Config();self.key(cfg);manager.install(cfg)
        data=cfg.payload();data['ProgramArguments'][0]=str(self.home/'removed python')
        cfg.plist.write_bytes(plistlib.dumps(data))
        readable=manager.Config(require_python=False)
        self.assertEqual(manager.snapshot(readable.out)['title'],'USD / KRW')
        manager.uninstall(readable)
        self.assertFalse(cfg.plist.exists());self.assertTrue(cfg.key.exists())

    def test_child_errors_return_safe_useful_categories(self):
        fresh=module('fx_manager_error_categories',ROOT/'scripts/manage_install.py')
        cfg=manager.Config()
        cases=[('Quote authorization/access failed PRIVATE_SENTINEL','access was denied'),
               ('Daily change is unavailable/inconsistent PRIVATE_SENTINEL','Daily change was missing'),
               ('API quota/rate limit reached PRIVATE_SENTINEL','quota/rate limit'),
               ('PRIVATE_SENTINEL','could not be verified')]
        for raw,message in cases:
            with patch.object(fresh.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'',raw)), \
                 self.assertRaises(fresh.SetupError) as error:
                fresh.run_producer(cfg,cfg.script)
            self.assertIn(message,str(error.exception));self.assertNotIn('PRIVATE_SENTINEL',str(error.exception))

    def test_concurrent_setup_is_rejected_and_lock_released(self):
        with manager.operation_lock():
            with self.assertRaisesRegex(manager.SetupError,'Another FX install'):
                with manager.operation_lock():
                    self.fail('concurrent setup acquired the same lock')
        with manager.operation_lock():
            pass

    def test_setup_lock_symlink_does_not_touch_target(self):
        target=self.home/'keep-key';target.write_text('untouched')
        lock=self.home/'Library/LaunchAgents'/('.'+manager.LABEL+'.setup.lock')
        lock.parent.mkdir(parents=True);lock.symlink_to(target)
        with self.assertRaises(manager.SetupError):
            with manager.operation_lock():
                self.fail('symlink setup lock followed')
        self.assertEqual(target.read_text(),'untouched')

    def test_explicit_override_changes_only_selected_setting(self):
        cfg=manager.Config();self.key(cfg);manager.install(cfg)
        output=self.home/'different output.json'
        new=manager.Config(output=str(output));manager.install(new)
        self.assertEqual(new.python,cfg.python)
        self.assertEqual(new.key,cfg.key)
        self.assertTrue(output.exists());self.assertTrue(cfg.out.exists())
        self.assertEqual(manager.Config().out,output)

    def test_missing_saved_python_is_not_silently_substituted(self):
        cfg=manager.Config();self.key(cfg);manager.install(cfg)
        data=cfg.payload();data['ProgramArguments'][0]=str(self.home/'missing python')
        cfg.plist.write_bytes(plistlib.dumps(data))
        with self.assertRaises(manager.SetupError):manager.Config()
        fixed=manager.Config(python=sys.executable)
        self.assertEqual(fixed.python,sys.executable)

    def test_unknown_source_or_plist_tuning_is_not_overwritten(self):
        cfg=manager.Config();self.old_install(cfg)
        cfg.script.write_text('unknown manual changes')
        with self.assertRaises(manager.SetupError):manager.install(manager.Config())
        self.assertEqual(cfg.script.read_text(),'unknown manual changes')
        data=cfg.payload();data['KeepAlive']=True;cfg.plist.write_bytes(plistlib.dumps(data))
        with self.assertRaises(manager.SetupError):manager.Config()

    def test_empty_explicit_override_is_not_a_reset(self):
        for field in ('python','data_dir','output','key_file'):
            with self.subTest(field=field),self.assertRaises(manager.SetupError):manager.Config(**{field:''})

    def test_unsupported_environment_and_secret_in_plist_rejected(self):
        cfg=manager.Config();self.old_install(cfg)
        data=cfg.payload();data['EnvironmentVariables']['TWELVEDATA_API_KEY']='PRIVATE'
        cfg.plist.write_bytes(plistlib.dumps(data))
        with self.assertRaises(manager.SetupError) as err:manager.Config()
        self.assertNotIn('PRIVATE',str(err.exception))

    def test_builtin_and_custom_other_metrics_are_protected(self):
        with self.assertRaises(manager.SetupError):manager.Config(output=str(self.home/'.codex/runcat-usage.json'))
        other=self.home/'Library/LaunchAgents/dev.runcat.battery-power.plist'
        other.parent.mkdir(parents=True,exist_ok=True)
        out=self.home/'another/custom-battery.json'
        other.write_bytes(plistlib.dumps({'EnvironmentVariables':{'RUNCAT_OUT_FILE':str(out)}}))
        with self.assertRaises(manager.SetupError):manager.Config(output=str(out))

    def test_key_output_log_collisions_and_paths_outside_home_rejected(self):
        for output in (self.home/'.runcat/runcat-fx/api-key',self.home/'.runcat/runcat-fx/stderr.log',Path('/tmp/outside.json')):
            with self.subTest(output=output),self.assertRaises(manager.SetupError):manager.Config(output=str(output))
        with self.assertRaises(manager.SetupError):manager.Config(output=str(self.home/'.runcat/runcat-fx/update-fx.py/sub.json'))

    def test_symlink_target_and_ancestor_rejected(self):
        directory=self.home/'linked';directory.symlink_to(self.home,target_is_directory=True)
        with self.assertRaises(manager.SetupError):manager.Config(output=str(directory/'out.json'))
        cfg=manager.Config();cfg.out.parent.mkdir(parents=True)
        target=self.home/'keep';target.write_text('untouched');cfg.out.symlink_to(target)
        with self.assertRaises(manager.SetupError):manager.Config()
        self.assertEqual(target.read_text(),'untouched')

    def test_bootstrap_failure_restores_files_and_loaded_state(self):
        initial=manager.Config();before=self.old_install(initial);cfg=manager.Config()
        self.launch.fail_bootstrap=1
        with self.assertRaisesRegex(manager.SetupError,'previous target files'):
            manager.install(cfg)
        self.assertTrue(self.launch.is_loaded)
        self.assertEqual({p:p.read_bytes() for p in cfg.targets()},before)

    def test_failed_fresh_install_restores_absence_and_disabled_state(self):
        cfg=manager.Config();self.key(cfg);self.launch.is_disabled=True
        self.launch.fail_bootstrap=1
        with self.assertRaises(manager.SetupError):manager.install(cfg)
        self.assertFalse(self.launch.is_loaded);self.assertTrue(self.launch.is_disabled)
        for p in cfg.targets():self.assertFalse(p.exists())
        self.assertEqual(cfg.key.read_text(),'SYNTHETIC_TEST_KEY\n')

    def test_restore_failure_is_reported_not_claimed_success(self):
        cfg=manager.Config();self.old_install(cfg);cfg=manager.Config()
        self.launch.fail_bootstrap=2
        with self.assertRaisesRegex(manager.SetupError,'RESTORE INCOMPLETE'):manager.install(cfg)
        self.assertNotIn('ALL LOCAL CHECKS PASSED',self.output.getvalue())

    def test_failed_stop_prevents_file_replacement(self):
        initial=manager.Config();before=self.old_install(initial);cfg=manager.Config()
        self.launch.fail_stop=True
        with self.assertRaises(manager.SetupError):manager.install(cfg)
        self.assertEqual({p:p.read_bytes() for p in cfg.targets()},before)

    def test_failed_installed_sample_and_final_verify_restore(self):
        initial=manager.Config();before=self.old_install(initial);cfg=manager.Config()
        with patch.object(manager,'live_sample',side_effect=manager.SetupError('failure')),self.assertRaises(manager.SetupError):
            manager.install(cfg)
        self.assertEqual({p:p.read_bytes() for p in cfg.targets()},before)
        with patch.object(manager,'verify',side_effect=manager.SetupError('failure')),self.assertRaises(manager.SetupError):
            manager.install(cfg)
        self.assertEqual({p:p.read_bytes() for p in cfg.targets()},before)

    def test_snapshot_freshness_and_atomic_replacement_are_checked(self):
        cfg=manager.Config();self.key(cfg);manager.install(cfg)
        with patch.object(manager,'run_producer',return_value=subprocess.CompletedProcess([],0,'','')),self.assertRaisesRegex(manager.SetupError,'atomic'):
            manager.live_sample(cfg)
        data=json.loads(cfg.out.read_text());data['lastUpdatedDate']='2000-01-01T00:00:00Z'
        cfg.out.write_text(json.dumps(data))
        with self.assertRaises(manager.SetupError):manager.snapshot(cfg.out,time.time())

    def test_uninstall_keeps_key_snapshot_logs_and_unrelated_files(self):
        cfg=manager.Config();self.key(cfg);manager.install(cfg)
        old=cfg.out.read_bytes();other=cfg.install/'unrelated.txt';other.write_text('keep')
        cfg.stderr.write_text('old log')
        manager.uninstall(manager.Config())
        self.assertFalse(cfg.script.exists());self.assertFalse(cfg.plist.exists());self.assertFalse(self.launch.is_loaded)
        self.assertEqual(cfg.out.read_bytes(),old);self.assertEqual(other.read_text(),'keep')
        self.assertEqual(cfg.stderr.read_text(),'old log');self.assertTrue(cfg.key.exists())

    def test_manual_rollback_restores_original_installation(self):
        initial=manager.Config();before=self.old_install(initial)
        manager.install(manager.Config())
        backup=next((initial.home/'backups').iterdir())
        manager.rollback(backup)
        self.assertEqual({p:p.read_bytes() for p in initial.targets()},before)
        self.assertTrue(self.launch.is_loaded)

    def test_corrupt_backup_not_used(self):
        cfg=manager.Config();self.old_install(cfg);manager.install(manager.Config())
        backup=next((cfg.home/'backups').iterdir());(backup/'0.bak').write_text('modified')
        with self.assertRaises(manager.SetupError):manager.rollback(backup)
        self.assertEqual(cfg.script.read_bytes(),(ROOT/'update-fx.py').read_bytes())

    def test_live_subprocess_has_explicit_saved_runtime_and_no_environment_key(self):
        actual=module('fx_fresh_manager_for_environment',ROOT/'scripts/manage_install.py')
        cfg=manager.Config()
        with patch.dict(os.environ,TWELVEDATA_API_KEY='PRIVATE',RUNCAT_OUT_FILE='/wrong',PYTHONPATH='/wrong'), \
             patch.object(actual.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            actual.run_producer(cfg,cfg.script)
        env=run.call_args.kwargs['env']
        self.assertNotIn('TWELVEDATA_API_KEY',env);self.assertNotIn('PYTHONPATH',env)
        self.assertEqual(env['RUNCAT_OUT_FILE'],str(cfg.out))
        self.assertEqual(run.call_args.args[0],[cfg.python,str(cfg.script)])


class ShellEntryTests(unittest.TestCase):
    def test_real_shell_entry_does_not_export_bootstrap_as_runtime_python(self):
        # Execute install.sh itself, with a harmless recording bootstrap interpreter.
        with tempfile.TemporaryDirectory(prefix="bootstrap path ' ") as tmp:
            root=Path(tmp);bin=root/'bin';bin.mkdir();capture=root/'capture.json'
            recorder=bin/'python3'
            recorder.write_text('#!/bin/sh\nexec '+__import__('shlex').quote(sys.executable)+' "$RECORD_SCRIPT" "$@"\n')
            recorder.chmod(0o755)
            program=root/'record.py'
            program.write_text("import json,os,sys\nfrom pathlib import Path\nPath(os.environ['CAPTURE']).write_text(json.dumps({'args':sys.argv[1:],'runtime_override':os.environ.get('PYTHON_BIN'),'fx_override':os.environ.get('RUNCAT_FX_PYTHON_BIN')}))\n")
            env={**os.environ,'PATH':str(bin)+os.pathsep+os.environ.get('PATH',''),'RECORD_SCRIPT':str(program),'CAPTURE':str(capture)}
            env.pop('PYTHON_BIN',None);env.pop('RUNCAT_FX_PYTHON_BIN',None)
            result=subprocess.run(['sh',str(ROOT/'install.sh'),'--python','/explicit/runtime'],env=env,capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            data=json.loads(capture.read_text())
            self.assertEqual(data['args'],['-B',str(ROOT/'scripts/manage_install.py'),'install','--python','/explicit/runtime'])
            self.assertIsNone(data['runtime_override']);self.assertIsNone(data['fx_override'])

    def test_shell_syntax(self):
        for path in ('install.sh','uninstall.sh','apply_local.sh'):
            result=subprocess.run(['sh','-n',str(ROOT/path)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
