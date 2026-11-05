import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'cluster'))
import ha_policy


class FakeApi:
    def __init__(self):
        self.nodes=[{'id':x,'wireguard':{'ip':'10.80.0.'+str(i+1)}} for i,x in enumerate('ABC')]
        self.config={**ha_policy.OLD_DEFAULT,'failsafe_mode':True,'postgresql':{'parameters':{'max_connections':300}},'synchronous_mode':False}
        self.calls=[];self.bad_voter=None;self.split=False;self.bad_revision=None;self.patch_code=200
    def __call__(self,url,method='GET',data=None,auth=None,timeout=6):
        self.calls.append((url,method,copy.deepcopy(data),auth))
        node=next(n for n in self.nodes if n['wireguard']['ip'] in url);ident=node['id']
        if method=='PATCH':self.config.update(data);return self.patch_code,copy.deepcopy(self.config)
        if url.endswith('/config'):return 200,copy.deepcopy(self.config)
        if url.endswith('/primary'):return (200 if ident=='A' else 503),{}
        if url.endswith('/patroni'):return 200,{'state':'running','role':'primary' if ident=='A' or self.split and ident=='B' else 'replica','patroni':{'name':ident}}
        if url.endswith('/cluster'):return 200,{'members':[{'name':n['id'],'role':'leader' if n['id']=='A' else 'replica'} for n in self.nodes]}
        if url.endswith('/ha/live'):return 200,{'node':ident,'ok':True,'reconciled':True,'revision':410508 if ident==self.bad_revision else 410509}
        if url.endswith('/health'):return (0,{}) if ident==self.bad_voter else (200,{'health':'true'})
        raise AssertionError(url)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.api=FakeApi();self.policy=ha_policy.Policy({'nodes':self.api.nodes},'A',self.temp.name,self.api,lambda seconds:None)
        self.env={'TASKFORGE_INTERNAL_KEY':'k'*64}
    def test_guarded_patch_only_changes_timers(self):
        self.assertEqual(self.policy.apply(self.env),'applied')
        patches=[c for c in self.api.calls if c[1]=='PATCH']
        self.assertEqual(len(patches),1);self.assertEqual(patches[0][2],ha_policy.TARGET)
        self.assertTrue(patches[0][3].startswith('Basic '))
        self.assertTrue(self.api.config['failsafe_mode']);self.assertFalse(self.api.config['synchronous_mode'])
        self.assertEqual(self.api.config['postgresql']['parameters']['max_connections'],300)
        self.assertEqual(sum(c[0].endswith('/health') for c in self.api.calls),6)
    def test_unreachable_voter_blocks_patch(self):
        self.api.bad_voter='B'
        with self.assertRaises(ha_policy.PolicyError):self.policy.apply(self.env)
        self.assertFalse(any(c[1]=='PATCH' for c in self.api.calls))
    def test_multiple_primaries_block_patch(self):
        self.api.split=True
        with self.assertRaises(ha_policy.PolicyError):self.policy.apply(self.env)
        self.assertFalse(any(c[1]=='PATCH' for c in self.api.calls))
    def test_old_standby_must_be_updated_first(self):
        self.api.bad_revision='C'
        with self.assertRaises(ha_policy.PolicyError):self.policy.apply(self.env)
    def test_standby_never_patches_cluster(self):
        self.policy.local=self.api.nodes[1];self.policy.node_id='B'
        self.assertEqual(self.policy.apply(self.env),'standby')
        self.assertFalse(any(c[1]=='PATCH' for c in self.api.calls))
    def test_custom_timing_not_silently_overwritten(self):
        self.api.config.update(ttl=90,retry_timeout=30)
        with self.assertRaises(ha_policy.PolicyError):self.policy.apply(self.env)
        self.assertFalse(any(c[1]=='PATCH' for c in self.api.calls))
    def test_ambiguous_patch_response_verified_without_retry(self):
        self.api.patch_code=0
        self.assertEqual(self.policy.apply(self.env),'applied')
        self.assertEqual(sum(c[1]=='PATCH' for c in self.api.calls),1)
    def test_rerun_is_idempotent(self):
        self.policy.apply(self.env);self.api.calls.clear()
        self.assertEqual(self.policy.apply(self.env),'unchanged')
        self.assertFalse(any(c[1]=='PATCH' for c in self.api.calls))
    def test_constraint_and_backup(self):
        with self.assertRaises(ha_policy.PolicyError):ha_policy.validate({'ttl':30,'loop_wait':10,'retry_timeout':45})
        self.policy.apply(self.env)
        before=json.loads(Path(self.temp.name,'ha-timing-before-v41.5.9.json').read_text())
        self.assertEqual(before['before'],ha_policy.OLD_DEFAULT)

    def test_explicit_rollback_restores_only_saved_timers(self):
        self.policy.apply(self.env)
        self.policy.apply(self.env,restore=True)
        self.assertEqual(ha_policy.timers(self.api.config),ha_policy.OLD_DEFAULT)
        self.assertTrue(self.api.config['failsafe_mode'])
        self.assertEqual(self.api.config['postgresql']['parameters']['max_connections'],300)

    def test_rollback_refuses_intervening_custom_policy(self):
        self.policy.apply(self.env)
        self.api.config['ttl']=150
        self.api.calls.clear()
        with self.assertRaises(ha_policy.PolicyError):self.policy.apply(self.env,restore=True)
        self.assertFalse(any(c[1]=='PATCH' for c in self.api.calls))


if __name__=='__main__':unittest.main(verbosity=2)
