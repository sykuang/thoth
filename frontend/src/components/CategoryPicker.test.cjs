const assert = require('node:assert/strict');
const {test} = require('node:test');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
function stub(id, exports) {
  const filename = require.resolve(id);
  require.cache[filename] = {id:filename,filename,loaded:true,exports};
}
const host = ({children, visible, testID, style}) => {
  if (style?.backgroundColor !== undefined) assert.equal(typeof style.backgroundColor, 'string');
  return visible === false ? null : React.createElement('div', {'data-testid':testID}, children);
};
stub('react-native', {...Object.fromEntries(['View','Text','Pressable','ScrollView','Modal'].map(name => [name,host])),
  Platform:{OS:'web'},useWindowDimensions:()=>({width:400,height:800})});
stub('lucide-react-native', new Proxy({}, {get:(_, name) => name === '__esModule' ? true :
  () => React.createElement('span', {'data-icon':String(name)})}));
const {CategoryPicker} = require('./CategoryPicker');
const {categoryColor,categoryEmoji} = require('../lib/category-color');
function nodes(tree) {
  return React.Children.toArray(tree).flatMap(n => React.isValidElement(n) ? [n,...nodes(n.props.children)] : []);
}
for (const category of [...Object.getOwnPropertyNames(Object.prototype), '自訂分類']) {
  test(`real category picker and presentation accept ${category}`, () => {
    let pick;
    let selected;
    function Driver() {
      const step = React.useRef(0);
      const [, update] = React.useState(0);
      const tree = CategoryPicker({value:category, options:[{label:'飲食',value:'飲食'},{label:category,value:category}],
        onChange:value=>{selected=value;},testID:'category-probe'});
      const all = nodes(tree);
      if (step.current === 0) all.find(n => n.props.testID === 'category-probe').props.onPress();
      if (step.current === 1) all.find(n => n.props.testID === 'category-probe-tab-neutral')?.props.onPress();
      if (step.current === 2) pick = all.find(n => n.props.testID === `category-probe-option-${category}`).props.onPress;
      if (step.current++ < 2) update(step.current);
      return tree;
    }
    const html = renderToStaticMarkup(React.createElement(Driver));
    assert.ok(html.includes(category));
    if (!category.startsWith('__')) assert.ok(html.includes('data-icon="Package"'));
    pick();
    assert.equal(selected, category);
    const color = categoryColor(category);
    assert.equal(typeof color?.bg, 'string');
    assert.equal(typeof color?.text, 'string');
    assert.equal(categoryEmoji(category), '📦');
  });
}
test('known category styling remains unchanged', () => {
  assert.equal(categoryEmoji('飲食'), '🍱');
  assert.equal(categoryColor('飲食').bg, 'bg-orange-100 dark:bg-orange-950/40');
});
