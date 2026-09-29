const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const ts = require('D:/ZMK-Firmware/zmk-sim/windows-app/node_modules/typescript');
const root = process.argv[2];
const mode = process.argv[3];
const file = path.join(root, 'windows-app/src/main.ts');
if (mode === 'build') {
  const config = ts.readConfigFile(path.join(root, 'windows-app/tsconfig.json'), ts.sys.readFile);
  if (config.error) throw Error(ts.flattenDiagnosticMessageText(config.error.messageText, '\n'));
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, path.join(root, 'windows-app'), {
    baseUrl: 'D:/ZMK-Firmware/zmk-sim/windows-app/node_modules'
  });
  const program = ts.createProgram(parsed.fileNames, parsed.options);
  const errors = ts.getPreEmitDiagnostics(program);
  for (const error of errors) console.error(ts.flattenDiagnosticMessageText(error.messageText, '\n'));
  if (errors.length) process.exit(1);
  console.log('PASS: Windows App TypeScript compiler, strict/noEmit.');
  import('file:///D:/ZMK-Firmware/zmk-sim/windows-app/node_modules/vite/dist/node/index.js').then(async ({build}) => {
    // Vite/Rollup cannot use a UNC project root reliably. Build a fresh exact source snapshot.
    const staging=fs.mkdtempSync(path.join(require('node:os').tmpdir(),'prospector-bonsai-build-'));
    for (const name of ['src','index.html','package.json']) fs.cpSync(path.join(root,'windows-app',name),path.join(staging,name),{recursive:true});
    await build({root:staging,configFile:false,clearScreen:false,
      resolve:{alias:[{find:/^@tauri-apps\/api\/(.+)$/,replacement:'D:/ZMK-Firmware/zmk-sim/windows-app/node_modules/@tauri-apps/api/$1'}]}});
    console.log('PASS: Vite production bundle at '+path.join(staging,'dist')+'. Native Tauri packaging not included.');
  }).catch(error=>{console.error(error);process.exitCode=1;});
} else {
const source = fs.readFileSync(file, 'utf8');
const ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
const functions = ast.statements.filter(ts.isFunctionDeclaration).map(n => n.getText(ast)).join('\n');
const js = ts.transpileModule(functions, {compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
function peer(slot, name, ready, bonded=true) {
  return {slot,name,ready,bonded,layer:'BASE',centralBattery:80,peripheralBattery:-1,rssi:-50,profile:0,output:'USB'};
}
function render(peers, mask) {
  const elements = new Map();
  const element = key => {
    if (!elements.has(key)) elements.set(key,{textContent:'',classList:{toggle(){}},querySelector:s=>element(key+s)});
    return elements.get(key);
  };
  const context = {revisionEl:element('revision'),maskEl:element('mask'),activeName:element('name'),activeLayer:element('layer'),document:{querySelector:s=>element(s)}};
  vm.createContext(context); vm.runInContext(js,context);
  context.renderState({revision:1,activeMask:mask,peers});
  return elements.get('name').textContent;
}
function parse(lines) {
  const context = {};
  vm.createContext(context); vm.runInContext(js,context);
  return context.parseState(lines);
}
function renderParsed(lines) {
  const state = parse(lines);
  assert.ok(state);
  const elements = new Map();
  const element = key => {
    if (!elements.has(key)) elements.set(key,{textContent:'',classList:{toggle(){}},querySelector:s=>element(key+s)});
    return elements.get(key);
  };
  const context = {revisionEl:element('revision'),maskEl:element('mask'),activeName:element('name'),activeLayer:element('layer'),document:{querySelector:s=>element(s)}};
  vm.createContext(context); vm.runInContext(js,context);
  context.renderState(state);
  return {
    state,
    name: elements.get('name').textContent,
    layer: elements.get('layer').textContent,
  };
}
const tests = [
  ['later ready peer overrides earlier saved peer',()=>assert.equal(render([peer(0,'Saved',false),peer(1,'Ready',true)],3),'Ready')],
  ['ready peer remains selected when saved peer follows',()=>assert.equal(render([peer(1,'Ready',true),peer(0,'Saved',false)],3),'Ready')],
  ['saved fallback with no ready peer',()=>assert.equal(render([peer(0,'Saved',false)],1),'Saved')],
  ['inactive ready peer is not hero',()=>assert.equal(render([peer(0,'Inactive',true),peer(1,'Saved',false)],2),'Saved')],
  ['no active peer',()=>assert.equal(render([peer(0,'Inactive',true)],0),'No active keyboard')],
  ['first ready peer remains stable',()=>assert.equal(render([peer(0,'First',true),peer(1,'Second',true)],3),'First')],
  ['protocol data flows through parseState to renderState',()=>{
    const result = renderParsed([
      'STATE\t1\t7\t3',
      'PEER\t0\t1\t1\t80\t70\t-40\t2\tBLE\tKeyboard-A\tGAMING',
      'PEER\t1\t1\t0\t60\t-1\t-55\t1\tUSB\tKeyboard-B\tBASE',
    ]);
    assert.equal(result.state.revision,7);
    assert.equal(result.state.activeMask,3);
    assert.equal(result.state.peers[0].slot,0);
    assert.equal(result.state.peers[0].ready,true);
    assert.equal(result.state.peers[0].output,'BLE');
    assert.equal(result.state.peers[0].name,'Keyboard-A');
    assert.equal(result.state.peers[0].layer,'GAMING');
    assert.equal(result.state.peers[1].ready,false);
    assert.equal(result.state.peers[1].output,'USB');
    assert.equal(result.state.peers[1].name,'Keyboard-B');
    assert.equal(result.name,'Keyboard-A');
    assert.equal(result.layer,'GAMING');
  }],
];
let failures=0;
for (const [name,test] of tests) {try {test();console.log('PASS '+name);} catch(e) {failures++;console.error('FAIL '+name+': '+e.message);}}
process.exit(failures?1:0);
}
