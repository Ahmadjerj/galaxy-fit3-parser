#!/usr/bin/env python3
import struct, os, sys, math, argparse
from pathlib import Path
from typing import List, Dict, Optional, Tuple

# ─── Magic / format constants ──────────────────────────────────────────────────
OPPO_MAGIC   = b'oppo'
STYLE_MAGIC  = 0x12345678
IMG_RGB565   = 0x0082
IMG_RGB565A  = 0x0080
FIT3_DISPLAY = (256, 402)

WT_STATIC   = 1
WT_HAND     = 2
WT_SPRITE   = 3
WT_PAIR     = 5
WT_BADGE    = 7
WT_COMP     = 13
WT_ARC      = 16
WT_LINEBAR  = 17

WT_NAMES = {1:'Static',2:'Hand',3:'Sprite',5:'Pair',
            7:'Badge',13:'Comp',16:'Arc',17:'LineBar'}

DATE_KEYS = {
    'weekday_name','month_name','date_day','date_month_num','date_year',
    'world_weekday_name','world_month_name','world_date_day',
    'world_date_month_num','world_date_year',
}

COMP_TYPE: Dict[int, str] = {
    0x00:'none', 0x08:'kcal',   0x09:'date_day',
    0x11:'weekday_name', 0x12:'date_day', 0x13:'month_name',
    0x15:'date_month_num', 0x16:'date_year', 0x18:'date_year',
    0x25:'battery', 0x3E:'temp', 0x72:'ampm',
}
COMP_DATE_TYPES   = {0x12, 0x15, 0x16, 0x18}
COMP_TYPE_ALIASES = {0x7A:0x15, 0x7B:0x11, 0x7C:0x12}
WORLD_COMP_KEYS: Dict[str,str] = {
    'weekday_name':'world_weekday_name','month_name':'world_month_name',
    'date_day':'world_date_day','date_month_num':'world_date_month_num',
    'date_year':'world_date_year',
}

# ─── WF_* binding → data_key ──────────────────────────────────────────────────
# Read from font_N.bin binding table; these are the designer's explicit declarations.
# Generic names (WF_VALUE, WF_COUNT, WF_BIG, WF_SMALL, WF_WORD, WF_OTHERS,
# WF_TIME, WF_BG) carry no intrinsic semantic — resolved via _SEQ_PROTOCOL.
_WF_BINDING_KEY: Dict[str,str] = {
    'WF_AM_PM':  'ampm',
    'WF_BAT':    'battery',   # alias for WF_BATT
    'WF_BATT':   'battery',
    'WF_BMP':    'bpm',       # alias for WF_BPM
    'WF_BPM':    'bpm',
    'WF_EQ':     'battery',   # equalizer arc = battery indicator
    'WF_NAME':   'world_name',
    'WF_PERCENT':'battery',   # percentage text = battery %
    'WF_SLEEP':  'sleep',
    # WF_STEP is a generic single-metric binding; seq_id determines actual data
    # (e.g. seq=41=bpm, seq=48=kcal). Only WF_STEPS (plural) is specifically steps.
    'WF_STEPS':  'steps',
    'WF_TEM':    'temp',
    'WF_WEEK':   'weekday',
}

# ─── Firmware seq_id → data_key ───────────────────────────────────────────────
# Source: The fact that all 14 .bin files I had seen used the same seq id mostly I think there is a better way to find them but i am not that great
# World clock seq_ids confirmed from SM-R390_00100 (only WF_NAME watchface in corpus).
# heuristics — these are firmware level if they were to be true if not they are a packer level that choose to do so and the parser can be fine if they aren't layed the way they are but we may never know unless further investgations
_SEQ_PROTOCOL: Dict[int,str] = {
    2:  'h_tens',
    3:  'h_units',
    10: 'm_tens',
    11: 'm_units',
    14: 's_tens',
    15: 's_units',
    # Activity / health metrics
    29:  'steps',
    37:  'battery',
    41:  'bpm',
    48:  'kcal',
    72:  'floors',
    104: 'sleep',
    115: 'water',
    # World clock (all confirmed from 00100 binary; frame counts match main clock)
    106: 'world_h_tens',
    107: 'world_h_units',
    109: 'world_m_tens',
    110: 'world_m_units',
    116: 'world_name',
    125: 'world_ampm',
}
# Among {29, 48}: lowest seq_id present in a style = 'steps', next = 'kcal'.
# Handles styles that only include one of the two.
# Pretty sure this is wrong but I have no better solution
_ACTIVITY_CHAIN: List[int] = [29, 48]


# ─── Data structures ───────────────────────────────────────────────────────────

class RawImage:
    __slots__ = ('width','height','fmt','ptr','pixel_data')
    def __init__(self,w,h,fmt,ptr,pix):
        self.width=w; self.height=h; self.fmt=fmt; self.ptr=ptr; self.pixel_data=pix


class Widget:
    __slots__ = ('gidx','wtype','seq_id','x','y','width','height',
                 'unk20','rec_size','frame_ptrs','_img_map')
    def __init__(self,gidx,wtype,seq_id,x,y,width,height,unk20,rec_size,ptrs):
        self.gidx=gidx; self.wtype=wtype; self.seq_id=seq_id
        self.x=x; self.y=y; self.width=width; self.height=height
        self.unk20=unk20; self.rec_size=rec_size; self.frame_ptrs=ptrs
        self._img_map: Dict[int,RawImage] = {}

    @property
    def static_image(self): return self._img_map.get(self.unk20)

    # ── Arc ──
    @property
    def arc_start(self) -> int:
        """
        Start angle stored directly as PIL degrees (0=3 o'clock, CW).
        Always ptr[0].hi for both mode=0 and mode=1.
        mode=0: fill start, arc grows CW from here.
        mode=1: fixed end point, fill grows CW from here.
        """
        if not self.frame_ptrs:
            return 270  # 12 o'clock
        return (self.frame_ptrs[0] >> 16) & 0xFFFF

    @property
    def arc_sweep(self) -> int:
        """
        Total track sweep in degrees.
        mode=0: ptr[1].hi - ptr[0].lo
        mode=1: ptr[1].hi
        """
        if not self.frame_ptrs:
            return 270
        mode = self.frame_ptrs[2] if len(self.frame_ptrs) > 2 else 0
        hi1 = (self.frame_ptrs[1] >> 16) & 0xFFFF if len(self.frame_ptrs) > 1 else 0
        if mode == 1:
            return hi1 if hi1 > 0 else 270
        lo0 = self.frame_ptrs[0] & 0xFFFF
        sweep = hi1 - lo0
        return sweep if sweep > 0 else (hi1 if hi1 > 0 else 270)

    @property
    def arc_fill_image(self) -> Optional['RawImage']:
        if len(self.frame_ptrs) >= 5:
            return self._img_map.get(self.frame_ptrs[4])
        return None

    @property
    def arc_mode(self) -> int:
        return self.frame_ptrs[2] if len(self.frame_ptrs) >= 3 else 0

    # ── Badge ──
    @property
    def badge_thickness(self) -> int:
        if len(self.frame_ptrs) >= 4:
            t = self.frame_ptrs[3] & 0xFF
            if t >= 2: return t
        return 8

    @property
    def badge_track_color(self) -> Optional[Tuple[int,int,int]]:
        if not self.frame_ptrs: return None
        return _bgra_color_allow_black(self.frame_ptrs[0])

    @property
    def badge_fill_color(self) -> Optional[Tuple[int,int,int]]:
        for i in (1,2):
            if len(self.frame_ptrs) > i:
                c = _bgra_color_allow_black(self.frame_ptrs[i])
                if c is not None: return c
        return None

    @property
    def badge_endpoints(self) -> Tuple[float,float,float,float]:
        return (float(self.x), float(self.y),
                float(_u16_to_s16(self.width)), float(_u16_to_s16(self.height)))

    # ── LineBar ──
    @property
    def linebar_width(self): return self.width
    @property
    def linebar_height(self): return self.height

    @property
    def linebar_bg_color(self) -> Optional[Tuple[int,int,int]]:
        if self.frame_ptrs:
            gray = (self.frame_ptrs[0] >> 16) & 0xFF
            if gray > 0: return (gray,gray,gray)
            lo16 = self.frame_ptrs[0] & 0xFFFF
            if lo16 not in (0,0xFFFF):
                c = _bgr565(lo16)
                if c != (0,0,0): return c
        return None

    @property
    def linebar_fill(self):
        if len(self.frame_ptrs) >= 3:
            p = self.frame_ptrs[2]
            img = self._img_map.get(p)
            if img: return img
            lo16 = p & 0xFFFF
            if lo16 not in (0,0xFFFF): return _bgr565(lo16)
        return None

    # ── Pair ──
    def pair_color(self) -> Tuple[int,int,int]:
        if self.frame_ptrs:
            c = _bgra_color_allow_black(self.frame_ptrs[0])
            if c is not None: return c
        return (0,0,0)

    def pair_font_idx(self) -> int:
        return self.frame_ptrs[1] & 0xFF if len(self.frame_ptrs) >= 2 else 0

    def pair_align(self) -> int:
        return (self.frame_ptrs[1] >> 8) & 0xFF if len(self.frame_ptrs) >= 2 else 0

    def pair_layout(self) -> int:
        return (self.frame_ptrs[1] >> 16) & 0xFF if len(self.frame_ptrs) >= 2 else 0

    def pair_suffix_glyph_idx(self) -> int:
        return self.frame_ptrs[2] & 0xFF if len(self.frame_ptrs) >= 3 else 0xFF

    # ── Sprite ──
    def sprite_frame_count(self) -> int:
        return self.unk20 & 0xFFFFFF

    def sprite_image(self, idx: int) -> Optional['RawImage']:
        if idx < len(self.frame_ptrs):
            return self._img_map.get(self.frame_ptrs[idx])
        return None

    # ── Hand ──
    def hand_rot_start(self) -> int:
        return self.frame_ptrs[0] & 0xFFFF if self.frame_ptrs else 0

    def hand_rot_range(self) -> int:
        return (self.frame_ptrs[0] >> 16) if self.frame_ptrs else 360

    @property
    def hand_pivot_x(self) -> int: return self.unk20 & 0xFF
    @property
    def hand_pivot_y(self) -> int: return (self.unk20 >> 16) & 0xFF

    def hand_image(self) -> Optional['RawImage']:
        if len(self.frame_ptrs) >= 2:
            img = self._img_map.get(self.frame_ptrs[1])
            if img: return img
        if self.frame_ptrs: return self._img_map.get(self.frame_ptrs[0])
        return None


class CompSub:
    __slots__ = ('data_type','prefix_idx','glyph_idx','spacing','word2')
    def __init__(self,data_type,prefix_idx,glyph_idx,spacing,word2):
        self.data_type=data_type; self.prefix_idx=prefix_idx
        self.glyph_idx=glyph_idx; self.spacing=spacing; self.word2=word2

    @property
    def has_separator(self): return (self.word2 & 0xFF) != 0


class GlyphGroup:
    __slots__ = ('chars',)
    def __init__(self, chars): self.chars = chars
    def joined(self): return ''.join(self.chars)
    def __len__(self): return len(self.chars)


class FontBinding:
    __slots__ = ('name','pt_size','font_family')
    def __init__(self, name, pt_size, font_family=0):
        self.name=name; self.pt_size=pt_size; self.font_family=font_family


class Style:
    __slots__ = ('index','images','widgets','_hand_metrics_cache')
    def __init__(self,index,images,widgets):
        self.index=index; self.images=images; self.widgets=widgets
        self._hand_metrics_cache=None


class Watchface:
    __slots__ = ('filename','wf_id','version','styles','bindings','glyphs')
    def __init__(self,filename,wf_id,version,styles,bindings,glyphs):
        self.filename=filename; self.wf_id=wf_id; self.version=version
        self.styles=styles; self.bindings=bindings; self.glyphs=glyphs

    def font_pt(self, font_idx: int, widget_unk20: int = 0) -> int:
        if 0 <= font_idx < len(self.bindings):
            pt = self.bindings[font_idx].pt_size
            if 8 <= pt <= 120: return pt
        fb_idx = widget_unk20 & 0xFF
        if 0 <= fb_idx < len(self.bindings):
            pt = self.bindings[fb_idx].pt_size
            if 8 <= pt <= 120: return pt
        if 12 <= font_idx <= 80: return font_idx
        return 16

    def comp_font_pt(self, widget: Widget) -> int:
        candidates = []
        for ptr_idx in (14, 15):
            if ptr_idx < len(widget.frame_ptrs):
                p = widget.frame_ptrs[ptr_idx]
                candidates.extend(((p >> 16) & 0xFFFF, p & 0xFFFF))
        candidates.extend(((widget.unk20 >> 16) & 0xFF, widget.unk20 & 0xFF))
        for cand in candidates:
            if 0 <= cand < len(self.bindings):
                pt = self.bindings[cand].pt_size
                if 8 <= pt <= 120: return pt
        literal = (widget.unk20 >> 16) & 0xFFFF
        if 8 <= literal <= 120: return literal
        return 20

    def glyph_groups(self, locale='en') -> List[GlyphGroup]:
        for k in (locale,'en','en_US','en_GB'):
            if k in self.glyphs: return self.glyphs[k]
        if self.glyphs: return next(iter(self.glyphs.values()))
        return []

    def glyph_str(self, group_idx: int, locale='en') -> str:
        if group_idx == 0xFF: return ''
        groups = self.glyph_groups(locale)
        if 0 <= group_idx < len(groups): return groups[group_idx].joined()
        return ''


# ─── Low-level helpers ─────────────────────────────────────────────────────────

def _u16_to_s16(v): return struct.unpack('<h',struct.pack('<H',v&0xFFFF))[0]

def _bgra_color(v) -> Optional[Tuple[int,int,int]]:
    if (v>>24)&0xFF != 0xFF: return None
    r=(v>>16)&0xFF; g=(v>>8)&0xFF; b=v&0xFF
    if r==0 and g==0 and b==0: return None
    return (r,g,b)

def _bgra_color_allow_black(v) -> Optional[Tuple[int,int,int]]:
    if (v>>24)&0xFF != 0xFF: return None
    return ((v>>16)&0xFF,(v>>8)&0xFF,v&0xFF)

def _bgr565(lo16) -> Tuple[int,int,int]:
    return ((lo16&0x1F)*255//31,((lo16>>5)&0x3F)*255//63,((lo16>>11)&0x1F)*255//31)


# ─── Binary parsing ────────────────────────────────────────────────────────────

def parse_container(data: bytes) -> List[Tuple[str,int,int]]:
    if data[:4] != OPPO_MAGIC:
        raise ValueError(f"Not OPPO magic (got {data[:4].hex()})")
    count = struct.unpack_from('<I',data,0x0C)[0]
    entries = []
    for i in range(count):
        base = 0x20 + i*74
        raw_name = data[base:base+64].split(b'\x00')[0]
        name = raw_name.decode('ascii','replace').split('/')[-1]
        off  = struct.unpack_from('<I',data,base+0x40)[0]
        size = struct.unpack_from('<I',data,base+0x44)[0]
        entries.append((name,off,size))
    return entries


def scan_images(raw_style: bytes, sec_rel: int) -> Tuple[List[RawImage],Dict[int,RawImage]]:
    section = raw_style[sec_rel:]
    images: List[RawImage] = []; pm: Dict[int,RawImage] = {}
    pos = 0
    while pos+12 <= len(section):
        w=struct.unpack_from('<H',section,pos)[0]; h=struct.unpack_from('<H',section,pos+2)[0]
        fmt=struct.unpack_from('<H',section,pos+4)[0]; sz=struct.unpack_from('<I',section,pos+8)[0]
        if fmt not in (IMG_RGB565,IMG_RGB565A): break
        bpp=3 if fmt==IMG_RGB565A else 2
        expected=w*h*bpp
        if sz==0 or abs(sz-expected)>16: break
        pix=section[pos+12:pos+12+sz]
        ri=RawImage(w,h,fmt,pos,pix); images.append(ri); pm[pos]=ri
        pos+=12+sz
    return images, pm


def walk_widgets(raw_style: bytes) -> Tuple[List[Widget],List[RawImage]]:
    magic = struct.unpack_from('<I',raw_style,0)[0]
    if magic != STYLE_MAGIC:
        raise ValueError(f"Bad style magic {magic:#010x}")
    wcount  = struct.unpack_from('<I',raw_style,0x04)[0]
    sec_rel = struct.unpack_from('<I',raw_style,0x14)[0]
    images, pm = scan_images(raw_style, sec_rel)
    stream = raw_style[0x18:sec_rel]
    widgets: List[Widget] = []
    off = 0
    while off < len(stream)-36 and len(widgets) < wcount:
        idx_sz   = struct.unpack_from('<I',stream,off+0x0C)[0]
        rec_size = idx_sz & 0xFFFF
        gidx     = idx_sz >> 16
        if not (36 <= rec_size <= 600 and rec_size%2==0 and gidx < 1024):
            off += 4; continue
        wtype  = struct.unpack_from('<I',stream,off+0x00)[0]
        seq_id = struct.unpack_from('<I',stream,off+0x04)[0]
        x      = struct.unpack_from('<h',stream,off+0x18)[0]
        y      = struct.unpack_from('<h',stream,off+0x1A)[0]
        width  = struct.unpack_from('<H',stream,off+0x1C)[0]
        height = struct.unpack_from('<H',stream,off+0x1E)[0]
        unk20  = struct.unpack_from('<I',stream,off+0x20)[0]
        n_ptrs = (rec_size-36)//4
        ptrs   = []
        for i in range(n_ptrs):
            ptr_off = off+0x24+i*4
            if ptr_off+4 <= len(stream):
                ptrs.append(struct.unpack_from('<I',stream,ptr_off)[0])
        w = Widget(gidx,wtype,seq_id,x,y,width,height,unk20,rec_size,ptrs)
        w._img_map = pm
        widgets.append(w)
        off += rec_size
    return widgets, images


def parse_glyph_file(raw: bytes, locale: str) -> Optional[List[GlyphGroup]]:
    if len(raw) < 0x18: return None
    if struct.unpack_from('<I',raw,0)[0] != STYLE_MAGIC: return None
    gc = struct.unpack_from('<I',raw,8)[0]
    groups: List[GlyphGroup] = []
    for i in range(gc):
        b = 0x18+i*8
        if b+8 > len(raw): break
        cnt  = struct.unpack_from('<I',raw,b)[0]
        soff = struct.unpack_from('<I',raw,b+4)[0]
        chars: List[str] = []; idx = soff
        for _ in range(cnt):
            if idx >= len(raw): break
            c = raw[idx]
            if   c < 0x80: chars.append(chr(c));                                    idx+=1
            elif c < 0xE0: chars.append(raw[idx:idx+2].decode('utf-8','replace'));  idx+=2
            elif c < 0xF0: chars.append(raw[idx:idx+3].decode('utf-8','replace'));  idx+=3
            else:           chars.append(raw[idx:idx+4].decode('utf-8','replace'));  idx+=4
        groups.append(GlyphGroup(chars))
    return groups


def parse_font_binding(raw: bytes) -> Optional[FontBinding]:
    if len(raw) < 92: return None
    name    = raw[0x48:0x58].split(b'\x00')[0].decode('ascii','replace')
    pt_size = struct.unpack_from('<I',raw,0x58)[0]
    # byte0 of [0x00] = font family index (0=default, 1-7=named families)
    font_family = raw[0] if len(raw) > 0 else 0
    return FontBinding(name,pt_size,font_family) if name else None


def parse_watchface(data: bytes, filename: str) -> Watchface:
    entries = parse_container(data)
    emap    = {n:(o,s) for n,o,s in entries}

    wf_id, version = '?', 0
    if 'setting.bin' in emap:
        o,s = emap['setting.bin']; raw = data[o:o+s]
        wf_id   = raw[0x10:0x20].split(b'\x00')[0].decode('ascii','replace')
        version = struct.unpack_from('<I',raw,0x30)[0]

    bindings: List[FontBinding] = []
    binding_names = sorted(
        [n for n,o,s in entries if n.startswith('font') and n.endswith('.bin') and 64<=s<=512],
        key=lambda n: int(''.join(c for c in n if c.isdigit()) or '-1')
    )
    for n in binding_names:
        o,s = emap[n]; raw_fb = data[o:o+s]
        if len(raw_fb)>=4 and struct.unpack_from('<I',raw_fb,0)[0]==STYLE_MAGIC: continue
        fb = parse_font_binding(raw_fb)
        if fb: bindings.append(fb)

    glyphs: Dict[str,List[GlyphGroup]] = {}
    for n,o,s in entries:
        if not (n.startswith('font') and n.endswith('.bin')) or s==92: continue
        locale = n.replace('font_','').replace('.bin','').replace('_r','-r')
        gs = parse_glyph_file(data[o:o+s], locale)
        if gs: glyphs[locale] = gs

    styles: List[Style] = []
    for n,o,s in entries:
        if not (n.startswith('style') and n.endswith('.bin')): continue
        if struct.unpack_from('<I',data,o)[0] != STYLE_MAGIC: continue
        idx = int(''.join(c for c in n if c.isdigit()) or '0')
        try:
            widgets, images = walk_widgets(data[o:o+s])
            styles.append(Style(idx,images,widgets))
        except Exception as e:
            print(f"  warn: style {n}: {e}")
    styles.sort(key=lambda s: s.index)

    return Watchface(filename,wf_id,version,styles,bindings,glyphs)


# ─── Composite sub-element parser ─────────────────────────────────────────────

def _normalize_comp_data_type(data_type: int) -> int:
    return COMP_TYPE_ALIASES.get(data_type, data_type)


def _world_comp_key(key: str) -> str:
    return WORLD_COMP_KEYS.get(key, key)


def parse_comp_subs(ptrs: List[int]) -> List[CompSub]:
    subs: List[CompSub] = []
    i = 0
    while i < len(ptrs):
        p    = ptrs[i]
        st   = p & 0xFFFF
        norm = _normalize_comp_data_type(st)
        hi16 = (p >> 16) & 0xFFFF
        if norm in COMP_TYPE and norm not in (0x0000,0xFFFF) and (hi16==0xFFFF or hi16<0x0100):
            word1 = ptrs[i+1] if i+1 < len(ptrs) else 0
            word2 = ptrs[i+2] if i+2 < len(ptrs) else 0
            prefix_idx = hi16 if hi16 < 0x0100 else 0xFFFF
            glyph_idx  = word1 & 0xFFFF
            spacing    = (word1 >> 16) & 0xFFFF
            step = 3
            if i+5 < len(ptrs) and ptrs[i+3]==0xFFFF0000:
                aux=ptrs[i+4]; aux_hi=(aux>>16)&0xFFFF; aux_lo=aux&0xFFFF
                if glyph_idx==0xFFFF and aux_lo==0xFFFF and aux_hi<0x0100:
                    glyph_idx=aux_hi; step=6
            subs.append(CompSub(st,prefix_idx,glyph_idx,spacing,word2))
            i += step; continue
        if p == 0xFFFFFFFF: i+=1; continue
        i += 1
    return subs


def _resolve_comp_key(subs: List[CompSub], idx: int,
                      groups: Optional[List[GlyphGroup]]=None) -> str:
    raw_st = subs[idx].data_type
    st     = _normalize_comp_data_type(raw_st)
    is_world = raw_st in COMP_TYPE_ALIASES
    if st == 0x15:
        sub = subs[idx]
        has_year = any(_normalize_comp_data_type(e.data_type) in (0x16,0x18) for e in subs)
        if has_year:
            return _world_comp_key('date_month_num') if is_world else 'date_month_num'
        sep = ''
        if groups and 0 <= sub.glyph_idx < len(groups) and sub.glyph_idx != 0xFFFF:
            sep = groups[sub.glyph_idx].joined()
        sep_s = sep.strip()
        if '/' in sep or '-' in sep:
            k = 'date_month_num'
        elif sep_s and all(not ch.isalpha() and not ch.isspace() for ch in sep_s):
            k = 'date_month_num'
        else:
            k = 'month_name'
        return _world_comp_key(k) if is_world else k
    key = COMP_TYPE.get(st,'')
    return _world_comp_key(key) if is_world else key


# ─── Seq_id → data_key inference ──────────────────────────────────────────────

def _hand_widget_metrics(style: Style) -> List[Tuple[Widget,int,int]]:
    if style._hand_metrics_cache is not None:
        return style._hand_metrics_cache
    result = []
    for w in style.widgets:
        if w.wtype != WT_HAND or w.seq_id==0 or not w.hand_image(): continue
        img = w.hand_image()
        px = w.hand_pivot_x if w.hand_pivot_x > 0 else (img.width//2 if img else 0)
        py = w.hand_pivot_y if w.hand_pivot_y > 0 else (img.height if img else 0)
        length = max(py, img.height if img else 0, px, img.width if img else 0)
        result.append((w,length,py))
    style._hand_metrics_cache = result
    return result


def _binding_name(widget: Widget, wf: Watchface) -> str:
    if widget.wtype != WT_PAIR: return ''
    fidx = widget.pair_font_idx()
    if 0 <= fidx < len(wf.bindings):
        return wf.bindings[fidx].name.strip()
    return ''


def _assign_clock_group(pool: List[Widget], prefix: str,
                        cap_at_minutes: bool=False) -> Dict[int,str]:
    """Assign h/m(/s) digit sprite roles from frame counts and binary stream order."""
    assigned: Dict[int,str] = {}
    threes = sorted([w for w in pool if w.sprite_frame_count()==3],  key=lambda w: w.gidx)
    sixes  = sorted([w for w in pool if w.sprite_frame_count()==6],  key=lambda w: w.gidx)
    tens   = sorted([w for w in pool if w.sprite_frame_count()==10], key=lambda w: w.gidx)
    if not threes: return assigned
    used: set = set()

    ht = threes[0]; assigned[ht.seq_id] = f'{prefix}h_tens'
    for u in tens:
        if u.gidx > ht.gidx:
            assigned[u.seq_id]=f'{prefix}h_units'; used.add(u.seq_id); break

    if sixes:
        mt = sixes[0]; assigned[mt.seq_id]=f'{prefix}m_tens'
        for u in tens:
            if u.seq_id not in used and u.gidx > mt.gidx:
                assigned[u.seq_id]=f'{prefix}m_units'; used.add(u.seq_id); break
        if not cap_at_minutes and len(sixes)>1:
            st2 = sixes[1]; assigned[st2.seq_id]=f'{prefix}s_tens'
            for u in tens:
                if u.seq_id not in used and u.gidx > st2.gidx:
                    assigned[u.seq_id]=f'{prefix}s_units'; used.add(u.seq_id); break

    return assigned


def infer_seq_map(style: Style, wf: Watchface) -> Dict[int,str]:
    """
    Build per-style seq_id → data_key mapping from binary data only.

    Path A — read from file:
      A1. Specific WF_* binding names (font_N.bin table)
      A2. Arc sweep angles: 270° = sleep, 120°/mode=1-limit=300° = battery
      A3. Sprite frame counts: 24=weather_icon, 3/6/10=clock digits
      A4. Analog hand geometry → hour/minute/second
      A5. seq 71 widget-type mix: LineBar+Pair=activity, Badge+Pair=active_hours

    Path B — firmware protocol table (_SEQ_PROTOCOL):
      For any seq_id still unresolved after Path A.
      Includes world clock seq_ids (106,107,109,110,116,125) — no spatial inference.
    """
    seq_map: Dict[int,str] = {}
    style_seq_ids = {w.seq_id for w in style.widgets if w.seq_id!=0}
    by_seq: Dict[int,List[Widget]] = {}
    for w in style.widgets:
        if w.seq_id!=0: by_seq.setdefault(w.seq_id,[]).append(w)

    # ── A1: WF_* binding names ────────────────────────────────────────────────
    ampm_seen: List[Widget] = []
    for w in style.widgets:
        if w.wtype != WT_PAIR or w.seq_id==0: continue
        fidx = w.pair_font_idx()
        if not (0 <= fidx < len(wf.bindings)): continue
        bname = wf.bindings[fidx].name.strip()

        if bname == 'WF_AM_PM':
            ampm_seen.append(w)
        elif bname == 'WF_DATE':
            sgi = w.pair_suffix_glyph_idx()
            suf = wf.glyph_str(sgi).strip().lower() if sgi!=0xFF else ''
            weekday_names = {'mon','tue','wed','thu','fri','sat','sun',
                             'monday','tuesday','wednesday','thursday',
                             'friday','saturday','sunday'}
            seq_map[w.seq_id] = 'weekday' if suf in weekday_names else 'date_day'
        elif bname in _WF_BINDING_KEY:
            seq_map[w.seq_id] = _WF_BINDING_KEY[bname]

    for i, w in enumerate(sorted(ampm_seen, key=lambda w: w.gidx)):
        seq_map[w.seq_id] = 'world_ampm' if i > 0 else 'ampm'

    # ── A2: Arc sweep angles ──────────────────────────────────────────────────
    for seq, ws in by_seq.items():
        if seq in seq_map: continue
        for w in ws:
            if w.wtype == WT_ARC:
                sweep = w.arc_sweep  # uses mode-aware property
                if sweep == 120: seq_map[seq] = 'battery'
                elif sweep == 270: seq_map[seq] = 'sleep'

    # ── A3: Sprite frame counts ───────────────────────────────────────────────
    sprites = [w for w in style.widgets if w.wtype==WT_SPRITE and w.seq_id!=0]
    for w in sprites:
        if w.sprite_frame_count()==24 and w.seq_id not in seq_map:
            seq_map[w.seq_id] = 'weather_icon'

    # World clock sprites already handled by _SEQ_PROTOCOL (path B),
    # but we need to know if world clock exists to cap main clock at minutes.
    has_world_clock = any(s in style_seq_ids for s in (106,107,109,110))

    unassigned = [w for w in sprites if w.seq_id not in seq_map]
    seq_map.update(_assign_clock_group(unassigned, '', cap_at_minutes=has_world_clock))
    if has_world_clock:
        remaining = [w for w in unassigned if w.seq_id not in seq_map]
        seq_map.update(_assign_clock_group(remaining, 'world_', cap_at_minutes=True))

    # ── A4: Analog hand geometry ──────────────────────────────────────────────
    hand_metrics = _hand_widget_metrics(style)
    if hand_metrics:
        ordered = sorted(hand_metrics, key=lambda t: t[1])
        if len(ordered) >= 3:
            seq_map[ordered[0][0].seq_id]  = 'hour'
            seq_map[ordered[1][0].seq_id]  = 'minute'
            seq_map[ordered[-1][0].seq_id] = 'second'
        elif len(ordered) == 2:
            seq_map[ordered[0][0].seq_id]  = 'hour'
            seq_map[ordered[-1][0].seq_id] = 'minute'

    # ── A5: seq 71 widget-type mix ────────────────────────────────────────────
    if 71 in by_seq and 71 not in seq_map:
        ws71 = by_seq[71]
        has_lb   = any(w.wtype==WT_LINEBAR for w in ws71)
        has_ba   = any(w.wtype==WT_BADGE   for w in ws71)
        has_pair = any(w.wtype==WT_PAIR     for w in ws71)
        if has_lb and has_pair:   seq_map[71] = 'activity'
        elif has_ba and has_pair: seq_map[71] = 'active_hours'
        elif has_ba:              seq_map[71] = 'activity'

    # ── B: Firmware protocol table ────────────────────────────────────────────
    for seq_id in style_seq_ids:
        if seq_id not in seq_map and seq_id in _SEQ_PROTOCOL:
            seq_map[seq_id] = _SEQ_PROTOCOL[seq_id]

    # Activity-chain ranking
    active_present = [s for s in _ACTIVITY_CHAIN if s in style_seq_ids]
    for sid, role in zip(active_present, ('steps','kcal')):
        seq_map[sid] = role

    return seq_map


# ─── Demo/simulation data ──────────────────────────────────────────────────────

def _default_demo() -> dict:
    hh,mm = 10,8
    return dict(
        hour=hh, minute=mm, second=30,
        h_tens=hh//10, h_units=hh%10,
        m_tens=mm//10, m_units=mm%10,
        date=28, month=11, weekday=5, year=2023,
        steps=3457, bpm=78, battery=100,
        temp=23, kcal=350, floors=3, sleep=7.833,
        activity=78, activity_goal=90,
        active_hours=3, active_hours_goal=8,
        step_goal=6000, kcal_goal=500, floors_goal=10,
        sleep_goal=10.0, water=250, water_goal=2000,
        temp_min=-20, temp_max=45,
    )


def _demo_value(dk: str, demo: dict) -> str:
    wh = int(demo.get('world_hour', demo['hour']))
    wdh = ((wh-1)%12)+1
    wm = int(demo.get('world_minute', demo['minute']))
    wd = int(demo.get('world_weekday', demo['weekday']))
    wdate = int(demo.get('world_date', demo['date']))
    wmonth= int(demo.get('world_month', demo['month']))
    wyear = int(demo.get('world_year', demo.get('year',2023)))

    if dk=='h_tens':         return str(demo['h_tens'])
    if dk=='h_units':        return str(demo['h_units'])
    if dk=='m_tens':         return str(demo['m_tens'])
    if dk=='m_units':        return str(demo['m_units'])
    if dk=='s_tens':         return str(demo['second']//10)
    if dk=='s_units':        return str(demo['second']%10)
    if dk=='world_h_tens':   return str(wdh//10)
    if dk=='world_h_units':  return str(wdh%10)
    if dk=='world_m_tens':   return str(wm//10)
    if dk=='world_m_units':  return str(wm%10)
    if dk=='hour':           return str(demo['hour'])
    if dk=='minute':         return f"{demo['minute']:02d}"
    if dk=='second':         return f"{demo['second']:02d}"
    if dk=='steps':          return str(demo['steps'])
    if dk=='bpm':            return str(demo['bpm'])
    if dk=='battery':        return str(demo['battery'])
    if dk=='temp':           return str(demo['temp'])
    if dk=='kcal':           return str(demo.get('kcal',350))
    if dk=='floors':         return str(demo.get('floors',3))
    if dk=='activity':       return str(demo.get('activity',78))
    if dk=='active_hours':   return str(demo.get('active_hours',3))
    if dk=='water':          return str(demo.get('water',250))
    if dk=='sleep':
        mins=int(round(demo.get('sleep',7)*60)); h2,m2=divmod(mins,60)
        return f"{h2}h{m2:02d}m"
    if dk=='ampm':           return 'a.m.' if demo['hour']<12 else 'p.m.'
    if dk=='world_ampm':     return 'a.m.' if wh<12 else 'p.m.'
    if dk=='weekday':
        return ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][demo['weekday']%7]
    if dk=='world_name':     return str(demo.get('world_name',''))
    if dk=='date_day':       return str(demo['date'])
    if dk=='date_month_num': return f"{demo['month']+1:02d}"
    if dk=='date_year':      return str(demo.get('year',2023))
    if dk=='world_date_day':       return str(wdate)
    if dk=='world_date_month_num': return f"{wmonth+1:02d}"
    if dk=='world_date_year':      return str(wyear)
    if dk=='weekday_name':
        return ['Monday','Tuesday','Wednesday','Thursday',
                'Friday','Saturday','Sunday'][demo['weekday']%7]
    if dk=='world_weekday_name':
        return ['Monday','Tuesday','Wednesday','Thursday',
                'Friday','Saturday','Sunday'][wd%7]
    if dk=='month_name':
        return ['Jan','Feb','Mar','Apr','May','Jun',
                'Jul','Aug','Sep','Oct','Nov','Dec'][demo['month']%12]
    if dk=='world_month_name':
        return ['Jan','Feb','Mar','Apr','May','Jun',
                'Jul','Aug','Sep','Oct','Nov','Dec'][wmonth%12]
    return ''


def _progress(dk: str, demo: dict) -> float:
    if dk=='battery':  return demo['battery']/100.0
    if dk=='steps':    return min(demo['steps']/max(demo.get('step_goal',6000),1),1.0)
    if dk=='bpm':      return min(demo['bpm']/220.0,1.0)
    if dk=='temp':
        tmin=demo.get('temp_min',-20); tmax=demo.get('temp_max',45)
        return min(max((demo['temp']-tmin)/max(tmax-tmin,1),0.0),1.0)
    if dk=='kcal':     return min(demo.get('kcal',350)/max(demo.get('kcal_goal',500),1),1.0)
    if dk=='sleep':    return min(demo.get('sleep',7)/max(demo.get('sleep_goal',10),1),1.0)
    if dk=='activity': return min(demo.get('activity',78)/max(demo.get('activity_goal',90),1),1.0)
    if dk=='active_hours':
        return min(demo.get('active_hours',3)/max(demo.get('active_hours_goal',8),1),1.0)
    if dk=='water':    return min(demo.get('water',250)/max(demo.get('water_goal',2000),1),1.0)
    if dk=='weekday':  return (demo['weekday']+1)/7.0
    if dk=='floors':   return min(demo.get('floors',3)/max(demo.get('floors_goal',10),1),1.0)
    return 0.0


# ─── PIL / image helpers ───────────────────────────────────────────────────────

_font_cache: Dict = {}

# System font fallbacks (full Unicode coverage, available on most Linux systems).
# Used when APK Samsung fonts are not present.  DejaVu is preferred because it
# has the widest character coverage including °, %, and CJK punctuation.
_SYS_FONTS_BOLD = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
]
_SYS_FONTS_REGULAR = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
]
_SYS_FONTS_CONDENSED = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
]
_SYS_FONTS_CONDENSED_BOLD = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
]

def _font_kind(text: str) -> str:
    s = text.strip()
    return 'numeric' if s and all(ch.isdigit() for ch in s) else 'text'


def _get_font(pt: int, bold: bool = False, text: str = '', font_family: int = 0):
    """
    Resolve a font for rendering.

    Priority:
      1. Samsung APK fonts (from extracted APK directory, if present)
      2. System fonts with full Unicode coverage (DejaVu Sans, Liberation Sans)

    font_family mapping (from font binding [0x00] byte0):
      0, 1 → regular weight
      2, 3 → bold / display weight
      7    → bold display (large numerics)
    """
    from PIL import ImageFont
    kind  = _font_kind(text)
    use_bold = bold or font_family >= 2
    key = (pt, use_bold, kind)
    if key in _font_cache: return _font_cache[key]

    search = []

    # 1. Samsung APK fonts (best match if APK is extracted)
    if use_bold:
        search += [
            'APK/com.samsung.wearable.fit3/assets/font/sec_bold.ttf',
            'APK/wearable_decoded/assets/font/sec_bold.ttf',
            'APK/wearable_decoded/res/font/newsec_700_bold.ttf',
        ]
    if kind == 'numeric':
        search += [
            'APK/com.samsung.wearable.fit3/assets/fonts/SamsungOneNum-R_20150313.ttf',
            'APK/wearable_decoded/assets/fonts/SamsungOneNum-R_20150313.ttf',
            'APK/com.samsung.wearable.fit3/assets/fonts/SamsungSansNum-3T_20150313.ttf',
            'APK/wearable_decoded/assets/fonts/SamsungSansNum-3T_20150313.ttf',
        ]
    else:
        search += [
            'APK/com.samsung.wearable.fit3/assets/fonts/SAMSUNGCONDENSED_LT_0.TTF',
            'APK/wearable_decoded/assets/fonts/SAMSUNGCONDENSED_LT_0.TTF',
            'APK/com.samsung.wearable.fit3/assets/fonts/SamsungOneFluid-R_20150313.ttf',
            'APK/wearable_decoded/assets/fonts/SamsungOneFluid-R_20150313.ttf',
            'APK/wearable_decoded/res/font/newsec_400_regular.ttf',
            'APK/wearable_decoded/res/font/newsec_300_light.ttf',
        ]

    # 2. System font fallbacks — full Unicode, always available on Linux
    if use_bold:
        search += _SYS_FONTS_CONDENSED_BOLD if kind == 'numeric' else _SYS_FONTS_BOLD
    else:
        search += _SYS_FONTS_CONDENSED if kind == 'numeric' else _SYS_FONTS_REGULAR

    font = None
    for p in search:
        if os.path.exists(p):
            try: font = ImageFont.truetype(p, pt); break
            except Exception: pass

    if font is None:
        # Absolute last resort: PIL bitmap font (no Unicode, but never crashes)
        font = ImageFont.load_default()

    _font_cache[key] = font
    return font


def _text_bbox(text: str, font) -> Tuple[int,int,int,int]:
    from PIL import ImageDraw, Image as PI
    if not text: return (0,0,0,0)
    d = ImageDraw.Draw(PI.new('L',(1,1)))
    try:    return d.textbbox((0,0),text,font=font)
    except Exception:
        try:    return font.getbbox(text)
        except: return (0,0,0,0)


def _text_width(text: str, font) -> int:
    if not text: return 0
    bb = _text_bbox(text,font)
    bw = max(bb[2]-bb[0],0)
    if bw > 0: return bw
    try:    return int(math.ceil(font.getlength(text)))
    except: return bw


def _raw_to_pil(img: RawImage):
    from PIL import Image as PI
    n = img.width*img.height
    def r565(pv): return(((pv>>11)&0x1F)*255//31,((pv>>5)&0x3F)*255//63,(pv&0x1F)*255//31)
    if img.fmt == IMG_RGB565:
        pil=PI.new('RGB',(img.width,img.height))
        pil.putdata([r565(struct.unpack_from('<H',img.pixel_data,i*2)[0]) for i in range(n)])
    else:
        pil=PI.new('RGBA',(img.width,img.height))
        
        def _get_rgba(i):
            rgb = r565(struct.unpack_from('<H',img.pixel_data,i*3)[0])
            a = img.pixel_data[i*3+2]
            return (0,0,0,0) if a == 0 else (*rgb, a)
            
        pil.putdata([_get_rgba(i) for i in range(n)])
    return pil


def _paste(canvas, pil_img, x, y):
    if pil_img is None: return
    W,H=canvas.size; iw,ih=pil_img.size
    sx=max(0,-x); sy=max(0,-y); dx=max(0,x); dy=max(0,y)
    cw=min(iw-sx,W-dx); ch=min(ih-sy,H-dy)
    if cw<=0 or ch<=0: return
    region=pil_img.crop((sx,sy,sx+cw,sy+ch))
    if region.mode=='RGBA': canvas.paste(region.convert('RGB'),(dx,dy),region.split()[3])
    else: canvas.paste(region,(dx,dy))


def _draw_text(canvas, text, x, y, pt, color, bold=False, align=0, font_family=0) -> int:
    from PIL import ImageDraw
    if not text: return 0
    font=_get_font(pt,bold,text,font_family); draw=ImageDraw.Draw(canvas)
    bb=_text_bbox(text,font); bw=max(bb[2]-bb[0],0)
    dx=x
    if align==1: dx-=bw/2
    elif align==2: dx-=bw
    # Firmware centers using the nominal pt size as the cell height (not the actual
    # bounding-box height which varies per-string and excludes descenders for digit-
    # only text).  Using pt/2 makes visual top ≈ ay - pt/2, which matches the watch
    # preview for all widget heights (large h that would otherwise shift text down).
    dx-=bb[0]; dy=y-(pt/2)-bb[1]
    draw.text((int(round(dx)),int(round(dy))),text,font=font,fill=color)
    return _text_width(text,font)


def _draw_text_piece(canvas, text, x, origin_y, font, color) -> int:
    from PIL import ImageDraw
    if not text: return 0
    draw=ImageDraw.Draw(canvas)
    bb=_text_bbox(text,font)
    draw.text((int(round(x-bb[0])),int(round(origin_y))),text,font=font,fill=color)
    return _text_width(text,font)


def _draw_capsule(canvas, x1, y1, x2, y2, color, thickness):
    from PIL import ImageDraw
    draw=ImageDraw.Draw(canvas)
    dx=x2-x1; dy=y2-y1; L=math.sqrt(dx*dx+dy*dy)
    if L<1: return
    nx=-dy/L*(thickness/2); ny=dx/L*(thickness/2)
    draw.polygon([(x1+nx,y1+ny),(x2+nx,y2+ny),(x2-nx,y2-ny),(x1-nx,y1-ny)],fill=color)
    r=thickness/2
    draw.ellipse([x1-r,y1-r,x1+r,y1+r],fill=color)
    draw.ellipse([x2-r,y2-r,x2+r,y2+r],fill=color)


def _linebar_baked_track_offset(canvas, x, y, width, height):
    """
    When a linebar's background track is baked into the canvas image, the track
    may start a few pixels above the widget's y coordinate due to rounded-corner
    overflow or misaligned encoding.  Detect the actual track start by scanning
    canvas rows above/below y for the first row where >=50% of the bar's width
    is occupied (avoids counting outer-glow / shadow pixels that are much dimmer).
    Returns (dy, actual_h) – the signed y offset from widget y and the true
    track height found in the canvas.  Returns (0, height) if nothing is found.
    """
    try:
        px = canvas.load()
        cw, ch = canvas.size
    except Exception:
        return 0, height
    search_lo = max(0, y - 8)
    search_hi = min(ch, y + height + 8)
    half_w = max(1, width // 2)
    x1 = max(0, x); x2 = min(cw, x + width)
    if x2 <= x1:
        return 0, height

    first = None; last = None
    for row in range(search_lo, search_hi):
        bright = sum(1 for col in range(x1, x2) if any(c > 30 for c in px[col, row][:3]))
        if bright >= half_w:
            if first is None: first = row
            last = row
    if first is None:
        return 0, height
    return first - y, last - first + 1


def _draw_linebar(canvas, x, y, width, height, bg_color, fill, progress):
    from PIL import ImageDraw, Image as PI, ImageChops
    draw=ImageDraw.Draw(canvas)
    radius=max(1,min(width,height)//2)
    rect=[x,y,x+max(width-1,0),y+max(height-1,0)]
    has_baked=(isinstance(fill,RawImage) and
               canvas.crop((x,y,x+max(width,0),y+max(height,0))).getbbox() is not None)
    if bg_color and not has_baked:
        draw.rounded_rectangle(rect,radius=radius,fill=bg_color)
    if fill is None: return
    is_vertical=height>width

    # When the track is already baked into the canvas, detect its actual y offset
    # so the fill image is pasted flush against the baked track, not at widget.y
    # which may be a few pixels off from the baked position.
    if has_baked:
        baked_dy, baked_h = _linebar_baked_track_offset(canvas, x, y, width, height)
    else:
        baked_dy, baked_h = 0, height

    # Use baked bounds when available; keep widget bounds for non-baked bars
    eff_y      = y + baked_dy
    eff_height = baked_h if baked_h > 0 else height

    if is_vertical:
        fill_h=max(0,int(eff_height*progress))
        if fill_h<=0: return
        fill_y=eff_y+eff_height-fill_h
        if isinstance(fill,RawImage):
            pf=_raw_to_pil(fill)
            if pf.size[0]==width and pf.size[1]>=fill_h:
                region=pf.crop((0,pf.size[1]-fill_h,width,pf.size[1]))
                mask=PI.new('L',(width,fill_h),0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    [0,0,max(width-1,0),max(fill_h-1,0)],
                    radius=max(1,min(radius,fill_h//2)),fill=255)
            else:
                region=pf.resize((width,fill_h),PI.LANCZOS); mask=None
            if region.mode=='RGBA':
                alpha=region.split()[3]
                if mask: alpha=ImageChops.multiply(alpha,mask)
                canvas.paste(region.convert('RGB'),(x,fill_y),alpha)
            else:
                if mask: canvas.paste(region,(x,fill_y),mask)
                else: canvas.paste(region,(x,fill_y))
        else:
            fr=max(1,min(radius,fill_h//2))
            draw.rounded_rectangle([x,fill_y,x+max(width-1,0),fill_y+max(fill_h-1,0)],
                                   radius=fr,fill=fill)
    else:
        fill_w=max(0,int(width*progress))
        if fill_w<=0: return
        if isinstance(fill,RawImage):
            pf=_raw_to_pil(fill)
            if pf.size[0]>=fill_w and pf.size[1]==eff_height:
                region=pf.crop((0,0,fill_w,eff_height))
                mask=PI.new('L',(fill_w,eff_height),0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    [0,0,max(fill_w-1,0),max(eff_height-1,0)],
                    radius=max(1,min(radius,fill_w//2)),fill=255)
            elif pf.size[0]>=fill_w and pf.size[1]>0:
                # height mismatch – scale fill image vertically to eff_height
                region=pf.resize((pf.size[0],eff_height),PI.LANCZOS).crop((0,0,fill_w,eff_height))
                mask=PI.new('L',(fill_w,eff_height),0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    [0,0,max(fill_w-1,0),max(eff_height-1,0)],
                    radius=max(1,min(radius,fill_w//2)),fill=255)
            else:
                region=pf.resize((fill_w,eff_height),PI.LANCZOS); mask=None
            if region.mode=='RGBA':
                alpha=region.split()[3]
                if mask: alpha=ImageChops.multiply(alpha,mask)
                canvas.paste(region.convert('RGB'),(x,eff_y),alpha)
            else:
                if mask: canvas.paste(region,(x,eff_y),mask)
                else: canvas.paste(region,(x,eff_y))
        else:
            fr=max(1,min(radius,fill_w//2))
            draw.rounded_rectangle([x,eff_y,x+max(fill_w-1,0),eff_y+max(eff_height-1,0)],
                                   radius=fr,fill=fill)


def _render_arc(canvas, arc_img: RawImage, cx: int, cy: int,
                arc_start: float, arc_sweep: float, progress: float):
    """
    Render a progress arc onto canvas.

    Coordinate system:
      arc_start : PIL degrees (0=3 o'clock, CW) — stored directly in ptr[0].hi
      arc_sweep : degrees CW from arc_start (total track span)
      progress  : 0.0–1.0, fills arc_start → arc_start + arc_sweep*progress
    """
    from PIL import Image as PI, ImageDraw, ImageChops
    fill = _raw_to_pil(arc_img)
    W2,H2 = fill.size
    if W2<=0 or H2<=0: return
    progress = min(max(progress,0.0),1.0)
    if progress <= 0.0: return

    actual_sweep = arc_sweep * progress
    mask = PI.new('L',(W2,H2),0)
    draw = ImageDraw.Draw(mask)
    mcx,mcy = W2//2, H2//2
    R = int(math.sqrt(W2**2+H2**2)/2)+4

    # arc_start and arc_sweep are already in PIL convention (0=3 o'clock, CW).
    # No angle conversion needed.
    pil_start = arc_start % 360
    pil_end   = (pil_start + actual_sweep) % 360

    if actual_sweep >= 359.9:
        draw.pieslice([mcx-R,mcy-R,mcx+R,mcy+R], start=0, end=360, fill=255)
    elif pil_end > pil_start:
        draw.pieslice([mcx-R,mcy-R,mcx+R,mcy+R], start=pil_start, end=pil_end, fill=255)
    else:
        # Arc wraps past 360°
        draw.pieslice([mcx-R,mcy-R,mcx+R,mcy+R], start=pil_start, end=360, fill=255)
        draw.pieslice([mcx-R,mcy-R,mcx+R,mcy+R], start=0, end=pil_end, fill=255)

    px = cx - W2//2; py = cy - H2//2
    if fill.mode=='RGBA':
        combined = ImageChops.darker(fill.split()[3], mask)
        canvas.paste(fill.convert('RGB'),(px,py),combined)
    else:
        canvas.paste(fill,(px,py),mask)


# ─── Reconstruction ────────────────────────────────────────────────────────────

def reconstruct(style: Style, demo: dict, wf: Watchface,
                display: Optional[Tuple[int,int]]=None,
                seq_map_override: Optional[Dict[int,str]]=None):
    from PIL import Image as PI
    if display is None: display = FIT3_DISPLAY
    W,H = display
    canvas = PI.new('RGB',(W,H),(0,0,0))
    groups = wf.glyph_groups('en')
    seq_map = seq_map_override or infer_seq_map(style, wf)

    def _dk(seq_id): return seq_map.get(seq_id,'')

    _WEEKDAY_PREFIXES = ['mon','tue','wed','thu','fri','sat','sun']
    _MONTH_PREFIXES   = ['jan','feb','mar','apr','may','jun',
                         'jul','aug','sep','oct','nov','dec']

    def _find_glyph_base(prefixes):
        for gi,grp in enumerate(groups):
            s=grp.joined().lower()
            if s.startswith(prefixes[0]) and gi+len(prefixes)<=len(groups):
                if all(groups[gi+k].joined().lower().startswith(prefixes[k])
                       for k in range(1,min(len(prefixes),len(groups)-gi))):
                    return gi
        return -1

    _wb = _find_glyph_base(_WEEKDAY_PREFIXES)
    _mb = _find_glyph_base(_MONTH_PREFIXES)

    def _glyph(gi):
        if gi==0xFF or gi>=len(groups): return ''
        return groups[gi].joined()

    def _glyph_weekday(idx):
        if _wb>=0:
            gi=_wb+(idx%7)
            if gi<len(groups): return groups[gi].joined()
        return ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][idx%7]

    def _glyph_month(idx):
        if _mb>=0:
            gi=_mb+(idx%12)
            if gi<len(groups): return groups[gi].joined()
        return ['Jan','Feb','Mar','Apr','May','Jun',
                'Jul','Aug','Sep','Oct','Nov','Dec'][idx%12]

    _UNIT_WHITELIST = {'°','%','°c','°f','\u2103','\u2109',
                       'km','mi','lb','kg','g','m','h','hr','min','sec','s',
                       'btu','w','kw'}

    def _is_unit_suffix(s):
        if not s: return False
        stripped=s.strip()
        if not stripped: return False
        if all(c in '°%+-/·· \u00b0\u2103\u2109' or not c.isalpha() for c in stripped): return True
        return stripped.lower() in _UNIT_WHITELIST

    def _pair_base_x(w):
        align=w.pair_align()
        if w.x<0 and align!=1: return W+w.x-w.width
        return w.x

    def _pair_anchor_x(w):
        bx=_pair_base_x(w); align=w.pair_align()
        if align==1: return bx+w.width//2
        if align==2: return bx+w.width
        return bx

    # Two-pass: non-hand widgets first, then hands (always on top)
    render_order = [w for w in style.widgets if w.wtype!=WT_HAND] + \
                   [w for w in style.widgets if w.wtype==WT_HAND]

    for w in render_order:

        if w.wtype == WT_STATIC:
            img=w.static_image
            if img: _paste(canvas,_raw_to_pil(img),w.x,w.y)

        elif w.wtype == WT_BADGE:
            x1,y1,x2,y2=w.badge_endpoints; thick=w.badge_thickness
            dk=_dk(w.seq_id); prog=_progress(dk,demo)
            tc=w.badge_track_color; fc=w.badge_fill_color
            if tc is not None: _draw_capsule(canvas,x1,y1,x2,y2,tc,thick)
            if prog>0.01 and fc is not None:
                fx=x1+(x2-x1)*prog; fy=y1+(y2-y1)*prog
                _draw_capsule(canvas,x1,y1,fx,fy,fc,thick)

        elif w.wtype == WT_SPRITE:
            dk=_dk(w.seq_id)
            if not dk: continue
            n_frames=w.sprite_frame_count(); idx=0
            if   dk=='h_tens':       idx=demo['h_tens']
            elif dk=='h_units':      idx=demo['h_units']
            elif dk=='m_tens':       idx=demo['m_tens']
            elif dk=='m_units':      idx=demo['m_units']
            elif dk=='s_tens':       idx=demo['second']//10
            elif dk=='s_units':      idx=demo['second']%10
            elif dk=='world_h_tens':
                idx=(((int(demo.get('world_hour',demo['hour']))-1)%12)+1)//10
            elif dk=='world_h_units':
                idx=(((int(demo.get('world_hour',demo['hour']))-1)%12)+1)%10
            elif dk=='world_m_tens': idx=int(demo.get('world_minute',demo['minute']))//10
            elif dk=='world_m_units':idx=int(demo.get('world_minute',demo['minute']))%10
            elif dk=='weekday':      idx=demo['weekday']%max(n_frames,1)
            elif dk=='ampm':         idx=0 if demo['hour']<12 else 1
            elif n_frames>1:
                prog=_progress(dk,demo); idx=int(round(prog*(n_frames-1)))
            idx=max(0,min(idx,max(n_frames-1,0)))
            img=w.sprite_image(idx)
            if img: _paste(canvas,_raw_to_pil(img),w.x,w.y)

        elif w.wtype == WT_HAND:
            hand_img=w.hand_image()
            if hand_img is None: continue
            hand=_raw_to_pil(hand_img)
            if hand.mode!='RGBA': hand=hand.convert('RGBA')
            dk=_dk(w.seq_id)
            if not dk: continue
            if   dk=='second': deg=demo['second']/60.0*360
            elif dk=='minute': deg=(demo['minute']+demo['second']/60.0)/60.0*360
            elif dk=='hour':   deg=((demo['hour']%12)+demo['minute']/60.0)/12.0*360
            else: continue
            rot_start=w.hand_rot_start(); rot_range=w.hand_rot_range()
            if rot_range>0: deg=rot_start+(deg%rot_range)
            else: deg=rot_start+deg
            hw,hh=hand.size
            px_off=w.hand_pivot_x if w.hand_pivot_x>0 else hw//2
            py_off=w.hand_pivot_y if w.hand_pivot_y>0 else hh
            cx2=w.x+px_off; cy2=w.y+py_off
            layer=PI.new('RGBA',(W,H),(0,0,0,0))
            layer.paste(hand,(w.x,w.y),hand)
            rot=layer.rotate(-deg,resample=PI.BICUBIC,center=(cx2,cy2))
            _paste(canvas,rot,0,0)

        elif w.wtype == WT_ARC:
            arc_img=w.arc_fill_image
            if arc_img is None: continue
            dk=_dk(w.seq_id); prog=_progress(dk,demo)
            cx2=w.x+w.width//2; cy2=w.y+w.height//2
            # arc_start and arc_sweep properties already handle mode=0/1 correctly
            _render_arc(canvas,arc_img,cx2,cy2,w.arc_start,w.arc_sweep,prog)

        elif w.wtype == WT_LINEBAR:
            dk=_dk(w.seq_id); prog=_progress(dk,demo)
            _draw_linebar(canvas,w.x,w.y,w.linebar_width,w.linebar_height,
                          w.linebar_bg_color,w.linebar_fill,prog)

        elif w.wtype == WT_PAIR:
            color=w.pair_color()
            if color is None: color=(255,255,255)
            font_idx=w.pair_font_idx(); pt=wf.font_pt(font_idx,w.unk20)
            font_fam=wf.bindings[font_idx].font_family if 0<=font_idx<len(wf.bindings) else 0
            align=w.pair_align(); layout=w.pair_layout()
            ax=_pair_anchor_x(w); ay=w.y+w.height//2
            suffix_idx=w.pair_suffix_glyph_idx(); suffix=_glyph(suffix_idx)
            dk=_dk(w.seq_id); binding_name=_binding_name(w,wf)

            if w.seq_id==0 and suffix:
                _draw_text(canvas,suffix,ax,ay,pt,color,align=align,font_family=font_fam); continue
            if not dk:
                if binding_name=='WF_NAME' and suffix:
                    _draw_text(canvas,suffix,ax,ay,pt,color,align=align)
                continue

            if dk=='weekday':
                _draw_text(canvas,_glyph_weekday(demo['weekday']),ax,ay,pt,color,align=align,font_family=font_fam); continue
            if dk=='world_name':
                val=_demo_value(dk,demo) or suffix.strip()
                if val: _draw_text(canvas,val,ax,ay,pt,color,align=align,font_family=font_fam)
                continue
            if dk in ('ampm','world_ampm'):
                hour=int(demo.get('world_hour',demo['hour'])) if dk=='world_ampm' else demo['hour']
                ampm_idx=0 if hour<12 else 1
                val=_glyph(suffix_idx+ampm_idx) if suffix_idx!=0xFF else _demo_value(dk,demo)
                _draw_text(canvas,val,ax,ay,pt,color,align=align,font_family=font_fam); continue

            value=_demo_value(dk,demo)
            if not value: continue
            use_glyph_suf=(suffix and _is_unit_suffix(suffix) and layout!=1)

            if layout==1 or not use_glyph_suf:
                _draw_text(canvas,value,ax,ay,pt,color,align=align,font_family=font_fam)
            elif layout==2:
                pt2=max(pt-4,8)
                # layout=2: value on top row, suffix on bottom row (stacked).
                # _draw_text treats y as the vertical mid of the text cell,
                # so pass midpoints: top row mid = w.y + pt/2, bottom mid = w.y + pt + pt2/2.
                _draw_text(canvas,value,ax,w.y+pt//2,pt,color,align=align,font_family=font_fam)
                _draw_text(canvas,suffix.strip(),ax,w.y+pt+pt2//2,pt2,color,align=align,font_family=font_fam)
            else:
                suf_s=suffix.strip()
                if suf_s:
                    pt2=max(pt-3,8)
                    vf=_get_font(pt,text=value); sf=_get_font(pt2,text=suf_s)
                    gap=0 if suf_s.startswith(('°','%','/',':')) else 2
                    total=_text_width(value,vf)+gap+_text_width(suf_s,sf)
                    sx=ax
                    if align==1: sx=ax-total//2
                    elif align==2: sx=ax-total
                    vw=_draw_text(canvas,value,sx,ay,pt,color,align=0,font_family=font_fam)
                    _draw_text(canvas,suf_s,sx+vw+gap,ay,pt2,color,align=0,font_family=font_fam)
                else:
                    _draw_text(canvas,value,ax,ay,pt,color,align=align,font_family=font_fam)

        elif w.wtype == WT_COMP:
            if w.x<0 or w.y<0: continue
            subs=parse_comp_subs(w.frame_ptrs)
            if not subs: continue

            # Determine COMP text color:
            # 1. Try to read explicit BGRA color from ptrs (skip FFFF-high and sentinel words)
            # 2. Fall back to inheriting from a PAIR widget that shares the same font binding index
            #    (ptr[14] hi16 = binding index for this COMP)
            # 3. Default to white (matches most watchface styles better than gray)
            default_color = None
            for p in w.frame_ptrs:
                if (p>>16)==0xFFFF or p==0xFFFFFFFF: continue
                c=_bgra_color_allow_black(p)
                if c is not None: default_color=c; break
            if default_color is None:
                # Inherit color from a PAIR using the same binding index
                comp_bi = (w.frame_ptrs[14]>>16)&0xFFFF if len(w.frame_ptrs)>14 else 0xFFFF
                if comp_bi < len(wf.bindings):
                    for pw in style.widgets:
                        if pw.wtype==WT_PAIR and pw.pair_font_idx()==comp_bi and pw.seq_id!=0:
                            default_color = pw.pair_color(); break
            if default_color is None:
                default_color = (255,255,255)  # white fallback
            # Font family for COMP from binding
            comp_bi = (w.frame_ptrs[14]>>16)&0xFFFF if len(w.frame_ptrs)>14 else 0xFFFF
            comp_font_family = wf.bindings[comp_bi].font_family if comp_bi < len(wf.bindings) else 0

            def _comp_text(idx, sub):
                dk2=_resolve_comp_key(subs,idx,groups)
                if not dk2 or dk2=='none': return ''
                if dk2=='weekday_name':       return _glyph_weekday(demo['weekday'])
                if dk2=='month_name':         return _glyph_month(demo['month'])
                if dk2=='world_weekday_name': return _glyph_weekday(int(demo.get('world_weekday',demo['weekday'])))
                if dk2=='world_month_name':   return _glyph_month(int(demo.get('world_month',demo['month'])))
                return _demo_value(dk2,demo)

            valid_subs=[(i,s,_comp_text(i,s)) for i,s in enumerate(subs) if _comp_text(i,s)]
            if not valid_subs: continue
            valid_keys=[_resolve_comp_key(subs,i,groups) for i,_,_ in valid_subs]

            pt=wf.comp_font_pt(w)
            sample=''.join(t for _,_,t in valid_subs)
            font=_get_font(pt,text=sample,font_family=comp_font_family)

            def _sub_pieces(vi,sub,text):
                prefix=_glyph(sub.prefix_idx) if 0<sub.prefix_idx<len(groups) else ''
                suffix2=_glyph(sub.glyph_idx) if 0<=sub.glyph_idx<len(groups) and sub.glyph_idx!=0xFFFF else ''
                is_last=(vi==len(valid_subs)-1)
                if suffix2: suffix2=suffix2.replace('\x00','')
                if is_last:
                    s2=suffix2.strip()
                    if not s2: suffix2=''
                    elif len(valid_subs)==1 and len(s2)>1 and not any(c.isalpha() for c in s2):
                        suffix2=s2[:1]
                    else: suffix2=s2
                else: suffix2=suffix2.strip()
                return prefix,text,suffix2

            segments=[]; total_w=0; prev_sp=0
            for vi,sub,text in valid_subs:
                prefix,val,suf2=_sub_pieces(vi,sub,text)
                pw=_text_width(prefix,font) if prefix else 0
                tw=_text_width(val,font); sw=_text_width(suf2,font) if suf2 else 0
                sp=max(sub.spacing-prev_sp,0) if segments else 0
                prev_sp=sub.spacing
                segments.append((sp,prefix,val,suf2,pw,tw,sw))
                total_w+=sp+pw+tw+sw

            ay=w.y+w.height//2
            line_bb=_text_bbox(sample or '0',font)
            pt=wf.comp_font_pt(w)
            # Use pt/2 (nominal cell half-height) instead of actual bh/2 so that
            # text is placed at the same vertical position regardless of descenders.
            line_oy=ay-(pt/2)-line_bb[1]

            comp_align=1
            if valid_keys and all(k in DATE_KEYS for k in valid_keys):
                for ow in style.widgets:
                    if ow.wtype!=WT_PAIR or ow.seq_id==0: continue
                    if abs(ow.x-w.x)>4 or abs(ow.width-w.width)>12: continue
                    if abs((ow.y+ow.height//2)-ay)>(w.height+ow.height+12): continue
                    comp_align=ow.pair_align(); break

            if comp_align==0:   px=w.x
            elif comp_align==2: px=w.x+max(w.width-total_w,0)
            else:               px=w.x+max((w.width-total_w)//2,0)

            if len(valid_subs)==1:
                only_key=_resolve_comp_key(subs,valid_subs[0][0],groups)
                if only_key=='battery':
                    for ow in style.widgets:
                        if ow.wtype!=WT_SPRITE or _dk(ow.seq_id)!='battery': continue
                        scy=ow.y+(ow.height//2 if ow.height>0 else 0)
                        if abs(scy-ay)>max(w.height,ow.height,20): continue
                        re=ow.x+max(ow.width,0)
                        if 0<=(w.x-re)<=20: px=max(w.x,re+4); break

            for sp,prefix,val,suf2,_,_,_ in segments:
                px+=sp
                if prefix: px+=_draw_text_piece(canvas,prefix,px,line_oy,font,default_color)
                px+=_draw_text_piece(canvas,val,px,line_oy,font,default_color)
                if suf2: px+=_draw_text_piece(canvas,suf2,px,line_oy,font,default_color)

    return canvas


# ─── CLI info ──────────────────────────────────────────────────────────────────

def print_info(wf: Watchface):
    print(f"\n╔════════════════════════════════════════════════")
    print(f"║  ID      : {wf.wf_id}")
    print(f"║  Version : {wf.version}")
    print(f"║  File    : {wf.filename}")
    print(f"╚════════════════════════════════════════════════")

    if wf.bindings:
        print("\n── Font Bindings ──")
        for i,fb in enumerate(wf.bindings):
            print(f"  [{i}] {fb.name:<20s} → {fb.pt_size}px  family={fb.font_family}")

    if wf.glyphs:
        print("\n── Glyph Groups (en) ──")
        for i,g in enumerate(wf.glyph_groups('en')):
            print(f"  [{i:2d}] {repr(g.joined()[:40])}")

    for s in wf.styles:
        seq_map=infer_seq_map(s,wf)
        print(f"\n── Style {s.index}  ({len(s.widgets)} widgets, {len(s.images)} images) ──")
        for w in s.widgets:
            tn=WT_NAMES.get(w.wtype,f'T{w.wtype}')
            dk=seq_map.get(w.seq_id,f'?{w.seq_id}')
            if w.wtype==WT_STATIC:
                img=w.static_image
                sz=f"{img.width}x{img.height}" if img else 'none'
                print(f"  W{w.gidx:02d} [{tn:<8s}] pos=({w.x},{w.y}) img={sz}")
            elif w.wtype==WT_SPRITE:
                img0=w.sprite_image(0)
                sz=f"{img0.width}x{img0.height}" if img0 else '?'
                print(f"  W{w.gidx:02d} [{tn:<8s}] seq={w.seq_id}({dk}) pos=({w.x},{w.y}) frames={w.sprite_frame_count()} sz={sz}")
            elif w.wtype==WT_BADGE:
                x1,y1,x2,y2=w.badge_endpoints; fc=w.badge_fill_color
                fd=f"#{fc[0]:02x}{fc[1]:02x}{fc[2]:02x}" if fc else 'none'
                print(f"  W{w.gidx:02d} [{tn:<8s}] seq={w.seq_id}({dk}) ({x1:.0f},{y1:.0f})→({x2:.0f},{y2:.0f}) thick={w.badge_thickness} fill={fd}")
            elif w.wtype==WT_LINEBAR:
                fill=w.linebar_fill
                if isinstance(fill,RawImage): fd=f"img({fill.width}x{fill.height})"
                elif fill: fd=f"#{fill[0]:02x}{fill[1]:02x}{fill[2]:02x}"
                else: fd='none'
                print(f"  W{w.gidx:02d} [{tn:<8s}] seq={w.seq_id}({dk}) pos=({w.x},{w.y}) {w.linebar_width}x{w.linebar_height} fill={fd}")
            elif w.wtype==WT_ARC:
                ai=w.arc_fill_image
                asz=f"{ai.width}x{ai.height}" if ai else 'none'
                print(f"  W{w.gidx:02d} [{tn:<8s}] seq={w.seq_id}({dk}) pos=({w.x},{w.y}) {w.width}x{w.height} "
                      f"start={w.arc_start}°(PIL) sweep={w.arc_sweep}°(CW) mode={w.arc_mode} fill={asz}")
            elif w.wtype==WT_PAIR:
                c=w.pair_color(); pt=wf.font_pt(w.pair_font_idx(),w.unk20)
                aln={0:'left',1:'center',2:'right'}.get(w.pair_align(),'?')
                suf=wf.glyph_str(w.pair_suffix_glyph_idx())
                print(f"  W{w.gidx:02d} [{tn:<8s}] seq={w.seq_id}({dk}) pos=({w.x},{w.y}) {w.width}x{w.height} "
                      f"{pt}px {aln} suffix={repr(suf)} layout={w.pair_layout()} color=#{c[0]:02x}{c[1]:02x}{c[2]:02x}")
            elif w.wtype==WT_COMP:
                subs=parse_comp_subs(w.frame_ptrs); pt=wf.comp_font_pt(w)
                desc=', '.join(f"{_resolve_comp_key(subs,i,wf.glyph_groups('en')) or '?'}({pt}px)"
                               for i,s2 in enumerate(subs))
                print(f"  W{w.gidx:02d} [{tn:<8s}] pos=({w.x},{w.y}) {w.width}x{w.height} [{desc}]")
            elif w.wtype==WT_HAND:
                img=w.hand_image(); sz=f"{img.width}x{img.height}" if img else '?'
                print(f"  W{w.gidx:02d} [{tn:<8s}] seq={w.seq_id}({dk}) pivot=({w.x},{w.y}) img={sz} "
                      f"img_pivot=({w.hand_pivot_x},{w.hand_pivot_y})")
            else:
                print(f"  W{w.gidx:02d} [T{w.wtype:<7d}] seq={w.seq_id} pos=({w.x},{w.y})")


def reconstruct_all(wf: Watchface, out_dir: str,
                    style_filter: Optional[int], demo: dict) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    for s in wf.styles:
        if style_filter is not None and s.index != style_filter: continue
        print(f"  style {s.index} ...", end=' ', flush=True)
        try:
            path=os.path.join(out_dir,f'style{s.index}.png')
            reconstruct(s,demo,wf).save(path)
            saved.append(path); print(f"→ {path}")
        except Exception as ex:
            import traceback; print(f"ERROR: {ex}"); traceback.print_exc()

    if len(saved) > 1:
        try:
            from PIL import Image as PI
            imgs=[PI.open(p) for p in saved]
            FW,FH=imgs[0].size; GAP=4
            sheet=PI.new('RGB',((FW+GAP)*len(imgs)-GAP,FH),(20,20,20))
            for i,img in enumerate(imgs): sheet.paste(img,(i*(FW+GAP),0))
            sp=os.path.join(out_dir,'_all_styles.png')
            sheet.save(sp); saved.append(sp); print(f"  sheet → {sp}")
        except Exception as ex:
            print(f"  sheet error: {ex}")
    return saved


def _resolve_binfile(binfile: str) -> str:
    p=Path(binfile)
    if p.exists():
        if p.is_dir():
            c=p/f'{p.name}.bin'
            if c.exists(): return str(c)
        return str(p)
    stem=p.stem if p.suffix else p.name
    roots=[Path('watchfaces-bins'),Path('watchface-bins'),
           Path('APK/com.samsung.wearable.fit3/assets/watchface/SM_R390')]
    for root in roots:
        if not root.exists(): continue
        matches=[m for m in root.glob(f'**/{p.name}') if m.is_file()]
        if not matches and not p.name.endswith('.bin'):
            matches=[m for m in root.glob(f'**/{stem}.bin') if m.is_file()]
        if matches: return str(matches[0])
    return binfile


def reconstruct_aod(entries, data: bytes, demo: dict, wf: Watchface, out_dir: str):
    aod=next(((n,o,s) for n,o,s in entries if n=='aod.bin'),None)
    if not aod: return
    _,off,sz=aod; raw=data[off:off+sz]
    print("  aod ...",end=' ',flush=True)
    try:
        if struct.unpack_from('<I',raw,0)[0]!=STYLE_MAGIC: print("skipped (bad magic)"); return
        widgets,images=walk_widgets(raw)
        s=Style(-1,images,widgets)
        path=os.path.join(out_dir,'aod.png')
        reconstruct(s,demo,wf).save(path); print(f"→ {path}")
    except Exception as ex:
        import traceback; print(f"ERROR: {ex}"); traceback.print_exc()


def extract_images(wf: Watchface, data: bytes, entries: List, out_dir: str):
    os.makedirs(out_dir,exist_ok=True)
    emap={n:(o,s) for n,o,s in entries}
    if 'preview.bin' in emap:
        o,s=emap['preview.bin']; raw=data[o:o+s]; pos=0; idx=0
        while pos+12<=len(raw):
            w2=struct.unpack_from('<H',raw,pos)[0]; h2=struct.unpack_from('<H',raw,pos+2)[0]
            fmt=struct.unpack_from('<H',raw,pos+4)[0]; sz=struct.unpack_from('<I',raw,pos+8)[0]
            if fmt not in(IMG_RGB565,IMG_RGB565A): break
            bpp=3 if fmt==IMG_RGB565A else 2
            if sz==0 or abs(sz-w2*h2*bpp)>16: break
            ri=RawImage(w2,h2,fmt,pos,raw[pos+12:pos+12+sz])
            name='preview.png' if idx==0 else f'preview_{idx}.png'
            _raw_to_pil(ri).save(os.path.join(out_dir,name))
            print(f"  {name} ({w2}x{h2})"); pos+=12+sz; idx+=1
    for s in wf.styles:
        sd=os.path.join(out_dir,f'style{s.index}'); os.makedirs(sd,exist_ok=True)
        for img in s.images:
            tag='A' if img.fmt==IMG_RGB565A else ''
            path=os.path.join(sd,f'img_{img.ptr:08x}_{img.width}x{img.height}{tag}.png')
            try: _raw_to_pil(img).save(path)
            except Exception as ex: print(f"  warn: {ex}")
    print(f"Images → {out_dir}")


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap=argparse.ArgumentParser(description='OPPO Watchface Parser (Samsung Galaxy Fit3) — v4.0.0')
    ap.add_argument('binfile', nargs='+', help='One or more .bin files (globs like "watchfaces-bins/*.bin" work too)')
    ap.add_argument('--reconstruct',    action='store_true')
    ap.add_argument('--extract-images', action='store_true')
    ap.add_argument('--info',           action='store_true')
    ap.add_argument('--style',   type=int, default=None)
    ap.add_argument('--output',  default=None)
    ap.add_argument('--time',    default='10:08')
    ap.add_argument('--date',    type=int, default=28)
    ap.add_argument('--month',   type=int, default=12,    help='1=Jan..12=Dec')
    ap.add_argument('--weekday', type=int, default=5,     help='0=Mon..6=Sun')
    ap.add_argument('--second',  type=int, default=30)
    ap.add_argument('--steps',   type=int, default=3457)
    ap.add_argument('--step-goal', type=int, default=6000)
    ap.add_argument('--bpm',     type=int, default=78)
    ap.add_argument('--battery', type=int, default=100)
    ap.add_argument('--temp',    type=int, default=23)
    ap.add_argument('--temp-min',type=int, default=-20)
    ap.add_argument('--temp-max',type=int, default=45)
    ap.add_argument('--kcal',    type=int, default=350)
    ap.add_argument('--kcal-goal', type=int, default=500)
    ap.add_argument('--activity',type=int, default=78)
    ap.add_argument('--activity-goal', type=int, default=90)
    ap.add_argument('--active-hours', type=int, default=3)
    ap.add_argument('--active-hours-goal', type=int, default=8)
    ap.add_argument('--floors',  type=int, default=3)
    ap.add_argument('--sleep',   type=float, default=7.833)
    ap.add_argument('--sleep-goal', type=float, default=10.0)
    ap.add_argument('--water',   type=int, default=250)
    ap.add_argument('--water-goal', type=int, default=2000)
    ap.add_argument('--year',    type=int, default=2023)
    ap.add_argument('--world-time',    default=None, help='World clock HH:MM')
    ap.add_argument('--world-date',    type=int, default=None)
    ap.add_argument('--world-month',   type=int, default=None, help='1=Jan..12=Dec')
    ap.add_argument('--world-weekday', type=int, default=None, help='0=Mon..6=Sun')
    ap.add_argument('--world-year',    type=int, default=None)
    ap.add_argument('--world-name',    default=None, help='World clock city label')
    args=ap.parse_args()

    import glob as _glob
    binfiles = []
    for pat in args.binfile:
        expanded = _glob.glob(pat)
        if expanded:
            binfiles.extend(sorted(expanded))
        else:
            binfiles.append(pat)  # let _resolve_binfile handle it

    hh,mm=map(int,args.time.split(':'))
    demo=_default_demo()
    demo.update(
        hour=hh, minute=mm, second=args.second,
        h_tens=hh//10, h_units=hh%10, m_tens=mm//10, m_units=mm%10,
        date=args.date, month=args.month-1, weekday=args.weekday, year=args.year,
        steps=args.steps, step_goal=args.step_goal, bpm=args.bpm,
        battery=args.battery, temp=args.temp,
        temp_min=args.temp_min, temp_max=args.temp_max,
        kcal=args.kcal, kcal_goal=args.kcal_goal,
        activity=args.activity, activity_goal=args.activity_goal,
        active_hours=args.active_hours, active_hours_goal=args.active_hours_goal,
        floors=args.floors, sleep=args.sleep, sleep_goal=args.sleep_goal,
        water=args.water, water_goal=args.water_goal,
    )
    if args.world_time:
        wh,wm=map(int,args.world_time.split(':'))
        demo.update(world_hour=wh,world_minute=wm)
    if args.world_date    is not None: demo['world_date']    = args.world_date
    if args.world_month   is not None: demo['world_month']   = args.world_month-1
    if args.world_weekday is not None: demo['world_weekday'] = args.world_weekday
    if args.world_year    is not None: demo['world_year']    = args.world_year
    if args.world_name    is not None: demo['world_name']    = args.world_name

    for binfile in binfiles:
        binfile = _resolve_binfile(binfile)
        if not os.path.exists(binfile):
            print(f"Error: '{binfile}' not found — skipping"); continue

        with open(binfile,'rb') as f: data=f.read()
        fname=os.path.basename(binfile)
        out_dir=args.output or os.path.splitext(binfile)[0]+'_out'

        print(f"Parsing {fname} ({len(data):,} bytes) ...")
        entries=parse_container(data)
        wf=parse_watchface(data,fname)

        if args.info or not any([args.reconstruct,args.extract_images]):
            print_info(wf)

        if args.extract_images:
            extract_images(wf,data,entries,os.path.join(out_dir,'images'))

        if args.reconstruct:
            print("\nReconstructing ...")
            reconstruct_all(wf,out_dir,args.style,demo)
            reconstruct_aod(entries,data,demo,wf,out_dir)


if __name__ == '__main__':
    main()
