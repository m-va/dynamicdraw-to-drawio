#!/usr/bin/env python3
"""Dynamic Draw が出力した SVG を draw.io (.drawio) の編集可能な図に変換する。

Dynamic Draw の SVG は
    <!-- 矩形部品 ID=416 --> <g id='Group-416'> ... </g> <!-- End of ... -->
という形で部品単位に区切られており、部品種別がコメントに残っている。
これを利用して
    矩形部品   -> mxCell vertex (rectangle)
    円弧部品   -> mxCell vertex (ellipse)
    多角線部品 -> mxCell edge   (waypoint 付き。端点が図形に接していれば接続する)
    text       -> 図形のラベル / 単独テキスト
に写す。

usage: python svg2drawio.py input.svg [-o output.drawio]
"""
import argparse
import base64
import os
import math
import re
import struct
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

MM_TO_PX = 96.0 / 25.4          # SVG のユーザー単位は mm。draw.io は px。
SNAP_TOL = 0.7                  # 線の端点を図形に接続するときの許容距離 (mm)
SMALL_SHAPE = 4.0               # これ以下の図形は内側で終わる線も接続扱いにする (mm)
OLE_MM = 4.5                    # OLE 部品 (数式など) の短辺の長さ (mm) -> --ole-size
OLE_LATEX = {}                  # {部品ID: LaTeX} -> --ole-latex で読み込む
OLE_FONT = None                 # 数式の文字サイズ (px)。None なら図中の最大文字サイズ
FONT_MAP = {
    'ＭＳ ゴシック': 'MS Gothic',
    'ＭＳ Ｐゴシック': 'MS PGothic',
    'ＭＳ 明朝': 'MS Mincho',
    'ＭＳ Ｐ明朝': 'MS PMincho',
}

# Adobe Symbol フォントの文字 -> Unicode (Dynamic Draw は μ などを Symbol で出す)
SYMBOL_MAP = {
    'a': 'α', 'b': 'β', 'c': 'χ', 'd': 'δ', 'e': 'ε',
    'f': 'φ', 'g': 'γ', 'h': 'η', 'i': 'ι', 'k': 'κ',
    'l': 'λ', 'm': 'μ', 'n': 'ν', 'p': 'π', 'q': 'θ',
    'r': 'ρ', 's': 'σ', 't': 'τ', 'u': 'υ', 'w': 'ω',
    'x': 'ξ', 'y': 'ψ', 'z': 'ζ',
    'A': 'Α', 'B': 'Β', 'D': 'Δ', 'F': 'Φ', 'G': 'Γ',
    'L': 'Λ', 'P': 'Π', 'Q': 'Θ', 'S': 'Σ', 'W': 'Ω',
    'X': 'Ξ', 'Y': 'Ψ',
    '£': '≤', '³': '≥', '¹': '≠', '¬': '←',
    '­': '↑', '®': '→', '¯': '↓', '¥': '∞',
    '¶': '∂', 'Ö': '√', 'ò': '∫', '´': '×',
}

# 部品名 -> 種類。Dynamic Draw は UI 言語ごとにコメントの部品名が変わるので、
# 日本語と英語のどちらでも拾えるようにしておく (未知の名前は形から推定する)
PART_KINDS = (
    (('矩形', 'rect', 'box', 'square'), 'rect'),
    (('円弧', '楕円', 'arc', 'circle', 'ellipse', 'oval'), 'ellipse'),
    (('多角線', '直線', '曲線', 'poly', 'line', 'curve', 'connector'), 'edge'),
)

NUM = re.compile(r'-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')
LF = chr(10)


FRAC = chr(92) + 'frac'


def em_height(latex):
    """LaTeX を組んだときの高さの見込み (文字サイズの何倍か)。"""
    depth = level = 0
    i = 0
    while True:
        i = latex.find(FRAC, i)
        if i < 0:
            break
        level = 1
        j, brace, seen = i + 5, 0, 0
        while j < len(latex) and seen < 2:      # 分子・分母の中を見る
            if latex[j] == '{':
                brace += 1
            elif latex[j] == '}':
                brace -= 1
                if brace == 0:
                    seen += 1
            j += 1
        level += em_height(latex[i + 5:j]) / 1.2 - 1.6 / 1.2
        depth = max(depth, level)
        i = j
    return 1.6 + 1.2 * depth


def mxfile(diagrams):
    """複数ページ分の <diagram> をまとめて 1 つの .drawio にする。"""
    return LF.join(['<mxfile host="svg2drawio" type="device">'] +
                   list(diagrams) + ['</mxfile>', ''])


def load_latex_map(path):
    """OLE 部品 ID -> LaTeX の対応表を読む。

    JSON ({"52": "x_{ref}"}) でも、1 行 1 件のテキストでもよい。
    テキスト形式は LaTeX のバックスラッシュをそのまま書けるので楽:
        52 = x_{ref}
        96: \frac{1}{M_d s^2}
        # 行頭 # はコメント
    """
    with open(path, encoding='utf-8') as f:
        body = f.read()
    if body.lstrip().startswith('{'):
        import json
        return {str(k): v for k, v in json.loads(body).items()}
    out = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = re.match(r'(\d+)\s*[=:	]\s*(.+)', line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def kind_from_name(name):
    low = name.lower()
    for keys, kind in PART_KINDS:
        if any(k in low for k in keys):
            return kind
    return None


# ---------------------------------------------------------------- parsing ---
def parse_path(d):
    """M/C だけで構成された Dynamic Draw の path から通過点の列を返す。"""
    pts, nums, cmd = [], [], None
    for tok in re.findall(r'[MmCcLlZzHhVv]|' + NUM.pattern, d):
        if tok[0].isalpha():
            cmd, nums = tok, []
            continue
        nums.append(float(tok))
        if cmd in ('M', 'L') and len(nums) == 2:
            pts.append(tuple(nums))
            nums = []
        elif cmd == 'C' and len(nums) == 6:
            pts.append((nums[4], nums[5]))
            nums = []
    return pts


def is_curved(d):
    """cp1 が始点・cp2 が終点と一致しない = 実際のベジェ曲線かどうか。"""
    toks = re.findall(r'[MC]|' + NUM.pattern, d)
    prev, i = None, 0
    while i < len(toks):
        if toks[i] == 'M':
            prev = (float(toks[i + 1]), float(toks[i + 2])); i += 3
        elif toks[i] == 'C':
            n = [float(x) for x in toks[i + 1:i + 7]]
            if prev and (abs(n[0] - prev[0]) > 1e-3 or abs(n[1] - prev[1]) > 1e-3 or
                         abs(n[2] - n[4]) > 1e-3 or abs(n[3] - n[5]) > 1e-3):
                return True
            prev = (n[4], n[5]); i += 7
        else:
            i += 1
    return False


def bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def parse_style(svg):
    """<style> の .sfont-N を {name: (size_mm, family, weight, style)} にする。"""
    fonts = {}
    for m in re.finditer(r'\.(sfont-\d+)\s*\{([^}]*)\}', svg):
        name, body = m.group(1), m.group(2)

        def get(key):
            mm = re.search(key + r'\s*:\s*([^;]+)', body)
            return mm.group(1).strip() if mm else ''

        size = get('font-size')
        fonts[name] = (
            float(NUM.search(size).group()) if size else 2.5,
            get('font-family') or 'MS Gothic',
            get('font-weight'),
            get('font-style'),
        )
    return fonts


def color(v):
    """'#rrggbbaa' / 'transparent' / 'none' -> (drawio色, 不透明度%)"""
    if not v or v in ('none', 'transparent'):
        return 'none', 100
    m = re.fullmatch(r'#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?', v)
    if not m:
        return v, 100
    alpha = int(m.group(2), 16) if m.group(2) else 255
    return '#' + m.group(1), round(alpha / 255 * 100)


def dash_style(dasharray, sw):
    """stroke-dasharray -> draw.io の dashed/dashPattern。

    draw.io は dashPattern の各値を線幅倍して使うので、線幅で割っておく。
    """
    vals = [float(v) / sw for v in NUM.findall(dasharray)]
    if not vals:
        return ['dashed=1']
    return ['dashed=1', 'fixDash=1',
            'dashPattern=' + ' '.join('%g' % round(v, 2) for v in vals)]


def attrs(tag):
    return dict(re.findall(r"([\w:-]+)\s*=\s*'([^']*)'", tag))


# 実フォントで幅を測れると寄せの精度が上がる (無ければ概算にフォールバック)
FONT_FILES = {
    'MS Gothic': ('C:/Windows/Fonts/msgothic.ttc', 0),
    'MS PGothic': ('C:/Windows/Fonts/msgothic.ttc', 1),
    'MS UI Gothic': ('C:/Windows/Fonts/msgothic.ttc', 2),
    'MS Mincho': ('C:/Windows/Fonts/msmincho.ttc', 0),
    'MS PMincho': ('C:/Windows/Fonts/msmincho.ttc', 1),
}
_font_cache = {}


def _load_font(family, size_px):
    key = (family, round(size_px, 1))
    if key not in _font_cache:
        font = None
        spec = FONT_FILES.get(family)
        if spec:
            try:
                from PIL import ImageFont
                font = ImageFont.truetype(spec[0], round(size_px, 1), index=spec[1])
            except Exception:
                font = None
        _font_cache[key] = font
    return _font_cache[key]


def text_width(s, size, family=None):
    """文字列の幅 (mm)。実フォントが読めれば実測、駄目なら全角1em/半角0.5emで概算。"""
    font = _load_font(family, size * MM_TO_PX) if family else None
    if font is not None:
        try:
            return font.getlength(s) / MM_TO_PX
        except Exception:
            pass
    return sum(1.0 if ord(c) > 0x2E80 else 0.5 for c in s) * size


# ------------------------------------------------------------- conversion ---
class Converter:
    def __init__(self, svg):
        self.svg = svg
        self.fonts = parse_style(svg)
        self.symbols, self.curvy = {}, set()
        for m in re.finditer(r"<symbol id='([^']+)'>\s*<path d='([^']*)'", svg):
            self.symbols[m.group(1)] = parse_path(m.group(2))
            if is_curved(m.group(2)):
                self.curvy.add(m.group(1))
        self.ole_images = dict(re.findall(
            r"<symbol id='(OleImage-[^']+)'>\s*<image[^>]*base64,([^']+)'", svg))
        self.cells, self.shapes = [], []
        self.prefix = ''                   # 複数ページ時に ID が衝突しないように
        self.equations = {}                # {部品ID: mdpf から読んだ数式}
        self.math = False                  # LaTeX を出したら draw.io の数式組版を有効にする
        self.stats = {'rect': 0, 'ellipse': 0, 'edge': 0, 'text': 0, 'label': 0,
                      'connected': 0, 'guessed': 0, 'ole': 0, 'ole_tex': 0,
                      'skipped': 0}

    # -- helpers
    def px(self, v):
        return round(v * MM_TO_PX, 2)

    def font_style(self, cls, fill):
        size, family, weight, fstyle = self.fonts.get(cls, (2.5, 'MS Gothic', '400', 'normal'))
        s = ['fontSize=%g' % round(size * MM_TO_PX, 1),
             'fontFamily=' + FONT_MAP.get(family, family)]
        col, _ = color(fill)
        if col != 'none':
            s.append('fontColor=' + col)
        flags = 0
        if weight and weight not in ('400', 'normal'):
            flags |= 1
        if fstyle == 'italic':
            flags |= 2
        if flags:
            s.append('fontStyle=%d' % flags)
        return s, size

    def is_symbol(self, cls):
        return self.fonts.get(cls, (0, ''))[1] == 'Symbol'

    def parse_text(self, frag):
        """1 つの <text> を (y, x, html, plain, cls, fill) のチャンク列にする。

        Dynamic Draw は tspan を入れ子にしてフォント切り替え (Symbol) と上付きを
        表現するので、文書順を保ったまま平坦化する。
        """
        try:
            el = ET.fromstring(frag)
        except ET.ParseError:
            return []
        runs = []

        def walk(node, x, y, cls, fill, sup):
            x = float(node.get('x', x))
            y = float(node.get('y', y))
            cls = node.get('class', cls)
            fill = node.get('fill', fill)
            sup = sup or node.get('baseline-shift') is not None or \
                node.get('font-size', '').endswith('%')
            if node.text:
                runs.append((y, x, node.text, cls, fill, sup))
            for child in node:
                walk(child, x, y, cls, fill, sup)
                if child.tail:
                    runs.append((y, x, child.tail, cls, fill, sup))

        walk(el, 0.0, 0.0, el.get('class', 'sfont-1'), el.get('fill', '#000000ff'), False)
        chunks = []
        for y, x, txt, cls, fill, sup in runs:
            sym = self.is_symbol(cls)
            if sym:                          # Symbol フォントは Unicode に置き換える
                txt = ''.join(SYMBOL_MAP.get(c, c) for c in txt)
            html = escape(txt)
            if sym:   # 和文フォントだと μ などが全角になるので欧文フォントを指定
                html = '<span style="font-family:Arial,Helvetica,sans-serif">%s</span>' % html
            if sup:
                html = '<sup>%s</sup>' % html
            if chunks and abs(chunks[-1][0] - y) < 0.05:
                y0, x0, h0, p0, c0, f0 = chunks[-1]
                chunks[-1] = (y0, x0, h0 + html, p0 + txt,
                              cls if self.is_symbol(c0) else c0, f0)
            else:
                chunks.append((y, x, html, txt, cls, fill))
        return chunks

    def group_text(self, body):
        """グループ内の文字を行にまとめる。
        戻り値: (行HTML, 行プレーン, クラス, fill, (xmin, y上, y下))"""
        chunks = []
        for tm in re.finditer(r'<text\b.*?</text>', body, re.S):
            chunks += self.parse_text(tm.group(0))
        if not chunks:
            return None
        chunks.sort(key=lambda c: (round(c[0], 2), c[1]))
        html, plain, ys, xs, lcls = [], [], [], [], []
        for y, x, h, p, cls, fill in chunks:
            if ys and abs(ys[-1] - y) < 0.05:
                html[-1] += h
                plain[-1] += p
                if self.is_symbol(lcls[-1]):
                    lcls[-1] = cls
            else:
                html.append(h)
                plain.append(p)
                ys.append(y)
                xs.append(x)
                lcls.append(cls)
        cls = next((c[4] for c in chunks if not self.is_symbol(c[4])), chunks[0][4])
        return html, plain, cls, chunks[0][5], (min(xs), ys[0], ys[-1], xs), lcls

    def text_align(self, plain, tb, size, family):
        """行の左端・中央・右端のうち最も揃っているものを、元の寄せ方とみなす。
        戻り値: ('left'|'center'|'right', その基準位置, 最大幅)"""
        xs = tb[3]
        widths = [text_width(t, size, family) for t in plain]
        lefts = list(xs)
        centers = [x + w / 2 for x, w in zip(xs, widths)]
        rights = [x + w for x, w in zip(xs, widths)]
        mean = lambda v: sum(v) / len(v)
        spread = lambda v: max(v) - min(v)
        which = min((spread(lefts), 0), (spread(centers), 1), (spread(rights), 2))[1]
        ref = mean((lefts, centers, rights)[which])
        return ('left', 'center', 'right')[which], ref, max(widths)

    def label_style(self, plain, geo, tb, size, family=None):
        """図形内ラベルの寄せと余白を、元の文字位置に合わせて決める。"""
        x, y, w, h = geo
        _, ytop, ybot, _ = tb
        align, ref, est = self.text_align(plain, tb, size, family)
        s = ['spacing=0', 'align=' + align]
        if align == 'left':
            d = ref - x                      # 左端から文字までの距離
        elif align == 'right':
            d = (x + w) - ref                # 右端から文字までの距離
        else:
            d = ref - (x + w / 2)            # 中央からのずれ (両側から詰めて寄せる)
        px = lambda v: round(v * MM_TO_PX, 1)
        if align == 'center':
            if abs(d) > 0.2:
                s.append('spacing%s=%g' % ('Left' if d > 0 else 'Right', px(2 * abs(d))))
        elif d > 0.2:
            s.append('spacing%s=%g' % ('Left' if align == 'left' else 'Right', px(d)))
        # 縦位置: 文字ブロックの中心を図形の中心に合わせ、ずれは余白で補正する
        s.append('verticalAlign=middle')
        dv = ((ytop - size * 0.85) + (ybot + size * 0.3)) / 2 - (y + h / 2)
        if abs(dv) > 0.2:
            s.append('spacing%s=%g' % ('Top' if dv > 0 else 'Bottom', px(2 * abs(dv))))
        return s

    def add_shape(self, cid, kind, geo, style, label, is_shape=True):
        x, y, w, h = geo
        cell = {'id': cid, 'style': ';'.join(style), 'value': label, 'vertex': True,
                'geo': (self.px(x), self.px(y), self.px(w), self.px(h))}
        self.cells.append(cell)
        if is_shape:
            self.shapes.append((cid, x, y, x + w, y + h))
        self.stats[kind] += 1

    # -- 部品ごとの変換
    def do_vertex(self, gid, kind, body):
        uses = [attrs(u) for u in re.findall(r'<use\b[^>]*>', body)]
        bodies = [u for u in uses if 'Body' in u.get('href', '')]
        pts = None
        for u in bodies:
            pts = self.symbols.get(u['href'].lstrip('#'), pts)
        if pts is None:                       # 非表示図形 (テキストだけの部品)
            sym = re.search(r"<symbol id='(ObjectBody-\d+)'", body)
            pts = self.symbols.get(sym.group(1)) if sym else None
        if not pts:
            self.stats['skipped'] += 1
            return
        x0, y0, x1, y1 = bbox(pts)
        geo = (x0, y0, x1 - x0, y1 - y0)

        fill, fill_op = 'none', 100
        stroke, sw, dash = 'none', None, None
        for u in bodies:
            f, fo = color(u.get('fill'))
            if f != 'none':
                fill, fill_op = f, fo
            s, _ = color(u.get('stroke'))
            if s != 'none':
                stroke = s
                sw = float(u.get('stroke-width', 0.25))
                dash = u.get('stroke-dasharray')

        style = ['rounded=0'] if kind == 'rect' else ['ellipse']
        style.append('html=1')
        style.append('fillColor=' + fill)
        if fill != 'none' and fill_op < 100:
            style.append('opacity=%d' % fill_op)
        style.append('strokeColor=' + stroke)
        if sw:
            style.append('strokeWidth=%g' % round(sw * MM_TO_PX, 2))
        if dash:
            style += dash_style(dash, sw)
        label, tinfo = '', self.group_text(body)
        if tinfo:
            html, plain, cls, tfill, tb, lcls = tinfo
            fs, size = self.font_style(cls, tfill)
            for i, lc in enumerate(lcls):    # 行ごとに文字サイズが違う場合に対応
                lsize = self.fonts.get(lc, (size,))[0]
                if abs(lsize - size) > 0.05:
                    html[i] = '<span style="font-size:%gpx">%s</span>' % (
                        round(lsize * MM_TO_PX, 1), html[i])
            family = fs[1].split('=', 1)[1]
            style += fs + self.label_style(plain, geo, tb, size, family)
            # Dynamic Draw は文字を自動折り返ししないので、こちらも折り返さない
            style += ['whiteSpace=nowrap', 'overflow=visible']
            label = '<br>'.join(html)
            if len(html) > 1:
                lh = (tb[2] - tb[1]) / (len(html) - 1) / size
                label = '<div style="line-height:%.2f">%s</div>' % (lh, label)
            self.stats['label'] += 1

        if not bodies:                        # 枠も塗りも無い = 単なるテキスト
            if not label:
                self.stats['skipped'] += 1
                return
            # 見えない矩形ではなく文字自身の位置から箱を作る (元図と重なるように)
            _, ytop, ybot, xs = tinfo[4]
            how, ref, est = self.text_align(plain, tinfo[4], size, family)
            tx = {'left': ref, 'center': ref - est / 2, 'right': ref - est}[how]
            align = 'align=' + how
            geo = (tx, ytop - size * 0.85, est, (ybot - ytop) + size * 1.15)
            keep = ('fontSize', 'fontFamily', 'fontColor', 'fontStyle')
            style = ['text', 'html=1', 'fillColor=none', 'strokeColor=none',
                     'whiteSpace=nowrap', 'overflow=visible', 'spacing=0',
                     'spacingTop=0', 'spacingLeft=0', 'spacingRight=0',
                     align, 'verticalAlign=middle'] + \
                    [s for s in style if s.split('=')[0] in keep]
            self.stats['label'] -= 1
            self.add_shape(self.prefix + 't%s' % gid, 'text', geo, style, label, is_shape=False)
            return
        self.add_shape(self.prefix + 'n%s' % gid, kind, geo, style, label)

    def do_edge(self, gid, body):
        uses = [attrs(u) for u in re.findall(r'<use\b[^>]*>', body)]
        bodies = [u for u in uses if 'Body' in u.get('href', '')]
        if not bodies:
            self.stats['skipped'] += 1
            return
        bid = bodies[0]['href'].lstrip('#')
        pts = self.symbols.get(bid)
        if not pts or len(pts) < 2:
            self.stats['skipped'] += 1
            return
        stroke, _ = color(bodies[0].get('stroke'))
        sw = float(bodies[0].get('stroke-width', 0.25))
        dash = bodies[0].get('stroke-dasharray')

        style = ['edgeStyle=none', 'html=1', 'rounded=0',
                 'strokeColor=' + (stroke if stroke != 'none' else '#000000'),
                 'strokeWidth=%g' % round(sw * MM_TO_PX, 2)]
        if dash:
            style += dash_style(dash, sw)
        if bid in self.curvy:                 # 元が曲線なら滑らかに繋ぐ
            style.append('curved=1')

        # 矢印は近いほうの端点に割り当てる。Dynamic Draw は線本体を矢じりの手前で
        # 止めて別パスで矢じりを描くので、端点を矢じりの先端まで伸ばす。
        pts = list(pts)
        arrows = {}
        for u in uses:
            href = u.get('href', '').lstrip('#')
            if 'Arrow' not in href:
                continue
            ap = self.symbols.get(href)
            if not ap:
                continue
            ax = sum(p[0] for p in ap) / len(ap)
            ay = sum(p[1] for p in ap) / len(ap)
            i = 0 if math.hypot(ax - pts[0][0], ay - pts[0][1]) < \
                math.hypot(ax - pts[-1][0], ay - pts[-1][1]) else -1
            base = pts[i]
            tip = max(ap, key=lambda p: math.hypot(p[0] - base[0], p[1] - base[1]))
            pts[i] = tip
            arrows[i] = math.hypot(tip[0] - base[0], tip[1] - base[1])

        for i, key in ((0, 'start'), (-1, 'end')):
            if i in arrows:
                style.append('%sArrow=blockThin' % key)   # 元の矢じりは細身
                style.append('%sFill=1' % key)
                size = round(arrows[i] * MM_TO_PX - sw * MM_TO_PX, 1)
                if size > 1:
                    style.append('%sSize=%g' % (key, size))
            else:
                style.append('%sArrow=none' % key)

        cell = {'id': self.prefix + 'e%s' % gid, 'style': ';'.join(style), 'value': '',
                'edge': True, 'src': pts[0], 'tgt': pts[-1], 'pts': pts[1:-1]}
        tinfo = self.group_text(body)
        if tinfo:
            html, _plain, cls, tfill, _tb, _lcls = tinfo
            fs, _ = self.font_style(cls, tfill)
            cell['style'] += ';' + ';'.join(fs)
            cell['value'] = '<br>'.join(html)
        self.cells.append(cell)
        self.stats['edge'] += 1

    def ole_font(self, height=None, latex=''):
        """数式の文字サイズ (px)。

        MathJax は 1 行の数式を文字サイズの約 1.6 倍の高さに組む。分数が入ると
        その分だけ背が高くなるので、入れ子の深さから見込みの高さ (em) を出して
        数式の実寸 (mm) から文字サイズを逆算する。
        """
        if OLE_FONT:
            return round(OLE_FONT, 1)
        return round((height or OLE_MM) * MM_TO_PX / em_height(latex), 1)

    def ole_parts(self):
        """[(部品ID, x, y, 縦横比)] を返す。mdpf の数式と突き合わせるのに使う。"""
        out = []
        for m in re.finditer(r'<!-- [^<>]+? ID=(\d+) -->(.*?)<!-- End of', self.svg, re.S):
            gid, body = m.group(1), m.group(2)
            href = re.search(r"href='#(OleImage-[^']+)'", body)
            pos = re.search(r'translate\(\s*([-\d.]+)[\s,]+([-\d.]+)\s*\)', body)
            b64 = self.ole_images.get(href.group(1)) if href else None
            if not b64 or not pos:
                continue
            try:
                raw = base64.b64decode(b64)
                pw, ph = struct.unpack('>II', raw[16:24])
            except Exception:
                continue
            if pw and ph:
                out.append((gid, float(pos.group(1)), float(pos.group(2)), pw / ph))
        return out

    def dump_ole(self, outdir):
        """OLE 部品の PNG を outdir に書き出す (中身を確認して LaTeX に起こす用)。"""
        import os
        os.makedirs(outdir, exist_ok=True)
        written = []
        for m in re.finditer(r'<!-- [^<>]+? ID=(\d+) -->(.*?)<!-- End of', self.svg, re.S):
            gid, body = m.group(1), m.group(2)
            href = re.search(r"href='#(OleImage-[^']+)'", body)
            b64 = self.ole_images.get(href.group(1)) if href else None
            if not b64:
                continue
            path = os.path.join(outdir, 'ole-%s.png' % gid)
            with open(path, 'wb') as f:
                f.write(base64.b64decode(b64))
            written.append(path)
        return written

    def do_ole(self, gid, body):
        """OLE 部品 (Microsoft 数式など) を埋め込み画像として復元する。

        Dynamic Draw の SVG 出力は OLE を <image width='0' height='0'> と
        transform='translate(x y) scale(inf, inf)' で書き出すため、そのままでは
        大きさゼロで表示されない。PNG のデータと左上座標・縦横比は残っているので、
        短辺を OLE_MM (mm) として復元する。
        """
        eq = self.equations.get(gid)
        latex = OLE_LATEX.get(gid) or (eq['latex'] if eq and eq['ok'] else None)
        href = re.search(r"href='#(OleImage-[^']+)'", body)
        pos = re.search(r'translate\(\s*([-\d.]+)[\s,]+([-\d.]+)\s*\)', body)
        b64 = self.ole_images.get(href.group(1)) if href else None
        if not b64 or not pos:
            self.stats['skipped'] += 1
            return
        try:
            raw = base64.b64decode(b64)
            pw, ph = struct.unpack('>II', raw[16:24])
        except Exception:
            self.stats['skipped'] += 1
            return
        if not pw or not ph:
            self.stats['skipped'] += 1
            return
        x, y = float(pos.group(1)), float(pos.group(2))
        if eq:                             # mdpf に本当の大きさが入っていた
            L, T, R, B = eq['rect']
            w, h = R - L, B - T
        elif pw >= ph:                     # 短辺を OLE_MM にして縦横比を保つ
            h = OLE_MM
            w = OLE_MM * pw / ph
        else:
            w = OLE_MM
            h = OLE_MM * ph / pw
        if latex:                          # LaTeX が与えられていれば数式として置く
            style = ['text', 'html=1', 'fillColor=none', 'strokeColor=none',
                     'whiteSpace=nowrap', 'overflow=visible', 'spacing=0',
                     'align=center', 'verticalAlign=middle',
                     'fontSize=%g' % self.ole_font(h, latex)]
            self.add_shape(self.prefix + 'o%s' % gid, 'ole_tex', (x, y, w, h), style,
                           '$$' + escape(latex) + '$$', is_shape=False)
            self.math = True
            return
        style = ['shape=image', 'imageAspect=1', 'aspect=fixed', 'noLabel=1',
                 'verticalLabelPosition=bottom', 'verticalAlign=top',
                 'image=data:image/png,' + b64]
        self.add_shape(self.prefix + 'o%s' % gid, 'ole', (x, y, w, h), style, '', is_shape=False)

    def connect_edges(self):
        """線の端点が図形の縁に乗っていれば source/target として接続する。"""
        for c in self.cells:
            if not c.get('edge'):
                continue
            for end, key in (('src', 'exit'), ('tgt', 'entry')):
                px, py = c[end]
                best = None
                for sid, x0, y0, x1, y1 in self.shapes:
                    if not (x0 - SNAP_TOL <= px <= x1 + SNAP_TOL and
                            y0 - SNAP_TOL <= py <= y1 + SNAP_TOL):
                        continue
                    # 枠線の上で終わっている線だけを接続とみなす。
                    # ただし小さい図形 (加算点の丸など) は内側で終わってもよい。
                    edge_dist = min(abs(px - x0), abs(px - x1), abs(py - y0), abs(py - y1))
                    small = (x1 - x0) <= SMALL_SHAPE and (y1 - y0) <= SMALL_SHAPE
                    if edge_dist > SNAP_TOL and not small:
                        continue
                    area = (x1 - x0) * (y1 - y0)
                    if best is None or area < best[0]:
                        best = (area, sid, x0, y0, x1, y1)
                if not best:
                    continue
                _, sid, x0, y0, x1, y1 = best
                rx = 0 if x1 - x0 < 1e-6 else min(1, max(0, (px - x0) / (x1 - x0)))
                ry = 0 if y1 - y0 < 1e-6 else min(1, max(0, (py - y0) / (y1 - y0)))
                c['source' if end == 'src' else 'target'] = sid
                # Perimeter=0: 指定した点をそのまま使わせる (既定だと外周へ投影されて線が歪む)
                c['style'] += ';%sX=%.3f;%sY=%.3f;%sDx=0;%sDy=0;%sPerimeter=0' % (
                    key, rx, key, ry, key, key, key)
                self.stats['connected'] += 1

    def body_points(self, body):
        """グループの本体パス (最初の ObjectBody) の通過点を返す。"""
        for u in re.findall(r'<use\b[^>]*>', body):
            href = attrs(u).get('href', '').lstrip('#')
            if 'Body' in href and href in self.symbols:
                return href, self.symbols[href]
        m = re.search(r"<symbol id='(ObjectBody-\d+)'", body)
        if m and m.group(1) in self.symbols:
            return m.group(1), self.symbols[m.group(1)]
        return None, None

    def kind_from_shape(self, body):
        """部品名から種類が分からないときに、形そのものから判定する。

        Dynamic Draw のコメントの部品名は UI 言語によって変わるため、
        英語版などでは名前で判定できない。
        """
        sid, pts = self.body_points(body)
        if not pts or len(pts) < 2:
            return None
        closed = (len(pts) > 2 and abs(pts[0][0] - pts[-1][0]) < 1e-6
                  and abs(pts[0][1] - pts[-1][1]) < 1e-6)
        if not closed:
            return 'edge'
        xs = {round(p[0], 3) for p in pts}
        ys = {round(p[1], 3) for p in pts}
        if len(xs) <= 2 and len(ys) <= 2:      # 軸に平行な四隅 = 矩形
            return 'rect'
        return 'ellipse' if sid in self.curvy else 'edge'

    def run(self):
        for m in re.finditer(r'<!-- ([^<>]+?) ID=(\d+) -->(.*?)<!-- End of', self.svg, re.S):
            name, gid, body = m.group(1), m.group(2), m.group(3)
            if 'OleImage-' in body:            # OLE 部品 (数式など)
                self.do_ole(gid, body)
                continue
            kind = kind_from_name(name)
            if kind is None:                   # 名前で分からなければ形から推定する
                kind = self.kind_from_shape(body)
                if kind:
                    self.stats['guessed'] += 1
            if kind == 'rect' or kind == 'ellipse':
                self.do_vertex(gid, kind, body)
            elif kind == 'edge':
                self.do_edge(gid, body)
            else:
                self.stats['skipped'] += 1
        self.connect_edges()
        return self.cells

    def to_xml(self, name='Page-1'):
        return mxfile([self.diagram_xml(name, 1)])

    def diagram_xml(self, name='Page-1', page=1):
        m = re.search(r"viewBox\s*=\s*'([^']+)'", self.svg)
        vb = [float(v) for v in NUM.findall(m.group(1))] if m else [0, 0, 297, 210]
        out = ['  <diagram name=%s id="page%d">' % (quoteattr(name), page),
               '    <mxGraphModel dx="1422" dy="798" grid="1" gridSize="10" guides="1" '
               'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
               'pageWidth="%d" pageHeight="%d" math="%d" shadow="0">'
               % (round(vb[2] * MM_TO_PX), round(vb[3] * MM_TO_PX), self.math),
               '      <root>',
               '        <mxCell id="0" />',
               '        <mxCell id="1" parent="0" />']
        for c in self.cells:
            if c.get('vertex'):
                x, y, w, h = c['geo']
                out.append('        <mxCell id=%s value=%s style=%s vertex="1" parent="1">'
                           % (quoteattr(c['id']), quoteattr(c['value']), quoteattr(c['style'])))
                out.append('          <mxGeometry x="%g" y="%g" width="%g" height="%g" as="geometry" />'
                           % (x, y, max(w, 1), max(h, 1)))
                out.append('        </mxCell>')
            else:
                ref = ''
                if c.get('source'):
                    ref += ' source=%s' % quoteattr(c['source'])
                if c.get('target'):
                    ref += ' target=%s' % quoteattr(c['target'])
                out.append('        <mxCell id=%s value=%s style=%s edge="1" parent="1"%s>'
                           % (quoteattr(c['id']), quoteattr(c['value']), quoteattr(c['style']), ref))
                out.append('          <mxGeometry relative="1" as="geometry">')
                out.append('            <mxPoint x="%g" y="%g" as="sourcePoint" />'
                           % (self.px(c['src'][0]), self.px(c['src'][1])))
                out.append('            <mxPoint x="%g" y="%g" as="targetPoint" />'
                           % (self.px(c['tgt'][0]), self.px(c['tgt'][1])))
                if c['pts']:
                    out.append('            <Array as="points">')
                    for p in c['pts']:
                        out.append('              <mxPoint x="%g" y="%g" />'
                                   % (self.px(p[0]), self.px(p[1])))
                    out.append('            </Array>')
                out.append('          </mxGeometry>')
                out.append('        </mxCell>')
        out += ['      </root>', '    </mxGraphModel>', '  </diagram>']
        return '\n'.join(out)


def collect_jobs(paths):
    """入力パスから「1つの .drawio にまとめる仕事」の一覧を作る。

    xxx.mdpf は xxx_1.svg, xxx_2.svg, ... (無ければ xxx.svg) と対になり、
    xxx.drawio に複数ページとして出力する。対になる mdpf が無い SVG は単独で変換する。
    """
    import glob
    svgs, mdpfs = [], {}
    for p in paths:
        if os.path.isdir(p):
            svgs += sorted(glob.glob(os.path.join(p, '*.svg')))
            for m in glob.glob(os.path.join(p, '*.mdpf')):
                mdpfs[os.path.splitext(m)[0]] = m
        elif p.lower().endswith('.mdpf'):
            mdpfs[os.path.splitext(p)[0]] = p
        else:
            svgs.append(p)
    for sv in svgs:                        # SVG と同じ場所の mdpf も拾う
        base = re.sub(r'_\d+$', '', os.path.splitext(sv)[0])
        if base not in mdpfs and os.path.exists(base + '.mdpf'):
            mdpfs[base] = base + '.mdpf'

    groups = {}
    for sv in svgs:
        stem = os.path.splitext(sv)[0]
        m = re.match(r'(.*)_(\d+)$', stem)
        base, sheet = (m.group(1), int(m.group(2))) if m else (stem, 1)
        if m and base not in mdpfs and not os.path.exists(base + '.mdpf'):
            base, sheet = stem, 1          # xxx_1.svg でも相方が無ければ単独扱い
        groups.setdefault(base, []).append((sheet, sv))
    jobs = []
    for base in sorted(groups):
        sheets = [sv for _n, sv in sorted(groups[base])]
        jobs.append({'base': base, 'svgs': sheets, 'mdpf': mdpfs.get(base)})
    return jobs


def convert_job(job, out=None):
    """1つの .drawio を書き出す。戻り値は集計用の統計。"""
    equations = []
    if job['mdpf']:
        try:
            import mdpf as mdpf_reader
            equations = mdpf_reader.read_equations(job['mdpf'])
            print('%s: 数式 %d 個を読み込みました' % (job['mdpf'], len(equations)))
        except Exception as e:
            print('%s を読めませんでした (%s)。画像として復元します' % (job['mdpf'], e))
    pages, total = [], {}
    for n, path in enumerate(job['svgs'], 1):
        with open(path, encoding='utf-8') as f:
            conv = Converter(f.read())
        conv.prefix = 'p%d-' % n
        if equations:
            import mdpf as mdpf_reader
            matched = mdpf_reader.match_to_svg(equations, conv.ole_parts())
            conv.equations = matched
        conv.run()
        pages.append(conv.diagram_xml(os.path.basename(os.path.splitext(path)[0]), n))
        print('  %s' % path)
        report(conv.stats, indent='    ')
        for k, v in conv.stats.items():
            total[k] = total.get(k, 0) + v
    dest = out or job['base'] + '.drawio'
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(mxfile(pages))
    print('  -> %s (%d ページ)' % (dest, len(pages)))
    return total


def report(st, indent='  '):
    print(indent + '矩形 %(rect)d / 円弧 %(ellipse)d / 線 %(edge)d (端点接続 %(connected)d) / '
          'テキスト単体 %(text)d / 図形内ラベル %(label)d / 変換不能 %(skipped)d' % st)
    if st.get('ole_tex'):
        print(indent + 'OLE部品 %(ole_tex)d 個を $$...$$ の数式として出力' % st)
    if st.get('ole'):
        print(indent + 'OLE部品 %(ole)d 個は埋め込み画像 '
              '(mdpf を渡すか --ole-latex を使うと数式にできます)' % st)
    if st.get('guessed'):
        print(indent + '※ 部品名から種類が分からず形から推定: %(guessed)d' % st)


def main():
    global OLE_MM
    global OLE_FONT
    global OLE_LATEX
    ap = argparse.ArgumentParser(
        description='Dynamic Draw の SVG を draw.io 形式に変換する',
        epilog='パスにはファイルもフォルダも指定できます。フォルダを渡すと、その中の '
               'xxx.mdpf と xxx_1.svg, xxx_2.svg ... を対応づけて xxx.drawio '
               '(シートごとのページ) にまとめます。')
    ap.add_argument('path', nargs='*', default=['.'],
                    help='SVG / mdpf / フォルダ (既定: カレントフォルダ)')
    ap.add_argument('-o', '--out', help='出力先 (入力が1つのときのみ)')
    ap.add_argument('--mdpf', help='使用する mdpf を明示指定する')
    ap.add_argument('--ole-size', type=float, default=OLE_MM, metavar='MM',
                    help='OLE 部品 (数式など) の短辺の長さ (mm, 既定 %(default)s)。'
                         'mdpf から実寸が読めた場合はそちらを使う')
    ap.add_argument('--ole-font', type=float, metavar='PX',
                    help='数式の文字サイズ (px)。既定は数式の高さから逆算')
    ap.add_argument('--dump-ole', metavar='DIR',
                    help='OLE 部品の PNG を DIR に書き出して終了する')
    ap.add_argument('--ole-latex', metavar='FILE',
                    help='部品ID と LaTeX の対応表 (JSON または「52 = x_{ref}」形式)')
    args = ap.parse_args()
    OLE_MM = args.ole_size
    OLE_FONT = args.ole_font
    if args.ole_latex:
        OLE_LATEX.update(load_latex_map(args.ole_latex))

    if args.dump_ole:
        for path in args.path:
            if path.lower().endswith('.svg'):
                with open(path, encoding='utf-8') as f:
                    files = Converter(f.read()).dump_ole(args.dump_ole)
                print('%s: OLE部品 %d 個を書き出しました -> %s'
                      % (path, len(files), args.dump_ole))
        return

    jobs = collect_jobs(args.path)
    if not jobs:
        print('変換対象の SVG が見つかりませんでした')
        return 1
    if args.mdpf:
        for job in jobs:
            job['mdpf'] = args.mdpf
    if args.out and len(jobs) > 1:
        print('-o は入力が1つのときだけ使えます')
        return 1
    grand = {}
    for job in jobs:
        st = convert_job(job, args.out if len(jobs) == 1 else None)
        for k, v in st.items():
            grand[k] = grand.get(k, 0) + v
    if len(jobs) > 1:
        print(LF + '合計 %d ファイル' % len(jobs))
        report(grand)


if __name__ == '__main__':

    sys.exit(main())
