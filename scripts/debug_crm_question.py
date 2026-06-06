import os
from export_client_thread_to_txt import (
    fetch_crm,
    _extract_client_question_from_crm,
    _extract_ids_from_crm,
    _fetch_crm_details_for_question
)
import json

os.environ['ENVYCRM_BASE_URL'] = 'https://arkvostok.envycrm.com'
os.environ['ENVYCRM_KEY'] = '77534f3aa14e92d8dec3d43956fe9fc96f59be1a'

crm = fetch_crm('cluke92@icloud.com')
q1 = _extract_client_question_from_crm(crm)
print(f'Q from initial search: {q1[:100] if q1 else "NOT FOUND"}')

deal_ids, client_ids = _extract_ids_from_crm(crm)
print(f'Deal IDs: {deal_ids}')
print(f'Client IDs: {client_ids}')

extras = _fetch_crm_details_for_question(deal_ids, client_ids)
print(f'Extra API requests made: {len(extras)}')

for e in extras:
    q = _extract_client_question_from_crm(e)
    if q:
        print(f'\nFOUND in {e["path"]}:')
        print(f'Question: {q[:150]}...')
        print(f'\nFull response structure:')
        print(json.dumps(e['body'], indent=2, ensure_ascii=False)[:500])
        break
