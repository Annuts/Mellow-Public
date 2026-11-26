"""One searchable-text projection shared by lexical and vector retrieval."""


def record_text(record):
    fields = {'text', 'title', 'body', 'summary', 'name', 'description', 'purpose',
              'next_step', 'progress', 'notes', 'key', 'content', 'location'}
    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in fields and isinstance(item, str):
                    yield item
                elif isinstance(item, (list, dict)) and key not in {'origin', 'sources', 'used_memories'}:
                    yield from collect(item)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    yield item
                else:
                    yield from collect(item)
    return '\n'.join(collect(record['data']))
