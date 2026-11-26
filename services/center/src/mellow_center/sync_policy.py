"""Small-record snapshot projection for metered, non-LAN phone sync."""
from .vault import canonical

LIGHT_RECORD_BYTES = 64 * 1024
HEAVY_KINDS = {'document', 'collection', 'attachment', 'blob', 'archive_export',
               'embedding', 'semantic_index', 'photo', 'pdf', 'voice'}
BINARY_FIELDS = {'original', 'attachments', 'blob', 'photo_png', 'content_base64', 'base64',
                 'embedding', 'embeddings', 'vector', 'vectors', 'archive_export'}


def light_record_allowed(record):
    def binary(value):
        if isinstance(value, dict):
            return any((key in BINARY_FIELDS and item is not None and item != '' and item != [])
                       or binary(item) for key, item in value.items())
        return isinstance(value, list) and any(binary(item) for item in value)
    return (record['kind'] not in HEAVY_KINDS and not binary(record.get('data'))
            and len(canonical(record)) <= LIGHT_RECORD_BYTES)
