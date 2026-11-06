"""Small JSON Schema subset and newline-delimited JSON-RPC helpers."""
import math


def validate(value, schema, path='arguments'):
    if 'oneOf' in schema:
        count=0
        for option in schema['oneOf']:
            try:
                validate(value,option,path)
                count+=1
            except ValueError:
                pass
        if count!=1:raise ValueError(f'{path}: no unique matching type')
    kind = schema.get('type')
    if isinstance(kind,list):
        if not any(matches(value,k) for k in kind):
            raise ValueError(f'{path}: invalid type')
    elif kind and not matches(value,kind):
        raise ValueError(f'{path}: expected {kind}')
    if isinstance(value,float) and not math.isfinite(value):
        raise ValueError(f'{path}: nonfinite number')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path}: invalid choice')
    if isinstance(value,dict):
        props=schema.get('properties',{})
        if set(schema.get('required',[]))-value.keys():
            raise ValueError(f'{path}: missing required properties')
        extra=schema.get('additionalProperties',True)
        for key,item in value.items():
            if key not in props and extra is False:
                raise ValueError(f'{path}: unsupported property {key}')
            validate(item,props.get(key,extra if isinstance(extra,dict) else {}),path+'.'+key)
    if isinstance(value,list):
        for item in value:
            validate(item,schema.get('items',{}),path+'[]')
        if len(value)>schema.get('maxItems',float('inf')):
            raise ValueError(f'{path}: too many items')
    if isinstance(value,str) and len(value)<schema.get('minLength',0):
        raise ValueError(f'{path}: empty string')


def matches(value,kind):
    if kind=='number': return type(value) in (int,float)
    if kind=='integer': return type(value) is int
    return type(value) is {'object':dict,'array':list,'string':str,'boolean':bool,'null':type(None)}.get(kind)
