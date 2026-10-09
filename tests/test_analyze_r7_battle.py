import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('analyze_r7', Path(__file__).resolve().parents[1] / 'use_cases/robot_fight/analyze_r7_battle.py')
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def fight(seed, comp=0, winner=0):
    return {'seed': seed, 'winner': winner, 'winner_node': f'ue{winner}', 'arena_status': 0, 'reason': 'fall', 'rtf': 1.0, 'lockstep': False,
            'brains': [{'robot_id': i, 'policy': 'balance_comp' if i == comp else 'balance',
                        'outcome': 'won' if i == winner else 'lost',
                        'rtt_us': {'p50': 20000+i}, 'link_est': {'cmd_one_way_us': 9000}} for i in (0,1)]}


def save(path, rows):
    (path/'fights').mkdir(parents=True)
    (path/'fights/summary.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))


def test_exclusions_pairing_controls_and_link_metrics(tmp_path):
    a, b = tmp_path/'a', tmp_path/'b'
    good = fight(1)
    invalid = []
    for seed, change in enumerate([{'rtf': 0.9}, {'lockstep': True}, {'arena_error': 'failed'}, {'brains': []}], 10):
        r = fight(seed)
        r.update(change)
        invalid.append(r)
    control = fight(3)
    for brain in control['brains']:
        brain['policy'] = 'balance'
    save(a, [good, fight(2), control, *invalid])
    save(b, [fight(1, comp=1, winner=1), fight(4, comp=1, winner=1)])
    (a/'report').mkdir()
    (a/'report/attach-summary.json').write_text(json.dumps({'status': 'passed', 'per_ue': {'ue0': {'rrc_connected': 1}}}))
    report = mod.analyse({'a': a, 'b': b}, [('baseline','a','b')])
    run = report['runs']['a']
    assert len(run['rejected']) == 4
    assert run['attach_summary']['status'] == 'passed'
    assert run['comparison']['wins']['balance_comp'] == 2
    assert run['comparison']['falls']['balance'] == 2
    assert run['controls']['balance']['fights'] == 1
    assert 'comp_share_of_decided' not in run['controls']['balance']
    assert run['comparison']['metrics_median_of_fights']['balance_comp']['rtt_us.p50'] == {'median': 20000.0, 'n': 2}
    pair = report['pairs']['baseline']
    assert pair['seeds'] == [1]
    assert pair['unpaired_seeds'] == {'a': [2], 'b': [4]}
    assert pair['comparison']['fights'] == 2
    assert pair['comparison']['comp_share_of_decided']['share'] == 1
    assert 0 < pair['comparison']['comp_share_of_decided']['ci95'][0] < 1
    assert 'pair:baseline' in mod.markdown(report)


def test_per_fight_fallback_and_malformed_summary(tmp_path):
    folder = tmp_path/'local/fight-000'
    folder.mkdir(parents=True)
    row = fight(7)
    brains = row.pop('brains')
    (folder/'arena.json').write_text(json.dumps(row))
    for i, brain in enumerate(brains):
        (folder/f'brain{i}.json').write_text(json.dumps(brain))
    assert mod.analyse({'local': folder.parent})['runs']['local']['valid'] == 1
    (folder.parent/'summary.jsonl').write_text('{broken\n')
    assert mod.analyse({'local': folder.parent})['runs']['local']['rejected'][0]['reason'] == 'error'


def test_interrupted_directory_and_remote_summary_paths(tmp_path):
    row = fight(1)
    row['dir'] = '/remote/run/fights/f000'
    save(tmp_path, [row])
    (tmp_path/'fights/f000').mkdir()
    interrupted = tmp_path/'fights/f001'
    interrupted.mkdir()
    (interrupted/'arena.json').write_text(json.dumps(fight(2)))
    report = mod.analyse({'run': tmp_path})['runs']['run']
    assert report['valid'] == 1
    assert len(report['rejected']) == 1
    assert report['rejected'][0]['dir'] == str(interrupted)
    assert 'unreported' in report['rejected'][0]['detail']


def test_f_number_fallback_and_control_side_wins(tmp_path):
    directory = tmp_path/'f002'
    directory.mkdir()
    row = fight(4, winner=1)
    for b in row['brains']:
        b['policy'] = 'balance'
    (directory/'fight.json').write_text(json.dumps(row))
    control = mod.analyse({'run': tmp_path})['runs']['run']['controls']['balance']
    assert control['side_wins'] == {'ue0': 0, 'ue1': 1}


def test_invalid_rtf_bounds():
    import pytest
    for low, high in [(float('nan'), 1.02), (0.98, float('inf')), (1.02, 0.98), (-1, 1)]:
        with pytest.raises(ValueError, match='RTF bounds'):
            mod.analyse({}, rtf_min=low, rtf_max=high)
