"""Credential generation, public profile validation and safe image normalization."""
import io
from pathlib import Path
import secrets
from threading import BoundedSemaphore
from urllib.parse import urlsplit
import warnings

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from PIL import Image, ImageOps, UnidentifiedImageError
from fastapi import HTTPException

hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_hash_slots = BoundedSemaphore(2)
WORDS = tuple(line.split()[1] for line in
              (Path(__file__).parent / 'data' / 'eff_large_wordlist.txt').read_text().splitlines() if line.strip())
if len(WORDS) != 7776 or len(set(WORDS)) != 7776:
    raise RuntimeError('Invalid passphrase word list')
Image.MAX_IMAGE_PIXELS = 4_194_304


def password():
    return '-'.join(secrets.choice(WORDS) for _ in range(5))


def verify(encoded, plaintext):
    if not _hash_slots.acquire(blocking=False):
        raise HTTPException(503, detail={'code': 'AUTH_BUSY'})
    try:
        try:
            return hasher.verify(encoded, plaintext)
        except (VerificationError, InvalidHashError):
            return False
    finally:
        _hash_slots.release()


def hash_password(plaintext):
    if not _hash_slots.acquire(blocking=False):
        raise HTTPException(503, detail={'code': 'AUTH_BUSY'})
    try:
        return hasher.hash(plaintext)
    finally:
        _hash_slots.release()


def email(value):
    value = value.strip().casefold()
    if len(value) > 254 or not value.isascii() or value.count('@') != 1:
        raise HTTPException(422, detail={'code': 'INVALID_EMAIL'})
    local, domain = value.split('@')
    if not local or '.' not in domain or any(c.isspace() or ord(c) < 33 for c in value):
        raise HTTPException(422, detail={'code': 'INVALID_EMAIL'})
    return value


def website(value):
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname and
                 not parsed.username and not parsed.password and
                 not any(c.isspace() or ord(c) < 32 for c in value))
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(422, detail={'code': 'INVALID_WEBSITE'})
    return value


def image(raw):
    if not raw or len(raw) > 1_048_576:
        raise HTTPException(413, detail={'code': 'IMAGE_TOO_LARGE'})
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in ('PNG', 'JPEG', 'WEBP') or getattr(source, 'n_frames', 1) != 1:
                    raise ValueError
                if source.width > 2048 or source.height > 2048:
                    raise ValueError
                source.load()
                normalized = ImageOps.exif_transpose(source).convert('RGB')
                normalized.thumbnail((960, 640))
                output = io.BytesIO()
                normalized.save(output, format='WEBP', quality=80)
                result = output.getvalue()
                if len(result) > 524288:
                    raise ValueError
                return result
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise HTTPException(422, detail={'code': 'INVALID_IMAGE'}) from None
