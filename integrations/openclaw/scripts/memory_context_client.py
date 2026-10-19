#!/usr/bin/env python3
"""Shared authenticated client for session context and observed-use feedback."""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request


def request(path, params=None, body=None):
    key=os.environ.get('MEMORY_API_KEY')
    if not key:raise RuntimeError('MEMORY_API_KEY is not configured')
    base=os.environ.get('MEMORY_API_URL','https://memory.backyardbrains.com').rstrip('/')
    url=base+path+('?' + urllib.parse.urlencode(params) if params else '')
    req=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,
        headers={'X-API-Key':key,'User-Agent':'curl/8.0','Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=20) as response:return json.load(response)


def validate_rules(data):
    if (data.get('status')!='ok' or data.get('truncated') is not False or
        not isinstance(data.get('rules'),list) or data.get('total')!=len(data['rules']) or
        any(not r.get('capture_id') or not r.get('content') for r in data['rules'])):
        raise RuntimeError('Incomplete or invalid active rules response')
    return data


def fetch_context(query='',project=None,client='openclaw'):
    params={'q':query,'client':client}
    if project:params['project_slug']=project
    data=request('/v1/context',params)
    if data.get('status')!='ok' or not isinstance(data.get('evidence'),list):
        raise RuntimeError('Invalid context response')
    validate_rules(data.get('rules',{}))
    return data


def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    context=sub.add_parser('context');context.add_argument('--q',default='');context.add_argument('--project')
    context.add_argument('--client',default='openclaw')
    rules=sub.add_parser('rules');rules.add_argument('--project')
    feedback=sub.add_parser('feedback');feedback.add_argument('--context',required=True)
    feedback.add_argument('--outcome',required=True,choices=['used','missed','stale','irrelevant','user_repeated','abstained'])
    feedback.add_argument('--capture-id',action='append',type=int,default=[])
    a=p.parse_args()
    try:
        if a.command=='context':data=fetch_context(a.q,a.project,a.client)
        elif a.command=='rules':data=validate_rules(request('/v1/rules',{'project_slug':a.project} if a.project else {}))
        else:data=request('/v1/feedback',body=dict(context_id=a.context,outcome=a.outcome,capture_ids=a.capture_id))
        print(json.dumps(data,ensure_ascii=False));return 0
    except Exception as exc:
        print(json.dumps(dict(status='error',error='Watson memory request failed',error_type=type(exc).__name__)),file=sys.stderr)
        return 1


if __name__=='__main__':sys.exit(main())
