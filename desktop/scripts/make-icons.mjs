// Draw OmniAPI's app icon and the tray status icons from the dashboard's D3 theme
// (gui/src/themes/d3.css): paper, ink, and the wordmark's riso overprint — a blue "O" under
// a pink one shifted up-right, multiplied where they cross. Square corners (--radius: 0).
//
//   node desktop/scripts/make-icons.mjs            -> src-tauri/icons/tray/*.png, icon-source.png,
//                                                    and a preview sheet in %TEMP%
// Then `npm run icons` (in desktop/) lets the Tauri CLI derive the bundle icons from icon-source.png.
//
// Tray icons are 32x32 (Windows scales them to 16/20/24/32 by DPI). Each state is a tile with
// an ink border, so the silhouette reads on a light taskbar (the border) and on a dark one (the
// fill): the script prints the contrast of each against both and refuses to write below 3:1.
// No dependencies: shapes are sampled on a sub-pixel grid and written with node's zlib.

import { deflateSync } from "node:zlib";
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";

const here = dirname(fileURLToPath(import.meta.url));
const iconsDir = join(here, "..", "src-tauri", "icons");

// --- D3 theme colours (d3.css) + one status red (the theme has none). Riso "Bright Red"
// (#f15060) left paper-on-red at 2.9:1, so this is a darker red: 3.8:1 under the paper glyph,
// 3.6:1 against a dark taskbar.
const C = {
  paper: [0xe9, 0xec, 0xee],
  ink: [0x1f, 0x23, 0x30],
  blue: [0x00, 0x78, 0xbf],
  pink: [0xff, 0x48, 0xb0],
  yellow: [0xff, 0xe8, 0x00],
  red: [0xdc, 0x35, 0x45],
};
// Windows 11 taskbars
const TASKBAR = { light: [0xf3, 0xf3, 0xf3], dark: [0x20, 0x20, 0x20] };

// --- shapes: (x, y) in icon units -> inside?
const rect = (x0, y0, x1, y1) => (x, y) => x >= x0 && x < x1 && y >= y0 && y < y1;
const ring = (cx, cy, r0, r1, gap) => (x, y) => {
  const d = Math.hypot(x - cx, y - cy);
  if (d < r0 || d > r1) return false;
  if (!gap) return true;
  // gap: [from, to] in degrees, 0 = right, counter-clockwise (screen y is down)
  let a = (Math.atan2(cy - y, x - cx) * 180) / Math.PI;
  if (a < 0) a += 360;
  return !(a >= gap[0] && a <= gap[1]);
};
const disc = (cx, cy, r) => (x, y) => Math.hypot(x - cx, y - cy) <= r;
const any = (...fs) => (x, y) => fs.some((f) => f(x, y));

// --- raster: layers = [{ shape, color, mode: "normal" | "multiply" }]
function render(size, layers, ss) {
  const out = Buffer.alloc(size * size * 4);
  const n = ss * ss;
  for (let py = 0; py < size; py++) {
    for (let px = 0; px < size; px++) {
      let r = 0, g = 0, b = 0, a = 0;
      for (let sy = 0; sy < ss; sy++) {
        for (let sx = 0; sx < ss; sx++) {
          const x = px + (sx + 0.5) / ss, y = py + (sy + 0.5) / ss;
          let col = null;
          for (const L of layers) {
            if (!L.shape(x, y)) continue;
            if (L.mode === "multiply" && col) col = col.map((v, i) => (v * L.color[i]) / 255);
            else col = L.color;
          }
          if (col) { r += col[0]; g += col[1]; b += col[2]; a += 1; }
        }
      }
      const i = (py * size + px) * 4;
      if (a) { out[i] = Math.round(r / a); out[i + 1] = Math.round(g / a); out[i + 2] = Math.round(b / a); }
      out[i + 3] = Math.round((255 * a) / n);
    }
  }
  return out;
}

// --- PNG (RGBA, 8 bit, no interlace)
const CRC = new Uint32Array(256).map((_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c >>> 0; });
const crc32 = (buf) => { let c = 0xffffffff; for (const v of buf) c = CRC[(c ^ v) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
function chunk(type, data) {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
}
function png(w, h, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 6; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  const raw = Buffer.alloc((w * 4 + 1) * h);
  for (let y = 0; y < h; y++) rgba.copy(raw, y * (w * 4 + 1) + 1, y * w * 4, (y + 1) * w * 4);
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw, { level: 9 })), chunk("IEND", Buffer.alloc(0))]);
}

// --- contrast (WCAG relative luminance)
const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
const contrast = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

// --- the tray icons (32 units). Tile 1..31 with a 2-unit ink border; the glyph in the middle.
const T = 32;
const tile = (fill) => [
  { shape: rect(1, 1, 31, 31), color: C.ink },
  { shape: rect(3, 3, 29, 29), color: fill },
];
const O = (dx = 0, dy = 0, gap) => ring(16 + dx, 16 + dy, 5.2, 10, gap);
const TRAY = {
  // running: the wordmark's overprint, blue under, pink 2 up-right, multiply
  up: { fill: "paper", layers: [...tile(C.paper), { shape: O(-1.2, 1.2), color: C.blue }, { shape: O(1.2, -1.2), color: C.pink, mode: "multiply" }] },
  // starting: ink "O" not closed yet, on the yellow ink
  starting: { fill: "yellow", layers: [...tile(C.yellow), { shape: O(0, 0, [10, 100]), color: C.ink }] },
  // not answering (the shell is restarting it): red tile, paper "O"
  down: { fill: "red", layers: [...tile(C.red), { shape: O(), color: C.paper }] },
  // gave up: red tile, paper "!" — needs the user
  failed: { fill: "red", layers: [...tile(C.red), { shape: any(rect(14, 6.5, 18, 19.5), disc(16, 24, 2.4)), color: C.paper }] },
};

mkdirSync(join(iconsDir, "tray"), { recursive: true });
let bad = 0;
const sheetRows = [];
for (const [name, def] of Object.entries(TRAY)) {
  const px = render(T, def.layers, 16);
  writeFileSync(join(iconsDir, "tray", `${name}.png`), png(T, T, px));
  const fill = C[def.fill];
  const line = Object.entries(TASKBAR).map(([k, bg]) => {
    const silhouette = Math.max(contrast(C.ink, bg), contrast(fill, bg));
    if (silhouette < 3) bad++;
    return `${k} ${silhouette.toFixed(1)}:1`;
  });
  const glyph = name === "up" ? contrast(C.blue, C.paper) : name === "starting" ? contrast(C.ink, C.yellow) : contrast(C.paper, C.red);
  console.log(`tray/${name}.png  silhouette vs taskbar: ${line.join(", ")}; glyph vs tile ${glyph.toFixed(1)}:1`);
  sheetRows.push(px);
}

// --- app icon (1024): paper tile, ink frame, the overprint "O" large
const A = 1024;
const app = render(A, [
  { shape: rect(40, 40, 984, 984), color: C.ink },
  { shape: rect(88, 88, 936, 936), color: C.paper },
  { shape: ring(512 - 34, 512 + 34, 178, 330), color: C.blue },
  { shape: ring(512 + 34, 512 - 34, 178, 330), color: C.pink, mode: "multiply" },
], 3);
writeFileSync(join(iconsDir, "icon-source.png"), png(A, A, app));
console.log(`icon-source.png 1024x1024 (feed to: tauri icon)`);

// --- preview sheet: each tray icon at 16/24/32 px on the light and the dark taskbar
const scales = [16, 24, 32];
const cell = 40, W = cell * scales.length * 2, H = cell * sheetRows.length;
const sheet = Buffer.alloc(W * H * 4);
const put = (x, y, rgb) => { const i = (y * W + x) * 4; sheet[i] = rgb[0]; sheet[i + 1] = rgb[1]; sheet[i + 2] = rgb[2]; sheet[i + 3] = 255; };
for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) put(x, y, x < W / 2 ? TASKBAR.light : TASKBAR.dark);
sheetRows.forEach((src, row) => {
  scales.forEach((s, si) => {
    for (const side of [0, 1]) {
      const bg = side ? TASKBAR.dark : TASKBAR.light;
      const ox = side * (W / 2) + si * cell + (cell - s) / 2, oy = row * cell + (cell - s) / 2;
      for (let y = 0; y < s; y++) for (let x = 0; x < s; x++) {
        // box-filter downscale from 32
        let r = 0, g = 0, b = 0, a = 0, k = 0;
        const f = T / s;
        for (let yy = Math.floor(y * f); yy < Math.ceil((y + 1) * f); yy++) for (let xx = Math.floor(x * f); xx < Math.ceil((x + 1) * f); xx++) {
          const i = (yy * T + xx) * 4; const al = src[i + 3] / 255;
          r += src[i] * al; g += src[i + 1] * al; b += src[i + 2] * al; a += al; k++;
        }
        const al = a / k;
        const c = a ? [r / a, g / a, b / a] : [0, 0, 0];
        put(ox + x, oy + y, c.map((v, i) => Math.round(v * al + bg[i] * (1 - al))));
      }
    }
  });
});
const preview = join(tmpdir(), "omniapi-tray-preview.png");
writeFileSync(preview, png(W, H, sheet));
console.log(`preview (16/24/32 px on light | dark taskbar): ${preview}`);
if (bad) { console.error(`${bad} silhouette(s) under 3:1`); process.exit(1); }
