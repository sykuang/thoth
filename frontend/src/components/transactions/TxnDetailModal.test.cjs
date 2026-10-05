const assert = require('node:assert/strict');
const {test} = require('node:test');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {QueryClient, QueryClientProvider} = require('@tanstack/react-query');
function stub(id, exports) {
  const filename = require.resolve(id);
  require.cache[filename] = {id:filename, filename, loaded:true, exports};
}
const host = ({children, visible, testID}) => visible === false ? null
  : React.createElement('div', {'data-testid':testID}, children);
stub('react-native', {...Object.fromEntries(['View','Text','Pressable','ScrollView','Modal','TextInput','ActivityIndicator'].map(name => [name,host])), Platform:{OS:'web'}});
for (const name of ['CategoryPicker','Dropdown','TagPicker']) stub(`../${name}`, {[name]:host});
const {SplitEditor, toApiSplits, toDraftSplits, emptyDraftSplit} = require('./SplitEditor');
const parent = {
  id:42, bank:'cathay', kind:'twd', date:'2026-01-01', description:'synthetic purchase',
  amount:-600, cashflow_amount:-600, cashflow_direction:'expense', currency:'TWD', account_or_card:null,
  category:'購物', subcategory:'日用品', description_overwrite:'parent description', tags:['fixture'], auto_excluded:false,
  splits:[
    {amount:100,category:'飲食',subcategory:'早餐',note:'first',auto_excluded:false},
    {amount:200,category:'交通',subcategory:'捷運',note:'second',auto_excluded:false},
    {amount:300,category:null,subcategory:null,note:'third',auto_excluded:true},
  ],
};
const child = (index = 1) => ({...parent, id:`42#${index}`, split_of:42, split_index:index, splits:undefined,
  category:parent.splits[index]?.category ?? null, subcategory:parent.splits[index]?.subcategory ?? null});
let effects = [];
// SSR has real state/rerenders but no commit phase. Queue only this component's
// effects, then flush before driving its production callbacks; no hook-slot mocks.
const effectReact = {...React, useEffect(effect, deps) {
  const previous = React.useRef();
  if (!previous.current || deps?.some((value, i) => !Object.is(value, previous.current[i]))) {
    previous.current = deps;
    effects.push(effect);
  }
}};
effectReact.useLayoutEffect = effectReact.useEffect;
const calls = [];
stub('../../lib/api', {api:async (path, options) => {calls.push({path,...options}); return parent;}, formatApiError:e=>e.message});
stub('../../hooks/useOwnerBoundApi', {useOwnerBoundApi:()=>assert.fail('bank detail does not use loan hook')});
stub('../../lib/replica', {assertReplicaOwnerEpoch:()=>{}});
stub('react', effectReact);
const {TxnDetailModal} = require('./TxnDetailModal');
stub('react', React);
function nodes(tree) {
  return React.Children.toArray(tree).flatMap(node => React.isValidElement(node) ? [node,...nodes(node.props.children)] : []);
}
function expandSplits(tree, splitTree) {
  if (!React.isValidElement(tree)) return tree;
  if (tree.type === SplitEditor) return splitTree;
  return React.cloneElement(tree, {}, React.Children.map(tree.props.children, child=>expandSplits(child,splitTree)));
}
const control = (tree, id) => {
  const found = nodes(tree).find(node => node.props.testID === `txn-detail-${id}`);
  assert.ok(found, `missing control ${id}`);
  return found.props;
};
function setup(txn = child(), data = parent) {
  calls.length = 0;
  const client = new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity,gcTime:Infinity},mutations:{retry:false}}});
  if (data) client.setQueryData(['transactions','detail',txn.bank,txn.kind,txn.split_of ?? txn.id], data);
  client.setQueryData(['rules','categories'], {categories:['飲食','交通','購物']});
  for (const category of ['飲食','交通','購物']) client.setQueryData(['rules','subcategories',category], {subcategories:['早餐','晚餐','捷運','日用品']});
  const listKey = ['transactions','fixture'];
  const statsKey = ['transactions','stats','fixture'];
  const replicaKey = ['frontend-dataset','fixture'];
  client.setQueryData(listKey, {items:[txn]});
  client.setQueryData(statsKey, {by_category:{交通:1},amount_by_category:{交通:600}});
  client.setQueryData(replicaKey, {transactions:[txn]});
  const session = {client,txn,listKey,statsKey,replicaKey,closes:0};
  session.render = actions => {
    function Driver() {
      const step = React.useRef(0);
      const [,rerender] = React.useState(0);
      const element = TxnDetailModal({txn:session.txn, fxMode:'original', onClose:()=>{session.closes++;}});
      const rawTree = element.type(element.props);
      const editor = nodes(rawTree).find(node=>node.type === SplitEditor);
      // Keep the nested component's hooks in a constant slot even while hidden.
      const splitTree = SplitEditor(editor?.props ?? {drafts:[],parentAmount:0,categoryOptions:[],onChange:()=>{}});
      const tree = expandSplits(rawTree, splitTree);
      session.beforeEffects?.(tree);
      const pending = effects;
      effects = [];
      if (pending.length) {pending.forEach(effect=>effect()); rerender(n=>n+1);}
      else if (step.current < actions.length) {actions[step.current++](tree); rerender(n=>n+1);}
      return tree;
    }
    return renderToStaticMarkup(React.createElement(QueryClientProvider,{client},React.createElement(Driver)));
  };
  session.settle = async save => {
    save();
    await new Promise(resolve=>setTimeout(resolve,20));
    const mutation = client.getMutationCache().getAll().at(-1);
    assert.equal(mutation?.state.status, 'success', mutation?.state.error?.message);
    return calls.at(-1);
  };
  return session;
}
function splitControl(tree, id) {
  return control(tree, `split-${id}`);
}

test('clicked child top pickers save full splits, preserving parent and siblings without optimistic category arithmetic', async () => {
  const s = setup();
  let save;
  s.render([
    tree => {
      assert.equal(control(tree,'category-dropdown').value, '交通');
      assert.equal(control(tree,'subcategory-dropdown').value, '捷運');
      assert.equal(control(tree,'save-btn').disabled, true);
      control(tree,'category-dropdown').onChange('飲食');
    },
    tree => {
      assert.equal(control(tree,'subcategory-dropdown').value, '');
      assert.equal(splitControl(tree,'category-1').value, '飲食');
      control(tree,'subcategory-dropdown').onChange('晚餐');
    },
    tree => {assert.equal(control(tree,'save-btn').disabled,false);save=control(tree,'save-btn').onPress;},
  ]);
  const request = await s.settle(save);
  assert.equal(request.path, '/transactions/cathay/twd/42');
  assert.deepEqual(request.body, {
    description_overwrite:parent.description_overwrite,tags:parent.tags,auto_excluded:false,
    splits:[parent.splits[0],{...parent.splits[1],category:'飲食',subcategory:'晚餐'},parent.splits[2]],
  });
  assert.deepEqual(s.client.getQueryData(s.listKey).items,[s.txn]);
  assert.deepEqual(s.client.getQueryData(s.statsKey).by_category,{交通:1});
  for (const key of [s.listKey,s.statsKey,s.replicaKey]) assert.equal(s.client.getQueryState(key).isInvalidated,true);
  s.client.clear();
});

test('subcategory-only edit is dirty and saves only the clicked part', async () => {
  const s = setup();
  let save;
  s.render([
    tree=>control(tree,'subcategory-dropdown').onChange('公車'),
    tree=>{assert.equal(control(tree,'save-btn').disabled,false);save=control(tree,'save-btn').onPress;},
  ]);
  const {body} = await s.settle(save);
  assert.equal(Object.hasOwn(body,'category'),false);
  assert.equal(Object.hasOwn(body,'subcategory'),false);
  assert.deepEqual(body.splits,[parent.splits[0],{...parent.splits[1],subcategory:'公車'},parent.splits[2]]);
  assert.deepEqual(s.client.getQueryData(s.listKey).items,[s.txn]);
  s.client.clear();
});

for (const next of ['飲食','']) test(`per-part main category ${JSON.stringify(next)} clears only its own incompatible subcategory`, async () => {
  const s = setup();
  let save;
  s.render([
    tree=>splitControl(tree,'category-1').onChange('交通'),
    tree=>{
      assert.equal(control(tree,'subcategory-dropdown').value,'捷運');
      assert.equal(control(tree,'save-btn').disabled,true);
      splitControl(tree,'category-1').onChange(next);
    },
    tree=>{
      assert.equal(control(tree,'category-dropdown').value,next);
      if (next) assert.equal(control(tree,'subcategory-dropdown').value,'');
      save=control(tree,'save-btn').onPress;
    },
  ]);
  const {body} = await s.settle(save);
  assert.deepEqual(body.splits,[parent.splits[0],{...parent.splits[1],category:next || null,subcategory:null},parent.splits[2]]);
  s.client.clear();
});

for (const index of [undefined,null,-1,3,1.5,'1',NaN]) test(`invalid split index ${String(index)} cannot save even through a retained handler`, async () => {
  const s = setup({...child(),split_index:index});
  let save;
  s.render([
    tree=>control(tree,'desc-input').onChangeText('unsaved'),
    tree=>{assert.equal(control(tree,'save-btn').disabled,true);save=control(tree,'save-btn').onPress;},
  ]);
  save();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.deepEqual(calls,[]);
  s.client.clear();
});

test('loading parent rejects direct save rather than writing a blank parent', async () => {
  const s = setup(child(), null);
  let save;
  s.render([tree=>{assert.equal(control(tree,'save-btn').disabled,true);save=control(tree,'save-btn').onPress;}]);
  save();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.deepEqual(calls,[]);
  s.client.clear();
});

test('removing a preceding part keeps the clicked part selected after reindexing', async () => {
  const s = setup();
  let save;
  s.render([
    tree=>splitControl(tree,'remove-0').onPress(),
    tree=>{
      assert.equal(control(tree,'category-dropdown').value,'交通');
      assert.equal(control(tree,'subcategory-dropdown').value,'捷運');
      control(tree,'subcategory-dropdown').onChange('公車');
    },
    tree=>splitControl(tree,'fill-0').onPress(),
    tree=>{assert.equal(control(tree,'save-btn').disabled,false);save=control(tree,'save-btn').onPress;},
  ]);
  const {body} = await s.settle(save);
  assert.deepEqual(body.splits,[{...parent.splits[1],amount:300,subcategory:'公車'},parent.splits[2]]);
  assert.equal(Object.hasOwn(body,'category'),false);
  s.client.clear();
});

for (const action of ['remove-1','clear']) test(`selected part ${action} never retargets top pickers but structural save remains possible`, async () => {
  const s = setup();
  let save;
  s.render([
    tree=>splitControl(tree,action).onPress(),
    tree=>{
      assert.equal(control(tree,'category-dropdown').disabled,true);
      assert.equal(control(tree,'category-dropdown').value,'');
      control(tree,'category-dropdown').onChange('不應寫入');
      if (action !== 'clear') splitControl(tree,'fill-0').onPress();
    },
    tree=>{assert.equal(control(tree,'save-btn').disabled,false);save=control(tree,'save-btn').onPress;},
  ]);
  const {body} = await s.settle(save);
  assert.deepEqual(body.splits,action === 'clear' ? [] : [{...parent.splits[0],amount:300},parent.splits[2]]);
  assert.equal(Object.hasOwn(body,'category'),false);
  assert.equal(Object.hasOwn(body,'subcategory'),false);
  s.client.clear();
});

test('same-parent child switch resets all drafts and rejects the previous save callback', async () => {
  const s = setup();
  let oldSave, save;
  s.render([
    tree=>{control(tree,'subcategory-dropdown').onChange('unsaved');control(tree,'desc-input').onChangeText('stale description');},
    tree=>{oldSave=control(tree,'save-btn').onPress;s.txn=child(0);},
    tree=>{
      assert.equal(control(tree,'category-dropdown').value,'飲食');
      assert.equal(control(tree,'subcategory-dropdown').value,'早餐');
      assert.equal(control(tree,'desc-input').value,parent.description_overwrite);
      assert.equal(control(tree,'save-btn').disabled,true);
      oldSave();
      control(tree,'subcategory-dropdown').onChange('晚餐');
    },
    tree=>{save=control(tree,'save-btn').onPress;},
  ]);
  const {body} = await s.settle(save);
  assert.equal(calls.length,1);
  assert.deepEqual(body.splits,[{...parent.splits[0],subcategory:'晚餐'},parent.splits[1],parent.splits[2]]);
  s.client.clear();
});

test('before initialization and during parent loading transitions no stale draft can save', async () => {
  const s = setup();
  let oldSave;
  let expectedBlock = true;
  s.beforeEffects = tree => {
    if (expectedBlock) {assert.equal(control(tree,'save-btn').disabled,true);control(tree,'save-btn').onPress();expectedBlock=false;}
  };
  s.render([
    tree=>control(tree,'subcategory-dropdown').onChange('unsaved'),
    tree=>{oldSave=control(tree,'save-btn').onPress;expectedBlock=true;s.txn={...child(),id:'99#1',split_of:99};},
    tree=>{assert.equal(control(tree,'save-btn').disabled,true);oldSave();s.client.setQueryData(['transactions','detail','cathay','twd',99],{...parent,id:99});expectedBlock=true;},
    tree=>{assert.equal(control(tree,'subcategory-dropdown').value,'捷運');assert.equal(control(tree,'save-btn').disabled,true);oldSave();},
  ]);
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.deepEqual(calls,[]);
  s.client.clear();
});

test('a top-picker callback retained across selected-part removal cannot edit the next part', async () => {
  const s = setup();
  let oldCategory, oldSubcategory, save;
  s.render([
    tree=>{oldCategory=control(tree,'category-dropdown').onChange;oldSubcategory=control(tree,'subcategory-dropdown').onChange;splitControl(tree,'remove-1').onPress();},
    tree=>{oldCategory('交通');oldSubcategory('不應寫入');},
    tree=>splitControl(tree,'fill-0').onPress(),
    tree=>{save=control(tree,'save-btn').onPress;},
  ]);
  const {body} = await s.settle(save);
  assert.deepEqual(body.splits,[{...parent.splits[0],amount:300},parent.splits[2]]);
  s.client.clear();
});

test('uncategorized child stays explicit null; same main category retains its subcategory', async () => {
  const s = setup(child(2));
  let save;
  s.render([
    tree=>{
      assert.equal(control(tree,'category-dropdown').value,'');
      assert.equal(control(tree,'category-dropdown').disabled,false);
      assert.equal(nodes(tree).some(n=>n.props.testID === 'txn-detail-subcategory-dropdown'),false);
      control(tree,'desc-input').onChangeText('parent-only description');
    },
    tree=>{save=control(tree,'save-btn').onPress;},
  ]);
  const {body} = await s.settle(save);
  assert.deepEqual(body.splits,parent.splits);
  assert.equal(Object.hasOwn(body,'category'),false);
  assert.equal(Object.hasOwn(body,'subcategory'),false);
  assert.deepEqual(s.client.getQueryData(s.listKey).items,[s.txn], 'even unrelated child saves skip the parent optimistic shortcut');
  s.client.clear();
});

for (const field of ['note','ignore','amount']) test(`independent ${field} draft change is dirty and preserves all subcategories`, async () => {
  const s = setup();
  let save;
  const expected = structuredClone(parent.splits);
  const actions = [];
  if (field === 'note') {actions.push(tree=>splitControl(tree,'note-0').onChangeText('new note'));expected[0].note='new note';}
  if (field === 'ignore') {actions.push(tree=>splitControl(tree,'ignore-0').onPress());expected[0].auto_excluded=true;}
  if (field === 'amount') {
    actions.push(tree=>splitControl(tree,'amount-0').onChangeText('150'));
    actions.push(tree=>{assert.equal(control(tree,'save-btn').disabled,true);splitControl(tree,'amount-1').onChangeText('150');});
    expected[0].amount=150;expected[1].amount=150;
  }
  s.render([...actions,tree=>{assert.equal(control(tree,'save-btn').disabled,false);save=control(tree,'save-btn').onPress;}]);
  assert.deepEqual((await s.settle(save)).body.splits,expected);
  s.client.clear();
});

for (const splits of [[],parent.splits]) test(`original parent editing (${splits.length} splits) does not inherit categories into parts`, async () => {
  const txn = {...parent,splits};
  const s = setup(txn);
  let save;
  s.render([
    tree=>{assert.equal(control(tree,'category-dropdown').value,'購物');control(tree,'category-dropdown').onChange('購物');},
    tree=>{assert.equal(control(tree,'subcategory-dropdown').value,'日用品');assert.equal(control(tree,'save-btn').disabled,true);control(tree,'category-dropdown').onChange('飲食');},
    tree=>{assert.equal(control(tree,'subcategory-dropdown').value,'');control(tree,'subcategory-dropdown').onChange('晚餐');},
    tree=>{save=control(tree,'save-btn').onPress;},
  ]);
  const {body} = await s.settle(save);
  assert.equal(body.category,'飲食');assert.equal(body.subcategory,'晚餐');
  assert.deepEqual(body.splits,splits);
  if (!splits.length) assert.equal(s.client.getQueryData(s.listKey).items[0].category,'飲食','original unsplit optimistic update remains');
  s.client.clear();
});

test('cancel splitting then start/add does not select a replacement for the removed child', async () => {
  const s = setup();
  let save;
  s.render([
    tree=>splitControl(tree,'clear').onPress(),
    tree=>splitControl(tree,'start').onPress(),
    tree=>splitControl(tree,'amount-0').onChangeText('200'),
    tree=>splitControl(tree,'amount-1').onChangeText('200'),
    tree=>splitControl(tree,'add').onPress(),
    tree=>splitControl(tree,'fill-2').onPress(),
    tree=>{assert.equal(control(tree,'category-dropdown').disabled,true);assert.equal(control(tree,'save-btn').disabled,false);save=control(tree,'save-btn').onPress;},
  ]);
  const {body} = await s.settle(save);
  assert.deepEqual(body.splits,Array.from({length:3},()=>({amount:200,category:null,subcategory:null,note:null,auto_excluded:false})));
  assert.equal(Object.hasOwn(body,'category'),false);
  s.client.clear();
});

for (const transition of ['selection','refetch']) {
  for (const field of ['category','note']) test(`retained per-part ${field} callback after ${transition} cannot replace fresh drafts`, async () => {
    const s = setup();
    const fresh = structuredClone(parent);
    const index = transition === 'selection' ? 0 : 1;
    if (transition === 'refetch') fresh.splits[2] = {...fresh.splits[2],category:'購物',subcategory:'日用品',note:'refreshed sibling'};
    let retained, save;
    s.render([
      tree=>{
        const part = splitControl(tree,`${field}-1`);
        retained = field === 'category' ? part.onChange : part.onChangeText;
        if (transition === 'selection') s.txn = child(index);
        else s.client.setQueryData(['transactions','detail','cathay','twd',42],fresh);
      },
      ()=>retained('stale callback'),
      tree=>{
        assert.equal(control(tree,'save-btn').disabled,true,'stale callback must not dirty the new context');
        control(tree,'subcategory-dropdown').onChange('晚餐');
      },
      tree=>{save=control(tree,'save-btn').onPress;},
    ]);
    fresh.splits[index].subcategory = '晚餐';
    assert.deepEqual((await s.settle(save)).body.splits,fresh.splits);
    assert.equal(calls.length,1);
    s.client.clear();
  });
}

test('successful save closes immediately before a parent refetch can replace the edit context', async () => {
  const s = setup();
  let save;
  s.render([
    tree=>control(tree,'subcategory-dropdown').onChange('公車'),
    tree=>{save=control(tree,'save-btn').onPress;},
  ]);
  await s.settle(save);
  assert.equal(s.closes,1);
  s.client.clear();
});

test('real split converters round-trip every subcategory, including uncategorized parts', () => {
  assert.deepEqual(toApiSplits(toDraftSplits(parent.splits)), parent.splits);
  assert.equal(emptyDraftSplit().subcategory, '');
});
