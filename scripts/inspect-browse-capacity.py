"""Cloud Shell: Azure metadata/metrics plus sequential read-only pod SQL probes."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION = '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85'
GROUP = 'trafficxx-lab-rg'
SERVER = 'trafficxx-pg-e8708a85'
RESOURCE = '/subscriptions/' + SUBSCRIPTION + '/resourceGroups/' + GROUP + '/providers/Microsoft.DBforPostgreSQL/flexibleServers/' + SERVER


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load('run-read-load')


def require(value, code):
    if not value:
        raise ValueError(code)


def summarize_metrics(payload):
    summaries = {}
    for row in payload['value']:
        name = row['name']['value']
        require(name in ('cpu_percent', 'memory_percent', 'active_connections') and name not in summaries,
                'UNEXPECTED_METRIC_SERIES')
        points = [point for series in row['timeseries'] for point in series['data']]
        values = [point[key] for point in points for key in ('average', 'maximum')
                  if point.get(key) is not None]
        require(all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in values),
                'INVALID_METRIC_VALUE')
        summaries[name] = {'observations': len({point['timeStamp'] for point in points
                                              if point.get('average') is not None or point.get('maximum') is not None}),
                           'window_maximum': max(values) if values else None}
    require(set(summaries) == {'cpu_percent', 'memory_percent', 'active_connections'}, 'METRICS_MISSING')
    return summaries


def execute():
    stage = 'context'
    try:
        require(base.command(['az', 'account', 'show', '--query', 'id', '-o', 'tsv']) == SUBSCRIPTION,
                'WRONG_SUBSCRIPTION')
        require(base.kube('config', 'current-context') == 'trafficxx-lab-aks', 'WRONG_CONTEXT')
        stage = 'active_load_guard'
        jobs = json.loads(base.kube('get', 'jobs', '-n', 'trafficxx', '-o', 'json'))['items']
        require(not any((row['metadata']['name'].startswith('browse-users-')
                         or row['metadata']['name'] == 'read-load-baseline')
                        and not any(c['type'] in ('Complete', 'Failed') and c['status'] == 'True'
                                    for c in row.get('status', {}).get('conditions', [])) for row in jobs),
                'OTHER_READ_LOAD_ACTIVE')
        flow = load('check-browse-concurrency')
        before = {api: base.snapshot(flow, api) for api in ('browse', 'booking')}
        stage = 'azure_metadata'
        server = json.loads(base.command(['az', 'postgres', 'flexible-server', 'show', '--resource-group', GROUP,
            '--name', SERVER, '--query', '{location:location,sku:sku.name,tier:sku.tier,version:version,state:state}', '-o', 'json']))
        aks_location = base.command(['az', 'aks', 'show', '--resource-group', GROUP, '--name', 'trafficxx-lab-aks',
                                     '--query', 'location', '-o', 'tsv'])
        print(json.dumps({'result': 'BROWSE_CAPACITY_AZURE_METADATA', 'postgres': server,
                          'aks_location': aks_location}), flush=True)
        stage = 'azure_recent_metrics'
        end = datetime.now(timezone.utc).replace(microsecond=0)
        start = end - timedelta(minutes=30)
        data = json.loads(base.command(['az', 'monitor', 'metrics', 'list', '--resource', RESOURCE,
            '--metric', 'cpu_percent', 'memory_percent', 'active_connections', '--aggregation', 'Average', 'Maximum',
            '--interval', 'PT1M', '--start-time', start.isoformat(), '--end-time', end.isoformat(), '-o', 'json']))
        print(json.dumps({'result': 'BROWSE_CAPACITY_RECENT_METRICS', 'start_utc': start.isoformat(),
                          'end_utc': end.isoformat(), 'interval': 'PT1M', 'metrics': summarize_metrics(data),
                          'note': 'Recent window, not exact failed-run alignment. Minute aggregation can hide subsecond pressure. '
                                  'active_connections includes idle sessions; absent samples are not zero.'}), flush=True)
        stage = 'read_only_sql'
        source = (ROOT / 'scripts/check-browse-db-capacity.py').read_text()
        for pod in before['browse']['pods']:
            result = json.loads(base.kube('exec', '-i', '-n', 'trafficxx', 'pod/' + pod['pod'],
                                          '-c', 'browse', '--', 'python', '-', data=source))
            require(result.get('result') == 'BROWSE_DB_CAPACITY_SQL_OK'
                    and result.get('transaction_read_only') is True and result.get('inventory_writes') is False,
                    'SQL_PROBE_DID_NOT_PASS')
            print(json.dumps({'pod': pod['pod'], **result}), flush=True)
        stage = 'pod_stability'
        require({api: base.snapshot(flow, api) for api in before} == before, 'SERVING_PODS_CHANGED')
        print(json.dumps({'result': 'BROWSE_CAPACITY_OBSERVATION_OK', 'serving_pods_unchanged': True,
                          'inventory_writes': False, 'additional_connections_max': 1,
                          'note': 'Read-only baseline; no new load, grants, configuration or capacity changes.'}))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_CAPACITY_OBSERVATION_FAILED', 'stage': stage,
                          'error_type': type(error).__name__,
                          'check': str(error) if type(error) is ValueError else None,
                          'command_exit_code': getattr(error, 'returncode', None), 'inventory_writes': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(execute())
