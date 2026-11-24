"""The encrypted TPAP session (the ``/stok=<id>/ds`` channel).

Derived from ``TpapEncryptionSession`` in python-kasa's
``kasa/transports/tpaptransport.py`` at ZeliardM/python-kasa commit
f4dff399016a9daa47316ffb209f6b74a989b6dc (GPL-3.0-or-later).

Changes from upstream, see ``UPSTREAM.md``:

* Only ``aes_128_ccm`` is kept; it is the only cipher the client offers.
* Reply sequence numbers must not go backwards. Upstream logs a mismatch and
  decrypts with whatever number the reply carries, which accepts a replayed
  older reply from the same session. Here a reply is accepted only if its
  number is at least the request's and higher than every reply accepted
  before. Like upstream, a device that answers with a higher number than the
  request still works.
* The 32-bit sequence number cannot wrap; the session must be renewed instead.
"""

from __future__ import annotations

import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ...exceptions import TpapProtocolError

CIPHER_AES_128_CCM = "aes_128_ccm"
_KEY_SALT = b"tp-kdf-salt-aes128-key"
_KEY_INFO = b"tp-kdf-info-aes128-key"
_NONCE_SALT = b"tp-kdf-salt-aes128-iv"
_NONCE_INFO = b"tp-kdf-info-aes128-iv"
_KEY_LEN = 16
TAG_LEN = 16
NONCE_LEN = 12
MAX_SEQUENCE = 0xFFFFFFFF


class SessionExhaustedError(TpapProtocolError):
    """The sequence number space is used up; log in again."""


def _hkdf(master: bytes, *, salt: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(
        master
    )


def key_nonce_from_shared(shared_key: bytes) -> tuple[bytes, bytes]:
    """Derive the AES-128-CCM key and base nonce from the SPAKE2+ shared key."""
    return (
        _hkdf(shared_key, salt=_KEY_SALT, info=_KEY_INFO, length=_KEY_LEN),
        _hkdf(shared_key, salt=_NONCE_SALT, info=_NONCE_INFO, length=NONCE_LEN),
    )


class TpapEncryptionSession:
    """An established session: key, base nonce and the next sequence number."""

    def __init__(self, shared_key: bytes, start_seq: int) -> None:
        """Derive the session keys."""
        if not 0 <= start_seq <= MAX_SEQUENCE:
            raise TpapProtocolError("TPAP session start sequence out of range")
        key, self._base_nonce = key_nonce_from_shared(shared_key)
        self._aead = AESCCM(key, tag_length=TAG_LEN)
        self._sequence = start_seq
        self._last_reply_seq = -1

    def _nonce(self, seq: int) -> bytes:
        return self._base_nonce[:-4] + struct.pack(">I", seq)

    def encrypt(self, plaintext: bytes) -> tuple[bytes, int]:
        """Return ``seq || ciphertext || tag`` and the sequence number used."""
        seq = self._sequence
        if seq > MAX_SEQUENCE:
            raise SessionExhaustedError("TPAP session sequence exhausted")
        self._sequence = seq + 1
        encrypted = self._aead.encrypt(self._nonce(seq), plaintext, None)
        return struct.pack(">I", seq) + encrypted, seq

    def decrypt(self, payload: bytes, request_seq: int) -> bytes:
        """Decrypt the reply to the request sent with ``request_seq``.

        Raises ``cryptography.exceptions.InvalidTag`` when authentication fails.
        """
        if len(payload) < 4 + TAG_LEN:
            raise TpapProtocolError("TPAP response too short")
        (response_seq,) = struct.unpack(">I", payload[:4])
        if response_seq < request_seq or response_seq <= self._last_reply_seq:
            raise TpapProtocolError("TPAP response sequence does not match request")
        plaintext = self._aead.decrypt(self._nonce(response_seq), payload[4:], None)
        # Only an authenticated reply moves the replay window forward.
        self._last_reply_seq = response_seq
        return plaintext
