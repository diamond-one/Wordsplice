// Tests orchestration with controlled playback/HTTP adapters; not a live capture test.
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
function harness(noAudio = false, options = {}) {
  const elements = new Map();
  const element = () => ({value:'',textContent:'',hidden:false,disabled:false,style:{},classList:{add(){},remove(){},toggle(){}},append(){},replaceChildren(){},setAttribute(){},removeAttribute(){},pause(){},addEventListener(){}});
  const get = id => {if (!elements.has(id)) elements.set(id,element());return elements.get(id);};
  Object.entries({sentence:'there are times',accent:'','phrase-size':'4',mode:'statement',texture:'warm',gap:'25'}).forEach(([id,value])=>get(id).value=value);
  let stopped = 0;
  const track = {readyState:'live', getSettings:()=>({displaySurface:'browser'}), addEventListener(){},stop(){stopped++;}};
  const calls = [];
  const context = vm.createContext({console,Blob,AbortController,URLSearchParams,setTimeout:()=>0,clearTimeout(){},
    document:{getElementById:get,createElement:element,head:{append(){}}},
    location:{origin:'http://127.0.0.1:8765'},window:{MediaRecorder:{},addEventListener(){}},
    navigator:{mediaDevices:{getDisplayMedia:async()=>({getAudioTracks:()=>noAudio?[]:[track],getVideoTracks:()=>[track],getTracks:()=>[track]})}},
    fetch:async(path,options)=>{
      if(path==='/api/status')return {json:async()=>({state:'ready'})};
      if(path==='/api/latest')return {ok:true,json:async()=>({session:null})};
      const body=options.body instanceof Blob?null:JSON.parse(options.body);calls.push({path,body});
      let result;
      if(path==='/api/session')result={id:'session',words:body.sentence.toLowerCase().match(/[a-z0-9]+(?:'[a-z0-9]+)*/g)};
      if(path==='/api/search')result={results:body.query==='there are times'?[]:[{video:'source12345',start:1,end:3,caption:body.query,url:'https://www.youtube.com/watch?v=source12345',youglish:'https://youglish.com'}]};
      if(path.startsWith('/api/clip?')){const phrase=new URLSearchParams(path.split('?')[1]).get('phrase');result={id:phrase,phrase,start:.1,end:1,duration:2,transcript:phrase,waveform:[.1,.5,1]};}
      if(path==='/api/render')result={url:'/audio/mix.wav',duration:1.825};
      return {ok:true,json:async()=>result};
    }});
  vm.runInContext(readFileSync(join(__dirname,'../app.js'),'utf8'),context);
  vm.runInContext("modelReady = true; ensurePlayer = async () => {player={pauseVideo(){}};}; recordCandidate = async () => new Blob(['test']);",context);
  return {context,get,calls,stopped:()=>stopped};
}
(async()=>{
  const good=harness();await vm.runInContext('start()',good.context);
  assert.deepEqual(good.calls.filter(c=>c.path==='/api/search').map(c=>c.body.query),['there are times','there are','times']);
  const render=good.calls.find(c=>c.path==='/api/render');
  assert.deepEqual(render.body.clips.map(c=>c.id),['there are','times']);
  assert.equal(good.get('mix-title').textContent,'Your statement is ready');
  assert.equal(good.get('download').download,'wordsplice-there-are-times.wav');
  assert.ok(good.stopped()>0,'Capture tracks released before audition');
  assert.equal(vm.runInContext('running',good.context),false);
  vm.runInContext('editClip(0)',good.context);
  assert.equal(good.get('mix-title').textContent,'Your statement is ready','Selecting a clip must preserve the completed mix status');
  assert.equal(good.get('remix').hidden,true,'A rendered mix has no unapplied edits');
  good.get('texture').value='tape70'; good.get('texture').onchange();
  assert.equal(good.get('mix-title').textContent,'Changes ready to apply');
  assert.equal(good.get('remix').hidden,false,'A sound change offers Update mix');
  assert.equal(good.get('mix-audio').hidden,true,'An outdated mix is not offered for playback');
  await vm.runInContext('renderMix()',good.context);
  assert.equal(good.get('remix').hidden,true);
  vm.runInContext("openCapture = async () => {throw Object.assign(new Error('cancelled'), {name:'NotAllowedError'});}",good.context);
  await vm.runInContext('start()',good.context);
  assert.equal(good.get('mix-audio').hidden,false,'Cancelling a new recording preserves the existing mix');
  const bad=harness(true);await vm.runInContext('start()',bad.context);
  assert.match(bad.get('error').textContent,/No tab audio/);
  assert.ok(!bad.calls.some(c=>c.path==='/api/render'));
  assert.ok(bad.stopped()>0);
  const manual=harness();manual.get('sentence').value='I want - to live - on a boat';
  await vm.runInContext('start()',manual.context);
  assert.deepEqual(manual.calls.filter(c=>c.path==='/api/search').map(c=>c.body.query),['i want','to live','on a boat']);
  assert.deepEqual(manual.calls.find(c=>c.path==='/api/render').body.clips.map(c=>c.id),['i want','to live','on a boat']);
  const strict=harness();strict.get('sentence').value='there are times - we live';
  await vm.runInContext('start()',strict.context);
  assert.deepEqual(strict.calls.filter(c=>c.path==='/api/search').map(c=>c.body.query),['there are times']);
  assert.match(strict.get('error').textContent,/Your phrase breaks are kept/);
  const edits=harness();await vm.runInContext('start()',edits.context);
  // Controlled take selection lets us exercise atomic editing without a capture permission dialog.
  vm.runInContext(`capturePhrase = async phrase => ({id:'new-'+phrase,phrase,start:.1,end:1,duration:2,pause:25,transcript:phrase,waveform:[.5],audio:'/new.wav',source:{video:'newvoice123',start:1,end:3,url:'https://www.youtube.com',youglish:'https://youglish.com'}});`,edits.context);
  await vm.runInContext("changePhrase(0, 'split', 1)",edits.context);
  assert.equal(vm.runInContext("clips.map(c=>c.phrase).join('|')",edits.context),'there|are|times');
  assert.equal(edits.get('sentence').value,'there - are - times');
  await vm.runInContext('undoEdit()',edits.context);
  assert.equal(vm.runInContext("clips.map(c=>c.phrase).join('|')",edits.context),'there are|times');
  await vm.runInContext("changePhrase(0, 'join')",edits.context);
  assert.equal(vm.runInContext("clips.map(c=>c.phrase).join('|')",edits.context),'there are times');
  await vm.runInContext('undoEdit()',edits.context);
  const before=vm.runInContext('JSON.stringify(clips)',edits.context);
  vm.runInContext("capturePhrase = async phrase => phrase === 'are' ? {unavailable:true,reason:'No take'} : {id:'temporary',phrase,start:0,end:1,duration:1,source:{video:'newvoice123'}};",edits.context);
  await vm.runInContext("changePhrase(0, 'split', 1)",edits.context);
  assert.equal(vm.runInContext('JSON.stringify(clips)',edits.context),before,'A failed second half must preserve both original clips');
  assert.match(edits.get('error').textContent,/original cuts and mix are unchanged/);
  const replacement=harness();await vm.runInContext('start()',replacement.context);
  await vm.runInContext("changePhrase(0, 'replace')",replacement.context);
  assert.match(replacement.get('error').textContent,/No unused exact-match source/,'Replacement must not reuse the current take');
  const voices=harness();voices.get('voice').value='female';voices.get('topic').value='history';await vm.runInContext('start()',voices.context);
  assert.ok(voices.calls.filter(c=>c.path==='/api/search').every(c=>c.body.voice==='female'));
  assert.equal(voices.calls.find(c=>c.path==='/api/render').body.settings.voice,'female');
  assert.equal(voices.calls.find(c=>c.path==='/api/render').body.settings.topic,'history');
  assert.ok(voices.calls.filter(c=>c.path==='/api/search').every(c=>c.body.topic==='history'));
  const beforeEdit=voices.calls.length;voices.get('voice').value='male';
  await vm.runInContext("changePhrase(0, 'split', 1)",voices.context);
  const editSearches=voices.calls.slice(beforeEdit).filter(c=>c.path==='/api/search');
  assert.equal(editSearches.length,2);assert.ok(editSearches.every(c=>c.body.voice==='male'));
  assert.ok(editSearches.every(c=>c.body.topic==='history'));
  console.log('PASS: automatic fallback; strict dash groups; split, join and undo; failed-edit rollback; replacement exclusion; voice selection; recording cleanup');
})().catch(error=>{console.error(error);process.exitCode=1;});
