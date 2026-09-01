from pathlib import Path
from app.models.connection import ADMEConnection, AuthMethod
from app.services.auth import acquire_cli_token
from app.services.downloaded_dataset import discover_parts
from app.services.dataset_verification import diff_part

root = Path(r'C:\Users\marielherzog\osdu-data\tno')
connection = ADMEConnection(
    endpoint='https://marielsmrttier.energy.azure.com',
    tenant_id='72f988bf-86f1-41af-91ab-2d7cd011db47',
    client_id='ef3f6421-4b33-42b4-9184-d7c5cb2efcf2',
    data_partition_id='opendes',
    token_scope='ef3f6421-4b33-42b4-9184-d7c5cb2efcf2/.default',
    auth_method=AuthMethod.USER_IMPERSONATION,
)
token = acquire_cli_token()
part = next(p for p in discover_parts(root) if p.key == 'work-products/markers')
diff = diff_part(connection, token, part)
print('EXPECTED', diff.expected)
print('PRESENT_RECORDS', diff.present_records)
print('UNIQUE_PRESENT', diff.unique_present)
print('MISSING', diff.missing_names)
print('DUPLICATE_EXTRA_IDS', diff.duplicate_extra_ids)
