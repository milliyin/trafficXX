"""User-run lab HTTP test: creates one hold and cancels it, retaining history."""
import json
import urllib.error
import urllib.request
from uuid import UUID, uuid4


def run(call):
    customer = 'lab-booking-' + uuid4().hex
    key = uuid4().hex
    body = {'event_id': 'demo-general', 'quantity': 2}
    reservation_id = None
    cleanup_required = False
    try:
        status, held = call('POST', '/reservations', customer, key, body)
        # Capture a valid ID before other assertions so a malformed response can
        # still be cleaned up. Network failures can leave an unknown outcome.
        reservation_id = str(UUID(held['id']))
        cleanup_required = True
        assert status == 201, 'First request must create a reservation'
        assert held['status'] == 'HELD' and held['quantity'] == 2
        assert held['event_id'] == 'demo-general'
        status, repeated = call('POST', '/reservations', customer, key, body)
        assert status == 200 and repeated == held, 'Retry changed the original hold'
        status, conflict = call('POST', '/reservations', customer, key,
                                {'event_id': 'demo-general', 'quantity': 3})
        assert status == 409 and conflict == {'detail': {'code': 'IDEMPOTENCY_KEY_REUSED'}}
        path = '/reservations/' + reservation_id
        status, hidden = call('GET', path, customer + '-other')
        assert status == 404 and hidden == {'detail': {'code': 'RESERVATION_NOT_FOUND'}}
        status, cancelled = call('POST', path + '/cancel', customer)
        assert status == 200 and cancelled['id'] == reservation_id
        assert cancelled['status'] == 'CANCELLED'
        status, second_cancel = call('POST', path + '/cancel', customer)
        assert status == 200 and second_cancel == cancelled
        status, replay = call('POST', '/reservations', customer, key, body)
        assert status == 200 and replay == cancelled, 'Old key created a new hold'
        cleanup_required = False
        return {'result': 'BOOKING_FLOW_OK', 'reservation_id': reservation_id,
                'duplicate_reused_hold': True, 'changed_request_rejected': True,
                'other_customer_hidden': True, 'final_status': 'CANCELLED',
                'cancelled_key_reused_result': True}
    finally:
        if cleanup_required:
            status, result = call('POST', '/reservations/' + reservation_id + '/cancel', customer)
            if status != 200 or result.get('status') != 'CANCELLED':
                raise RuntimeError('Cleanup not verified; inspect the diagnostic reservation')


def main():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    recovery = {}

    def call(method, path, customer, key=None, body=None):
        headers = {'X-Customer-Id': customer}
        if key:
            headers.update({'Idempotency-Key': key, 'Content-Type': 'application/json'})
            recovery.update(customer=customer, idempotency_key=key)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request('http://127.0.0.1:18002' + path,
                                         data=data, headers=headers, method=method)
        try:
            response = opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.load(response)

    try:
        print(json.dumps(run(call)))
        return 0
    except Exception as error:
        # Random lab identifiers allow recovery after an ambiguous network result.
        # They are simulated customer IDs, never Azure credentials or tokens.
        print(json.dumps({'result': 'BOOKING_FLOW_FAILED',
                          'error_type': type(error).__name__, **recovery,
                          'note': 'Inspect using this same key; do not retry with a new key. '
                                  'An uncancelled hold expires after five minutes.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
