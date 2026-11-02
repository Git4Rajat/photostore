"""Prepare legacy invitations for core management; defaults to a read-only audit.

Run with the same storage settings/identity as archive. First upgrade ALL core,
archive and worker writers to the dual-writing backend. Then run --apply before
releasing the new frontend. Do not roll backend writers back across this change
after migration; a frontend rollback remains supported by archive compatibility.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library_utils import LibraryStore, _now


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--library', action='append', help='Library ID; repeat for multiple libraries')
    scope.add_argument('--all', action='store_true', help='Process all library records')
    parser.add_argument('--apply', action='store_true', help='Write lookup rows and readiness markers')
    parser.add_argument('--writers-upgraded', action='store_true', help='All backend writers maintain invitation lookups')
    args = parser.parse_args()
    if args.apply and not args.writers_upgraded:
        parser.error('--apply requires --writers-upgraded; deploy every backend writer first')

    from azure.data.tables import TableServiceClient
    connection = os.getenv('STORAGE_CONNECTION_STRING')
    if connection:
        service = TableServiceClient.from_connection_string(connection)
    else:
        from azure.identity import DefaultAzureCredential
        endpoint = os.getenv('TABLES_ENDPOINT') or os.getenv('TABLE_ENDPOINT')
        if not endpoint:
            account = os.getenv('STORAGE_ACCOUNT_NAME') or os.getenv('AZURE_STORAGE_ACCOUNT_NAME')
            if not account:
                parser.error('Set STORAGE_CONNECTION_STRING, TABLES_ENDPOINT, or STORAGE_ACCOUNT_NAME')
            endpoint = f'https://{account}.table.core.windows.net'
        service = TableServiceClient(endpoint=endpoint, credential=DefaultAzureCredential())
    table = lambda env, default: service.get_table_client(os.getenv(env, default))
    store = LibraryStore(
        users_table=table('USERS_TABLE', 'photousers'),
        libraries_table=table('LIBRARIES_TABLE', 'photolibraries'),
        memberships_table=table('MEMBERSHIPS_TABLE', 'photomemberships'),
        invites_table=table('INVITES_TABLE', 'photoinvites'),
        audit_table=table('AUDIT_TABLE', 'photoaudit'),
    )
    libraries = (str(row['RowKey']) for row in store.libraries.query_entities("PartitionKey eq 'library'")) if args.all else args.library
    for library_id in libraries:
        if store.get_library(library_id) is None:
            raise RuntimeError(f'Library does not exist or could not be read: {library_id}')
        if args.apply:
            count = store.backfill_invite_index(library_id)
        else:
            pk = library_id.replace("'", "''")
            count = sum(1 for row in store.invites.query_entities(
                f"PartitionKey eq '{pk}' and RowKey ge 'inv' and RowKey lt 'inw'")
                if row.get('status') == 'pending' and int(row.get('expiresAt', 0) or 0) > _now())
        print(f'{library_id}: {count} unexpired pending invitations; {"ready" if args.apply else "audit only"}')


if __name__ == '__main__':
    main()
