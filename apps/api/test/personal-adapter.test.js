import test from 'node:test';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createPersonalAdapter } from '../src/personal-adapter.js';

async function fixture(t) {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'personal-native-'))); t.after(() => rm(root, { recursive:true, force:true }));
  const configPath=join(root,'config.json');
  await writeFile(configPath,JSON.stringify({version:1,public_url:'https://api.example.test',installer_url:'https://release.example.test/install.sh',artifact_url:'https://release.example.test/client.tgz',artifact_sha256:'a'.repeat(64),gateway:{config_path:'/etc/railshot/gateway.json',command:'/usr/local/sbin/railshot-personal-gateway'}}),{mode:0o600});
  const calls=[], ssh=[];
  const runner=async(command,args,options)=>{const request=JSON.parse(await readFile(args[args.indexOf('--request')+1],'utf8'));calls.push({command,args,options,request});return {status:'succeeded',reachable:true,tunnel:{address:'10.80.0.2/32',server_public_key:'B'.repeat(43)+'=',endpoint:'gateway.example.test:51820',allowed_ips:'10.80.0.1/32'}};};
  const transport=async(command,args,options)=>{const request=JSON.parse(options.input);ssh.push({command,args,request});return JSON.stringify({version:1,job_id:request.job_id,action:request.action,ok:true,result:[],error:null});};
  const options={configPath,stateDirectory:join(root,'personal'),runner,transport};
  const target={id:'personal-11111111-1111-4111-8111-111111111111',generation:1,project_id:'project-one',public_key:'A'.repeat(43)+'=',runtime:{ssh_host_key:'ssh-ed25519 '+'A'.repeat(68)},tunnel:{address:'10.80.0.2/32',allowed_ips:'10.80.0.1/32'}};
  return {root,options,target,calls,ssh};
}

test('registration pins SSH identity and verifies OpenStack CLI without a profile or K3s runtime',async(t)=>{
  const f=await fixture(t), adapter=await createPersonalAdapter(f.options);
  await adapter.register(f.target);assert.equal(f.calls[0].command,'/usr/bin/sudo');assert.equal(f.calls[0].request.target_id,f.target.id);
  assert.equal(await adapter.prepare(f.target),false,'app deployment is separate from CLI connectivity');
  assert.match(adapter.runtimeAccess(f.target).public_key,/^ssh-ed25519 /);
  const verification=await adapter.verify(f.target);assert.equal(verification.openstack_verified,true);
  assert.deepEqual(f.ssh[0].request.params.argv,['server','list']);assert.equal(f.ssh[0].args.at(-1),'railshot-openstack-v1');
  assert.ok(f.ssh[0].args.includes('railshot-openstack@10.80.0.2'));assert.ok(f.ssh[0].args.includes('IdentityAgent=none'));
  assert.ok(f.ssh[0].args.includes('10.80.0.1'));assert.match(await readFile(join(f.root,'personal',f.target.id,'known_hosts'),'utf8'),/^\[10\.80\.0\.2\]:2222 ssh-ed25519 /);
  await adapter.prepare(f.target);
  await assert.rejects(()=>adapter.prepare({...f.target,runtime:{ssh_host_key:'ssh-ed25519 '+'B'.repeat(68)}}),/identity changed/);
});

test('response binding, output bounds and CLI failures cannot fabricate a verified connection',async(t)=>{
  const f=await fixture(t);
  for(const transport of [async()=>JSON.stringify({version:1,job_id:'foreign',action:'openstack.execute',ok:true,result:[],error:null}),async()=> 'x'.repeat(1024*1024+1),async()=>{throw new Error('timeout');}]){
    const adapter=await createPersonalAdapter({...f.options,transport}); await adapter.prepare(f.target);
    assert.equal((await adapter.verify(f.target)).openstack_verified,false);
  }
  const adapter=await createPersonalAdapter(f.options);
  await assert.rejects(()=>adapter.execute(f.target,{job_id:'unsafe;job',argv:['server','list']}),/Invalid/);
  const result=await adapter.execute(f.target,{job_id:'fixed-job',argv:['server','delete','owned-id'],delete_data:true});
  assert.equal(result.job_id,'fixed-job');assert.deepEqual(f.ssh.at(-1).request.params,{argv:['server','delete','owned-id'],delete_data:true});
});


test('server adapter exchanges real JSON bytes with the customer Python CLI protocol (cloud calls simulated)',async(t)=>{
  const f=await fixture(t), script=join(f.root,'protocol-fixture.py');
  await writeFile(script,`import sys, types, json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
stub=types.ModuleType('client_setup.credentials'); stub.CredentialStore=object
sys.modules['client_setup.credentials']=stub
from apps.agent.openstack_control import execute_raw
class Cloud:
    def __init__(self,auth): pass
    def run(self,argv):
        if argv == ['token','issue']: return {'project_id':'project-one','id':'never-publish-this-token'}
        if argv == ['server','list']: return [{'ID':'server-one','Name':'customer-node','Status':'ACTIVE'}]
        raise RuntimeError('cloud call intentionally not implemented')
result=execute_raw(sys.stdin.buffer.read(),sys.argv[3],lambda:{'password':'never-publish-this-password'}, {'project_id':'project-one','target_id':sys.argv[4]},cli_factory=Cloud,home=Path(sys.argv[2]))
print(json.dumps(result)); sys.exit(0 if result['ok'] else 1)
`,{mode:0o600});
  const transport=(_command,args,options)=>new Promise((resolve,reject)=>{
    const child=spawn(process.env.RAILSHOT_TEST_PYTHON || 'python3',[script,fileURLToPath(new URL('../../../',import.meta.url)),join(f.root,'cli'),args.at(-1),f.target.id],{stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='';child.stdout.on('data',(b)=>stdout+=b);child.stderr.on('data',(b)=>stderr+=b);child.on('error',reject);child.on('close',(code)=>{if(code>1)reject(new Error(stderr));else resolve({stdout,code});});child.stdin.end(options.input);
  });
  const adapter=await createPersonalAdapter({...f.options,transport});await adapter.prepare(f.target);
  assert.equal((await adapter.verify(f.target)).openstack_verified,true);
  const success=await adapter.execute(f.target,{job_id:'protocol-read',argv:['server','list']});assert.equal(success.result[0].ID,'server-one');
  await assert.rejects(()=>adapter.execute(f.target,{job_id:'protocol-denied',argv:['server','list','--os-password','secret']}),/response binding invalid/);
  assert.ok(!JSON.stringify(success).includes('never-publish'));
});
