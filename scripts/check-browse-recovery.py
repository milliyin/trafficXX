"""User-run Cloud Shell drill: replace exactly one healthy Browse API pod."""
import json
import re
import subprocess
import sys
import time

NAMESPACE = 'trafficxx'
SELECTOR = 'app.kubernetes.io/name=trafficxx-browse'
SUBSCRIPTION = '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85'


class CheckFailed(Exception):
    pass


def command(args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=25).stdout.strip()


def kubectl(*args):
    return command(['kubectl', '--request-timeout=20s', *args])


def deployment():
    return json.loads(kubectl('get', 'deployment', 'browse', '-n', NAMESPACE, '-o', 'json'))


def ready(pod):
    return (not pod['metadata'].get('deletionTimestamp')
            and pod.get('status', {}).get('phase') == 'Running'
            and any(condition.get('type') == 'Ready' and condition.get('status') == 'True'
                    for condition in pod.get('status', {}).get('conditions', [])))


def controller_uid(resource, kind):
    owners = [owner['uid'] for owner in resource['metadata'].get('ownerReferences', [])
              if owner.get('kind') == kind and owner.get('controller') is True]
    return owners[0] if len(owners) == 1 else None


def check_deployment(resource):
    if resource['spec'].get('replicas') != 2:
        raise CheckFailed('EXPECTED_TWO_REPLICAS')
    status = resource.get('status', {})
    if (status.get('observedGeneration') != resource['metadata']['generation']
            or any(status.get(key) != 2 for key in (
                'replicas', 'updatedReplicas', 'readyReplicas', 'availableReplicas'))):
        raise CheckFailed('DEPLOYMENT_NOT_STABLE')
    template = resource['spec']['template']
    images = [container.get('image', '') for container in template['spec']['containers']
              if container['name'] == 'browse']
    if (template['spec'].get('serviceAccountName') != 'browse' or len(images) != 1
            or not re.fullmatch(r'trafficxxacre8708a85\.azurecr\.io/trafficxx@sha256:[0-9a-f]{64}', images[0])):
        raise CheckFailed('UNEXPECTED_BROWSE_CONFIGURATION')


def owned_pods(replica_set_uids):
    items = json.loads(kubectl('get', 'pods', '-n', NAMESPACE, '-l', SELECTOR, '-o', 'json'))['items']
    return [pod for pod in items if controller_uid(pod, 'ReplicaSet') in replica_set_uids]


def run():
    if command(['az', 'account', 'show', '--query', 'id', '-o', 'tsv']) != SUBSCRIPTION:
        raise CheckFailed('WRONG_SUBSCRIPTION')
    if kubectl('config', 'current-context') != 'trafficxx-lab-aks':
        raise CheckFailed('WRONG_KUBERNETES_CONTEXT')
    initial = deployment()
    check_deployment(initial)
    uid = initial['metadata']['uid']
    generation = initial['metadata']['generation']
    replica_sets = json.loads(kubectl('get', 'replicasets', '-n', NAMESPACE, '-l', SELECTOR, '-o', 'json'))['items']
    replica_set_uids = {rs['metadata']['uid'] for rs in replica_sets if controller_uid(rs, 'Deployment') == uid}
    before = owned_pods(replica_set_uids)
    if len(before) != 2 or not all(ready(pod) for pod in before):
        raise CheckFailed('EXPECTED_EXACTLY_TWO_HEALTHY_OWNED_PODS')
    before.sort(key=lambda pod: pod['metadata']['name'])
    victim, survivor = before
    victim_name = victim['metadata']['name']
    victim_uid = victim['metadata']['uid']
    survivor_uid = survivor['metadata']['uid']
    original_uids = {victim_uid, survivor_uid}
    print(f'Deleting one Browse pod normally: {victim_name}', flush=True)
    started = time.monotonic()
    kubectl('delete', 'pod', victim_name, '-n', NAMESPACE, '--wait=false')
    deadline = started + 180
    previous = None
    while time.monotonic() < deadline:
        current = owned_pods(replica_set_uids)
        survivors = [pod for pod in current if pod['metadata']['uid'] == survivor_uid]
        if len(survivors) != 1 or not ready(survivors[0]):
            raise CheckFailed('SURVIVING_POD_NOT_READY')
        replacements = [pod for pod in current if pod['metadata']['uid'] not in original_uids]
        state = (len(current), tuple((pod['metadata']['name'], ready(pod)) for pod in replacements))
        if state != previous:
            print(f'Waiting for replacement: owned_pods={len(current)}, new_ready={sum(ready(p) for p in replacements)}', flush=True)
            previous = state
        if (not any(pod['metadata']['uid'] == victim_uid for pod in current)
                and len(current) == 2 and len(replacements) == 1 and ready(replacements[0])):
            final = deployment()
            if final['metadata']['uid'] != uid or final['metadata']['generation'] != generation:
                raise CheckFailed('DEPLOYMENT_CHANGED_DURING_DRILL')
            try:
                check_deployment(final)
            except CheckFailed as error:
                if str(error) != 'DEPLOYMENT_NOT_STABLE':
                    raise
            else:
                print(json.dumps({'result': 'BROWSE_POD_RECOVERY_OK', 'deleted_pod': victim_name,
                                  'replacement_pod': replacements[0]['metadata']['name'],
                                  'surviving_pod': survivor['metadata']['name'], 'ready_replicas': 2,
                                  'observed_seconds': round(time.monotonic() - started, 1)}))
                return
        time.sleep(2)
    raise CheckFailed('REPLACEMENT_NOT_READY_WITHIN_180_SECONDS')


if __name__ == '__main__':
    try:
        run()
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_POD_RECOVERY_FAILED', 'error_type': type(error).__name__,
                          'reason': str(error) if isinstance(error, CheckFailed) else 'COMMAND_OR_RESPONSE_ERROR'}))
        sys.exit(1)
