import json
from unittest.mock import patch
from fastapi.testclient import TestClient
import main,memory_client
packet={'status':'ok','rules':{'status':'ok','rules':[],'total':0,'truncated':False},'evidence':[],'context_id':'fixture'}
with patch.object(memory_client,'_get',return_value=packet) as get:
    c=TestClient(main.app)
    assert c.get('/memory/context').status_code in (401,403)
    r=c.get('/memory/context',params={'secret':'fixture-secret','q':'what is current','project_slug':'spikerecorder'})
    assert r.status_code==200,r.text
    assert r.json()==packet
    get.assert_called_with('/v1/context',{'q':'what is current','client':'watson-rest','project_slug':'spikerecorder'})
    fn=getattr(main.get_memory_context,'fn',main.get_memory_context)
    try:fn(privateKey='wrong')
    except ValueError:pass
    else:raise AssertionError('MCP authentication not enforced')
    assert fn(privateKey='fixture-secret',q='task question',project_slug='byb-shipping')==packet
with patch.object(memory_client,'_post',return_value={'status':'recorded','capture_created':False}) as post:
    fn=getattr(main.record_memory_feedback,'fn',main.record_memory_feedback)
    assert fn('fixture','used',[42],privateKey='fixture-secret')['capture_created'] is False
    post.assert_called_with('/v1/feedback',{'context_id':'fixture','outcome':'used','capture_ids':[42]})
print(json.dumps({'adapter_checks':'passed','external_network':'disabled','real_messages_sent':0}))
