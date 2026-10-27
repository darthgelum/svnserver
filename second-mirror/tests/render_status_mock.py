"""Build a self-contained, network-free interactive status mock from the UI."""
from pathlib import Path
import re
import sys

web = Path(__file__).resolve().parents[1] / 'web'
output = Path(sys.argv[1])
markup = (web / 'index.html').read_text(encoding='utf-8').split('<body>', 1)[1].split('<script src="app.js">', 1)[0]
css = (web / 'style.css').read_text(encoding='utf-8')
css = css.replace(':root{color-scheme:light dark;', '#mirror-status{color-scheme:inherit;')
css = re.sub(r'body\{[^}]*\}', '', css)
css = css.replace('*{box-sizing:border-box}', '#mirror-status *{box-sizing:border-box}')
css = css.replace('min-height:100vh', 'min-height:0')
script = (web / 'app.js').read_text(encoding='utf-8')
script = re.sub(r'// LIVE_ONLY_START.*?// LIVE_ONLY_END', '', script, flags=re.S)
assert 'fetch(' not in script
fragment = ('<style>\n' + css + '\n</style>\n' + markup + '\n<script>\n' +
            (web / 'demo.js').read_text(encoding='utf-8') + '\n' + script + '\n</script>\n')
output.write_text(fragment, encoding='utf-8')
print(output)
