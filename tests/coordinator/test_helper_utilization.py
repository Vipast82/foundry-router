"""Utilization regressions: actual classifier inputs and bounded lane recovery.
Only fake Kilo/Foundry; no paid calls or production state.
"""
import collections
import json
import pytest
from foundry_coordinator import lanes as L
from tests.coordinator.lane_env import setup, admit, CLAUDE, GPT, answer_helper, tool_part

ONLY = '[lanes]\nenabled=true\ntranscript_poll_seconds=0\n[lanes.failure_triage]\nenabled=true\n'

@pytest.fixture
def env(tmp_path):
    e=setup(tmp_path,lanes=ONLY)
    try: yield e
    finally: e['fake'].stop()

def context(e):
    c=e['c'];p=c.projects['game'];t=c.store.task('game','T2')
    return c,p,t,L.settings(p)

def add(e,key='same',run=None,lane='failure_triage',ls=None):
    c,p,t,settings=context(e)
    if run:t={**t,'run_id':run}
    return c.helpers.lanes.add(p,t,lane,key,{},ls or settings)

@pytest.mark.parametrize('command',[
    'py -3 tools/guard/outline.py tests/GateRunner.lua --grep bootFault | Select-Object -First 20',
    'rg gate.ps1 docs/progress/UI-01_EVIDENCE_RAW.log',
    'Get-Content tests/GateRunner.lua',
])
def test_reading_test_names_is_not_test_execution(command):
    tv=L.tool_view(tool_part(command,'FILE GateRunner.lua\nGREP hits=5',exit_code=1))
    assert not any(x['kind']=='failure' for x in L.classify(tv,'UI-01',L.settings(type('P',(),{'profile':type('F',(),{'raw':{}})()})())))

@pytest.mark.parametrize('tool',['robloxstudio-mcp_eval_server_runtime','robloxstudio-mcp_eval_client_runtime','robloxstudio-mcp_execute_luau'])
def test_runtime_tool_error_triggers_triage(tool):
    tv=L.tool_view(tool_part('', '',status='error',exit_code=None,tool=tool));tv['error']='invalid argument to format'
    events=L.classify(tv,'T2',{'failure_commands':[]})
    assert [x['kind'] for x in events]==['failure']

def test_structured_runtime_failure_and_successful_negative_control():
    tv=L.tool_view(tool_part('',json.dumps({'ok':False,'error':'require argument invalid'}),tool='robloxstudio-mcp_eval_client_runtime',exit_code=None))
    assert any(x['kind']=='failure' for x in L.classify(tv,'T2',{'failure_commands':[]}))
    tv['output']='FAIL injected fault\nPASS negative control detected fault'
    assert not L.classify(tv,'T2',{'failure_commands':[]})

def test_failed_test_piped_to_filter_still_triggers():
    tv=L.tool_view(tool_part('lune run tests/run.luau | Select-String FAIL','FAIL assertion',exit_code=1))
    assert any(x['kind']=='failure' for x in L.classify(tv,'T2',{'failure_commands':['lune']}))

def test_old_runs_do_not_exhaust_current_lane_or_coalesce(env):
    c,p,t,ls=context(env)
    for i in range(4):
        uid,_=add(env,str(i),run='old')
        c.store.x("UPDATE helper_units SET status='failed' WHERE unit_id=?",(uid,))
    uid,how=add(env,'0')
    assert how=='created'
    assert c.store.q('SELECT run_id FROM helper_units WHERE unit_id=?',(uid,))[0]['run_id']==t['run_id']

def test_same_run_recovery_is_bounded_and_history_preserved(env):
    c,*_=context(env);old,_=add(env)
    c.store.x("UPDATE helper_units SET status='failed' WHERE unit_id=?",(old,))
    new,how=add(env);assert how=='created' and new!=old
    c.store.x("UPDATE helper_units SET status='cancelled' WHERE unit_id=?",(new,))
    assert add(env)==(None,'limit')
    assert c.store.q("SELECT status FROM helper_units WHERE unit_id=?",(old,))[0]['status']=='failed'

def test_coalescing_does_not_fabricate_progress(env):
    c,*_=context(env);uid,_=add(env);before=c.store.q('SELECT updated_ts FROM helper_units WHERE unit_id=?',(uid,))[0]['updated_ts']
    env['clock'][0]+=100
    assert add(env)==(uid,'coalesced')
    assert c.store.q('SELECT updated_ts FROM helper_units WHERE unit_id=?',(uid,))[0]['updated_ts']==before

def test_global_task_budget_still_spans_runs(env):
    *_,ls=context(env);ls['max_lane_units_per_task']=1
    assert add(env,'a',run='old',ls=ls)[1]=='created'
    assert add(env,'b',ls=ls)==(None,'limit')

def test_six_helpers_distribute_three_per_backend_and_retain_selective_policy(env):
    c,p,t,ls=context(env)
    p.profile.raw['helpers'].update(max_running=6,backend_max_running={'meridian':3,'cpa':3})
    p.profile.raw['lanes'].update(high_value_lanes=['failure_triage'])
    p.profile.raw['lanes']['failure_triage']['max_per_run']=8
    p.profile.raw['lanes']['failure_triage']['max_running']=6
    env['ff'].admission.update({CLAUDE:admit(),GPT:admit()})
    for i in range(7):add(env,str(i))
    c.tick()
    running=c.helpers.units('game','T2','running')
    assert collections.Counter(u['backend'] for u in running)=={'meridian':3,'cpa':3}
    assert len(c.helpers.units('game','T2','pending'))==1
    normal={**running[0],'value':'normal'}
    assert c.helpers.lanes.candidates(p,normal,c.helpers.settings(p))==[('meridian',CLAUDE)]

def test_review_is_recorded_only_after_valid_result(env):
    c,p,t,ls=context(env)
    p.profile.raw['lanes']['incremental_review']={'enabled':True,'min_interval_seconds':0}
    env['ff'].admission[CLAUDE]=admit()
    (env['root']/'src/menu.luau').write_text('return {updated=true}\n')
    lanes=c.helpers.lanes;ev={'event_key':'manual-review','signature':'head','detail':{}}
    lanes._event(p,t,{'kind':'commit','key':'manual-review','signature':'head','detail':{}},env['primary'])
    c.tick()
    [u]=[u for u in c.helpers.units('game','T2') if u['lane']=='incremental_review']
    assert not c.store.task('game','T2')['data'].get('lanes_reviewed')
    answer_helper(env,u['session_id'],{'assignment':u['unit_id'],'base':u['snapshot_id'],'summary':'Reviewed snapshot','findings':[]})
    c.tick()
    assert c.store.task('game','T2')['data']['lanes_reviewed']['snapshot']==u['snapshot_id']

def test_explicit_recovery_is_paused_idempotent_and_does_not_replay_errors(env):
    c,p,t,ls=context(env)
    p.profile.raw['lanes']['test_design']={'enabled':True}
    with pytest.raises(ValueError):
        c.helpers.lanes.recover_current(p,t,p.adapter.describe('T2'),'repair')
    c.pause('game','T2','maintenance')
    t=c.store.task('game','T2')
    first=c.helpers.lanes.recover_current(p,t,p.adapter.describe('T2'),'model configuration repaired')
    units=c.helpers.units('game','T2')
    assert any(u['lane']=='test_design' and u['status']=='pending' for u in units)
    assert not any(u['lane'] in ('research','failure_triage') for u in units)
    assert not [s for s in c.store.sessions('game','T2') if s['role']=='helper']
    assert c.helpers.lanes.recover_current(p,t,p.adapter.describe('T2'),'same repair')==first
    assert len(c.helpers.units('game','T2'))==len(units)
