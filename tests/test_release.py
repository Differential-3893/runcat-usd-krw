import ast
import json
from pathlib import Path
import re
import unittest

from common import ROOT, fx, manager, publisher


class ReleaseContractTests(unittest.TestCase):
    def test_all_python_sources_parse_at_minimum_version(self):
        for path in ROOT.rglob('*.py'):
            if '.git' not in path.parts:
                with self.subTest(path=path.relative_to(ROOT)):
                    ast.parse(path.read_text(encoding='utf-8'),feature_version=(3,10))

    def test_manifest_is_complete_and_contains_no_runtime_payload(self):
        files=publisher.verify_package()
        actual={str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.git' not in p.parts}
        self.assertTrue(set(files).issubset(actual))
        for name in files:
            self.assertNotIn('api-key',name)
            self.assertFalse(name.endswith('.log'))
            self.assertIsNone(re.search(r'/Users/[A-Za-z0-9._-]+/',files[name].decode('utf-8')))

    def test_actions_are_sha_pinned_and_checkout_does_not_keep_credentials(self):
        workflow=(ROOT/'.github/workflows/ci.yml').read_text()
        uses=re.findall(r'uses:\s*([^\s]+)',workflow)
        self.assertEqual(len(uses),2)
        self.assertTrue(all(re.fullmatch(r'actions/[a-z-]+@[a-f0-9]{40}',x) for x in uses))
        self.assertIn('persist-credentials: false',workflow)
        self.assertIn('contents: read',workflow)
        self.assertNotIn('contents: write',workflow)
        self.assertNotIn('pull_request_target',workflow)
        self.assertIn('timeout-minutes: 10',workflow)
        self.assertIn("['3.10', '3.13']",workflow)
        self.assertIn('test-${{ matrix.os }}-py${{ matrix.python }}',workflow)

    def test_readme_and_data_contract_name_real_fields_and_commands(self):
        readme=(ROOT/'README.md').read_text()
        contract=(ROOT/'docs/DATA_CONTRACT.md').read_text()
        for term in ('Python **3.10+**','interval=1day','previous_close','--require-change','300',
                     'scripts/manage_install.py verify','scripts/manage_install.py diagnose','--push --public --wait-ci'):
            self.assertIn(term,readme)
        for term in ('percent_change','previous_close','Korean','ROUND_HALF_UP','Market closed','timestamp','synthetic'):
            self.assertIn(term,contract)
        self.assertEqual(fx.SYMBOL,'USD/KRW');self.assertEqual(fx.INTERVAL,'1day')
        self.assertEqual(fx.ENDPOINT,'https://api.twelvedata.com/quote')

    def test_local_markdown_links_resolve(self):
        for doc in ROOT.rglob('*.md'):
            for ref in re.findall(r'\]\(([^)]+)\)',doc.read_text()):
                if '://' not in ref and not ref.startswith('#'):
                    path=(doc.parent/ref.split('#')[0])
                    self.assertTrue(path.exists(),f'{doc.name}: {ref}')

    def test_fixture_is_explicitly_synthetic(self):
        fixture=json.loads((ROOT/'tests/fixtures/quote_synthetic.json').read_text())
        self.assertTrue(fixture['synthetic'])
        self.assertNotIn('apikey',json.dumps(fixture))
        self.assertNotIn('Authorization',json.dumps(fixture))

    def test_runtime_sensitive_names_are_ignored(self):
        ignore=(ROOT/'.gitignore').read_text().splitlines()
        for name in ('api-key','.env','*.log','*.lock','usd-krw.json','backups/'):
            self.assertIn(name,ignore)

    def test_no_private_api_key_or_user_plist_is_shipped(self):
        self.assertNotIn('dev.runcat.fx-usdkrw.plist',publisher.PUBLIC_FILES)
        self.assertNotIn('usd-krw.json',publisher.PUBLIC_FILES)
        self.assertNotIn('api-key',publisher.PUBLIC_FILES)
        self.assertEqual(len(manager.LEGACY_SOURCE_SHA256),64)
        # Direct-URL query credentials from the old implementation are not kept.
        source=(ROOT/'update-fx.py').read_text()
        self.assertNotIn('"apikey":',source)
        self.assertIn('"Authorization": "apikey " + key',source)


if __name__=='__main__':unittest.main()
