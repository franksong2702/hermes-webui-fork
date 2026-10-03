"""Real renderMd boundaries: malformed predecessors and literal raw code."""
import html
from html.parser import HTMLParser
import itertools

import pytest

from tests import test_renderer_js_behaviour as _renderer

_render = _renderer._render
driver_path = _renderer.driver_path


class _Rendered(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.code = []
        self._in_code = False
        self._code_text = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs).get('href'))
        if tag == 'code':
            self._in_code = True
            self._code_text = []

    def handle_endtag(self, tag):
        if tag == 'code':
            self.code.append(''.join(self._code_text))
            self._in_code = False

    def handle_data(self, data):
        if self._in_code:
            self._code_text.append(data)


@pytest.mark.parametrize('context', ['{}', '- {}', '> {}', '| label |\n| --- |\n| {} |'])
@pytest.mark.parametrize('bad', ['[bad](https://broken.test/path ', '[bad](file:///tmp/broken path ', '[bad](https://broken.test/path [broken](https://broken.test/ '])
@pytest.mark.parametrize('good', ['[Good](https://good.test/path)', '[Good](https://good.test/a b.pdf "Title")'])
def test_malformed_predecessor_never_consumes_valid_successor(driver_path, context, bad, good):
    expected = _Rendered(_render(driver_path, context.format(good))).links
    actual = _Rendered(_render(driver_path, context.format(bad + good))).links
    assert expected[-1] in actual
    assert not any('[Good]' in (link or '') for link in actual)
    assert '[bad]' in html.unescape(_render(driver_path, context.format(bad + good)))


@pytest.mark.parametrize('context', ['See {} here', '- {}', '> {}', '| label |\n| --- |\n| {} |'])
@pytest.mark.parametrize('literal', ['[Literal](https://gw.example/a b.pdf)', 'https://gw.example/a b.pdf', '**[Literal](https://gw.example/a b.pdf)**', '[Literal](file:///tmp/a b.pdf)'])
def test_raw_code_is_literal_through_all_link_passes(driver_path, context, literal):
    rendered = _Rendered(_render(driver_path, context.format('<code>' + literal + '</code>')))
    assert rendered.links == []
    assert rendered.code == [literal]


@pytest.mark.parametrize('dest', ['https://good.test/a b.pdf', 'file:///tmp/a b.pdf', 'mailto:a@example.test', 'https://good.test/a%20b.pdf'])
def test_invalid_prefix_insertion_preserves_successor_link_property(driver_path, dest):
    target = f'[Good]({dest})'
    expected = _Rendered(_render(driver_path, target)).links
    for count, separator in itertools.product([1, 2, 8, 32], [' ', '\t']):
        prefix = ('[bad](https://broken.test/path' + separator) * count
        rendered = _Rendered(_render(driver_path, prefix + target))
        assert expected[-1] in rendered.links
        assert not any('[Good]' in (link or '') for link in rendered.links)


@pytest.mark.parametrize('literal', ['`backticks` [Literal](https://gw.example/a b.pdf)', '$x$ [Literal](https://gw.example/a b.pdf)', 'A &amp; B [Literal](https://gw.example/a b.pdf)', 'file:///tmp/a.pdf [Literal](https://gw.example/a b.pdf)'])
@pytest.mark.parametrize('wrapper', ['<code>{}</code>', '<pre><code>{}</code></pre>'])
def test_raw_preformatted_regions_keep_nested_syntax_and_entities(driver_path, literal, wrapper):
    source = _render(driver_path, wrapper.format(literal))
    rendered = _Rendered(source)
    assert rendered.links == []
    assert rendered.code == [html.unescape(literal)]
    assert '\x00' not in source


@pytest.mark.parametrize('url', ['https://good.test/a[part](name', 'https://good.test/a%5Bpart%5D%28name%29'])
def test_attached_bracket_bytes_do_not_create_a_separate_link(driver_path, url):
    source = _render(driver_path, f'[Good]({url})')
    assert _Rendered(source).links == [url]


@pytest.mark.parametrize('title', ['"See [Other](https://other.test/path)"', "'See [Other](https://other.test/path)'"])
def test_link_looking_title_text_does_not_split_a_valid_destination(driver_path, title):
    source = _render(driver_path, f'[Good](https://good.test/a b.pdf {title})')
    assert _Rendered(source).links == ['https://good.test/a b.pdf']


def test_repeated_spaced_bad_openers_keep_successor_with_bounded_growth(tmp_path):
    import json
    import subprocess
    script = _renderer._TIMING_DRIVER_SRC.replace(
        "const inputs = [('[x](').repeat(4096), ('[x](').repeat(8192)];",
        "const inputs = [2048,4096,8192].map(n => '[bad](https://broken.test/path '.repeat(n)+'[Good](https://good.test/path)');",
    )
    path = tmp_path / 'bad_opener_growth.js'
    path.write_text(script)
    result = subprocess.run([_renderer.NODE, str(path), str(_renderer.UI_JS_PATH)], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    small, middle, large = json.loads(result.stdout)['medians']
    assert large < 500, (small, middle, large)
    assert large <= max(3 * middle, 3 * small, 20), (small, middle, large)
