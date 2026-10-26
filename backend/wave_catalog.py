"""Real catalog publishers -> verified immutable generation -> exact receipt.

Default legacy publishers are unchanged. Only an explicit wave index consumer
uses this adapter. Serving aliases use their existing layouts, pinned by ETag
before a full build; immutable copies preserve exactly what a receipt covers.
"""
import hashlib
import json
import tempfile

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from execution_lease import guard_operations
from index_publication import guarded_publication
from upload_waves import digest

SCOPES = ('sort', 'access', 'people', 'albums', 'search', 'vector', 'people_embedding', 'dedup')
PREREQUISITES = {'search': ('people','albums'), 'people_embedding': ('people',)}


class BuildGuard:
    def __init__(self, raw, aliases, check):
        self.raw, self.aliases, self.owned = raw, {}, check
        self.outputs, self.lost = {}, None
        for container, name in aliases:
            try:
                version = raw(container, name).get_blob_properties().etag
            except ResourceNotFoundError:
                version = None
            self.aliases[container, name] = version

    def check(self):
        if self.lost is not None:
            raise RuntimeError('Wave publication failed') from self.lost
        self.owned()

    def before_upload(self, client, options):
        key = client.container_name, client.blob_name
        if key not in self.aliases:
            self.lost = ValueError('Publisher attempted an undeclared wave output')
            raise self.lost
        version = self.aliases[key]
        if version:
            options.update(etag=version, match_condition=MatchConditions.IfNotModified)
        else:
            options.pop('etag', None); options.pop('match_condition', None)
            options['overwrite'] = False

    def after_upload(self, client, result):
        props = client.get_blob_properties()
        committed = result.get('etag') if result else None
        if not committed or str(props.etag) != str(committed):
            self.lost = RuntimeError('Publication alias changed after upload')
            raise self.lost
        key = client.container_name, client.blob_name
        self.aliases[key] = props.etag
        self.outputs[key] = str(props.etag)


class CatalogPublisher:
    def __init__(self, store, wave_id, storage, check):
        self.store, self.wave_id, self.su, self.check = store, wave_id, storage, check
        service = storage._CTX['blob_service_client']
        self.raw = lambda container, name: service.get_blob_client(container=container, blob=name)

    def _aliases(self, scope):
        su, library = self.su, self.store.library
        container = su._lexical_index_container_name()
        if scope in ('vector', 'people_embedding'):
            container = getattr(su, f'_{scope}_index_container_name')()
            return [(container, getattr(su, f'_{scope}_index_{suffix}_blob_name')(library))
                    for suffix in ('npz','manifest')]
        if scope == 'search':
            import search_db
            key=su._vector_index_blob_key(library)
            names=[su._listing_index_json_blob_name(library),su._listing_index_manifest_blob_name(library),
                   su._lexical_index_manifest_blob_name(library),*search_db._blob_names(library),
                   key+'-explore.json',key+'-timeline.json']
            return [(container,name) for name in names]
        return [(container,getattr(su,f'_{scope}_index_{suffix}_blob_name')(library))
                for suffix in ('json','manifest')]

    def _copy(self, container, name, version, obligation):
        self.check()
        source = self.raw(container,name)
        props = source.get_blob_properties()
        if str(props.etag) != version:
            raise RuntimeError('Published artifact changed before receipt capture')
        content = hashlib.sha256()
        with tempfile.TemporaryFile() as data:
            for chunk in source.download_blob(etag=props.etag, match_condition=MatchConditions.IfNotModified,
                                              decompress=False).chunks():
                self.check(); content.update(chunk); data.write(chunk)
            size = data.tell(); data.seek(0)
            sha = content.hexdigest()
            target = 'wave-projections/'+digest(self.store.library)+'/'+obligation+'/'+digest([container,name])+'/'+sha
            client = self.raw(container,target)
            self.check()
            try:
                client.upload_blob(data, overwrite=False, metadata={'sha256':sha}, content_settings=props.content_settings)
            except ResourceExistsError:
                pass
            published = client.get_blob_properties()
            if published.size != size or published.metadata.get('sha256') != sha:
                raise RuntimeError('Immutable publication generation differs')
        return dict(container=container, blob=target, etag=str(published.etag), sha256=sha, size=size)

    def _verify(self, proof):
        if proof.get('libraryId') != self.store.library or proof.get('waveId') != self.wave_id:
            raise ValueError('Publication proof scope mismatch')
        for output in proof['outputs']:
            self.check()
            props = self.raw(output['container'],output['blob']).get_blob_properties()
            if (str(props.etag) != output['etag'] or props.size != output['size']
                    or props.metadata.get('sha256') != output['sha256']):
                raise RuntimeError('Publication generation missing or changed')

    def publish(self, scope, obligation):
        from index_ownership import require_catalog_owner
        require_catalog_owner()
        if scope not in SCOPES:
            raise ValueError('Unknown wave publication scope')
        self.check()
        key = f'publication!{self.wave_id}!{obligation}'
        previous = self.store._get(key)
        if previous:
            proof = json.loads(previous['payload'])
            if digest(proof) != previous.get('generation') or proof.get('obligationId') != obligation or proof.get('scope') != scope:
                raise ValueError('Publication generation identity mismatch')
            self._verify(proof)
            return previous['generation']
        su, library = self.su, self.store.library
        if scope == 'dedup':
            # Its real publisher has a create-only blob and delete-epoch CAS.
            import upload_hash_snapshot
            result = upload_hash_snapshot.rebuild_snapshot(library, publication_check=self.check)
            if not result:
                raise RuntimeError('Dedup publication unavailable')
            aliases = [(su._lexical_index_container_name(),result['blobName'])]
            outputs = {pair:str(self.raw(*pair).get_blob_properties().etag) for pair in aliases}
        else:
            aliases = self._aliases(scope)
            guard = BuildGuard(self.raw, aliases, self.check)
            with guarded_publication(guard):
                if scope == 'search':
                    if su.LEXICAL_BUILD_HOOK is None:
                        raise RuntimeError('Streaming search publisher is not configured')
                    result=su.LEXICAL_BUILD_HOOK(library,source_version=obligation)
                else:
                    options=dict(source_version=obligation)
                    if scope in ('sort','access','people','vector'):
                        options['force_full']=True
                    result=getattr(su,f'refresh_user_{scope}_index')(library,**options)
                if result is None:
                    raise RuntimeError(f'Required publisher produced no artifact (scope={scope})')
                if set(guard.outputs) != set(aliases):
                    raise RuntimeError('Publisher did not commit every required artifact')
            outputs = guard.outputs
        proof = dict(schemaVersion=1,libraryId=library,waveId=self.wave_id,scope=scope,
            obligationId=obligation,outputs=[self._copy(c,n,v,obligation) for (c,n),v in sorted(outputs.items())])
        self._verify(proof)
        body = json.dumps(proof,sort_keys=True,separators=(',',':'))
        if len(body.encode('utf-16-le')) > 60000:
            raise ValueError('Publication proof exceeds Table budget')
        generation = digest(proof)
        def commit():
            self.check()
            existing=self.store._get(key)
            if existing:
                if existing.get('payload') != body: raise RuntimeError('Publication generation rebound')
                return
            guards=guard_operations(self.store)
            if not guards: raise RuntimeError('Publication requires stage-fenced ownership')
            self.store.table.submit_transaction([('create',self.store._row(key,payload=body,generation=generation)),*guards])
        self.store._retry(commit)
        return generation
