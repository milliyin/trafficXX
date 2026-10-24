"""Read-only capacity observation, redaction and failure timing boundaries."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sql = load('check-browse-db-capacity')
runner = load('inspect-browse-capacity')


def plan():
    return [{'Planning Time': .2, 'Execution Time': .3,
             'Plan': {'Actual Rows': 1, 'Actual Loops': 1, 'Shared Hit Blocks': 10,
                      'Output': ['PRIVATE SQL TEXT'], 'Filter': 'PRIVATE DATA'}}]


class Capacity(unittest.TestCase):
    def test_sql_is_read_only_bounded_and_never_outputs_raw_plans(self):
        factory, conn = MagicMock(), MagicMock()
        factory.return_value.__enter__.return_value = conn
        rows = iter([None, None, None,
                     {'identity': 'trafficxx-lab-browse', 'database': 'trafficxx',
                      'read_only': 'on', 'ssl': True, 'tls_version': 'TLSv1.3'},
                     {'max_connections': 100, 'superuser_reserved_connections': 3, 'reserved_connections': 0},
                     {'visible_server_sessions': 20, 'database_sessions': 15, 'browse_sessions': 9,
                      'hidden_database_states': 4},
                     *[{'n': 1} for _ in range(6)], *[{'QUERY PLAN': plan()} for _ in range(2)]])
        def execute(statement, *params):
            result = MagicMock()
            result.fetchone.return_value = next(rows)
            return result
        conn.execute.side_effect = execute
        result = sql.run(factory)
        self.assertEqual(result['result'], 'BROWSE_DB_CAPACITY_SQL_OK')
        self.assertTrue(result['transaction_read_only'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        statements = [call.args[0] for call in conn.execute.call_args_list]
        self.assertEqual(statements[0], 'SET TRANSACTION READ ONLY')
        self.assertIn("SET LOCAL statement_timeout = '2000ms'", statements)
        explains = [call for call in conn.execute.call_args_list if call.args[0].startswith('EXPLAIN')]
        self.assertEqual(len(explains), 2)
        self.assertEqual([call.args[1] for call in explains], [('demo-general',), ('demo-seated',)])
        self.assertEqual(statements.count('SELECT 1 AS n'), 6)
        factory.return_value.__exit__.assert_called_once()

    def test_plan_redaction_and_nonfinite_values(self):
        self.assertNotIn('PRIVATE', json.dumps(sql.plan_summary(plan())))
        bad = plan()
        bad[0]['Execution Time'] = float('nan')
        with self.assertRaises(ValueError):
            sql.plan_summary(bad)

    def test_metric_null_is_missing_not_zero_and_unknown_metrics_fail(self):
        data = {'value': [{'name': {'value': name}, 'timeseries': [{'data': [
            {'timeStamp': 't1', 'average': 3, 'maximum': 8}, {'timeStamp': 't2'}]}]}
            for name in ('cpu_percent', 'memory_percent', 'active_connections')]}
        result = runner.summarize_metrics(data)
        self.assertEqual(result['cpu_percent'], {'observations': 1, 'window_maximum': 8})
        data['value'][0]['timeseries'][0]['data'] = [{'timeStamp': 't2'}]
        self.assertEqual(runner.summarize_metrics(data)['cpu_percent'],
                         {'observations': 0, 'window_maximum': None})
        data['value'][0]['name']['value'] = 'PRIVATE'
        with self.assertRaises(ValueError):
            runner.summarize_metrics(data)

    def scenario(self, fault=None):
        commands, kube_calls, snapshots = [], [], [0]
        before = {'generation': 1, 'pods': [{'pod': 'browse-a'}, {'pod': 'browse-b'}]}
        def command(args):
            commands.append(args)
            if args[:3] == ['az', 'account', 'show']:
                return 'wrong' if fault == 'context' else runner.SUBSCRIPTION
            if args[:4] == ['az', 'postgres', 'flexible-server', 'show']:
                return json.dumps({'location': 'centralus', 'sku': 'example', 'tier': 'GeneralPurpose'})
            if args[:3] == ['az', 'aks', 'show']:
                return 'eastus'
            if args[:4] == ['az', 'monitor', 'metrics', 'list']:
                return json.dumps({'value': [] if fault == 'metrics' else [
                    {'name': {'value': name}, 'timeseries': []}
                    for name in ('cpu_percent', 'memory_percent', 'active_connections')]})
            self.fail('Unexpected command')
        def kube(*args, **kwargs):
            kube_calls.append((args, kwargs))
            if args == ('config', 'current-context'):
                return 'trafficxx-lab-aks'
            if args[:2] == ('get', 'jobs'):
                return json.dumps({'items': [{'metadata': {'name': 'browse-users-250-singlequery'},
                    'status': {} if fault == 'active' else {'conditions': [{'type': 'Failed', 'status': 'True'}]}}]})
            if args[0] == 'exec':
                self.assertEqual(args[-3:], ('--', 'python', '-'))
                self.assertIn('SET TRANSACTION READ ONLY', kwargs['data'])
                return json.dumps({'result': 'BROWSE_DB_CAPACITY_SQL_OK',
                                   'transaction_read_only': True, 'inventory_writes': False})
            self.fail('Unexpected mutation')
        def snapshot(*args):
            snapshots[0] += 1
            return dict(before, generation=2) if fault == 'rollout' and snapshots[0] > 2 else before
        output = io.StringIO()
        with patch.object(runner.base, 'command', side_effect=command), \
                patch.object(runner.base, 'kube', side_effect=kube), \
                patch.object(runner.base, 'snapshot', side_effect=snapshot), redirect_stdout(output):
            code = runner.execute()
        return code, output.getvalue(), commands, kube_calls

    def test_runner_only_reads_metadata_metrics_and_executes_one_probe_at_a_time(self):
        code, output, commands, kube = self.scenario()
        self.assertEqual(code, 0, output)
        self.assertIn('BROWSE_CAPACITY_OBSERVATION_OK', output)
        self.assertEqual(sum(args[0] == 'exec' for args, _ in kube), 2)
        self.assertFalse(any(args[0] in ('patch', 'apply', 'create', 'delete', 'scale') for args, _ in kube))
        for fault in ('context', 'active', 'metrics', 'rollout'):
            code, output, commands, kube = self.scenario(fault)
            self.assertEqual(code, 1, output)
            self.assertNotIn('BROWSE_CAPACITY_OBSERVATION_OK', output)
            if fault != 'rollout':
                self.assertFalse(any(args[0] == 'exec' for args, _ in kube))
