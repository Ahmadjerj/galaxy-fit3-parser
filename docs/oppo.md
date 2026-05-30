# OPPO Parser Documentation

Based on `parser.py` from the fit3-jailbreak project.

## Overview

This document describes the OPPO binary format parser used for parsing FIT3 watchface files (.bin containers).

## Constants

### Magic Numbers
- `OPPO_MAGIC` = b'oppo' - File container magic
- `STYLE_MAGIC` = 0x12345678 - Style block magic

### Image Formats
- `IMG_RGB565` = 0x0082
- `IMG_RGB565A` = 0x0080

### Display Dimensions
- `FIT3_DISPLAY` = (256, 402)

### Widget Types
```python
WT_STATIC   = 1
WT_HAND     = 2
WT_SPRITE   = 3
WT_PAIR     = 5
WT_BADGE    = 7
WT_COMP     = 13
WT_ARC      = 16
WT_LINEBAR  = 17
```

### Widget Type Names
```python
WT_NAMES = {
    1:'Static', 2:'Hand', 3:'Sprite', 5:'Pair',
    7:'Badge', 13:'Comp', 16:'Arc', 17:'LineBar'
}
```

### Date Keys
```python
DATE_KEYS = {
    'weekday_name','month_name','date_day','date_month_num','date_year',
    'world_weekday_name','world_month_name','world_date_day',
    'world_date_month_num','world_date_year',
}
```

### Component Types
```python
COMP_TYPE: Dict[int, str] = {
    0x00:'none', 0x08:'kcal',   0x09:'date_day',
    0x11:'weekday_name', 0x12:'date_day', 0x13:'month_name',
    0x15:'date_month_num', 0x16:'date_year', 0x18:'date_year',
    0x25:'battery', 0x3E:'temp', 0x72:'ampm',
}
COMP_DATE_TYPES   = {0x12, 0x15, 0x16, 0x18}
COMP_TYPE_ALIASES = {0x7A:0x15, 0x7B:0x11, 0x7C:0x12}
```

### World Component Keys
```python
WORLD_COMP_KEYS: Dict[str,str] = {
    'weekday_name':'world_weekday_name','month_name':'world_month_name',
    'date_day':'world_date_day','date_month_num':'world_date_month_num',
    'date_year':'world_date_year',
}
```

### WF Binding Keys
```python
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
```

### Firmware Seq Protocol
```python
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
_ACTIVITY_CHAIN: List[int] = [29, 48]
```

## Data Structures

### RawImage
```python
class RawImage:
    __slots__ = ('width','height','fmt','ptr','pixel_data')
    def __init__(self,w,h,fmt,ptr,pix):
        self.width=w; self.height=h; self.fmt=fmt; self.ptr=ptr; self.pixel_data=pix
```

### Widget
```python
class Widget:
    __slots__ = ('gidx','wtype','seq_id','x','y','width','height',
                 'unk20','rec_size','frame_ptrs','_img_map')
    def __init__(self,gidx,wtype,seq_id,x,y,width,height,unk20,rec_size,ptrs):
        self.gidx=gidx; self.wtype=wtype; self.seq_id=seq_id
        self.x=x; self.y=y; self.width=width; self.height=height
        self.unk20=unk20; self.rec_size=rec_size; self.frame_ptrs=ptrs
        self._img_map: Dict[int,RawImage] = {}
```

### CompSub
```python
class CompSub:
    __slots__ = ('data_type','prefix_idx','glyph_idx','spacing','word2')
    def __init__(self,data_type,prefix_idx,glyph_idx,spacing,word2):
        self.data_type=data_type; self.prefix_idx=prefix_idx
        self.glyph_idx=glyph_idx; self.spacing=spacing; self.word2=word2
```

### GlyphGroup
```python
class GlyphGroup:
    __slots__ = ('chars',)
    def __init__(self, chars): self.chars = chars
    def joined(self): return ''.join(self.chars)
    def __len__(self): return len(self.chars)
```

### FontBinding
```python
class FontBinding:
    __slots__ = ('name','pt_size','font_family')
    def __init__(self, name, pt_size, font_family=0):
        self.name=name; self.pt_size=pt_size; self.font_family=font_family
```

### Style
```python
class Style:
    __slots__ = ('index','images','widgets','_hand_metrics_cache')
    def __init__(self,index,images,widgets):
        self.index=index; self.images=images; self.widgets=widgets
        self._hand_metrics_cache=None
```

### Watchface
```python
class Watchface:
    __slots__ = ('filename','wf_id','version','styles','bindings','glyphs')
    def __init__(self,filename,wf_id,version,styles,bindings,glyphs):
        self.filename=filename; self.wf_id=wf_id; self.version=version
        self.styles=styles; self.bindings=bindings; self.glyphs=glyphs
```

## Functions

### Binary Parsing

#### parse_container(data: bytes) -> List[Tuple[str,int,int]]
Parses the OPPO container format to extract file entries.

#### scan_images(raw_style: bytes, sec_rel: int) -> Tuple[List[RawImage],Dict[int,RawImage]]
Scans for images in a style block.

#### walk_widgets(raw_style: bytes) -> Tuple[List[Widget],List[RawImage]]
Walks through widgets in a style block.

#### parse_glyph_file(raw: bytes, locale: str) -> Optional[List[GlyphGroup]]
Parses a glyph file.

#### parse_font_binding(raw: bytes) -> Optional[FontBinding]
Parses a font binding.

#### parse_watchface(data: bytes, filename: str) -> Watchface
Main entry point to parse a watchface file.

### Composite Sub-element Parser

#### _normalize_comp_data_type(data_type: int) -> int
Normalizes component data types using aliases.

#### _world_comp_key(key: str) -> str
Converts a component key to its world clock equivalent.

#### parse_comp_subs(ptrs: List[int]) -> List[CompSub]
Parses composite sub-elements from pointer array.

#### _resolve_comp_key(subs: List[CompSub], idx: int, groups: Optional[List[GlyphGroup]]=None) -> str
Resolves a composite key to its semantic meaning.

### Seq_id → data_key Inference

#### _hand_widget_metrics(style: Style) -> List[Tuple[Widget,int,int]]
Calculates hand widget metrics for analog hand detection.

#### _binding_name(widget: Widget, wf: Watchface) -> str
Gets the binding name for a pair widget.

#### _assign_clock_group(pool: List[Widget], prefix: str, cap_at_minutes: bool=False) -> Dict[int,str]
Assigns clock group roles (h/m/s) based on sprite frame counts.

#### infer_seq_map(style: Style, wf: Watchface) -> Dict[int,str]
Builds per-style seq_id → data_key mapping from binary data only.

### Demo/Simulation Data

#### _default_demo() -> dict
Returns default demo values for simulation.

#### _demo_value(dk: str, demo: dict) -> str
Gets a demo value for a data key.

#### _progress(dk: str, demo: dict) -> float
Gets a progress value (0.0-1.0) for a data key.

### PIL / Image Helpers

#### _font_cache: Dict = {}
Cache for PIL font objects.

#### _SYS_FONTS_BOLD, _SYS_FONTS_REGULAR, _SYS_FONTS_CONDENSED, _SYS_FONTS_CONDENSED_BOLD
System font fallbacks.

#### _font_kind(text: str) -> str
Determines if text is numeric or text for font selection.

#### _get_font(pt: int, bold: bool = False, text: str = '', font_family: int = 0):
Resolves a font for rendering.

#### _text_bbox(text: str, font) -> Tuple[int,int,int,int]
Gets text bounding box.

#### _text_width(text: str, font) -> int
Gets text width.

#### _raw_to_pil(img: RawImage) -> PIL.Image
Converts RawImage to PIL Image.

#### _paste(canvas, pil_img, x, y)
Pastes PIL image onto canvas with bounds checking.

#### _draw_text(canvas, text, x, y, pt, color, bold=False, align=0, font_family=0) -> int
Draws text on canvas.

#### _draw_text_piece(canvas, text, x, origin_y, font, color) -> int
Draws text piece on canvas.

#### _draw_capsule(canvas, x1, y1, x2, y2, color, thickness)
Draws a capsule shape.

#### _linebar_baked_track_offset(canvas, x, y, width, height)
Detects actual track start for linebars with baked-in backgrounds.

#### _draw_linebar(canvas, x, y, width, height, bg_color, fill, progress)
Draws a linebar widget.

#### _render_arc(canvas, arc_img: RawImage, cx: int, cy: int,
              arc_start: float, arc_sweep: float, progress: float)
Renders a progress arc onto canvas.

### Reconstruction

#### reconstruct(style: Style, demo: dict, wf: Watchface,
              display: Optional[Tuple[int,int]]=None,
              seq_map_override: Optional[Dict[int,str]]=None)
Reconstructs a watchface style into a PIL image.

## Usage

The parser can be used to:
1. Parse OPPO container files (.bin)
2. Extract images, widgets, fonts, and glyphs
3. Infer semantic meanings from widget properties
4. Render watchfaces using demo data or real sensor data