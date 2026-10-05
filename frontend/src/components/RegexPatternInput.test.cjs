const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

// Only the native host boundary is stubbed; hooks and callbacks are real React.
let fontScale = 1;
let width = 240;
const platform = { OS: 'ios' };
const native = require.resolve('react-native');
require.cache[native] = { id: native, filename: native, loaded: true, exports: {
  View: 'view', Text: 'label', TextInput: 'input', Platform: platform,
  useWindowDimensions: () => ({ fontScale, width }),
} };
function render(checks, initialValue = '') {
  const { RegexPatternInput } = require('./RegexPatternInput');
  let checked = 0;
  function Driver() {
    const [value, setValue] = React.useState(initialValue);
    const step = React.useRef(0);
    const tree = RegexPatternInput({ value, onChangeText: setValue, className: 'existing-input', testID: 'regex-canary' });
    const children = React.Children.toArray(tree.props.children);
    const input = children.find((node) => node.type === 'input');
    assert.ok(input, 'editor must use native TextInput');
    if (step.current < checks.length) { checked++; checks[step.current++](input.props, children); }
    return null;
  }
  renderToStaticMarkup(React.createElement(Driver));
  assert.equal(checked, checks.length, 'every state transition must render');
}

test('regex editor wraps with a persistent label and preserves literal edits', () => {
  const raw = '  ^(?:北捷|台鐵)\\d+|["\'--]\\n\r\n尾端  ';
  render([
    (props, children) => {
      assert.equal(props.multiline, true);
      assert.equal(props.textAlignVertical, 'top');
      assert.equal(props.accessibilityLabel, '比對規則（Regex）');
      assert.ok(children.some((node) => node.type === 'label' && node.props.children === '比對規則（Regex）'));
      assert.equal(props.testID, 'regex-canary');
      assert.equal(props.className, 'existing-input');
      assert.equal(props.autoCapitalize, 'none');
      assert.equal(props.autoCorrect, false);
      assert.equal(props.spellCheck, false);
      assert.equal(props.smartInsertDelete, false);
      assert.equal(props.keyboardType, undefined);
      assert.equal(props.submitBehavior, 'newline');
      assert.notEqual(props.allowFontScaling, false);
      assert.equal(props.value, raw);
      props.onChangeText(`${raw}\n| pasted\\s+ `);
    },
    (props) => { assert.equal(props.value, `${raw}\n| pasted\\s+ `); props.onChangeText(''); },
    (props) => assert.equal(props.value, ''),
  ], raw);
});

test('web editor remeasures shared edits, prop replacements, resets and dimensions', () => {
  platform.OS = 'web';
  fontScale = 1;
  const layoutEffect = React.useLayoutEffect;
  let pending = [];
  // SSR has no DOM commit: capture only layout effects, retaining real state/ref hooks.
  React.useLayoutEffect = (effect, deps) => {
    const previous = React.useRef();
    if (!previous.current || deps.some((dep, i) => !Object.is(dep, previous.current[i]))) {
      pending.push(effect);
    }
    previous.current = deps;
  };
  const hosts = ['create', 'preview'].map((name) => ({
    name, value: '', style: {}, previousHeight: undefined,
    get scrollHeight() {
      const natural = this.value.length > 100 ? 400 : this.value.length > 40 ? (width < 240 ? 160 : 120) : 40;
      return this.style.height === 'auto' ? natural : Math.max(natural, parseFloat(this.style.height) - 2);
    },
  }));
  let checked = 0;
  try {
    const { RegexPatternInput } = require('./RegexPatternInput');
    function Driver() {
      const [value, setValue] = React.useState('');
      const [step, setStep] = React.useState(0);
      const inputs = hosts.map(() => {
        const tree = RegexPatternInput({ value, onChangeText: setValue });
        return React.Children.toArray(tree.props.children).find((node) => node.type === 'input').props;
      });
      inputs.forEach((props, i) => {
        const host = hosts[i];
        host.value = props.value;
        // React only writes a style property when its declared value changes.
        if (host.previousHeight !== props.style.height) host.style.height = `${props.style.height}px`;
        host.previousHeight = props.style.height;
        if (props.ref) props.ref.current = host;
      });
      const effects = pending;
      pending = [];
      effects.forEach((effect) => effect());
      const heights = [102, 182, 102, 182, 102, 182, 102, 182, 310, 166, 122, 162, 122];
      hosts.forEach((host) => assert.equal(host.style.height, `${heights[step]}px`, `${host.name}, transition ${step}`));
      checked++;
      if (step === 0) {
        // Let the old native-size path grow too, so RED isolates the prop-shrink bug.
        inputs.forEach((props) => props.onContentSizeChange?.({ nativeEvent: { contentSize: { height: 400 } } }));
        inputs[0].onChangeText('long'.repeat(100));
      } else if (step === 1) setValue('short'); // programmatic replacement
      else if (step === 3) inputs[0].onChangeText('short'); // sibling receives only props
      else if (step === 5 || step === 8) setValue(''); // create-success reset
      else if (step === 7) fontScale = 1.8; // value unchanged: size limits must be dependencies
      else if (step === 9) { fontScale = 1; setValue('m'.repeat(50)); }
      else if (step === 10) width = 120; // unchanged value wraps after viewport resize
      else if (step === 11) width = 240;
      else if (step < 9) setValue('long'.repeat(100));
      if (step < heights.length - 1) setStep(step + 1);
      return null;
    }
    renderToStaticMarkup(React.createElement(Driver));
    assert.equal(checked, 13, 'every mounted transition must render');
  } finally {
    React.useLayoutEffect = layoutEffect;
    platform.OS = 'ios';
    fontScale = 1;
    width = 240;
  }
});

test('edit, create and preview use the same regex editor', () => {
  const source = readFileSync(require.resolve('../app/(tabs)/settings/categories'), 'utf8');
  const fields = source.match(/<RegexPatternInput\b[\s\S]*?\/>/g) ?? [];
  assert.equal(fields.length, 3);
  for (const mode of ['edit', 'create', 'preview']) {
    assert.ok(fields.some((field) => field.includes(`testID="rules-${mode}-pattern"`)));
  }
});

for (const scale of [1, 1.8]) test(`height grows from four to eight scaled lines, then scrolls (${scale}x)`, () => {
  fontScale = scale;
  const height = (lines) => 20 * scale * lines + 22; // padding + border, matching text-body
  const resize = (props, lines) => props.onContentSizeChange({ nativeEvent: { contentSize: { width: 240, height: height(lines) - 2 } } });
  render([
    (props) => {
      assert.equal(props.style?.height, height(4));
      assert.equal(props.scrollEnabled, false);
      resize(props, 6);
    },
    (props) => { assert.equal(props.style.height, height(6)); assert.equal(props.scrollEnabled, false); resize(props, 8); },
    (props) => { assert.equal(props.style.height, height(8)); assert.equal(props.scrollEnabled, false); resize(props, 12); },
    (props) => { assert.equal(props.style.height, height(8)); assert.equal(props.scrollEnabled, true); resize(props, 1); },
    (props) => { assert.equal(props.style.height, height(4)); assert.equal(props.scrollEnabled, false); },
  ]);
});
