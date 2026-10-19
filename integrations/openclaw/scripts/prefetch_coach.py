#!/usr/bin/env python3
"""Fetch all active, applicable Watson corrections; fail visibly on service errors."""
import json
import os
import sys
import urllib.parse
import urllib.request
try:
    from .memory_context_client import fetch_context
except ImportError:
    from memory_context_client import fetch_context


def fetch_rules():
    key = os.environ.get('MEMORY_API_KEY')
    if not key:
        raise RuntimeError('MEMORY_API_KEY is not configured')
    base = os.environ.get('MEMORY_API_URL', 'https://memory.backyardbrains.com').rstrip('/')
    project = os.environ.get('MEMORY_PROJECT_SLUG')
    query = '?' + urllib.parse.urlencode({'project_slug': project}) if project else ''
    req = urllib.request.Request(base + '/v1/rules' + query,
                                 headers={'X-API-Key': key, 'User-Agent': 'curl/8.0'})
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    if (data.get('status') != 'ok' or data.get('truncated') is not False
            or not isinstance(data.get('rules'), list)
            or data.get('total') != len(data['rules'])):
        raise RuntimeError('Incomplete rules response')
    if any(not r.get('capture_id') or not r.get('content') for r in data['rules']):
        raise RuntimeError('Invalid rule in response')
    return data


def main():
    try:
        packet=fetch_context(os.environ.get('MEMORY_CONTEXT_QUERY',''),
                             os.environ.get('MEMORY_PROJECT_SLUG'),
                             os.environ.get('MEMORY_CLIENT','routine'))
        print(json.dumps(packet, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Do not expose URLs/headers/credentials from transport exceptions.
        print(json.dumps({'status': 'error', 'error': 'Coach correction retrieval failed',
                          'error_type': type(exc).__name__}), file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
