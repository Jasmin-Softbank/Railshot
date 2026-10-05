#!/usr/bin/env python3
"""Build private synthetic test inputs only. Does not contact any host or cluster."""
import argparse,hashlib,json,os,re
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--metadata',required=True);p.add_argument('--output',required=True);p.add_argument('--eso',required=True);p.add_argument('--images',required=True);p.add_argument('--storage-suffix',default='');a=p.parse_args()
if not re.fullmatch(r'(?:-[a-z0-9]{1,20})?',a.storage_suffix):p.error('storage suffix must be a short lowercase identifier prefixed by -')
storage_class='railshot-test-local-retain'+a.storage_suffix;storage_name='railshot-test-vault'+a.storage_suffix
m=json.loads(Path(a.metadata).read_text());images=json.loads(Path(a.images).read_text());root=Path(a.output).resolve();root.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(root,0o700)
def save(name,obj):
 q=root/name;q.write_text(json.dumps(obj,indent=2)+'\n');q.chmod(0o600);return str(q)
env=m['environment_id'];ssh=m['ssh'];node={'id':env,'resource_id':m['resource_id'],'private_ipv4':m['private_ip'],'ssh':ssh}
base={'schema_version':'1.0','target':{'id':env,'provider':'openstack','placement':m['project_id'],'os':'linux','architecture':'amd64','initialization':'cloud-init'},'inventory':{'control_plane':[node],'workers':[]},'timeout_seconds':1800}
for op in ['guest.check','runtime.install','secrets.configure','secrets.verify']:
 req={**base,'request_id':env+'.'+op+'.01','operation':op}
 if op=='runtime.install':req['runtime']={'wait_timeout_seconds':600}
 save(op+'-request.json',req)
storage={'apiVersion':'v1','kind':'List','items':[
 {'apiVersion':'storage.k8s.io/v1','kind':'StorageClass','metadata':{'name':storage_class},'provisioner':'kubernetes.io/no-provisioner','reclaimPolicy':'Retain','volumeBindingMode':'WaitForFirstConsumer'},
 {'apiVersion':'v1','kind':'PersistentVolume','metadata':{'name':storage_name},'spec':{'capacity':{'storage':'5Gi'},'volumeMode':'Filesystem','accessModes':['ReadWriteOnce'],'persistentVolumeReclaimPolicy':'Retain','storageClassName':storage_class,'local':{'path':'/var/lib/'+storage_name},'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':[m['hostname']]}]}]}}}}
]}
storage_path=save('storage.json',storage);eso_path=root/'external-secrets.yaml';eso_path.write_bytes(Path(a.eso).read_bytes());eso_path.chmod(0o600)
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
profile={'version':1,'environment_id':env,'central_provisioning':{'helper':'/usr/local/libexec/railshot-provision-environment','state_dir':'/var/lib/railshot-api/credentials'},'provider_profile':{'provider':'openstack','namespace':'railshot-secrets','storage':{'class_name':storage_class,'capacity':'5Gi','manifest_file':storage_path,'manifest_sha256':digest(storage_path)},'vault':{'image':images['vault'],'tls_secret':'vault-tls','seal_secret':'vault-seal','seal':{},'tls':{},'seal_env_file':''},'external_secrets':{'manifest_file':str(eso_path),'manifest_sha256':digest(eso_path)},'recovery':{}}}
save('secrets-profile.json',profile)
server={'id':m['resource_id'],'project_id':m['project_id'],'status':'ACTIVE','addresses':[{'network':m['network'],'version':4,'address':m['private_ip']}]}
server_file=save('server-observed.json',server)
registry={'version':1,'targets':{env:{'server_file':server_file,'resource_id':m['resource_id'],'project_id':m['project_id'],'management_network':m['network'],'placement':m['project_id'],'architecture':'amd64','initialization':'cloud-init','ssh':ssh,'purpose':'runtime','timeout_seconds':1200}}}
save('targets.json',registry)
app='railshot-secret-canary';ns=app
manifest={'apiVersion':'v1','kind':'List','items':[
 {'apiVersion':'v1','kind':'Namespace','metadata':{'name':ns}},
 {'apiVersion':'v1','kind':'ServiceAccount','metadata':{'name':app,'namespace':ns}},
 {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':app,'namespace':ns},'spec':{'replicas':1,'selector':{'matchLabels':{'app':app}},'template':{'metadata':{'labels':{'app':app}},'spec':{'serviceAccountName':app,'containers':[{'name':'app','image':images['sample'],'ports':[{'name':'http','containerPort':80}],'env':[{'name':'GREETING','value':'old-explicit-value'}],'readinessProbe':{'httpGet':{'path':'/','port':'http'},'periodSeconds':2}}]}}}},
 {'apiVersion':'v1','kind':'Service','metadata':{'name':app,'namespace':ns},'spec':{'selector':{'app':app},'ports':[{'name':'http','port':80,'targetPort':'http'}]}}
]}
save('sample-app.json',manifest)
request={'version':1,'operation_id':'canary-delivery-01','project_id':'canary-project','binding_id':'canary-binding','revision_id':'canary-revision-01','environment_id':env,'application_id':'canary-app','app':app,'phase':'prepare','variables':[{'name':'GREETING','kind':'plain','value':'synthetic-hello','required':True},{'name':'API_TEST_TOKEN','kind':'secret','value':'synthetic-canary-only-20261004','required':True}]}
for phase in ['prepare','apply','verify']:save('delivery-'+phase+'.json',{**request,'phase':phase})
# Resolve only after helper has issued the environment profile.
profile_path=Path('/var/lib/railshot-api/credentials')/env/'transit-profile.json'
if profile_path.exists():
 g=json.loads(profile_path.read_text())
 save('delivery-config.json',{'version':1,'environment_id':env,'registry_file':str(root/'targets.json'),'applications':{'canary-app':{'app':app,'namespace':ns,'deployment':app,'service':app,'container':'app','service_account':app,'secret_store':{'kind':'SecretStore','name':'railshot-vault'}}},'vault':{'namespace':'railshot-secrets','pod':'vault-0','mount':'railshot','auth_mount':'kubernetes','token_file':str(root/'delivery-credential.json'),'ca_file':g['vault_tls']['ca_file'],'custody_config_file':g['delivery_recovery_config_file']}})
print(json.dumps({'status':'prepared','environment_id':env,'output':str(root),'storage_sha256':digest(storage_path),'eso_sha256':digest(eso_path)}))
