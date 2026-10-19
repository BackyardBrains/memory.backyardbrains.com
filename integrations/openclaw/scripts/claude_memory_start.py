#!/usr/bin/env python3
"""SessionStart output with verified full rules, and an honest failure message."""
import json
import os
import urllib.parse
import urllib.request
from memory_context_client import fetch_context


def session_context():
    try:
        packet=fetch_context(project=os.environ.get('MEMORY_PROJECT_SLUG'),client='claude-session')
        context='Watson active rules loaded successfully. Apply the applicable rules before work.\n'+json.dumps(packet,ensure_ascii=False)
    except Exception as exc:
        context=('Watson active rules were NOT loaded ('+type(exc).__name__+'). '
                 'Fetch memory context before relying on corrections or taking memory-dependent action. '
                 'Do not claim the memory check succeeded.')
    secret=os.environ.get('API_SECRET')
    if secret:
        try:
            url='https://watson.backyardbrains.com/summary?'+urllib.parse.urlencode({'secret':secret})
            req=urllib.request.Request(url,headers={'User-Agent':'curl/8.0'})
            with urllib.request.urlopen(req,timeout=5) as response:data=json.load(response)
            summary={k:data[k] for k in ('source_of_truth','freshness_contract','headline','recommended_now') if k in data}
            context+='\nWatson priority summary (verify live evidence before action):\n'+json.dumps(summary,ensure_ascii=False)
        except Exception:
            context+='\nWatson priority summary unavailable; no current-priority claim is made.'
    return {'hookSpecificOutput':{'hookEventName':'SessionStart','additionalContext':context}}


if __name__=='__main__':print(json.dumps(session_context(),ensure_ascii=False))
