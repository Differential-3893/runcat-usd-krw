import contextlib
import copy
from decimal import Decimal as D
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
import urllib.parse
import urllib.request

from common import fx, FIXTURE, NOW, ROOT


class QuoteTests(unittest.TestCase):
    def payload(self, **changes):
        return {**copy.deepcopy(FIXTURE['payload']), **changes}

    def parse(self, **changes):
        return fx.parse_quote(self.payload(**changes), now=NOW)

    def test_exact_symbol_interval_and_provider_percent(self):
        q = self.parse()
        self.assertEqual(q.previous_close, D('1340.12'))
        self.assertEqual(q.percent_change, D('0.27982569'))
        self.assertEqual(q.change_status, 'ok')
        self.assertEqual(fx.build_snapshot(q, now=NOW)['metricsBarValue'], '₩1,344 ▲0.28%')

    def test_arrows_up_down_zero_and_half_up(self):
        for raw, text in [('0.28','▲0.28%'),('-0.28','▼0.28%'),('0.003','0.00%'),('-0.003','0.00%'),
                          ('0.005','▲0.01%'),('-0.005','▼0.01%'),('0','0.00%'),('-0','0.00%')]:
            with self.subTest(value=raw):
                self.assertEqual(fx.change_text(D(raw)), text)
        self.assertEqual(fx.change_text(None), '—')

    def test_wrong_or_missing_currency_pair_and_interval(self):
        for value in ('KRW/USD','EUR/USD',None,'usd/krw',1):
            with self.subTest(symbol=value), self.assertRaises(fx.FXError):
                self.parse(symbol=value)
        with self.assertRaises(fx.FXError):
            self.parse(interval='1min')
        p = self.payload(); p.pop('interval')
        self.assertEqual(fx.parse_quote(p, now=NOW).change_status, 'ok')

    def test_invalid_price_is_not_displayed(self):
        for value in (None,True,False,'NaN','Infinity','-Infinity',float('inf'),float('nan'),
                      0,-1,'','1344KRW',[],{},'0e10000000','1e999999','1e9999999','1e-200','1'*100):
            with self.subTest(value=value), self.assertRaises(fx.FXError):
                self.parse(close=value)

    def test_timestamp_rejects_boolean_fraction_negative_and_future(self):
        for value in (True,False,None,-1,0,1,1.5,str(NOW),NOW+301,float('inf'),10**200):
            with self.subTest(timestamp=value), self.assertRaises(fx.FXError):
                self.parse(timestamp=value)

    def test_daily_bar_open_is_not_the_quote_time(self):
        q = self.parse()
        self.assertEqual(q.timestamp,FIXTURE['payload']['last_quote_at'])
        self.assertEqual(q.bar_timestamp,FIXTURE['payload']['timestamp'])
        self.assertGreater(q.timestamp,q.bar_timestamp+6*3600)
        snap=fx.build_snapshot(q,now=NOW)
        self.assertEqual(len(snap['metrics']),3)
        self.assertEqual(snap['metrics'][2]['formattedValue'],fx.local_quote_time(q.timestamp))
        self.assertNotEqual(snap['metrics'][2]['formattedValue'],fx.local_quote_time(q.bar_timestamp))

    def test_missing_quote_time_does_not_guess_bar_open_is_a_quote(self):
        for absent in (False,True):
            payload=self.payload(last_quote_at=None)
            if absent:payload.pop('last_quote_at')
            q=fx.parse_quote(payload,now=NOW)
            self.assertIsNone(q.timestamp)
            rows={m['title']:m['formattedValue'] for m in fx.build_snapshot(q,now=NOW)['metrics']}
            self.assertEqual(rows['Quote Time'],'—')
            self.assertEqual(rows['Bar Start'],fx.local_quote_time(q.bar_timestamp))
            self.assertEqual(rows['Quote Status'],'Quote time unavailable')
            self.assertEqual(rows['Change'],'▲0.28%')

    def test_malformed_last_quote_at_cannot_bypass_clock_validation(self):
        for value in (True,False,-1,0,1,1.5,str(NOW),NOW+301,float('inf'),10**200):
            with self.subTest(last_quote_at=value),self.assertRaises(fx.FXError):
                self.parse(last_quote_at=value)

    def test_diagnostic_keeps_both_clocks_and_names_the_source(self):
        d=fx.diagnostics(self.parse(),now=NOW)
        self.assertEqual(d['quoteTimeSource'],'last_quote_at')
        self.assertEqual(d['quoteTimestamp'],FIXTURE['payload']['last_quote_at'])
        self.assertEqual(d['barTimestamp'],FIXTURE['payload']['timestamp'])
        self.assertEqual(d['quoteAgeSeconds'],60)
        missing=fx.diagnostics(self.parse(last_quote_at=None),now=NOW)
        self.assertIsNone(missing['quoteAgeSeconds'])
        self.assertIsNone(missing['quoteTimeLocal'])
        self.assertEqual(missing['quoteTimeSource'],'unavailable')

    def test_closed_market_does_not_fabricate_missing_quote_time(self):
        rows={m['title']:m['formattedValue'] for m in
              fx.build_snapshot(self.parse(last_quote_at=None,is_market_open=False),now=NOW)['metrics']}
        self.assertEqual(rows['Quote Time'],'—')
        self.assertEqual(rows['Quote Status'],'Market closed')
        self.assertIn('Bar Start',rows)

    def test_weekend_old_quote_keeps_provider_baseline(self):
        q = self.parse(timestamp=NOW-3*86400, last_quote_at=NOW-2*86400, is_market_open=False)
        snap = fx.build_snapshot(q, now=NOW)
        self.assertEqual(snap['metricsBarValue'], '₩1,344 ▲0.28%')
        self.assertEqual(snap['metrics'][-1]['formattedValue'], 'Market closed')
        self.assertEqual(q.previous_close,D('1340.12'))

    def test_stale_open_or_unknown_market_is_not_inferred_closed(self):
        for flag in (None,True,'false',0):
            with self.subTest(flag=flag):
                snap = fx.build_snapshot(self.parse(last_quote_at=NOW-4000,is_market_open=flag),now=NOW)
                self.assertIn('Older quote',snap['metrics'][-1]['formattedValue'])

    def test_missing_comparison_does_not_become_zero(self):
        for changes in ({'previous_close':None},{'previous_close':0},{'percent_change':None},
                        {'percent_change':True},{'percent_change':'Infinity'}):
            with self.subTest(changes=changes):
                q=self.parse(**changes)
                self.assertIsNone(q.percent_change)
                self.assertEqual(q.change_status,'unavailable')
                self.assertEqual(fx.build_snapshot(q,now=NOW)['metricsBarValue'],'₩1,344 —')

    def test_inconsistent_change_is_not_silently_recomputed(self):
        for percent in ('-0.28','12.00','-100','500000'):
            q=self.parse(percent_change=percent)
            self.assertEqual(q.change_status,'inconsistent')
            self.assertIsNone(q.percent_change)

    def test_numeric_rounding_precision_is_respected(self):
        self.assertTrue(fx.change_consistent(D('1343.87'),D('1340.12'),D('0.28')))
        self.assertFalse(fx.change_consistent(D('1343.87'),D('1340.12'),D('0.50')))
        # Whole-number prices can themselves have been rounded by the provider.
        self.assertTrue(fx.change_consistent(D('1344'),D('1340'),D('0.28')))

    def test_current_price_is_not_derived_from_previous_run(self):
        a=fx.build_snapshot(self.parse(),now=NOW)
        b=fx.build_snapshot(self.parse(),now=NOW+3600)
        self.assertEqual(a['metricsBarValue'],b['metricsBarValue'])
        self.assertNotEqual(a['lastUpdatedDate'],b['lastUpdatedDate'])

    def test_local_time_has_timezone_and_epoch_is_not_relabelled(self):
        old=os.environ.get('TZ')
        try:
            os.environ['TZ']='Asia/Seoul'; time.tzset()
            text=fx.local_quote_time(FIXTURE['payload']['last_quote_at'])
            self.assertTrue(text.endswith('KST'),text)
            os.environ['TZ']='UTC'; time.tzset()
            self.assertTrue(fx.local_quote_time(FIXTURE['payload']['last_quote_at']).endswith('UTC'))
        finally:
            if old is None: os.environ.pop('TZ',None)
            else: os.environ['TZ']=old
            time.tzset()

    def test_diagnostics_and_snapshot_exclude_extra_provider_fields(self):
        p=self.payload(api_key='PRIVATE_SENTINEL', account={'email':'PRIVATE_SENTINEL'},name='PRIVATE_SENTINEL')
        q=fx.parse_quote(p,now=NOW)
        for output in (fx.diagnostics(q,now=NOW),fx.build_snapshot(q,now=NOW)):
            self.assertNotIn('PRIVATE_SENTINEL',json.dumps(output))

    def test_error_payload_does_not_echo_server_message(self):
        for code in (400,401,403,429,500):
            with self.assertRaises(fx.FXError) as err:
                fx.parse_quote({'status':'error','code':code,'message':'PRIVATE_SENTINEL'},now=NOW)
            self.assertNotIn('PRIVATE_SENTINEL',str(err.exception))

    def test_response_size_duplicate_keys_and_invalid_json(self):
        for raw in (b'x'*65537,b'{"symbol":"USD/KRW","symbol":"EUR/USD"}',b'NaN',b'Infinity',b'\xff',b'not json',b'['*2000):
            with self.subTest(raw=raw[:40]),self.assertRaises(fx.FXError):
                fx.decode_payload(raw)
        p=fx.decode_payload(json.dumps(self.payload()).encode())
        self.assertEqual(fx.parse_quote(p,now=NOW).close,D('1343.87'))


class IOTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="fx space ' 한글 ")
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.settings=fx.Settings(self.root,self.root/'api-key',self.root/'usd-krw.json')
        self.settings.key_file.write_text('SYNTHETIC_KEY_ONLY\n')
        self.settings.key_file.chmod(0o600)
        self.stack=contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ,{k:v for k,v in os.environ.items() if k!='TWELVEDATA_API_KEY'},clear=True))

    def test_key_file_and_direct_environment_key(self):
        self.assertEqual(fx.read_api_key(self.settings),'SYNTHETIC_KEY_ONLY')
        with patch.dict(os.environ,TWELVEDATA_API_KEY='SYNTHETIC_ENV_ONLY'):
            self.assertEqual(fx.read_api_key(self.settings),'SYNTHETIC_ENV_ONLY')
        for key in ('', 'line\nbreak', 'space key'):
            with patch.dict(os.environ,TWELVEDATA_API_KEY=key), self.assertRaises(fx.FXError):
                fx.read_api_key(self.settings)

    def test_key_permissions_symlink_and_length_are_checked(self):
        self.settings.key_file.chmod(0o644)
        with self.assertRaises(fx.FXError): fx.read_api_key(self.settings)
        self.settings.key_file.chmod(0o600)
        self.settings.key_file.write_bytes(b'x'*4097)
        with self.assertRaises(fx.FXError): fx.read_api_key(self.settings)
        target=self.root/'target'; target.write_text('SECRET')
        self.settings.key_file.unlink();self.settings.key_file.symlink_to(target)
        with self.assertRaises(fx.FXError): fx.read_api_key(self.settings)

    def test_one_request_uses_header_not_key_in_url_and_explicit_daily_interval(self):
        response=MagicMock(); response.status=200
        response.read.return_value=json.dumps(FIXTURE['payload']).encode()
        response.__enter__.return_value=response
        opener=MagicMock();opener.open.return_value=response
        with patch.object(fx.urllib.request,'build_opener',return_value=opener) as build:
            fx.request_payload(self.settings)
        req=opener.open.call_args.args[0]
        self.assertEqual(opener.open.call_count,1)
        self.assertEqual(req.get_header('Authorization'),'apikey SYNTHETIC_KEY_ONLY')
        self.assertNotIn('SYNTHETIC_KEY_ONLY',req.full_url)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query),
                         {'symbol':['USD/KRW'],'interval':['1day'],'format':['JSON']})
        self.assertEqual(opener.open.call_args.kwargs['timeout'],15)
        response.read.assert_called_once_with(65537)
        self.assertIsInstance(build.call_args.args[0],fx.NoRedirect)

    def test_redirects_are_rejected_before_forwarding(self):
        with self.assertRaises(fx.FXError):
            fx.NoRedirect().redirect_request(None,None,302,'x',{},'https://untrusted.invalid')

    def test_request_errors_are_redacted_and_not_retried(self):
        for error in (OSError('PRIVATE_SENTINEL'),urllib.error.URLError('PRIVATE_SENTINEL'),
                      urllib.error.HTTPError('https://a?apikey=PRIVATE_SENTINEL',429,'PRIVATE_SENTINEL',{},None)):
            opener=MagicMock();opener.open.side_effect=error
            with patch.object(fx.urllib.request,'build_opener',return_value=opener),self.assertRaises(fx.FXError) as err:
                fx.request_payload(self.settings)
            self.assertNotIn('PRIVATE_SENTINEL',str(err.exception))
            self.assertEqual(opener.open.call_count,1)

    def test_only_official_endpoint_and_distinct_key_output_are_allowed(self):
        env={'RUNCAT_HOME':str(self.root),'RUNCAT_OUT_FILE':str(self.settings.out),'RUNCAT_FX_KEY_FILE':str(self.settings.key_file)}
        for base in ('http://api.twelvedata.com/quote','https://example.com/quote','https://api.twelvedata.com/quote?x=y',''):
            with patch.dict(os.environ,{**env,'RUNCAT_FX_API_BASE':base}),self.assertRaises(fx.FXError):
                fx.Settings.from_environment()
        with patch.dict(os.environ,{**env,'RUNCAT_FX_API_BASE':'https://api.twelvedata.com/exchange_rate'}):
            self.assertEqual(fx.Settings.from_environment().out,self.settings.out)
        with patch.dict(os.environ,{**env,'RUNCAT_OUT_FILE':str(self.settings.key_file)}),self.assertRaises(fx.FXError):
            fx.Settings.from_environment()

    def test_atomic_write_failure_preserves_old_file_and_cleans_temp(self):
        self.settings.out.write_text('OLD')
        with patch.object(fx.os,'replace',side_effect=OSError('failure')),self.assertRaises(OSError):
            fx.atomic_write_json(self.settings.out,{'new':True})
        self.assertEqual(self.settings.out.read_text(),'OLD')
        self.assertEqual(set(x.name for x in self.root.iterdir()),{'api-key','usd-krw.json'})

    def test_atomic_write_rejects_nonfinite_json_and_symlink(self):
        self.settings.out.write_text('OLD')
        with self.assertRaises(ValueError):fx.atomic_write_json(self.settings.out,{'bad':float('nan')})
        self.assertEqual(self.settings.out.read_text(),'OLD')
        self.settings.out.unlink(); self.settings.out.symlink_to(self.settings.key_file)
        with self.assertRaises(fx.FXError):fx.atomic_write_json(self.settings.out,{})
        self.assertEqual(self.settings.key_file.read_text(),'SYNTHETIC_KEY_ONLY\n')

    def invoke(self,payload,flags=()):
        stdout,stderr=io.StringIO(),io.StringIO()
        with patch.object(fx.Settings,'from_environment',return_value=self.settings), \
             patch.object(fx,'request_payload',return_value=payload),patch.object(fx.time,'time',return_value=NOW), \
             contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            result=fx.main(list(flags))
        return result,stdout.getvalue(),stderr.getvalue()

    def test_normal_sample_and_diagnose_have_explicit_write_semantics(self):
        rc,out,err=self.invoke(FIXTURE['payload'])
        self.assertEqual((rc,out,err),(0,'',''))
        old=self.settings.out.read_bytes()
        rc,out,err=self.invoke(FIXTURE['payload'],('--diagnose',))
        self.assertEqual(rc,0);self.assertEqual(json.loads(out)['changeStatus'],'ok')
        self.assertEqual(self.settings.out.read_bytes(),old)

    def test_invalid_response_preserves_existing_snapshot_and_timestamp(self):
        self.settings.out.write_text('OLD WITH TIMESTAMP')
        rc,out,err=self.invoke({'status':'error','message':'PRIVATE_SENTINEL'})
        self.assertEqual(rc,1);self.assertNotIn('PRIVATE_SENTINEL',out+err)
        self.assertEqual(self.settings.out.read_text(),'OLD WITH TIMESTAMP')

    def test_missing_change_is_visible_but_strict_install_check_fails(self):
        p={**FIXTURE['payload'],'previous_close':None}
        self.assertEqual(self.invoke(p)[0],0)
        old=self.settings.out.read_bytes()
        self.assertIn('₩1,344 —',old.decode())
        self.assertEqual(self.invoke(p,('--require-change',))[0],1)
        self.assertEqual(old,self.settings.out.read_bytes())

    def test_key_lock_conflict_rejected_and_real_interprocess_lock_releases(self):
        code='''import importlib.util,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('childfx',sys.argv[1]);m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m)
try:
 with m.sample_lock(Path(sys.argv[2])): print('ACQUIRED')
except m.FXError: print('BUSY')
'''
        cmd=[sys.executable,'-B','-c',code,str(ROOT/'update-fx.py'),str(self.settings.out)]
        with fx.sample_lock(self.settings.out):
            result=subprocess.run(cmd,capture_output=True,text=True,timeout=5)
            self.assertEqual(result.stdout.strip(),'BUSY')
        result=subprocess.run(cmd,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.stdout.strip(),'ACQUIRED')

    def test_lock_symlink_does_not_touch_target(self):
        lock=self.settings.out.with_name('.'+self.settings.out.name+'.lock')
        lock.symlink_to(self.settings.key_file)
        with self.assertRaises(OSError):
            with fx.sample_lock(self.settings.out):self.fail('lock followed a symlink')
        self.assertEqual(self.settings.key_file.read_text(),'SYNTHETIC_KEY_ONLY\n')


if __name__=='__main__':unittest.main()
