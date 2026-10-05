import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from common import ROOT, publisher as p


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.stdout=io.StringIO()
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.files={'README.md':b'synthetic source\n'}
        self.repo='example/runcat-usd-krw'
        self.sha='a'*40

    def tree(self):
        return {'truncated':False,'tree':[{'path':name,'mode':'100644','type':'blob','sha':p.git_blob(raw)} for name,raw in self.files.items()]}

    def test_manifest_matches_and_extra_local_file_not_selected(self):
        data=p.verify_package()
        self.assertEqual(set(data),set(p.PUBLIC_FILES)|{'release-manifest.json'})
        for excluded in ('api-key','usd-krw.json','stderr.log','dev.runcat.fx-usdkrw.plist'):
            self.assertNotIn(excluded,data)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,raw in data.items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            (root/'api-key').write_text('PRIVATE_DO_NOT_PUBLISH')
            self.assertNotIn('api-key',p.verify_package(root))

    def test_changed_file_and_manifest_extra_path_rejected(self):
        data=p.verify_package()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,raw in data.items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            (root/'README.md').write_text('changed')
            with self.assertRaises(p.PublishError):p.verify_package(root)
            manifest=json.loads(data['release-manifest.json']);manifest['sha256']['api-key']='a'*64
            (root/'release-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(p.PublishError):p.verify_package(root)

    def test_paths_symlinks_and_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/'file';target.write_text('source');(root/'link').symlink_to(target)
            for name in ('../file','/etc/passwd','link'):
                with self.subTest(name=name),self.assertRaises(p.PublishError):p.source_bytes(root,name)

    def test_gh_api_uses_fixed_host_input_and_redacts_errors(self):
        result=subprocess.CompletedProcess([],0,'{"ok":true}','')
        with patch.dict(os.environ,GH_DEBUG='api',GIT_TRACE='1',TWELVEDATA_API_KEY='PRIVATE'), \
             patch.object(p.subprocess,'run',return_value=result) as run:
            self.assertEqual(p.api('/bin/gh','user/repos','POST',{'name':'example'}),{'ok':True})
        self.assertIn('--hostname',run.call_args.args[0]);self.assertIn('github.com',run.call_args.args[0])
        self.assertEqual(json.loads(run.call_args.kwargs['input']),{'name':'example'})
        self.assertNotIn('GH_DEBUG',run.call_args.kwargs['env']);self.assertNotIn('TWELVEDATA_API_KEY',run.call_args.kwargs['env'])
        with patch.object(p.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','PRIVATE (HTTP 403)')),self.assertRaises(p.APIError) as err:
            p.api('/bin/gh','user')
        self.assertEqual(err.exception.status,403);self.assertNotIn('PRIVATE',str(err.exception))

    def test_only_404_means_missing_repository(self):
        for code in (401,403,429,500,None):
            with patch.object(p,'api',side_effect=p.APIError(code)),self.assertRaises(p.APIError):p.remote_state('gh',self.repo,self.files)
        with patch.object(p,'api',side_effect=p.APIError(404)):
            self.assertEqual(p.remote_state('gh',self.repo,self.files),('missing',None))

    def test_only_empty_references_mean_empty(self):
        meta={'full_name':self.repo,'fork':False,'archived':False}
        with patch.object(p,'api',side_effect=[meta,[]]):
            self.assertEqual(p.remote_state('gh',self.repo,self.files),('empty',None))
        with patch.object(p,'api',side_effect=[meta,p.APIError(409)]):
            self.assertEqual(p.remote_state('gh',self.repo,self.files),('empty',None))

    def test_identical_remote_is_idempotent_and_different_remote_rejected(self):
        meta={'full_name':self.repo,'fork':False,'archived':False}
        refs=[{'ref':'refs/heads/main','object':{'sha':self.sha}}]
        with patch.object(p,'api',side_effect=[meta,refs,self.tree()]):
            self.assertEqual(p.remote_state('gh',self.repo,self.files),('identical',self.sha))
        bad=self.tree();bad['tree'][0]['sha']='b'*40
        with patch.object(p,'api',side_effect=[meta,refs,bad]),self.assertRaises(p.PublishError):p.remote_state('gh',self.repo,self.files)

    def test_truncated_tree_and_symlink_modes_not_accepted(self):
        meta={'full_name':self.repo};refs=[{'ref':'refs/heads/main','object':{'sha':self.sha}}]
        for tree in ({'truncated':True,'tree':[]},{'truncated':False,'tree':[{'path':'README.md','mode':'120000','type':'blob','sha':p.git_blob(self.files['README.md'])}]}):
            with patch.object(p,'api',side_effect=[meta,refs,tree]),self.assertRaises(p.PublishError):p.remote_state('gh',self.repo,self.files)

    def test_preview_never_creates_or_pushes(self):
        with patch.object(p,'api',return_value={'login':'example','id':123}) as api, \
             patch.object(p,'remote_state',return_value=('missing',None)),patch.object(p,'stage_commit') as stage:
            p.publish('gh','git',self.repo,self.files)
        self.assertEqual(api.call_count,1);stage.assert_not_called()
        self.assertIn('PREVIEW ONLY',self.stdout.getvalue())

    def test_wrong_authenticated_owner_is_not_allowed(self):
        with patch.object(p,'api',return_value={'login':'other','id':123}),self.assertRaises(p.PublishError):
            p.publish('gh','git',self.repo,self.files,push=True,visibility='public')

    def test_creation_requires_explicit_visibility(self):
        with patch.object(p,'api',return_value={'login':'example','id':123}), \
             patch.object(p,'remote_state',return_value=('missing',None)),self.assertRaises(p.PublishError):
            p.publish('gh','git',self.repo,self.files,push=True)

    def test_identical_publication_has_no_write_or_new_commit(self):
        with patch.object(p,'api',return_value={'login':'example','id':123}) as api, \
             patch.object(p,'remote_state',return_value=('identical',self.sha)),patch.object(p,'stage_commit') as stage:
            p.publish('gh','git',self.repo,self.files,push=True,visibility='public')
        stage.assert_not_called();self.assertEqual(api.call_count,1)
        self.assertIn('ALREADY UP TO DATE',self.stdout.getvalue())

    def test_new_publication_creates_only_target_and_never_force_pushes(self):
        with patch.object(p,'api',side_effect=[{'login':'example','id':123},{'full_name':self.repo},{'default_branch':'main'}]) as api, \
             patch.object(p,'remote_state',side_effect=[('missing',None),('identical',self.sha)]), \
             patch.object(p,'stage_commit',return_value=self.sha),patch.object(p,'git_run',return_value='') as git:
            p.publish('gh','git',self.repo,self.files,push=True,visibility='public')
        self.assertEqual(api.call_args_list[1].args[1:3],('user/repos','POST'))
        self.assertEqual(api.call_args_list[1].args[3]['private'],False)
        push=git.call_args_list[-1].args[1]
        self.assertEqual(push,['push','https://github.com/'+self.repo+'.git','HEAD:refs/heads/main'])
        self.assertNotIn('--force',push)
        self.assertIn('PUBLISHED:',self.stdout.getvalue())

    def test_remote_race_stops_before_push(self):
        with patch.object(p,'api',side_effect=[{'login':'example','id':123},{'full_name':self.repo}]), \
             patch.object(p,'remote_state',return_value=('missing',None)),patch.object(p,'stage_commit',return_value=self.sha), \
             patch.object(p,'git_run',return_value='unexpected refs') as git,self.assertRaises(p.PublishError):
            p.publish('gh','git',self.repo,self.files,push=True,visibility='public')
        self.assertEqual(git.call_count,1)

    def test_failed_push_is_not_force_retried(self):
        with patch.object(p,'api',side_effect=[{'login':'example','id':123},{'full_name':self.repo}]), \
             patch.object(p,'remote_state',return_value=('missing',None)),patch.object(p,'stage_commit',return_value=self.sha), \
             patch.object(p,'git_run',side_effect=['',p.PublishError('push failed')]) as git,self.assertRaises(p.PublishError):
            p.publish('gh','git',self.repo,self.files,push=True,visibility='public')
        self.assertEqual(git.call_count,2)

    def checks(self,sha=None,conclusion='success'):
        return {'total_count':4,'check_runs':[{'id':i+1,'name':name,'head_sha':sha or self.sha,'status':'completed',
                 'conclusion':conclusion,'app':{'slug':'github-actions'}} for i,name in enumerate(sorted(p.CI_NAMES))]}

    def test_ci_wait_requires_all_exact_sha_checks(self):
        with patch.object(p,'api',return_value=self.checks()) as api:
            p.wait_ci('gh',self.repo,self.sha,timeout=0)
        self.assertTrue(all(len(c.args)==2 for c in api.call_args_list))  # GET only
        self.assertIn('ALL GITHUB CI CHECKS PASSED',self.stdout.getvalue())

    def test_wrong_sha_skipped_neutral_or_failed_ci_is_not_success(self):
        for data in (self.checks(sha='b'*40),self.checks(conclusion='failure'),self.checks(conclusion='skipped'),self.checks(conclusion='neutral'),{'total_count':0,'check_runs':[]}):
            with patch.object(p,'api',return_value=data),self.assertRaises(p.PublishError):p.wait_ci('gh',self.repo,self.sha,timeout=0)

    def test_pending_then_passing_ci_does_not_mutate_repository(self):
        with patch.object(p,'api',side_effect=[{'total_count':0,'check_runs':[]},self.checks()]) as api,patch.object(p.time,'sleep'):
            p.wait_ci('gh',self.repo,self.sha)
        self.assertEqual(api.call_count,2);self.assertTrue(all(len(c.args)==2 for c in api.call_args_list))

    def test_real_git_commit_contains_only_explicit_source_files(self):
        git=shutil.which('git')
        if not git:self.skipTest('git not installed')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'api-key').write_text('PRIVATE_LOCAL_KEY')
            directory=root/'stage'
            files={'README.md':b'public\n','update-fx.py':b'# code\n'}
            sha=p.stage_commit(files,directory,git,'example',123)
            names=p.git_run(git,['ls-tree','--name-only',sha],directory).splitlines()
            self.assertEqual(set(names),set(files))
            content=p.git_run(git,['show',sha+':README.md'],directory)
            self.assertNotIn('PRIVATE_LOCAL_KEY',content)
            self.assertFalse((directory/'api-key').exists())


if __name__=='__main__':unittest.main()
