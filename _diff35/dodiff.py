import difflib
a = open('build35.py', encoding='utf-8').read().splitlines()
b = open('../build.py', encoding='utf-8').read().splitlines()
sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
out = []
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag == 'equal':
        continue
    out.append('=' * 20 + f' {tag} a[{i1+1}:{i2}] b[{j1+1}:{j2}] ' + '=' * 20)
    if tag in ('replace', 'delete'):
        for ln in a[i1:i2]:
            out.append('  A| ' + ln[:300])
    if tag in ('replace', 'insert'):
        for ln in b[j1:j2]:
            out.append('  B| ' + ln[:300])
open('diff.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('diff blocks written:', len(out), 'lines')
