"""Pinned Hindsight 0.9.2 text projection; immutable canonical text stays intact.

Native engine/llm_wrapper.py sanitize_text strips these controls at ingress.
Every deletion is accounted for with canonical UTF-8/character coordinates and
the corresponding native coordinates. TAB, LF, CR and ordinary whitespace stay.
"""
from __future__ import annotations

import re

from .preservation import PreservationError, digest

CONTRACT = 'hindsight-0.9.2-sanitize-v1'
CONTROL_PATTERN = r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]'
_RUNS = re.compile('(' + CONTROL_PATTERN + r')\1*')
MAX_REMOVED_SPANS = 4096


def project_text(text: str, expected_sha256: str | None = None) -> tuple[str, dict]:
    if not isinstance(text, str):
        raise PreservationError('invalid_canonical_text')
    try:
        raw = text.encode('utf-8')
    except UnicodeError:
        # A canonical UTF-8 Store cannot contain unpaired surrogates.
        raise PreservationError('invalid_canonical_text') from None
    canonical_hash = digest(raw)
    if expected_sha256 is not None and canonical_hash != expected_sha256:
        raise PreservationError('canonical_text_hash_mismatch')
    spans, pieces, cursor, raw_byte, removed = [], [], 0, 0, 0
    for match in _RUNS.finditer(text):
        if len(spans) >= MAX_REMOVED_SPANS:
            raise PreservationError('native_projection_mapping_limit')
        prefix = text[cursor:match.start()]
        pieces.append(prefix)
        raw_byte += len(prefix.encode('utf-8'))
        count = match.end() - match.start()
        spans.append({'char_start': match.start(), 'char_end': match.end(),
                      'byte_start': raw_byte, 'byte_end': raw_byte + count,
                      'native_char_start': match.start() - removed,
                      'native_byte_start': raw_byte - removed, 'codepoint': ord(match[1])})
        raw_byte += count
        removed += count
        cursor = match.end()
    pieces.append(text[cursor:])
    native = ''.join(pieces)
    return native, {'schema': 1, 'contract': CONTRACT, 'canonical_sha256': canonical_hash,
                    'native_sha256': digest(native.encode()), 'canonical_chars': len(text),
                    'native_chars': len(native), 'canonical_bytes': len(raw),
                    'native_bytes': len(raw) - removed, 'removed_chars': removed,
                    'removed_bytes': removed, 'removed_spans': spans}


def validate_projection(value: dict, canonical_hash: str) -> None:
    """Validate persisted structural evidence; checksum binds its full mapping."""
    try:
        if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('contract') != CONTRACT
                or value.get('canonical_sha256') != canonical_hash
                or not re.fullmatch('[a-f0-9]{64}', value['native_sha256'])):
            raise ValueError
        counts = ('canonical_chars', 'native_chars', 'canonical_bytes', 'native_bytes', 'removed_chars', 'removed_bytes')
        if any(type(value[key]) is not int or value[key] < 0 for key in counts):
            raise ValueError
        if (value['canonical_chars'] - value['native_chars'] != value['removed_chars']
                or value['canonical_bytes'] - value['native_bytes'] != value['removed_bytes']
                or value['removed_chars'] != value['removed_bytes']
                or value['canonical_bytes'] < value['canonical_chars']
                or value['native_bytes'] < value['native_chars']
                or not value['removed_chars'] and value['native_sha256'] != canonical_hash):
            raise ValueError
        spans = value['removed_spans']
        if not isinstance(spans, list) or len(spans) > MAX_REMOVED_SPANS:
            raise ValueError
        removed, previous_char, previous_byte = 0, 0, 0
        for span in spans:
            if any(type(span[key]) is not int for key in ('char_start','char_end','byte_start','byte_end','native_char_start','native_byte_start','codepoint')):
                raise ValueError
            count = span['char_end'] - span['char_start']
            if (count <= 0 or span['char_start'] < previous_char or span['byte_start'] < previous_byte
                    or span['byte_start'] < span['char_start']
                    or span['char_end'] > value['canonical_chars'] or span['byte_end'] > value['canonical_bytes']
                    or span['byte_end'] - span['byte_start'] != count
                    or span['native_char_start'] != span['char_start'] - removed
                    or span['native_byte_start'] != span['byte_start'] - removed
                    or not 0 <= span['codepoint'] <= 127
                    or not re.fullmatch(CONTROL_PATTERN, chr(span['codepoint']))):
                raise ValueError
            previous_char, previous_byte = span['char_end'], span['byte_end']
            removed += count
        if removed != value['removed_chars']:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise PreservationError('native_projection_proof_invalid') from None
