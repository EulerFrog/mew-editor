import os
import random
import struct
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox

DEFAULT_TILE = 0


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
        self.tiles = [DEFAULT_TILE] * (self.width * self.height)
        self.entities = {}  # (x,y) -> [SpawnObject, ...]
        self.raw_prefix = b""
        self.raw_tiles = b""
        self.raw_spawns = b""
        self.original_tiles = []
        self.original_entities = []
        self.tail = b""

    def clear(self):
        self.__init__()


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


def load_tile_defs(path):
    if not os.path.exists(path):
        return {}
    tile_defs = {}
    current_id = None
    in_editor = False
    name = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            if current_id is None and stripped[0].isdigit() and stripped.endswith("{"):
                try:
                    current_id = int(stripped.split()[0])
                    name = None
                    in_editor = False
                except Exception:
                    current_id = None
                continue
            if current_id is None:
                continue
            if stripped.startswith("editor"):
                in_editor = True
                continue
            if in_editor and stripped.startswith("name "):
                try:
                    name = stripped.split('name "', 1)[1].split('"', 1)[0]
                except Exception:
                    pass
                continue
            if stripped == "}":
                if in_editor:
                    in_editor = False
                    continue
                if current_id is not None:
                    tile_defs[current_id] = {
                        "name": name or f"Tile {current_id}",
                    }
                current_id = None
                name = None
    return tile_defs


def load_spawn_defs(path):
    if not os.path.exists(path):
        return {}
    spawn_defs = {}
    current_id = None
    name = None
    depth = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            if current_id is None:
                if stripped[0].isdigit() and stripped.endswith("{"):
                    try:
                        current_id = int(stripped.split()[0])
                        name = None
                        depth = 1
                    except Exception:
                        current_id = None
                        depth = 0
                continue
            if "{" in stripped:
                depth += stripped.count("{")
            if "}" in stripped:
                depth -= stripped.count("}")
            if name is None and 'name "' in stripped:
                try:
                    name = stripped.split('name "', 1)[1].split('"', 1)[0]
                except Exception:
                    pass
            if depth <= 0:
                if name:
                    spawn_defs[current_id] = name
                current_id = None
                name = None
                depth = 0
    return spawn_defs


class LevelEditor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Level Editor")
        self.resizable(True, True)

        self.level = LevelData()
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.spawn_names = load_spawn_defs(self._resolve_local_path("spawns.gon"))
        self.tile_defs = load_tile_defs(self._resolve_local_path("tiles.gon"))
        self.tile_names = {k: v.get("name", f"Tile {k}") for k, v in self.tile_defs.items()}
        self.tile_colors = self._build_tile_colors()

        self.cell_size = 36
        self.grid_origin = (10, 10)
        self.canvas_size = self.cell_size * 10 + 20

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
        tk.Button(top, text="New", command=self._new_room).pack(side="left", padx=4)
        tk.Button(top, text="Load", command=self._load).pack(side="left", padx=4)
        tk.Button(top, text="Save", command=self._save).pack(side="left", padx=4)
        tk.Button(top, text="Save As", command=self._save_as).pack(side="left")

        controls = tk.Frame(self)
        controls.pack(fill="x", padx=8, pady=6)

        self.mode_var = tk.StringVar(value="tile")
        tk.Label(controls, text="Mode:").pack(side="left")
        tk.Radiobutton(controls, text="Tile", variable=self.mode_var, value="tile", command=self._on_mode_change).pack(side="left")
        tk.Radiobutton(controls, text="Entity", variable=self.mode_var, value="entity", command=self._on_mode_change).pack(side="left")

        self.tile_var = tk.IntVar(value=DEFAULT_TILE)
        self.entity_id_var = tk.StringVar(value="2050")
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

        self.status_var = tk.StringVar(value="Ready.")
        status = tk.Label(self, textvariable=self.status_var, anchor="w")
        status.pack(fill="x", padx=8, pady=(6, 6))

    def _resolve_local_path(self, filename):
        local = os.path.join(self.base_dir, filename)
        return local if os.path.exists(local) else filename

    def _normalize_input_path(self, raw_path):
        p = (raw_path or "").strip().strip('"').strip("'")
        if not p:
            return ""
        p = os.path.expandvars(os.path.expanduser(p))
        if os.name != "nt" and "\\" in p and "/" not in p:
            p = p.replace("\\", os.sep)
        return os.path.normpath(p)

    def _reload_defs_for_level(self, level_path):
        level_dir = os.path.dirname(level_path)
        spawn_path = os.path.join(level_dir, "spawns.gon")
        tile_path = os.path.join(level_dir, "tiles.gon")

        if not os.path.exists(spawn_path):
            spawn_path = self._resolve_local_path("spawns.gon")
        if not os.path.exists(tile_path):
            tile_path = self._resolve_local_path("tiles.gon")

        self.spawn_names = load_spawn_defs(spawn_path)
        self.tile_defs = load_tile_defs(tile_path)
        self.tile_names = {k: v.get("name", f"Tile {k}") for k, v in self.tile_defs.items()}
        self.tile_colors = self._build_tile_colors()

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

    def _new_room(self):
        if self.preview_active:
            self._reset_preview(silent=True)
        self.level = LevelData()
        self.random_pool = []
        self._refresh_pool_list()
        self.path_var.set("")
        self._populate_sidebar_list()
        self._draw_grid()
        self.status_var.set("New blank room.")

    def _on_canvas_resize(self, event):
        size = min(event.width, event.height)
        cell = max(16, (size - 20) // 10)
        if cell != self.cell_size:
            self.cell_size = cell
            self.grid_origin = (10, 10)
            self._draw_grid()

    def _draw_grid(self):
        self.canvas.delete("all")
        ox, oy = self.grid_origin
        for y in range(10):
            for x in range(10):
                self._draw_cell(x, y)
        self._update_status()

    def _draw_cell(self, x, y):
        ox, oy = self.grid_origin
        x0 = ox + x * self.cell_size
        y0 = oy + y * self.cell_size
        x1 = x0 + self.cell_size
        y1 = y0 + self.cell_size
        tile_id = self.level.tiles[y * 10 + x]
        color = self.tile_colors.get(tile_id, "#9ca3af")
        self.canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline="#cbd5e1")

        tile_text = None
        tile_id = self.level.tiles[y * 10 + x]
        if tile_id in (15, 16, 17, 18):
            arrows = {15: "↑", 16: "↓", 17: "→", 18: "←"}
            tile_text = arrows.get(tile_id)
            if tile_text:
                self.canvas.create_text(
                    (x0 + x1) / 2,
                    (y0 + y1) / 2,
                    text=tile_text,
                    fill="#111827",
                    font=("Arial", max(9, self.cell_size // 3), "bold"),
                )

        ent_list = self.level.entities.get((x, y), [])
        if ent_list:
            ent = ent_list[0]
            ent_id = ent.id
            if ent.is_random:
                if self.preview_active:
                    resolved_id = self.preview_map.get((x, y, 0), 0)
                    label = self.spawn_names.get(resolved_id, str(resolved_id))
                else:
                    first_id = ent.options[0][0] if ent.options else 0
                    label = f"RND:{self.spawn_names.get(first_id, str(first_id))}"
            else:
                label = self.spawn_names.get(ent_id, str(ent_id))
            text = label
            if len(ent_list) > 1:
                text = f"{text}*"
            self.canvas.create_text(
                (x0 + x1) / 2,
                (y0 + y1) / 2,
                text=text,
                fill="#111827",
                font=("Arial", max(6, self.cell_size // 7), "bold"),
                width=max(8, self.cell_size - 10),
            )


    def _tile_label(self, tile_id):
        name = self.tile_names.get(tile_id, "Unknown")
        if tile_id in (15, 16, 17, 18):
            arrow = {15: "↑", 16: "↓", 17: "→", 18: "←"}.get(tile_id, "")
            if arrow:
                name = f"{name} {arrow}"
        return f"{name} ({tile_id})"

    def _color_from_name(self, name):
        n = name.lower()
        if "water" in n:
            return "#3b82f6"
        if "ice" in n or "snow" in n or "supercooled" in n:
            return "#38bdf8"
        if "grass" in n or "flower" in n or "bramble" in n:
            return "#22c55e"
        if "lava" in n or "fire" in n:
            return "#f97316"
        if "toxic" in n or "sludge" in n:
            return "#84cc16"
        if "rock" in n or "stalagmite" in n:
            return "#9ca3af"
        if "metal" in n or "road" in n:
            return "#64748b"
        if "dirt" in n:
            return "#a16207"
        if "shadow" in n:
            return "#111827"
        if "glass" in n or "glitch" in n:
            return "#d1d5db"
        if "creep" in n:
            return "#a855f7"
        if "oil" in n:
            return "#1f2937"
        return "#e5e7eb"

    def _build_tile_colors(self):
        overrides = {
            0: "#e5e7eb",  # Empty
            1: "#2563eb",  # Water
            2: "#22c55e",  # Grass
            3: "#15803d",  # Tall Grass
            4: "#f97316",  # Fire
            5: "#7dd3fc",  # Ice
            6: "#dc2626",  # Lava
            7: "#94a3b8",  # Metal
            8: "#6b7280",  # Rock
            9: "#a855f7",  # Creep
            10: "#0f172a",  # Oil
            11: "#84cc16",  # Toxic Sludge
            12: "#111827",  # Shadow
            13: "#e5e7eb",  # Glass Shards
            14: "#f8fafc",  # Snow
            15: "#1d4ed8",  # Water current N
            16: "#2563eb",  # Water current S
            17: "#3b82f6",  # Water current E
            18: "#60a5fa",  # Water current W
            19: "#a16207",  # Dirt
            20: "#64748b",  # Stalagmites
            21: "#475569",  # Road Tile
            22: "#166534",  # Brambles
            23: "#ec4899",  # Flowers
            24: "#f472b6",  # Tall Flower Tile
            25: "#06b6d4",  # Supercooled Water
            26: "#9ca3af",  # Glitch Tile
        }
        colors = {}
        for tile_id, data in self.tile_defs.items():
            name = data.get("name", f"Tile {tile_id}")
            if tile_id in overrides:
                colors[tile_id] = overrides[tile_id]
            else:
                base = self._color_from_name(name)
                colors[tile_id] = base
        if DEFAULT_TILE not in colors:
            colors[DEFAULT_TILE] = "#e5e7eb"
        return colors

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
        self._reload_defs_for_level(path)
        self.preview_active = False
        self.preview_map = {}
        self.level = loaded
        self.random_pool = []
        self._refresh_pool_list()
        self.path_var.set(path)
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
            self.level.tiles[y * 10 + x] = DEFAULT_TILE
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
        ent_count = sum(len(v) for v in self.level.entities.values())
        self.status_var.set(f"Tiles: {len(self.level.tiles)}  Entities: {ent_count}")

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
        else:
            self.entity_controls.pack_forget()
            self.random_controls.pack_forget()
            self.pool_list.pack_forget()
            self.random_tools.pack_forget()

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

        raw_prefix = self.level.raw_prefix if self.level.raw_prefix else self._build_default_prefix(self.level)
        new_data = bytearray(raw_prefix + tile_bytes + entity_bytes + self.level.tail)
        struct.pack_into("<I", new_data, 16, entity_count)

        with open(path, "wb") as f:
            f.write(new_data)


if __name__ == "__main__":
    app = LevelEditor()
    app.mainloop()
