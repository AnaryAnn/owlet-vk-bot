"""Send percentage quest milestones after an ordinary statistics request."""
import hashlib
import os
import requests
from quest_engine import quest_api


def send_milestones(stats_loader, splitter, max_events=10):
    stats=stats_loader({'action':'stats'})
    if not stats.get('success'):
        raise RuntimeError(stats.get('error','Stats unavailable'))
    goal=stats.get('teamGoal')
    if goal in (None,'','0',0):
        return {'success':True,'reason':'team goal not configured','sent':[]}
    records=stats.get('currentRecords') or []
    fact=sum(float(str(row.get('fact') or 0).replace(',','.')) for row in records)
    percent=100*fact/float(str(goal).replace(',','.'))
    week=stats.get('currentWeek') or {}
    week_key=str(week.get('start') or '').strip()
    if not week_key:
        raise RuntimeError('Missing currentWeek.start')
    sent=[]
    for _ in range(max_events):
        item=quest_api('milestoneClaim',percent=percent,weekKey=week_key).get('event')
        if not item:break
        eid=item['eventId']
        chunks=splitter(item['text'])
        photo=str(item.get('photo') or '').strip()
        for index,chunk in enumerate(chunks):
            # Stable VK random_id across retries for the same event and chunk.
            key=f"{eid}|{week_key}|{index}".encode('utf-8')
            rid=int.from_bytes(hashlib.sha256(key).digest()[:4],'big')%2147483646+1
            data={'access_token':os.environ['VK_TOKEN'],'v':'5.199','peer_id':int(item['peerId']),
                  'random_id':rid,'message':chunk}
            if index==0 and photo:data['attachment']=photo
            response=requests.post('https://api.vk.com/method/messages.send',data=data,timeout=(5,15))
            response.raise_for_status()
            result=response.json()
            if result.get('error'):raise RuntimeError(str(result['error']))
        quest_api('milestoneAck',eventId=eid,weekKey=week_key)
        sent.append(eid)
    return {'success':True,'percent':percent,'sent':sent}
