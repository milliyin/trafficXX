"""Private, immutable banners. No client paths, keys, SAS, or credential fallback."""
import hashlib
import atexit
import os
import re
from functools import lru_cache
from uuid import UUID


class StorageUnavailable(Exception):
    pass


def enabled():
    return bool(os.environ.get('CLICKCLASH_BLOB_ACCOUNT_URL'))


def name(player_id, digest):
    if not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('Invalid banner digest')
    return f'banners/{UUID(str(player_id))}/{digest}.webp'


@lru_cache(maxsize=1)
def container():
    from azure.identity import WorkloadIdentityCredential
    from azure.storage.blob import ContainerClient
    url = os.environ['CLICKCLASH_BLOB_ACCOUNT_URL']
    if not re.fullmatch(r'https://[a-z0-9]{3,24}\.blob\.core\.windows\.net', url):
        raise StorageUnavailable()
    credential = WorkloadIdentityCredential()
    client = ContainerClient(url, 'banners', credential=credential,
                             retry_total=0, connection_timeout=3, read_timeout=5,
                             logging_enable=False)
    atexit.register(credential.close)
    atexit.register(client.close)
    return client


def read(blob_name, player_id, digest):
    if blob_name != name(player_id, digest):
        raise StorageUnavailable()
    try:
        data = container().get_blob_client(blob_name).download_blob(
            offset=0, length=524289, max_concurrency=1, timeout=5).readall()
        if len(data) > 524288 or hashlib.sha256(data).hexdigest() != digest:
            raise StorageUnavailable()
        return data
    except Exception:
        raise StorageUnavailable() from None


def put(player_id, data):
    if not data or len(data) > 524288:
        raise StorageUnavailable()
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import ContentSettings
    digest = hashlib.sha256(data).hexdigest()
    blob_name = name(player_id, digest)
    try:
        container().get_blob_client(blob_name).upload_blob(data, overwrite=False,
            content_settings=ContentSettings(content_type='image/webp', cache_control='no-store'), timeout=5)
    except ResourceExistsError:
        pass  # Retries never overwrite an existing object.
    except Exception:
        raise StorageUnavailable() from None
    if read(blob_name, player_id, digest) != data:
        raise StorageUnavailable()
    return blob_name, digest


def delete_unreferenced(conn, player_id, digest):
    """Caller holds control row lock: uploads/migration/cleanup cannot race."""
    blob_name = name(player_id, digest)
    if conn.execute('SELECT 1 FROM clickclash.image_blobs WHERE blob_name=%s', (blob_name,)).fetchone():
        return
    try:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            container().get_blob_client(blob_name).delete_blob(timeout=5)
        except ResourceNotFoundError:
            pass
    except Exception:
        raise StorageUnavailable() from None
