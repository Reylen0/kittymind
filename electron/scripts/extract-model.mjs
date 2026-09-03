/**
 * 从 PSD 文件提取每个图层到 PNG 文件
 * 用法: node scripts/extract-model.mjs [psd文件路径] [输出目录]
 *
 * 不依赖 node-canvas，自带最小 Canvas polyfill
 */
import { readPsd, initializeCanvas } from 'ag-psd'
import { readFileSync, mkdirSync, writeFileSync, existsSync } from 'fs'
import { join, dirname, basename } from 'path'
import { fileURLToPath } from 'url'
import { PNG } from 'pngjs'

const __dir = dirname(fileURLToPath(import.meta.url))

// ── 最小 Canvas polyfill (ag-psd 需要 putImageData / createImageData) ──────

class MemContext2d {
  constructor(canvas) { this.canvas = canvas }
  putImageData(imageData, x = 0, y = 0) {
    const { width: cw, height: ch } = this.canvas
    const { width: iw, height: ih, data } = imageData
    if (!this.canvas._data) {
      this.canvas._data = new Uint8ClampedArray(cw * ch * 4)
    }
    for (let row = 0; row < ih; row++) {
      const srcOff = row * iw * 4
      const dstRow = y + row
      if (dstRow < 0 || dstRow >= ch) continue
      const dstOff = (dstRow * cw + x) * 4
      this.canvas._data.set(data.subarray(srcOff, srcOff + iw * 4), dstOff)
    }
  }
  createImageData(w, h) { return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) } }
  getImageData(x, y, w, h) {
    const out = this.createImageData(w, h)
    const cw = this.canvas.width
    for (let row = 0; row < h; row++) {
      const srcOff = ((y + row) * cw + x) * 4
      const dstOff = row * w * 4
      out.data.set(this.canvas._data.subarray(srcOff, srcOff + w * 4), dstOff)
    }
    return out
  }
}

class MemCanvas {
  constructor(w, h) { this.width = w; this.height = h; this._data = null }
  getContext(type) { return type === '2d' ? new MemContext2d(this) : null }
  toRGBA() {
    if (!this._data) return new Uint8ClampedArray(this.width * this.height * 4)
    return this._data
  }
}

initializeCanvas((w, h) => new MemCanvas(w, h))

// ── 编码 RGBA → PNG buffer ─────────────────────────────────────────────────

function rgbaToPng(rgba, width, height) {
  const png = new PNG({ width, height, colorType: 6 })
  png.data = Buffer.from(rgba)
  return PNG.sync.write(png)
}

// ── 主流程 ───────────────────────────────────────────────────────────────────

const psdPath = process.argv[2] ?? join(__dir, '../../tool/_up_/public/models/deepseek.psd')
const modelName = basename(psdPath, '.psd')
const outDir = process.argv[3] ?? join(__dir, '../src/pet/layers', modelName)

if (!existsSync(psdPath)) { console.error('PSD not found:', psdPath); process.exit(1) }
mkdirSync(outDir, { recursive: true })

console.log(`Reading ${psdPath} …`)
const psd = readPsd(readFileSync(psdPath), { skipCompositeImageData: true })
const { width, height } = psd

console.log(`Canvas: ${width}×${height}, layers: ${psd.children?.length}`)

// 写入每个图层
const manifest = { width, height, layers: [] }

for (const layer of (psd.children ?? [])) {
  const layerName = layer.name
  const canvas = layer.canvas

  if (!canvas) {
    console.warn(`  [skip] "${layerName}" — no pixel data`)
    manifest.layers.push({ name: layerName, file: null, hidden: layer.hidden ?? false })
    continue
  }

  // ag-psd 将每层读为该层自身尺寸的 canvas（layer.left/top 记录在全图中的偏移）
  // 必须将层数据贴回全图坐标，否则小尺寸层的内容会错位到左上角
  const lx = layer.left ?? 0
  const ly = layer.top  ?? 0
  const lw = canvas.width
  const lh = canvas.height
  const layerRGBA = canvas.toRGBA()

  // 创建全尺寸透明缓冲，把层像素写到正确位置
  const fullRGBA = new Uint8ClampedArray(width * height * 4)
  for (let row = 0; row < lh; row++) {
    const dstRow = ly + row
    if (dstRow < 0 || dstRow >= height) continue
    const srcOff = row * lw * 4
    const dstOff = (dstRow * width + lx) * 4
    const copyLen = Math.min(lw * 4, (width - lx) * 4)
    if (copyLen > 0) fullRGBA.set(layerRGBA.subarray(srcOff, srcOff + copyLen), dstOff)
  }

  const pngBuf = rgbaToPng(fullRGBA, width, height)
  const filename = `${layerName.replace(/[^a-zA-Z0-9_-]/g, '_')}.png`
  writeFileSync(join(outDir, filename), pngBuf)
  manifest.layers.push({ name: layerName, file: filename, hidden: layer.hidden ?? false })
  console.log(`  ✓ "${layerName}" → ${filename}  [layer ${lw}×${lh} at (${lx},${ly})]`)
}

// 写入 manifest
writeFileSync(join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2))
console.log(`\nDone! Output: ${outDir}`)
console.log(`Manifest: ${JSON.stringify(manifest, null, 2)}`)
