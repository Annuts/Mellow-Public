"""Portable source documents and collections. Sources are never personal assertions."""
import base64
import hashlib
from .domain import fields, require, string


def validate(record):
    d=record['data']
    require(record['mode']=='source_only')
    if record['kind']=='collection':
        fields(d,['title','tags','members','excluded'])
        string(d['title'],200)
        for key,limit,width in [('tags',12,40),('members',10000,128),('excluded',10000,128)]:
            require(isinstance(d[key],list) and len(d[key])<=limit)
            for value in d[key]: string(value,width)
        return
    fields(d,['title','channel','mime','pages','summary','tags','organization','state','manual'],['original','ai','error'])
    string(d['title'],200);string(d['channel'],80)
    require(d['mime'] in {'text/plain','image/png','image/jpeg','image/webp','application/pdf'})
    require(d['state'] in {'pending','processing','done','failed'} and d['organization'] in {'local','processing','model','failed'})
    require(type(d['manual']) is bool and type(d.get('ai',False)) is bool)
    require(isinstance(d['summary'],str) and len(d['summary'])<=64000)
    require(isinstance(d['tags'],list) and len(d['tags'])<=12)
    for tag in d['tags']: string(tag,40)
    require(isinstance(d['pages'],list) and len(d['pages'])<=100)
    require(sum(len(p.get('text','')) for p in d['pages'] if isinstance(p,dict)) <= 500000)
    for number,p in enumerate(d['pages'],1):
        fields(p,['number','text','state','engine'],['error','manual'])
        require(p['number']==number and isinstance(p['text'],str) and len(p['text'])<=64000)
        require(p['state'] in {'pending','done','failed'} and isinstance(p['engine'],str) and len(p['engine'])<=80)
        require(type(p.get('manual',False)) is bool)
        if p.get('error') is not None: string(p['error'],1000)
    if d.get('error') is not None: string(d['error'],1000)
    if 'original' in d:
        fields(d['original'],['content','sha256'])
        string(d['original']['content'],12*1024*1024)
        try: raw=base64.b64decode(d['original']['content'],validate=True)
        except (ValueError,TypeError): require(False)
        require(0<len(raw)<=8*1024*1024 and hashlib.sha256(raw).hexdigest()==d['original']['sha256'])
    else: require(d['mime']=='text/plain')
