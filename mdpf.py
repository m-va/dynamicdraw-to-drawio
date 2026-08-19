#!/usr/bin/env python3
"""Dynamic Draw の .mdpf から OLE 数式 (MathType MTEF) を取り出して LaTeX にする。

.mdpf は PNG コンテナで、図面データは私的チャンク mdRw に zlib 圧縮された
MFC のシリアライズとして入っている。その中に OLE 部品が複合ドキュメント
(CFB) として並んでおり、各 CFB の直前には部品の外接矩形が double 4 個
(left, top, right, bottom, 単位 mm) で置かれている。

    ... [外接矩形 double x4][ヘッダ][CFB: D0CF11E0...] ...

ヘッダの長さは通常 26 バイトだが、その種類で最初に現れる部品だけは MFC が
クラス名を書き出す分だけ長くなる (find_rect 参照)。

CFB の中には Microsoft 数式 3.0 / MathType の "Equation Native" ストリームが
あり、その 28 バイトのヘッダに続いて MTEF (MathType Equation Format) v3 の
レコード列が入っている。ここではその MTEF を LaTeX に変換する。

MTEF の仕様は rtf2latex2e の MTEF3 解説 および MathType SDK ドキュメントを参照。
"""
import re
import struct
import zlib

CFB_SIG = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
EQN_SIG = b'\x1c\x00\x00\x00\x02\x00'          # Equation Native ヘッダ

# レコード種別 (タグ下位 4bit)
END, LINE, CHAR, TMPL, PILE, MATRIX, EMBELL, RULER, FONT, SIZE = range(10)

# 記号 -> LaTeX。MTEF v3 の文字コードは Unicode なのでこれで足りる
SYMBOLS = {
    0x03b1: r'\alpha', 0x03b2: r'\beta', 0x03b3: r'\gamma', 0x03b4: r'\delta',
    0x03b5: r'\epsilon', 0x03b6: r'\zeta', 0x03b7: r'\eta', 0x03b8: r'\theta',
    0x03b9: r'\iota', 0x03ba: r'\kappa', 0x03bb: r'\lambda', 0x03bc: r'\mu',
    0x03bd: r'\nu', 0x03be: r'\xi', 0x03c0: r'\pi', 0x03c1: r'\rho',
    0x03c3: r'\sigma', 0x03c4: r'\tau', 0x03c5: r'\upsilon', 0x03c6: r'\phi',
    0x03c7: r'\chi', 0x03c8: r'\psi', 0x03c9: r'\omega',
    0x0393: r'\Gamma', 0x0394: r'\Delta', 0x0398: r'\Theta', 0x039b: r'\Lambda',
    0x039e: r'\Xi', 0x03a0: r'\Pi', 0x03a3: r'\Sigma', 0x03a6: r'\Phi',
    0x03a8: r'\Psi', 0x03a9: r'\Omega',
    0x00b1: r'\pm', 0x00d7: r'\times', 0x00f7: r'\div', 0x2260: r'\neq',
    0x2264: r'\leq', 0x2265: r'\geq', 0x2248: r'\approx', 0x2261: r'\equiv',
    0x221e: r'\infty', 0x2202: r'\partial', 0x2207: r'\nabla', 0x221a: r'\sqrt',
    0x222b: r'\int', 0x2211: r'\sum', 0x220f: r'\prod', 0x2192: r'\to',
    0x2190: r'\leftarrow', 0x2191: r'\uparrow', 0x2193: r'\downarrow',
    0x2194: r'\leftrightarrow', 0x21d2: r'\Rightarrow', 0x21d4: r'\Leftrightarrow',
    0x2208: r'\in', 0x2209: r'\notin', 0x2286: r'\subseteq', 0x2282: r'\subset',
    0x222a: r'\cup', 0x2229: r'\cap', 0x2205: r'\emptyset', 0x2200: r'\forall',
    0x2203: r'\exists', 0x00b7: r'\cdot', 0x22c5: r'\cdot', 0x2032: "'",
    0x2026: r'\dots', 0x00b0: r'^{\circ}', 0x2220: r'\angle', 0x223c: r'\sim',
    0x2245: r'\cong', 0x221d: r'\propto', 0x22a5: r'\perp', 0x2225: r'\parallel',
    0x2212: '-', 0x2010: '-', 0x2013: '-', 0x00a0: r'\,',
    # MathType は空白を MT Extra フォントの私用領域の文字で表す
    0xeb01: r'\,', 0xeb02: r'\;', 0xeb03: r'\quad ', 0xeb04: r'\qquad ',
    0xeb05: r'\,', 0xeb06: r'\,', 0xeb07: r'\!', 0xeb08: r'\,',
}
ESCAPES = {'%': r'\%', '&': r'\&', '#': r'\#', '_': r'\_', '$': r'\$',
           '{': r'\{', '}': r'\}', '~': r'\sim ', '^': r'\hat{}'}

# 括弧テンプレート (selector 0-12) の左右
FENCES = {
    0: (r'\langle ', r'\rangle '), 1: ('(', ')'), 2: (r'\{', r'\}'),
    3: ('[', ']'), 4: ('|', '|'), 5: (r'\|', r'\|'),
    6: ('(', ']'), 7: ('[', ')'), 8: (r'\lfloor ', r'\rfloor '),
    9: (r'\lceil ', r'\rceil '), 10: ('(', '.'), 11: ('.', ')'), 12: ('|', '.'),
}


FRAC_CMD = chr(92) + 'frac'


class Unsupported(Exception):
    """LaTeX に落とせない構造に当たった。"""


# ------------------------------------------------------------- MTEF 解析 ---
class Mtef:
    def __init__(self, data):
        self.b = data
        self.i = 5                      # version/platform/product/version/subversion
        self.ok = True                  # 未対応構造に当たったら False

    # -- 低レベル
    def byte(self):
        if self.i >= len(self.b):
            raise Unsupported('データ終端')
        v = self.b[self.i]
        self.i += 1
        return v

    def word(self):
        v = struct.unpack('<H', self.b[self.i:self.i + 2])[0]
        self.i += 2
        return v

    def nudge(self):
        dx = self.byte()
        dy = self.byte()
        if dx == 128 and dy == 128:     # 大きなずらし量は 16bit で続く
            self.word()
            self.word()

    # -- レコード列 (END まで)
    def objects(self):
        out = []
        while True:
            if self.i >= len(self.b):
                break
            tag = self.byte()
            typ, opt = tag & 0x0F, tag >> 4
            if typ == END:
                break
            elif typ == LINE:
                if opt & 0x8:
                    self.nudge()
                if opt & 0x4:
                    self.word()         # 行間
                if opt & 0x2:
                    self.ruler()
                out.append([] if opt & 0x1 else self.objects())
            elif typ == CHAR:
                if opt & 0x8:
                    self.nudge()
                self.byte()             # typeface (+128 バイアス)
                ch = self.word()
                embell = []
                if opt & 0x2:
                    embell = self.embellishments()
                out.append(self.char_latex(ch, embell))
            elif typ == TMPL:
                if opt & 0x8:
                    self.nudge()
                sel, var = self.byte(), self.byte()
                self.byte()             # テンプレート固有オプション
                out.append(self.template(sel, var, self.objects()))
            elif typ == PILE:
                if opt & 0x8:
                    self.nudge()
                self.byte()
                self.byte()
                if opt & 0x2:
                    self.ruler()
                rows = self.objects()
                out.append(r'\begin{array}{c}' + r'\\'.join(
                    flat(r) for r in rows) + r'\end{array}')
            elif typ == MATRIX:
                if opt & 0x8:
                    self.nudge()
                self.byte(); self.byte(); self.byte()
                rows, cols = self.byte(), self.byte()
                self.i += (rows + cols + 3) // 4 * 2     # 罫線種別のビット列
                cells = self.objects()
                self.ok = False
                out.append(r'\begin{array}{' + 'c' * max(cols, 1) + '}' +
                           r'\\'.join(flat(c) for c in cells) + r'\end{array}')
            elif typ == EMBELL:
                if opt & 0x8:
                    self.nudge()
                self.byte()
                self.ok = False
            elif typ == RULER:
                self.ruler(read_tag=False)
            elif typ == FONT:
                self.byte(); self.byte()
                while self.i < len(self.b) and self.b[self.i]:
                    self.i += 1
                self.i += 1
            elif typ == SIZE:
                v = self.byte()
                if v == 100:
                    self.byte(); self.word()
                elif v == 101:
                    self.word()
                else:
                    self.byte()
            elif 10 <= typ <= 14:       # FULL/SUB/SUB2/SYM/SUBSYM (大きさ指定)
                pass
            else:
                raise Unsupported('未知のレコード %d' % typ)
        return out

    def ruler(self, read_tag=True):
        if read_tag:
            tag = self.byte()
            if tag & 0x0F != RULER:
                self.i -= 1
                return
        n = self.byte()
        self.i += n * 3

    def embellishments(self):
        out = []
        while True:
            tag = self.byte()
            typ, opt = tag & 0x0F, tag >> 4
            if typ == END:
                break
            if typ != EMBELL:
                self.i -= 1
                break
            if opt & 0x8:
                self.nudge()
            out.append(self.byte())
        return out

    # -- 変換
    def char_latex(self, ch, embell):
        if ch in SYMBOLS:
            s = SYMBOLS[ch] + ' '
        elif 32 <= ch < 127:
            s = ESCAPES.get(chr(ch), chr(ch))
        else:
            self.ok = False
            s = '?'
        for e in embell:
            if e in (2, 3):             # プライム
                s += "'"
            elif e == 5:
                s = r'\bar{%s}' % s
            elif e == 6:
                s = r'\vec{%s}' % s
            elif e == 8:
                s = r'\dot{%s}' % s
            elif e == 9:
                s = r'\ddot{%s}' % s
            elif e == 11:
                s = r'\hat{%s}' % s
            elif e == 12:
                s = r'\tilde{%s}' % s
            else:
                self.ok = False
        return s

    def template(self, sel, var, slots):
        s = [flat(x) for x in slots] + ['', '', '']
        if sel in FENCES:
            left, right = FENCES[sel]
            # 中身が背の高い構造のときだけ \left..\right で括弧を伸ばす。
            # 単純な式まで伸縮括弧にすると、組版系によっては極端に幅を取る。
            tall = (r'\frac' in s[0] or r'\sqrt' in s[0] or r'\begin' in s[0]
                    or r'\left' in s[0])
            if tall:
                return r'\left%s %s \right%s ' % (left, s[0], right)
            return '%s%s%s' % (left, s[0], right)
        if sel == 13:                   # 根号
            return r'\sqrt{%s}' % s[0] if var == 0 else r'\sqrt[%s]{%s}' % (s[1], s[0])
        if sel == 14:                   # 分数
            return r'\frac{%s}{%s}' % (s[0], s[1])
        if sel == 15:                   # 上付き / 下付き (スロットは [下, 上])
            if var == 0:
                return '^{%s}' % s[1]
            if var == 1:
                return '_{%s}' % s[0]
            return '_{%s}^{%s}' % (s[0], s[1])
        if sel == 16:
            return r'\underline{%s}' % s[0]
        if sel == 17:
            return r'\overline{%s}' % s[0]
        if sel in (18, 19, 20):
            return r'\vec{%s}' % s[0]
        if sel in (21, 22, 23, 24, 25, 26):      # 積分
            cmd = {21: r'\int', 22: r'\iint', 23: r'\iiint'}.get(sel, r'\int')
            return big_op(cmd, s, var)
        if sel == 29 or 30 <= sel <= 38:         # 総和・総積など
            cmd = {29: r'\sum', 30: r'\prod', 31: r'\coprod',
                   32: r'\bigcup', 33: r'\bigcap'}.get(sel, r'\sum')
            return big_op(cmd, s, var)
        if sel == 39:                   # lim
            return r'\lim_{%s} %s' % (s[1], s[0])
        if sel == 41:                   # 斜め分数
            return '{%s}/{%s}' % (s[0], s[1])
        self.ok = False                 # 未対応テンプレートは中身だけ残す
        return ''.join(s[:len(slots)])


def big_op(cmd, s, var):
    """積分・総和。var の下位ビットで上下限の有無が決まる。"""
    if var & 0x01 and var & 0x02:
        return '%s_{%s}^{%s} %s' % (cmd, s[1], s[2], s[0])
    if var & 0x01:
        return '%s_{%s} %s' % (cmd, s[1], s[0])
    return '%s %s' % (cmd, s[0])


def flat(node):
    if isinstance(node, list):
        return ''.join(flat(x) for x in node)
    return node


def mtef_to_latex(data):
    """MTEF のバイト列 -> (LaTeX, 変換に自信があるか)"""
    m = Mtef(data)
    try:
        body = flat(m.objects())
    except Unsupported:
        return '', False
    body = re.sub(r'\s+', ' ', body).strip()
    return body, m.ok and bool(body)


# ----------------------------------------------- OLE 複合ドキュメント読み ---
def _chain(fat, start, limit):
    """FAT を辿ってセクタ番号の並びを返す。"""
    out, n = [], start
    while 0 <= n < limit and len(out) < limit:
        out.append(n)
        n = fat[n] if n < len(fat) else 0xFFFFFFFE
    return out


def cfb_stream(data, want):
    """複合ドキュメント (CFB) から指定名のストリームを取り出す。

    小さいストリームは 64 バイトのミニセクタに分割され、連続配置とは限らないので、
    ミニ FAT を辿って組み立てる必要がある。
    """
    if len(data) < 512 or data[:8] != CFB_SIG:
        return None
    ssz = 1 << struct.unpack('<H', data[30:32])[0]
    msz = 1 << struct.unpack('<H', data[32:34])[0]
    cutoff = struct.unpack('<I', data[56:60])[0]
    dir_start = struct.unpack('<I', data[48:52])[0]
    mini_start = struct.unpack('<I', data[60:64])[0]
    difat_start, difat_count = struct.unpack('<II', data[68:76])
    nsect = max(0, (len(data) - 512) // ssz)

    def sector(n):
        off = 512 + n * ssz
        return data[off:off + ssz]

    # DIFAT -> FAT
    difat = list(struct.unpack('<109I', data[76:512]))
    n, guard = difat_start, 0
    while 0 <= n < nsect and guard < nsect:
        blk = sector(n)
        difat += list(struct.unpack('<%dI' % (ssz // 4 - 1), blk[:ssz - 4]))
        n = struct.unpack('<I', blk[ssz - 4:ssz])[0]
        guard += 1
    fat = []
    for fs in difat:
        if 0 <= fs < nsect:
            fat += list(struct.unpack('<%dI' % (ssz // 4), sector(fs)))

    def read_chain(start, size, unit, store):
        buf = b''
        for sec in _chain(fat if unit == ssz else minifat, start, nsect * (ssz // unit) + 8):
            if unit == ssz:
                buf += sector(sec)
            else:
                off = sec * unit
                buf += store[off:off + unit]
            if len(buf) >= size:
                break
        return buf[:size]

    # ディレクトリを読み、ルートからミニストリーム本体を得る
    dirdata = b''.join(sector(n) for n in _chain(fat, dir_start, nsect))
    entries = []
    for off in range(0, len(dirdata) - 127, 128):
        e = dirdata[off:off + 128]
        nlen = struct.unpack('<H', e[64:66])[0]
        name = e[:max(0, nlen - 2)].decode('utf-16-le', 'ignore')
        entries.append((name, e[66], struct.unpack('<I', e[116:120])[0],
                        struct.unpack('<Q', e[120:128])[0]))
    root = next((e for e in entries if e[1] == 5), None)
    if root is None:
        return None
    minifat = []
    for n in _chain(fat, mini_start, max(difat_count, 1) + nsect):
        minifat += list(struct.unpack('<%dI' % (ssz // 4), sector(n)))
    ministore = b''.join(sector(n) for n in _chain(fat, root[2], nsect))

    for name, typ, start, size in entries:
        if typ == 2 and name == want:
            if size < cutoff:
                return read_chain(start, size, msz, ministore)
            return read_chain(start, size, ssz, None)
    return None


# ------------------------------------------------------------ mdpf 読み ---
def read_payload(path):
    """mdpf (PNG) の mdRw チャンクを展開して中身を返す。"""
    with open(path, 'rb') as f:
        data = f.read()
    i = 8
    while i < len(data) - 8:
        ln = struct.unpack('>I', data[i:i + 4])[0]
        typ = data[i + 4:i + 8]
        if typ == b'mdRw':
            blob = data[i + 8:i + 8 + ln]
            return zlib.decompressobj().decompress(blob[blob.index(b'\x78\x01'):])
        i += 12 + ln
    raise ValueError('%s は Dynamic Draw の mdpf ではないようです' % path)


def find_rect(doc, cfb_off, near=26, far=200):
    """OLE データの直前に置かれた外接矩形 (double 4 個, mm) を探す。

    通常は CFB の 58 バイト手前だが、その種類で最初に現れる部品だけは
    MFC がクラス名 ("CMolipDrawCntrItem4_0" など) を書き出す分だけ前にずれる。
    そのため固定オフセットにせず、CFB に近いほうから妥当な矩形を探す。
    """
    for back in range(near, far):
        seg = doc[cfb_off - back - 32:cfb_off - back]
        if len(seg) < 32:
            continue
        try:
            L, T, R, B = struct.unpack('<4d', seg)
        except struct.error:
            continue
        if (0 <= L < R < 1e5 and 0 <= T < B < 1e5
                and 0.3 < R - L < 500 and 0.3 < B - T < 500):
            return (L, T, R, B)
    return None


def read_equations(path):
    """mdpf 内の OLE 数式を [{rect, latex, ok, mtef}] で返す。

    rect は図面座標 (mm) の (left, top, right, bottom)。
    """
    doc = read_payload(path)
    out = []
    starts = [m.start() for m in re.finditer(re.escape(CFB_SIG), doc)]
    for n, off in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(doc)
        rect = find_rect(doc, off)
        if rect is None:
            continue
        stream = cfb_stream(doc[off:end], 'Equation Native')
        if stream is None:                 # CFB として読めなければ従来どおり直読み
            m = re.search(re.escape(EQN_SIG), doc[off:end])
            if not m:
                continue
            eoff = off + m.start()
            size = struct.unpack('<I', doc[eoff + 8:eoff + 12])[0]
            stream = doc[eoff:eoff + 28 + max(0, min(size, 200000))]
        if len(stream) < 34:
            continue
        size = struct.unpack('<I', stream[8:12])[0]
        mtef = stream[28:28 + size] if 8 <= size <= 200000 else stream[28:]
        latex, ok = mtef_to_latex(mtef)
        out.append({'rect': rect, 'latex': latex, 'ok': ok, 'mtef': mtef})
    return out


def match_to_svg(equations, svg_oles, tol=0.15):
    """mdpf の数式と SVG の OLE 部品を座標で対応づける。

    SVG は図面の一部を切り出して出力されるため、両者の座標は
    「シートごとに一定の平行移動」の関係にある。縦横比が近い組み合わせで
    平行移動量を投票し、最も支持された量で突き合わせる。

    svg_oles: [(id, x, y, 縦横比)]   戻り値: {id: 数式dict}
    """
    votes = {}
    for _gid, x, y, asp in svg_oles:
        for eq in equations:
            L, T, R, B = eq['rect']
            h = B - T
            if h > 0 and abs((R - L) / h - asp) < 0.06:
                key = (round(L - x, 1), round(T - y, 1))
                votes[key] = votes.get(key, 0) + 1
    if not votes:
        return {}
    (dx, dy), _ = max(votes.items(), key=lambda kv: kv[1])
    used, out = set(), {}
    for gid, x, y, _asp in svg_oles:
        best = None
        for n, eq in enumerate(equations):
            if n in used:
                continue
            d = abs(eq['rect'][0] - x - dx) + abs(eq['rect'][1] - y - dy)
            if d < tol * 2 and (best is None or d < best[0]):
                best = (d, n)
        if best:
            used.add(best[1])
            out[gid] = equations[best[1]]
    return out
