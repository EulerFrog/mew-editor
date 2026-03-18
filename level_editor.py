import os
import random
import re
import struct
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox


@dataclass
class SpawnObject:
    id: int
    wave: int = 0
    roll_index: int = 0
    options: list = field(default_factory=list)  # [(spawn_id, weight), ...]

    def __post_init__(self):
        self.id &= 0xFFFF
        self.wave &= 0xFF
        self.roll_index &= 0xFF
        cleaned = []
        for pid, weight in self.options:
            cleaned.append((int(pid) & 0xFFFF, int(weight) & 0xFFFF))
        self.options = cleaned

    @property
    def is_random(self):
        return self.id == 0xFFFF


class LevelData:
    def __init__(self):
        self.path = None
        self.version = 2
        self.width = 10
        self.height = 10
        self.mode = 1
        self.spawn_file = "spawns.gon"
        self.tiles_file = "tiles.gon"
        self.tiles = [0] * (self.width * self.height)
        self.entities = {}  # (x,y) -> [SpawnObject, ...]
        self.raw_prefix = b""
        self.raw_tiles = b""
        self.raw_spawns = b""
        self.original_tiles = []
        self.original_entities = []
        self.tail = b""


def load_level_file(path):
    with open(path, "rb") as f:
        data = f.read()

    # Original LevelResource layout:
    # version,width,height,nlayers,nspawns,camx,camy,camw,camh
    version, width, height, nlayers, entity_count, camx, camy, camw, camh = struct.unpack_from("<9i", data, 0)

    offset = 36
    spawn_name_len = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    spawn_file = data[offset:offset + max(0, spawn_name_len)]
    offset += max(0, spawn_name_len)

    tiles_name_len = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    tiles_file = data[offset:offset + max(0, tiles_name_len)]
    offset += max(0, tiles_name_len)

    # Two reserved int32s.
    offset += 4
    offset += 4

    tiles_start = offset

    # Parse all layers so stream offset remains accurate; editor uses layer 0.
    layer0 = []
    tile_count = width * height
    for layer_idx in range(max(0, nlayers)):
        values = []
        for _y in range(height):
            for _x in range(width):
                tile_id = struct.unpack_from("<H", data, offset)[0]
                offset += 2
                resolved = tile_id
                if tile_id == 0xFFFF:
                    num_poss = struct.unpack_from("<B", data, offset)[0]
                    offset += 1
                    roll_index = struct.unpack_from("<B", data, offset)[0]
                    offset += 1
                    poss = []
                    for _ in range(num_poss):
                        pid, weight = struct.unpack_from("<HH", data, offset)
                        offset += 4
                        poss.append((pid, weight))
                    resolved = poss[roll_index % len(poss)][0] if poss else 0
                values.append(resolved)
        if layer_idx == 0:
            layer0 = values

    spawns_start = offset
    entities = []
    for _ in range(max(0, entity_count)):
        x, y, id_ = struct.unpack_from("<hhH", data, offset)
        offset += 6
        wave = struct.unpack_from("<B", data, offset)[0]
        offset += 1
        _reserved = struct.unpack_from("<B", data, offset)[0]
        offset += 1

        record = SpawnObject(id=id_, wave=wave)
        if id_ == 0xFFFF:
            num_poss = struct.unpack_from("<B", data, offset)[0]
            offset += 1
            roll_index = struct.unpack_from("<B", data, offset)[0]
            offset += 1
            options = []
            for _ in range(num_poss):
                pid, weight = struct.unpack_from("<HH", data, offset)
                offset += 4
                options.append((pid, weight))
                
            record = SpawnObject(id=id_, wave=wave, roll_index=roll_index, options=options)
        entities.append((x, y, record))

    spawns_end = offset
    tail = data[offset:]
    return {
        "data": data,
        "version": version,
        "width": width,
        "height": height,
        "mode": nlayers,
        "entity_count": entity_count,
        "tiles_start": tiles_start,
        "spawns_start": spawns_start,
        "spawns_end": spawns_end,
        "tile_grid": layer0 if layer0 else [0] * tile_count,
        "entities": entities,
        "spawn_file": spawn_file.decode("utf-8", errors="ignore"),
        "tiles_file": tiles_file.decode("utf-8", errors="ignore"),
        "raw_tiles": data[tiles_start:spawns_start],
        "raw_spawns": data[spawns_start:spawns_end],
        "tail": tail,
    }


def _parse_gon_value(val):
    """Parse a gon value string into a str or list."""
    val = val.strip()
    if val.startswith('['):
        inner = val[1:-1].strip()
        items = []
        while inner:
            inner = inner.lstrip(', ')
            if not inner:
                break
            if inner.startswith('['):
                end = inner.index(']')
                items.append(inner[:end + 1])
                inner = inner[end + 1:]
            else:
                m = re.match(r'"([^"]+)"|(\S+)', inner)
                if m:
                    items.append(m.group(1) or m.group(2))
                    inner = inner[m.end():]
                else:
                    break
        return items
    if val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    return val


def _parse_gon(path):
    """Parse a .gon file capturing all editor-block fields into {id: {key: value}}."""
    if not os.path.exists(path):
        return {}
    defs = {}
    current_id = None
    depth = 0
    in_editor = False
    entry = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("//"):
                continue
            opens = s.count("{")
            closes = s.count("}")
            if current_id is None:
                if s[0].isdigit() and "{" in s:
                    try:
                        current_id = int(s.split()[0])
                        depth = 1
                        entry = {}
                        in_editor = False
                    except Exception:
                        pass
                continue
            if re.match(r'^editor\b', s):
                in_editor = True
            depth += opens - closes
            if in_editor and depth > 1:
                m = re.match(r'^(\w+)\s+(.+)$', s.rstrip('{').strip())
                if m:
                    key, raw_val = m.group(1), m.group(2).strip()
                    if key not in entry:
                        entry[key] = _parse_gon_value(raw_val)
            if in_editor and depth <= 1:
                in_editor = False
                if "images" not in entry and "image" in entry:
                    v = entry["image"]
                    entry["images"] = [os.path.splitext(f)[0].lower() for f in (v if isinstance(v, list) else [v])]
                elif "images" in entry:
                    entry["images"] = [os.path.splitext(f)[0].lower() for f in entry["images"]]
            if depth <= 0:
                defs[current_id] = entry
                current_id = None
                depth = 0
                entry = {}
    return defs


def load_defs(tiles_path, spawns_path):
    return {
        "tiles": _parse_gon(tiles_path),
        "spawns": _parse_gon(spawns_path),
    }


class LevelEditor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Level Editor")
        self.resizable(True, True)

        self.level = LevelData()
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self._load_defs(
            self._resolve_local_path("tiles.gon"),
            self._resolve_local_path("spawns.gon"),
        )

        self.cell_size = 32
        self.grid_origin = (10, 10)
        self.canvas_size = self.cell_size * 10 + 20
        self._icon_raw_cache = {}    # stem -> PhotoImage (full-size, color-keyed)
        self._icon_tinted_cache = {} # (stem, tint) -> PhotoImage (full-size, tinted)
        self._icon_cache = {}        # (stem, tint, cell_size) -> PhotoImage (subsampled)

        self._build_ui()
        self._on_mode_change()
        self._on_spawn_type_change()
        self._draw_grid()

    def _build_ui(self):
        top = tk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)

        tk.Label(top, text="File:").pack(side="left")
        self.path_var = tk.StringVar(value="")
        path_entry = tk.Entry(top, textvariable=self.path_var, width=40)
        path_entry.pack(side="left", padx=6)

        tk.Button(top, text="Browse", command=self._browse).pack(side="left")
        tk.Button(top, text="Create", command=self._create_room).pack(side="left", padx=4)
        tk.Button(top, text="Load", command=self._load).pack(side="left", padx=4)
        tk.Button(top, text="Save", command=self._save).pack(side="left", padx=4)
        tk.Button(top, text="Save As", command=self._save_as).pack(side="left")

        controls = tk.Frame(self)
        controls.pack(fill="x", padx=8, pady=6)

        self.mode_var = tk.StringVar(value="tile")
        tk.Label(controls, text="Mode:").pack(side="left")
        tk.Radiobutton(controls, text="Tile", variable=self.mode_var, value="tile", command=self._on_mode_change).pack(side="left")
        tk.Radiobutton(controls, text="Entity", variable=self.mode_var, value="entity", command=self._on_mode_change).pack(side="left")

        def_row = tk.Frame(self)
        def_row.pack(fill="x", padx=8, pady=(0, 4))
        self.def_file_var = tk.StringVar(value=self.level.tiles_file)
        tk.Entry(def_row, textvariable=self.def_file_var, width=20, state="readonly").pack(side="left", padx=(0, 6))
        tk.Button(def_row, text="Change", command=self._change_def_file).pack(side="left")

        self.tile_var = tk.IntVar(value=0)
        self.entity_id_var = tk.StringVar(value="")
        self.entity_extra_var = tk.StringVar(value="0")
        self.entity_spawn_type_var = tk.StringVar(value="fixed")
        self.entity_roll_index_var = tk.StringVar(value="0")
        self.pool_weight_var = tk.StringVar(value="1")
        self.random_pool = []
        self.preview_active = False
        self.preview_map = {}

        self.entity_controls = tk.Frame(self)
        self.entity_controls.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(self.entity_controls, text="ID").pack(side="left")
        tk.Entry(self.entity_controls, textvariable=self.entity_id_var, width=8).pack(side="left", padx=(4, 10))
        tk.Label(self.entity_controls, text="Wave").pack(side="left")
        tk.Entry(self.entity_controls, textvariable=self.entity_extra_var, width=5).pack(side="left", padx=(4, 10))
        tk.Radiobutton(
            self.entity_controls,
            text="Fixed",
            variable=self.entity_spawn_type_var,
            value="fixed",
            command=self._on_spawn_type_change,
        ).pack(side="left")
        tk.Radiobutton(
            self.entity_controls,
            text="Random",
            variable=self.entity_spawn_type_var,
            value="random",
            command=self._on_spawn_type_change,
        ).pack(side="left", padx=(4, 10))

        self.random_controls = tk.Frame(self)
        self.random_controls.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(self.random_controls, text="Roll Index").pack(side="left")
        tk.Entry(self.random_controls, textvariable=self.entity_roll_index_var, width=5).pack(side="left", padx=(4, 10))
        tk.Label(self.random_controls, text="Weight").pack(side="left")
        tk.Entry(self.random_controls, textvariable=self.pool_weight_var, width=6).pack(side="left", padx=(4, 10))
        tk.Button(self.random_controls, text="Add", command=self._pool_add).pack(side="left")
        tk.Button(self.random_controls, text="Remove", command=self._pool_remove).pack(side="left", padx=(4, 0))
        tk.Button(self.random_controls, text="Clear", command=self._pool_clear).pack(side="left", padx=(4, 0))

        self.pool_list = tk.Listbox(self, height=5, exportselection=False)
        self.pool_list.pack(fill="x", padx=8, pady=(0, 6))

        self.random_tools = tk.Frame(self)
        self.random_tools.pack(fill="x", padx=8, pady=(0, 6))
        tk.Button(self.random_tools, text="Preview Randomization", command=self._preview_randomization).pack(side="left", padx=(8, 0))
        tk.Button(self.random_tools, text="Reset Preview", command=self._reset_preview).pack(side="left", padx=(4, 0))

        content = tk.Frame(self)
        content.pack(fill="both", expand=True, padx=8, pady=6)

        sidebar = tk.Frame(content)
        sidebar.pack(side="left", fill="y", padx=(0, 8))
        self.sidebar_title = tk.Label(sidebar, text="Tiles")
        self.sidebar_title.pack(anchor="w")

        self.sidebar_search_var = tk.StringVar()
        sidebar_search = tk.Entry(sidebar, textvariable=self.sidebar_search_var, width=24)
        sidebar_search.pack(fill="x", pady=(4, 6))
        sidebar_search.bind("<KeyRelease>", self._on_sidebar_search)

        self.sidebar_listbox = tk.Listbox(sidebar, width=26, height=16, exportselection=False)
        sidebar_scroll = tk.Scrollbar(sidebar, orient="vertical", command=self.sidebar_listbox.yview)
        self.sidebar_listbox.configure(yscrollcommand=sidebar_scroll.set)
        self.sidebar_listbox.pack(side="left", fill="y")
        sidebar_scroll.pack(side="left", fill="y")

        self._populate_sidebar_list()
        self.sidebar_listbox.bind("<<ListboxSelect>>", self._on_sidebar_select)

        self.canvas = tk.Canvas(content, width=self.canvas_size, height=self.canvas_size, bg="#f8fafc")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_left_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Button-2>", self._pick_from_cell)
        self.canvas.bind("<Shift-Button-1>", self._pick_from_cell)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        self.status_var = tk.StringVar(value="")
        status = tk.Label(self, textvariable=self.status_var, anchor="w")
        status.pack(fill="x", padx=8, pady=(6, 6))

    def _load_defs(self, tiles_path, spawns_path):
        defs = load_defs(tiles_path, spawns_path)
        self.tile_defs = defs["tiles"]
        self.spawn_defs = defs["spawns"]
        self.tile_names = {k: v.get("name", f"Tile {k}") for k, v in self.tile_defs.items()}
        self.spawn_names = {k: v.get("name", str(k)) for k, v in self.spawn_defs.items()}

    def _resolve_local_path(self, filename):
        local = os.path.join(self.base_dir, filename)
        return local if os.path.exists(local) else filename

    def _icons_dir(self):
        return os.path.join(self.base_dir, "editor_icons")

    def _icon_pairs(self, data, fallback_stem=None):
        """Return [(stem, tint_str_or_None), ...] for a def entry."""
        images = data.get("images", [fallback_stem] if fallback_stem else [])
        tints = data.get("image_tint", [])
        return [(stem, tints[i] if i < len(tints) else None) for i, stem in enumerate(images)]

    def _icon_stems_for_tile(self, tile_id):
        return self._icon_pairs(self.tile_defs.get(tile_id, {}), str(tile_id))

    def _icon_stems_for_entity(self, ent_id):
        return self._icon_pairs(self.spawn_defs.get(ent_id, {}))

    def _parse_tint_color(self, tint_str):
        """Convert a tint string to an (r, g, b) tuple 0-255, or None."""
        if not tint_str or tint_str.lower() == "none":
            return None
        tint_str = tint_str.strip()
        if tint_str.startswith("["):
            nums = re.findall(r"[\d.]+", tint_str)
            if len(nums) >= 3:
                return tuple(int(float(n) * 255) for n in nums[:3])
            return None
        try:
            r16, g16, b16 = self.winfo_rgb(tint_str)
            return (r16 // 256, g16 // 256, b16 // 256)
        except Exception:
            return None

    def _tint_image(self, img, tint_rgb):
        """Return a tinted copy of img using multiplicative blending."""
        tr, tg, tb = tint_rgb
        copy = img.copy()
        w, h = copy.width(), copy.height()
        for py in range(h):
            for px in range(w):
                if copy.transparency_get(px, py):
                    continue
                color = copy.get(px, py)
                if isinstance(color, str):
                    parts = color.split()
                    r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                else:
                    r, g, b = color[0], color[1], color[2]
                    
                copy.put(f"#{r*tr//255:02x}{g*tg//255:02x}{b*tb//255:02x}", (px, py))
                
        return copy

    def _apply_color_key(self, img):
        """Make all pixels matching _COLOR_KEY transparent."""
        kr, kg, kb = self._TRANSPARENT
        w, h = img.width(), img.height()
        pixels = []
        for py in range(h):
            for px in range(w):
                color = img.get(px, py)
                if isinstance(color, str):
                    parts = color.split()
                    r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                else:
                    r, g, b = color[0], color[1], color[2]
                if r == kr and g == kg and b == kb:
                    pixels.append((px, py))
        for px, py in pixels:
            img.transparency_set(px, py, True)

    def _get_raw_icon(self, stem):
        """Return the full-size color-keyed PhotoImage for stem, or None."""
        if stem in self._icon_raw_cache:
            return self._icon_raw_cache[stem]
        path = os.path.join(self._icons_dir(), f"{stem}.png")
        if not os.path.exists(path):
            self._icon_raw_cache[stem] = None
            return None
        try:
            raw = tk.PhotoImage(file=path)
            self._apply_color_key(raw)
        except Exception:
            self._icon_raw_cache[stem] = None
            return None
        self._icon_raw_cache[stem] = raw
        return raw

    def _get_icon(self, stem, tint_str=None):
        """Return a subsampled PhotoImage for stem+tint at the current cell_size, or None."""
        tint_key = tint_str if (tint_str and tint_str.lower() != "none") else None
        key = (stem, tint_key, self.cell_size)
        if key in self._icon_cache:
            return self._icon_cache[key]
        raw = self._get_raw_icon(stem)
        if raw is None:
            self._icon_cache[key] = None
            return None
        source = raw
        if tint_key:
            tc = (stem, tint_key)
            if tc not in self._icon_tinted_cache:
                tint_rgb = self._parse_tint_color(tint_key)
                self._icon_tinted_cache[tc] = self._tint_image(raw, tint_rgb) if tint_rgb else raw
            source = self._icon_tinted_cache[tc]
        try:
            factor = self._ICON_SIZE // self.cell_size
            if factor > 1:
                img = source.subsample(factor)
            elif factor < 1:
                img = source.zoom(self.cell_size // self._ICON_SIZE)
            else:
                img = source
        except Exception:
            self._icon_cache[key] = None
            return None
        self._icon_cache[key] = img
        return img

    def _icon_draw_pos(self, img, x0, y0):
        """Return (draw_x, draw_y) anchor-nw to center img on the cell."""
        offset_x = (self.cell_size - img.width()) // 2
        offset_y = (self.cell_size - img.height()) // 2
        return x0 + offset_x, y0 + offset_y

    def _normalize_input_path(self, raw_path):
        p = (raw_path or "").strip().strip('"').strip("'")
        if not p:
            return ""
        p = os.path.expandvars(os.path.expanduser(p))
        if os.name != "nt" and "\\" in p and "/" not in p:
            p = p.replace("\\", os.sep)
        return os.path.normpath(p)

    def _change_def_file(self):
        path = filedialog.askopenfilename(filetypes=[("GON files", "*.gon"), ("All files", "*.*")])
        if not path:
            return
        is_entity = self.mode_var.get() == "entity"
        if is_entity:
            self.level.spawn_file = path
        else:
            self.level.tiles_file = path
        self.def_file_var.set(path)
        level_path = self.level.path or self.base_dir
        self._reload_defs_for_level(level_path, self.level.spawn_file, self.level.tiles_file)
        self._populate_sidebar_list()
        self._draw_grid()
        self.status_var.set(f"Def file changed: {os.path.basename(path)}")

    def _reload_defs_for_level(self, level_path, spawn_file, tiles_file):
        level_dir = os.path.dirname(level_path)

        def resolve(filename):
            p = os.path.join(level_dir, filename)
            if os.path.exists(p):
                return p
            return self._resolve_local_path(filename)

        self._load_defs(resolve(tiles_file), resolve(spawn_file))
        self._icon_raw_cache.clear()
        self._icon_tinted_cache.clear()
        self._icon_cache.clear()

    def _build_default_prefix(self, level):
        spawn_file = (level.spawn_file or "spawns.gon").encode("utf-8", errors="ignore")
        tiles_file = (level.tiles_file or "tiles.gon").encode("utf-8", errors="ignore")
        header = struct.pack(
            "<9i",
            int(level.version),
            int(level.width),
            int(level.height),
            int(level.mode),
            0,  # entity count, patched later
            0,  # cam x
            0,  # cam y
            int(level.width),  # cam w
            int(level.height),  # cam h
        )
        return (
            header
            + struct.pack("<i", len(spawn_file))
            + spawn_file
            + struct.pack("<i", len(tiles_file))
            + tiles_file
            + struct.pack("<ii", 0, 0)
        )

    def _create_room(self):
        dlg = tk.Toplevel(self)
        dlg.title("Create New Level")
        dlg.resizable(False, False)
        dlg.grab_set()

        spawns_var = tk.StringVar(value=self._resolve_local_path("spawns.gon"))
        tiles_var = tk.StringVar(value=self._resolve_local_path("tiles.gon"))
        name_var = tk.StringVar(value="new_level.lvl")

        def browse(var):
            p = filedialog.askopenfilename(filetypes=[("GON files", "*.gon"), ("All files", "*.*")])
            if p:
                var.set(self._normalize_input_path(p))

        for row, (label, var, cmd) in enumerate([
            ("Spawns:", spawns_var, lambda: browse(spawns_var)),
            ("Tiles:",  tiles_var,  lambda: browse(tiles_var)),
        ]):
            tk.Label(dlg, text=label, anchor="w").grid(row=row, column=0, padx=8, pady=4, sticky="w")
            tk.Entry(dlg, textvariable=var, width=40).grid(row=row, column=1, padx=4)
            tk.Button(dlg, text="Browse", command=cmd).grid(row=row, column=2, padx=8)

        tk.Label(dlg, text="Level name:", anchor="w").grid(row=2, column=0, padx=8, pady=4, sticky="w")
        tk.Entry(dlg, textvariable=name_var, width=40).grid(row=2, column=1, padx=4)

        def confirm():
            spawns_path = spawns_var.get().strip()
            tiles_path = tiles_var.get().strip()
            level_name = name_var.get().strip()
            if not level_name:
                messagebox.showerror("Create", "Level name is required.", parent=dlg)
                return
            dlg.destroy()
            if self.preview_active:
                self._reset_preview(silent=True)
            self._load_defs(tiles_path, spawns_path)
            self.level = LevelData()
            self.level.spawn_file = os.path.basename(spawns_path)
            self.level.tiles_file = os.path.basename(tiles_path)
            self.random_pool = []
            self._refresh_pool_list()
            self.path_var.set(level_name)
            self._populate_sidebar_list()
            self._draw_grid()
            self.status_var.set(f"Created new level: {level_name}")

        btn_frame = tk.Frame(dlg)
        btn_frame.grid(row=3, column=0, columnspan=3, pady=8)
        tk.Button(btn_frame, text="Create", command=confirm).pack(side="left", padx=8)
        tk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(side="left")

    _ICON_SIZE = 128
    _VALID_CELL_SIZES = [16, 32, 64]  # 128 // 8, 128 // 4, 128 // 2
    _TRANSPARENT = (255, 0, 255)  # magenta background used in all editor icons

    def _snap_cell_size(self, raw):
        """Snap raw pixel size to the nearest icon-compatible cell size."""
        return min(self._VALID_CELL_SIZES, key=lambda s: abs(s - raw))

    def _on_canvas_resize(self, event):
        size = min(event.width, event.height)
        raw = max(16, (size - 20) // 10)
        cell = self._snap_cell_size(raw)
        if cell != self.cell_size:
            self.cell_size = cell
            self._icon_tinted_cache.clear()
            self._icon_cache.clear()
            self.grid_origin = (10, 10)
            self._draw_grid()

    def _draw_grid(self):
        self.canvas.delete("all")
        # Pass 1: all backgrounds and tile icons
        for y in range(10):
            for x in range(10):
                self._draw_cell_bg(x, y)
        # Pass 2: all entity icons/text on top
        for y in range(10):
            for x in range(10):
                self._draw_cell_fg(x, y)
        self._update_status()

    def _cell_coords(self, x, y):
        ox, oy = self.grid_origin
        x0 = ox + x * self.cell_size
        y0 = oy + y * self.cell_size
        return x0, y0, x0 + self.cell_size, y0 + self.cell_size

    def _draw_cell_bg(self, x, y):
        x0, y0, x1, y1 = self._cell_coords(x, y)
        tile_id = self.level.tiles[y * 10 + x]
        self.canvas.create_rectangle(x0, y0, x1, y1, fill="#e5e7eb", outline="#cbd5e1")

        if tile_id == 0:
            return

        for stem, tint in self._icon_stems_for_tile(tile_id):
            tile_icon = self._get_icon(stem, tint)
            if tile_icon:
                ix, iy = self._icon_draw_pos(tile_icon, x0, y0)
                self.canvas.create_image(ix, iy, image=tile_icon, anchor="nw")


    def _draw_cell_fg(self, x, y):
        x0, y0, x1, y1 = self._cell_coords(x, y)
        ent_list = self.level.entities.get((x, y), [])
        if not ent_list:
            return
        ent = ent_list[0]
        ent_id = ent.id
        if ent.is_random:
            if self.preview_active:
                resolved_id = self.preview_map.get((x, y, 0), 0)
                label = self.spawn_names.get(resolved_id, str(resolved_id))
                display_id = resolved_id
            else:
                first_id = ent.options[0][0] if ent.options else 0
                label = f"RND:{self.spawn_names.get(first_id, str(first_id))}"
                display_id = first_id
        else:
            label = self.spawn_names.get(ent_id, str(ent_id))
            display_id = ent_id
        if len(ent_list) > 1:
            label = f"{label}*"

        drew_icon = False
        for stem, tint in self._icon_stems_for_entity(display_id):
            ent_icon = self._get_icon(stem, tint)
            if ent_icon:
                ix, iy = self._icon_draw_pos(ent_icon, x0, y0)
                self.canvas.create_image(ix, iy, image=ent_icon, anchor="nw")
                drew_icon = True
        if not drew_icon:
            self.canvas.create_text(
                (x0 + x1) / 2,
                (y0 + y1) / 2,
                text=label,
                fill="#111827",
                font=("Arial", max(6, self.cell_size // 7), "bold"),
                width=max(8, self.cell_size - 10),
            )



    def _populate_tile_list(self, filter_text=""):
        self.sidebar_listbox.delete(0, tk.END)
        self.tile_index_by_id = {}
        self.tile_id_order = sorted(self.tile_names.keys())
        if filter_text:
            f = filter_text.lower()
            self.tile_id_order = [tid for tid in self.tile_id_order if f in self.tile_names.get(tid, "").lower() or f in str(tid)]
        for idx, tile_id in enumerate(self.tile_id_order):
            self.tile_index_by_id[tile_id] = idx
            display = self.tile_names.get(tile_id, f"Tile {tile_id}")
            if tile_id in (15, 16, 17, 18):
                arrow = {15: "↑", 16: "↓", 17: "→", 18: "←"}.get(tile_id, "")
                if arrow:
                    display = f"{display} {arrow}"
            self.sidebar_listbox.insert(tk.END, display)
        self._select_tile_in_list(self.tile_var.get())

    def _select_tile_in_list(self, tile_id):
        idx = self.tile_index_by_id.get(tile_id)
        if idx is None:
            return
        self.sidebar_listbox.selection_clear(0, tk.END)
        self.sidebar_listbox.selection_set(idx)
        self.sidebar_listbox.see(idx)

    def _populate_entity_list(self, filter_text=""):
        self.sidebar_listbox.delete(0, tk.END)
        items = []
        for ent_id, name in self.spawn_names.items():
            items.append((name, ent_id))
        items.sort(key=lambda t: (t[1], t[0].lower()))
        if filter_text:
            f = filter_text.lower()
            items = [it for it in items if f in it[0].lower()]
        self.entity_items = items
        for name, ent_id in items:
            self.sidebar_listbox.insert(tk.END, name)
        self._select_entity_in_list()

    def _select_entity_in_list(self):
        try:
            current_id = int(self.entity_id_var.get(), 0)
        except Exception:
            return
        for idx, (_name, ent_id) in enumerate(self.entity_items):
            if ent_id == current_id:
                self.sidebar_listbox.selection_clear(0, tk.END)
                self.sidebar_listbox.selection_set(idx)
                self.sidebar_listbox.see(idx)
                return

    def _on_sidebar_select(self, _event):
        selection = self.sidebar_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        if self.mode_var.get() == "tile":
            if idx < len(self.tile_id_order):
                tile_id = self.tile_id_order[idx]
                self.tile_var.set(tile_id)
        else:
            if idx < len(self.entity_items):
                _name, ent_id = self.entity_items[idx]
                self.entity_id_var.set(str(ent_id))

    def _browse(self):
        path = filedialog.askopenfilename(filetypes=[("Level files", "*.lvl"), ("All files", "*.*")])
        if path:
            self.path_var.set(self._normalize_input_path(path))

    def _load(self):
        path = self._normalize_input_path(self.path_var.get())
        if not path:
            messagebox.showerror("Load", "Please choose a .lvl file.")
            return
        if not os.path.exists(path):
            messagebox.showerror("Load", f"File not found:\n{path}")
            return
        try:
            loaded = self._load_level(path)
        except Exception as exc:
            messagebox.showerror("Load failed", str(exc))
            return
        self._reload_defs_for_level(path, loaded.spawn_file, loaded.tiles_file)
        self.preview_active = False
        self.preview_map = {}
        self.level = loaded
        self.random_pool = []
        self._refresh_pool_list()
        self.path_var.set(path)
        is_entity = self.mode_var.get() == "entity"
        self.def_file_var.set(loaded.spawn_file if is_entity else loaded.tiles_file)
        self._populate_sidebar_list()
        self._draw_grid()
        self.status_var.set(f"Loaded {path}")

    def _save(self):
        path = self._normalize_input_path(self.path_var.get())
        if not path:
            self._save_as()
            return
        if self.preview_active:
            self._reset_preview(silent=True)
        try:
            self._save_level(path)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.status_var.set(f"Saved {path}")

    def _save_as(self):
        path = filedialog.asksaveasfilename(defaultextension=".lvl", filetypes=[("Level files", "*.lvl")])
        if not path:
            return
        self.path_var.set(self._normalize_input_path(path))
        self._save()

    def _cell_from_event(self, event):
        ox, oy = self.grid_origin
        x = (event.x - ox) // self.cell_size
        y = (event.y - oy) // self.cell_size
        if 0 <= x < 10 and 0 <= y < 10:
            return int(x), int(y)
        return None

    def _on_left_click(self, event):
        cell = self._cell_from_event(event)
        if not cell:
            return
        if self.preview_active:
            self._reset_preview(silent=True)
        x, y = cell
        if self.mode_var.get() == "tile":
            tile_id = int(self.tile_var.get())
            self.level.tiles[y * 10 + x] = tile_id
            self._select_tile_in_list(tile_id)
        else:
            try:
                ent_extra = int(self.entity_extra_var.get(), 0) & 0xFF
            except Exception:
                self.status_var.set("Invalid entity id/extra.")
                return
            if self.entity_spawn_type_var.get() == "random":
                try:
                    roll_index = int(self.entity_roll_index_var.get(), 0) & 0xFF
                except Exception:
                    self.status_var.set("Invalid roll index.")
                    return
                if not self.random_pool:
                    self.status_var.set("Random pool is empty.")
                    return
                record = SpawnObject(id=0xFFFF, wave=ent_extra, roll_index=roll_index, options=list(self.random_pool))
            else:
                try:
                    ent_id = int(self.entity_id_var.get(), 0) & 0xFFFF
                except Exception:
                    self.status_var.set("Invalid entity id.")
                    return
                record = SpawnObject(id=ent_id, wave=ent_extra)
            self.level.entities[(x, y)] = [record]
        self._draw_grid()

    def _on_right_click(self, event):
        cell = self._cell_from_event(event)
        if not cell:
            return
        if self.preview_active:
            self._reset_preview(silent=True)
        x, y = cell
        if self.mode_var.get() == "tile":
            self.level.tiles[y * 10 + x] = 0
        else:
            if (x, y) in self.level.entities:
                del self.level.entities[(x, y)]
        self._draw_grid()

    def _pick_from_cell(self, event):
        if self.mode_var.get() != "entity":
            return
        cell = self._cell_from_event(event)
        if not cell:
            return
        ent_list = self.level.entities.get(cell, [])
        if not ent_list:
            return
        ent = ent_list[0]
        self.entity_extra_var.set(str(ent.wave))
        if ent.is_random:
            self.entity_spawn_type_var.set("random")
            self.entity_roll_index_var.set(str(ent.roll_index))
            self.random_pool = list(ent.options)
        else:
            self.entity_spawn_type_var.set("fixed")
            self.entity_id_var.set(str(ent.id))
            self.random_pool = []
        self._on_spawn_type_change()
        self._refresh_pool_list()

    def _update_status(self):
        pass

    def _populate_sidebar_list(self):
        filter_text = self.sidebar_search_var.get().strip()
        if self.mode_var.get() == "tile":
            self.sidebar_title.config(text="Tiles")
            self._populate_tile_list(filter_text=filter_text)
        else:
            self.sidebar_title.config(text="Entities")
            self._populate_entity_list(filter_text=filter_text)

    def _on_mode_change(self):
        self._populate_sidebar_list()
        is_entity = self.mode_var.get() == "entity"
        if is_entity:
            self.entity_controls.pack(fill="x", padx=8, pady=(0, 6))
            self.random_tools.pack(fill="x", padx=8, pady=(0, 6))
            self._on_spawn_type_change()
            self.def_file_var.set(self.level.spawn_file or "spawns.gon")
        else:
            self.entity_controls.pack_forget()
            self.random_controls.pack_forget()
            self.pool_list.pack_forget()
            self.random_tools.pack_forget()
            self.def_file_var.set(self.level.tiles_file or "tiles.gon")

    def _on_spawn_type_change(self):
        if self.mode_var.get() != "entity":
            self.random_controls.pack_forget()
            self.pool_list.pack_forget()
            return
        if self.entity_spawn_type_var.get() == "random":
            self.random_controls.pack(fill="x", padx=8, pady=(0, 6))
            self.pool_list.pack(fill="x", padx=8, pady=(0, 6))
        else:
            self.random_controls.pack_forget()
            self.pool_list.pack_forget()
        self._refresh_pool_list()

    def _pool_add(self):
        if self.preview_active:
            self._reset_preview(silent=True)
        try:
            spawn_id = int(self.entity_id_var.get(), 0) & 0xFFFF
            weight = int(self.pool_weight_var.get(), 0) & 0xFFFF
        except Exception:
            self.status_var.set("Invalid random pool id/weight.")
            return
        if weight <= 0:
            self.status_var.set("Weight must be > 0.")
            return
        self.random_pool.append((spawn_id, weight))
        self._refresh_pool_list(select_idx=len(self.random_pool) - 1)

    def _pool_remove(self):
        if self.preview_active:
            self._reset_preview(silent=True)
        sel = self.pool_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self.random_pool):
            del self.random_pool[idx]
            self._refresh_pool_list(select_idx=max(0, idx - 1))

    def _pool_clear(self):
        if self.preview_active:
            self._reset_preview(silent=True)
        self.random_pool = []
        self._refresh_pool_list()

    def _refresh_pool_list(self, select_idx=None):
        self.pool_list.delete(0, tk.END)
        total = sum(weight for _pid, weight in self.random_pool)
        for idx, (pid, weight) in enumerate(self.random_pool):
            pct = (100.0 * weight / total) if total > 0 else 0.0
            name = self.spawn_names.get(pid, str(pid))
            self.pool_list.insert(tk.END, f"{idx + 1}. {name} ({pid})  w={weight}  {pct:.1f}%")
        if select_idx is not None and 0 <= select_idx < len(self.random_pool):
            self.pool_list.selection_clear(0, tk.END)
            self.pool_list.selection_set(select_idx)
            self.pool_list.see(select_idx)

    def _roll_from_options(self, options, roll_value=None):
        if not options:
            return 0
        total = sum(max(0, weight) for _pid, weight in options)
        if total <= 0:
            return options[0][0]
        if roll_value is None:
            roll_value = random.random()
        if roll_value < 0.0:
            roll_value = 0.0
        if roll_value >= 1.0:
            roll_value = 0.999999
        target = roll_value * total
        running = 0.0
        for pid, weight in options:
            running += max(0, weight)
            if target < running:
                return pid
        return options[-1][0]

    def _preview_randomization(self):
        shared_rolls = {}
        preview = {}
        random_count = 0
        for (x, y), ent_list in self.level.entities.items():
            for idx, ent in enumerate(ent_list):
                if not ent.is_random:
                    continue
                roll_index = ent.roll_index & 0xFF
                roll_value = None
                if roll_index != 0:
                    if roll_index not in shared_rolls:
                        shared_rolls[roll_index] = random.random()
                    roll_value = shared_rolls[roll_index]
                preview[(x, y, idx)] = self._roll_from_options(ent.options, roll_value=roll_value)
                random_count += 1
        self.preview_map = preview
        self.preview_active = True
        self._draw_grid()
        self.status_var.set(f"Preview randomization: {random_count} random spawn(s) resolved.")

    def _reset_preview(self, silent=False):
        self.preview_active = False
        self.preview_map = {}
        self._draw_grid()
        if not silent:
            self.status_var.set("Preview reset.")

    def _on_sidebar_search(self, _event):
        self._populate_sidebar_list()

    def _load_level(self, path):
        lvl_data = load_level_file(path)
        
        if lvl_data["width"] != 10 or lvl_data["height"] != 10:
            raise ValueError("This editor supports only 10x10 levels.")
        
        data = LevelData()
        data.path = path
        data.version = lvl_data["version"]
        data.width = lvl_data["width"]
        data.height = lvl_data["height"]
        data.mode = lvl_data["mode"]
        data.spawn_file = lvl_data.get("spawn_file", "spawns.gon") or "spawns.gon"
        data.tiles_file = lvl_data.get("tiles_file", "tiles.gon") or "tiles.gon"
        # Flip vertically to match editor origin (0,0 at bottom-left).
        tiles = [0] * (data.width * data.height)
        for y in range(data.height):
            for x in range(data.width):
                src_y = data.height - 1 - y
                tiles[y * data.width + x] = lvl_data["tile_grid"][src_y * data.width + x]
        data.tiles = tiles
        data.original_tiles = list(tiles)
        data.tail = lvl_data["tail"]
        data.raw_prefix = lvl_data["data"][:lvl_data["tiles_start"]]
        data.raw_tiles = lvl_data["raw_tiles"]
        data.raw_spawns = lvl_data["raw_spawns"]

        # Flip entities vertically to match editor origin (0,0 at bottom-left).
        ent_map = {}
        for x, y, spawn in lvl_data["entities"]:
            ny = data.height - 1 - y
            ent_map.setdefault((x, ny), []).append(spawn)
        data.entities = ent_map
        data.original_entities = []
        return data

    def _save_level(self, path):
        if len(self.level.tiles) != 100:
            raise ValueError("Tile grid must be 10x10.")

        # Flatten entities
        entities = []
        for (x, y) in sorted(self.level.entities.keys(), key=lambda p: (p[1], p[0])):
            for ent in self.level.entities[(x, y)]:
                ny = 10 - 1 - y
                entities.append((x, ny, ent))

        entity_count = len(entities)
        if self.level.original_tiles == self.level.tiles and self.level.raw_tiles:
            tile_bytes = self.level.raw_tiles
        else:
            # Inverse of load transform: flip vertically back to file order.
            tiles_out = [0] * 100
            for y in range(10):
                for x in range(10):
                    src_y = 9 - y
                    tiles_out[y * 10 + x] = self.level.tiles[src_y * 10 + x]
            tile_bytes = struct.pack("<100H", *tiles_out)

        chunks = []
        for x, y, ent in entities:
            ent_id = ent.id
            wave = ent.wave & 0xFF
            chunks.append(struct.pack("<hhHBB", x, y, ent_id, wave, 0))
            if ent.is_random:
                options = list(ent.options)
                if len(options) > 255:
                    raise ValueError("Random spawn has more than 255 options.")
                chunks.append(struct.pack("<BB", len(options) & 0xFF, ent.roll_index & 0xFF))
                for pid, weight in options:
                    chunks.append(struct.pack("<HH", pid & 0xFFFF, weight & 0xFFFF))
                    
        entity_bytes = b"".join(chunks)

        raw_prefix = self._build_default_prefix(self.level)
        new_data = bytearray(raw_prefix + tile_bytes + entity_bytes + self.level.tail)
        struct.pack_into("<I", new_data, 16, entity_count)

        with open(path, "wb") as f:
            f.write(new_data)


if __name__ == "__main__":
    app = LevelEditor()
    app.mainloop()
