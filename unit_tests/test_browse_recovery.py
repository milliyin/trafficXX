"""Offline checks for a single-pod recovery drill's scope and evidence gates."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('browse_recovery', ROOT / 'scripts/check-browse-recovery.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def pod(name, uid, healthy=True, owner='rs'):
    return {'metadata': {'name': name, 'uid': uid, 'ownerReferences': [
        {'kind': 'ReplicaSet', 'uid': owner, 'controller': True}]},
        'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True' if healthy else 'False'}]}}


def deployment():
    return {'metadata': {'uid': 'deployment-uid', 'generation': 1},
            'spec': {'replicas': 2, 'template': {'spec': {'serviceAccountName': 'browse', 'containers': [
                {'name': 'browse', 'image': 'trafficxxacre8708a85.azurecr.io/trafficxx@sha256:' + '0' * 64}]}}},
            'status': {'observedGeneration': 1, 'replicas': 2, 'updatedReplicas': 2,
                       'readyReplicas': 2, 'availableReplicas': 2}}


class RecoveryDrill(unittest.TestCase):
    def exercise(self, initial=None, snapshots=None, final=None, subscription=None, context='trafficxx-lab-aks'):
        initial = initial if initial is not None else deployment()
        final = final if final is not None else copy.deepcopy(initial)
        before = [pod('browse-a', 'old'), pod('browse-b', 'survivor')]
        states = snapshots if snapshots is not None else [before, [pod('browse-b', 'survivor'), pod('browse-c', 'new')]]
        states = iter(states)
        last = before
        deletes, deployment_calls = [], 0

        def kubectl(*args):
            nonlocal last, deployment_calls
            if args == ('config', 'current-context'):
                return context
            if args[:2] == ('get', 'deployment'):
                deployment_calls += 1
                return json.dumps(initial if deployment_calls == 1 else final)
            if args[:2] == ('get', 'replicasets'):
                return json.dumps({'items': [{'metadata': {'uid': 'rs', 'ownerReferences': [
                    {'kind': 'Deployment', 'uid': 'deployment-uid', 'controller': True}]}}]})
            if args[:2] == ('get', 'pods'):
                last = next(states, last)
                return json.dumps({'items': last})
            if args[:2] == ('delete', 'pod'):
                deletes.append(args)
                return 'deleted'
            raise AssertionError(args)

        clock = SimpleNamespace(monotonic=MagicMock(side_effect=iter(range(1000))), sleep=MagicMock())
        output, error = io.StringIO(), None
        with patch.object(module, 'command', return_value=subscription or module.SUBSCRIPTION), \
                patch.object(module, 'kubectl', side_effect=kubectl), patch.object(module, 'time', clock), \
                contextlib.redirect_stdout(output):
            try:
                module.run()
            except module.CheckFailed as caught:
                error = str(caught)
        return deletes, output.getvalue(), error

    def test_deletes_only_one_owned_pod_and_requires_new_uid_ready(self):
        terminating = pod('browse-a', 'old')
        terminating['metadata']['deletionTimestamp'] = 'timestamp'
        snapshots = [[pod('browse-a', 'old'), pod('browse-b', 'survivor')],
                     [terminating, pod('browse-b', 'survivor'), pod('browse-c', 'new', False)],
                     [pod('browse-b', 'survivor'), pod('browse-c', 'new')]]
        deletes, output, error = self.exercise(snapshots=snapshots)
        self.assertIsNone(error)
        self.assertEqual(deletes, [('delete', 'pod', 'browse-a', '-n', 'trafficxx', '--wait=false')])
        result = json.loads(output.splitlines()[-1])
        self.assertEqual(result['result'], 'BROWSE_POD_RECOVERY_OK')
        self.assertEqual(result['replacement_pod'], 'browse-c')

    def test_wrong_subscription_or_context_never_deletes(self):
        for options in ({'subscription': 'other'}, {'context': 'other'}):
            with self.subTest(options=options):
                deletes, _, error = self.exercise(**options)
                self.assertEqual(deletes, [])
                self.assertIn(error, ('WRONG_SUBSCRIPTION', 'WRONG_KUBERNETES_CONTEXT'))

    def test_unhealthy_baseline_never_deletes(self):
        deletes, _, error = self.exercise(snapshots=[[pod('browse-a', 'old', False), pod('browse-b', 'survivor')]])
        self.assertEqual(deletes, [])
        self.assertEqual(error, 'EXPECTED_EXACTLY_TWO_HEALTHY_OWNED_PODS')

    def test_unstable_scaled_or_unexpected_image_deployment_never_deletes(self):
        for change in ('scale', 'unready', 'image'):
            with self.subTest(change=change):
                initial = deployment()
                if change == 'scale':
                    initial['spec']['replicas'] = 1
                elif change == 'unready':
                    initial['status']['readyReplicas'] = 1
                else:
                    initial['spec']['template']['spec']['containers'][0]['image'] = 'other:latest'
                deletes, _, error = self.exercise(initial=initial)
                self.assertEqual(deletes, [])
                self.assertIsNotNone(error)

    def test_foreign_owner_is_not_selected_for_deletion(self):
        deletes, _, error = self.exercise(snapshots=[[pod('browse-a', 'old', owner='foreign'), pod('browse-b', 'survivor')]])
        self.assertEqual(deletes, [])
        self.assertIsNotNone(error)

    def test_survivor_becoming_unready_fails_the_drill(self):
        states = [[pod('browse-a', 'old'), pod('browse-b', 'survivor')],
                  [pod('browse-b', 'survivor', False), pod('browse-c', 'new')]]
        deletes, _, error = self.exercise(snapshots=states)
        self.assertEqual(len(deletes), 1)
        self.assertEqual(error, 'SURVIVING_POD_NOT_READY')

    def test_stale_original_uids_cannot_be_reported_as_recovery(self):
        _, output, error = self.exercise(snapshots=[[pod('browse-a', 'old'), pod('browse-b', 'survivor')]])
        self.assertNotIn('BROWSE_POD_RECOVERY_OK', output)
        self.assertEqual(error, 'REPLACEMENT_NOT_READY_WITHIN_180_SECONDS')

    def test_replacement_not_ready_or_foreign_is_not_success(self):
        for replacement in (pod('browse-c', 'new', False), pod('browse-c', 'new', owner='foreign')):
            with self.subTest(replacement=replacement):
                states = [[pod('browse-a', 'old'), pod('browse-b', 'survivor')],
                          [pod('browse-b', 'survivor'), replacement]]
                _, output, error = self.exercise(snapshots=states)
                self.assertNotIn('BROWSE_POD_RECOVERY_OK', output)
                self.assertIsNotNone(error)

    def test_concurrent_deployment_change_is_not_claimed_as_recovery(self):
        final = deployment()
        final['metadata']['generation'] = 2
        final['status']['observedGeneration'] = 2
        _, _, error = self.exercise(final=final)
        self.assertEqual(error, 'DEPLOYMENT_CHANGED_DURING_DRILL')


if __name__ == '__main__':
    unittest.main()
