import os,sys,json,copy,time,io,datetime
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit,parse_qs
from types import SimpleNamespace
os.environ['AUTH_REQUIRED']='false'
os.environ['INDEX_EVENTS_ENDPOINT']=''
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3] / 'backend'))
import app as backend
from routes.people import people_bp
if people_bp.name not in backend.app.blueprints: backend.app.register_blueprint(people_bp)
from routes.tools import tools_bp
if tools_bp.name not in backend.app.blueprints: backend.app.register_blueprint(tools_bp)
from PIL import Image,ImageDraw
LIB='__default__'
photos={f'audit-{n}.jpg':{'PartitionKey':LIB,'RowKey':f'audit-{n}.jpg','filename':f'audit-{n}.jpg','rating':1,'likes':0,'likedBy':'[]','rotation':0,'captureDate':'2026-10-20T12:00:00Z','processing_state':'done'} for n in range(1,4)}
albums={'audit-album':{'PartitionKey':LIB,'RowKey':'audit-album','id':'audit-album','name':'Audit Original','photoCount':3}}
people={'audit-person':{'PartitionKey':LIB,'RowKey':'audit-person','name':'Audit Person','faceIds':'[]','faceCount':1}}
state={'version':'2026-10-22T01:00:00Z','fail':0,'delay':0,'sourceDown':False,'jobs':[],'requests':[]}
def snapshot():
 state['people']={'sourceVersion':state['version'],'rows':[{'personId':k,'name':p['name'],'isNamed':True,'faceCount':1} for k,p in people.items()]}
 state['sort']={'sourceVersion':state['version'],'rows':[{'RowKey':k,'rating':p['rating'],'likes':p['likes'],'thumb':k,'captureDate':p['captureDate'],'gallery':{'liked':LIB in json.loads(p['likedBy']),'rotation':p['rotation'],'thumbnailRotation':0,'personIds':[],'tags':[],'processing':{'thumbnail':'done'}}} for k,p in photos.items()]}
 state['albums']={'sourceVersion':state['version'],'rows':[{'albumId':k,'name':a['name'],'photoCount':3,'coverFilename':'audit-1.jpg'} for k,a in albums.items()]}
snapshot()
backend._require_user_id=lambda *a,**k:(LIB,None)
backend._get_metadata_entity=lambda library,name:copy.deepcopy(photos.get(name))
def update(library,name,fields):
 photos[name].update(fields)
 state['jobs']=[{'jobId':'audit-index','kind':'index_build','status':'running','title':'Audit rebuild','updatedAt':str(time.time())}]
backend._update_metadata_entity_fields=update
backend._albums_table_available=lambda:True
backend._load_album_entity=lambda library,name:copy.deepcopy(albums.get(name))
backend._save_album_entity=lambda a:albums.update({a['RowKey']:copy.deepcopy(a)})
backend._album_entity_to_payload=lambda a:{'id':a['RowKey'],'name':a['name'],'photoCount':3}
class Entity(dict):
 @property
 def metadata(self):return {'etag':'audit'}
class Table:
 def __init__(self,rows):self.rows=rows
 def get_entity(self,partition_key,row_key):
  if state['sourceDown']:raise RuntimeError('Synthetic source outage')
  if row_key not in self.rows:raise backend.ResourceNotFoundError('missing')
  return Entity(copy.deepcopy(self.rows[row_key]))
 def update_entity(self,entity,**kwargs):self.rows[entity['RowKey']]=dict(entity)
backend.metadata_table_client=Table(photos)
backend.albums_table_client=Table(albums)
backend.person_table_client=Table(people)
backend._people_features_available=lambda:True
backend._invalidate_people_scan_cache=lambda *args:None
backend.publish_person_label_intent=lambda *args:True
image=Image.new('RGB',(800,600),'#427c75');ImageDraw.Draw(image).text((280,270),'SYNTHETIC AUDIT PHOTO',fill='white');out=io.BytesIO();image.save(out,format='JPEG');photo_bytes=out.getvalue()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def send(self,body,status=200,kind='application/json'):
  data=body if isinstance(body,bytes) else json.dumps(body).encode()
  self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Access-Control-Allow-Origin','*');self.send_header('Access-Control-Allow-Headers','*');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
 def do_OPTIONS(self):self.send({})
 def do_POST(self):
  path=urlsplit(self.path).path;body=self.rfile.read(int(self.headers.get('Content-Length',0)))
  if path=='/audit/control':
   data=json.loads(body or '{}');state.update({k:v for k,v in data.items() if k in ['fail','delay','sourceDown']})
   if data.get('publish'):
    state['version']=datetime.datetime.now(datetime.timezone.utc).isoformat();snapshot();state['jobs']=[{'jobId':'audit-index','kind':'index_build','status':'done','indexScope':'sort','title':'Audit publication','updatedAt':state['version']}]
   return self.send({'ok':True})
  state['requests'].append({'method':'POST','path':path,'body':json.loads(body or '{}')})
  real=path in ['/photos/rate-multiple','/api/tools/pending-edits/reconcile'] or path.endswith('/rotation') or path.endswith('/like') or path.endswith('/rename') or path.endswith('/label')
  if real:
   if path!='/api/tools/pending-edits/reconcile':
    if state['delay']:time.sleep(state['delay'])
    if state['fail']>0:state['fail']-=1;return self.send({'error':'Synthetic write failure'},400)
   with backend.app.test_client() as client:
    res=client.post(path,data=body,content_type='application/json')
    return self.send(res.data,res.status_code)
  if 'access-batch' in path:return self.send({'photos':[],'urls':{}})
  if 'index-events/negotiate' in path:return self.send({'available':False})
  return self.send({})
 def do_GET(self):
  path=urlsplit(self.path).path;params=parse_qs(urlsplit(self.path).query)
  state['requests'].append({'method':'GET','path':path})
  if path=='/audit/state':return self.send({'version':state['version'],'photos':photos,'albums':albums,'requests':state['requests'][-100:]})
  if path=='/health':return self.send({'status':'ok'})
  if path=='/api/photos/index-status':return self.send({'ready':True,'building':False})
  if path in ['/api/photos/sort-index','/api/albums/index','/api/persons/index']:
   kind={'/api/photos/sort-index':'sort','/api/albums/index':'albums','/api/persons/index':'people'}[path]
   return self.send({'available':True,'sourceVersion':state['version'],'indexUrl':f'http://127.0.0.1:5001/fixture/{kind}.json'})
  if path.startswith('/fixture/'):
   kind=path.split('/')[-1].split('.')[0];return self.send(state.get(kind,{'rows':[],'sourceVersion':state['version']}))
  if path=='/api/photos/media-token':return self.send({'available':False})
  if path=='/api/jobs/status':return self.send({'jobs':state['jobs']})
  if path=='/api/persons/page':
   rows=copy.deepcopy(state['people']['rows'])
   if 'ids' in params:rows=[r for r in rows if r['personId'] in params['ids'][0].split(',')]
   if 'q' in params:rows=[r for r in rows if params['q'][0].lower() in r['name'].lower()]
   return self.send({'available':True,'rows':rows,'total':len(rows),'hasMore':False,'sourceVersion':state['version']})
  if path in ['/api/photos/summary','/photos/summary','/api/library/stats']:return self.send({'total':3,'photoCount':3,'totalPhotos':3,'storageBytes':1000})
  if path=='/photos':return self.send({'photos':list(photos.values()),'total':3})
  if path=='/albums':return self.send({'albums':[{'id':k,'name':a['name'],'photoCount':3} for k,a in albums.items()]})
  if path.startswith('/albums/audit-album'):return self.send({'album':{'id':'audit-album','name':albums['audit-album']['name'],'photoCount':3},'photos':list(photos.values()),'total':3})
  if path=='/photos/timeline':return self.send({'years':{}})
  if 'thumbnail' in path or 'preview' in path or path.startswith('/images/'):return self.send(photo_bytes,kind='image/jpeg')
  if path.endswith('/metadata'):return self.send({'rotation':0,'rating':photos.get(path.split('/')[-2],{}).get('rating',1)})
  if path=='/explore':return self.send({'places':[],'things':[]})
  return self.send({})
print('Isolated audit fixture listening on 127.0.0.1:5001',flush=True)
ThreadingHTTPServer(('127.0.0.1',5001),Handler).serve_forever()
